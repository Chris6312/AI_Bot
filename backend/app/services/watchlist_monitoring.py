from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.watchlist_monitor_state import MONITOR_ONLY, WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.services.execution_lifecycle import execution_lifecycle
from app.services.kraken_service import crypto_ledger, kraken_service
from app.services.lifecycle_state_machine import describe_lifecycle
from app.services.market_sessions import get_scope_session_status
from app.services.position_sizer import position_sizer
from app.services.pre_trade_gate import pre_trade_gate
from app.services.runtime_state import runtime_state
from app.services.template_evaluator import ENTRY_CANDIDATE, template_evaluation_service
from app.services.tradier_client import tradier_client
from app.services.watchlist_service import ACTIVE, MANAGED_ONLY, PENDING_EVALUATION, WATCHLIST_SCOPE, watchlist_service

logger = logging.getLogger(__name__)

ELIGIBLE_DUE_STATUSES = (ACTIVE, MANAGED_ONLY)
DEFAULT_SCOPES: tuple[WATCHLIST_SCOPE, ...] = ("stocks_only", "crypto_only")
ENTRY_EXECUTION_SOURCE = "WATCHLIST_MONITOR_ENTRY"
ACTIVE_ENTRY_INTENT_STATUSES = {"READY", "SUBMITTED", "PARTIALLY_FILLED", "SUBMISSION_PENDING"}
COOLDOWN_DECISION_STATE = "COOLDOWN_ACTIVE"
SUBMISSION_PENDING_DECISION_STATE = "SUBMISSION_PENDING"


@dataclass
class MonitorLoopRuntime:
    enabled: bool = True
    poll_seconds: int = 20
    last_started_at_utc: str | None = None
    last_finished_at_utc: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_run_summary: dict[str, Any] = field(default_factory=dict)


class WatchlistMonitoringOrchestrator:
    def __init__(self) -> None:
        self._runtime = MonitorLoopRuntime(
            enabled=bool(settings.WATCHLIST_MONITOR_ENABLED),
            poll_seconds=max(5, int(settings.WATCHLIST_MONITOR_POLL_SECONDS)),
        )

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(UTC)

    async def bootstrap_startup_state(self, *, refresh_crypto_monitor_state: bool = True) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._bootstrap_startup_state_sync,
            refresh_crypto_monitor_state=refresh_crypto_monitor_state,
        )

    def _bootstrap_startup_state_sync(self, *, refresh_crypto_monitor_state: bool = True) -> dict[str, Any]:
        started_at = self._utcnow()
        summary: dict[str, Any] = {
            "capturedAtUtc": started_at.isoformat(),
            "assetPairsRefreshed": False,
            "assetPairCount": 0,
            "cryptoMonitorRefreshAttempted": bool(refresh_crypto_monitor_state),
            "cryptoMonitorRefreshApplied": False,
            "evaluatedCount": 0,
            "evaluationSummary": {},
            "monitoringSnapshot": None,
        }

        asset_pairs = kraken_service.refresh_asset_pairs(force=True)
        summary["assetPairsRefreshed"] = True
        summary["assetPairCount"] = len(asset_pairs)

        if not refresh_crypto_monitor_state:
            return summary

        db = SessionLocal()
        try:
            watchlist_service.reconcile_scope_statuses(db, scope="crypto_only")
            eligible_count = (
                db.query(WatchlistMonitorState)
                .filter(
                    WatchlistMonitorState.scope == "crypto_only",
                    WatchlistMonitorState.monitoring_status.in_(ELIGIBLE_DUE_STATUSES),
                )
                .count()
            )
            evaluation = template_evaluation_service.evaluate_scope(
                db,
                scope="crypto_only",
                limit=max(1, eligible_count),
                force=True,
                eligible_statuses=ELIGIBLE_DUE_STATUSES,
            )
            summary["cryptoMonitorRefreshApplied"] = True
            summary["evaluatedCount"] = evaluation["evaluatedCount"]
            summary["evaluationSummary"] = evaluation["summary"]
            summary["monitoringSnapshot"] = evaluation["monitoringSnapshot"]
            return summary
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_runtime_status(self, db: Session | None = None, *, scope: WATCHLIST_SCOPE | None = None) -> dict[str, Any]:
        due_snapshot = None
        if db is not None:
            due_snapshot = self.get_due_snapshot(db, scope=scope)
        return {
            "enabled": self._runtime.enabled,
            "pollSeconds": self._runtime.poll_seconds,
            "lastStartedAtUtc": self._runtime.last_started_at_utc,
            "lastFinishedAtUtc": self._runtime.last_finished_at_utc,
            "lastError": self._runtime.last_error,
            "consecutiveFailures": self._runtime.consecutive_failures,
            "lastRunSummary": self._runtime.last_run_summary,
            "dueSnapshot": due_snapshot,
        }

    def get_due_snapshot(self, db: Session, *, scope: WATCHLIST_SCOPE | None = None) -> dict[str, Any]:
        scopes: tuple[WATCHLIST_SCOPE, ...] = (scope,) if scope is not None else DEFAULT_SCOPES
        observed_at = self._utcnow()
        result: dict[str, Any] = {
            "capturedAtUtc": observed_at.isoformat(),
            "scopes": {},
            "summary": {
                "totalDueCount": 0,
                "activeDueCount": 0,
                "managedOnlyDueCount": 0,
            },
        }
        for scope_value in scopes:
            watchlist_service._backfill_missing_monitor_states(db, scope=scope_value, observed_at=observed_at)
            scope_due = self._query_due_rows(db, scope=scope_value, observed_at=observed_at)
            active_due = scope_due.filter(WatchlistMonitorState.monitoring_status == ACTIVE).count()
            managed_only_due = scope_due.filter(WatchlistMonitorState.monitoring_status == MANAGED_ONLY).count()
            total_due = active_due + managed_only_due
            monitoring_snapshot = watchlist_service.get_monitoring_snapshot(db, scope=scope_value, include_inactive=False)
            session_status = get_scope_session_status(scope_value, observed_at)
            eligible_due = total_due if session_status.session_open else (total_due if scope_value == "crypto_only" else 0)
            blocked_due = 0 if session_status.session_open else (0 if scope_value == "crypto_only" else total_due)
            result["scopes"][scope_value] = {
                "scope": scope_value,
                "dueCount": total_due,
                "eligibleDueCount": eligible_due,
                "blockedDueCount": blocked_due,
                "activeDueCount": active_due,
                "managedOnlyDueCount": managed_only_due,
                "nextEvaluationAtUtc": monitoring_snapshot["summary"]["nextEvaluationAtUtc"],
                "activeUploadId": monitoring_snapshot["activeUploadId"],
                "session": session_status.to_dict(),
            }
            result["summary"]["totalDueCount"] += total_due
            result["summary"].setdefault("eligibleDueCount", 0)
            result["summary"].setdefault("blockedDueCount", 0)
            result["summary"]["eligibleDueCount"] += eligible_due
            result["summary"]["blockedDueCount"] += blocked_due
            result["summary"]["activeDueCount"] += active_due
            result["summary"]["managedOnlyDueCount"] += managed_only_due
        if scope is not None:
            return result["scopes"][scope]
        return result

    def run_due_once(
        self,
        db: Session,
        *,
        scope: WATCHLIST_SCOPE | None = None,
        limit_per_scope: int | None = None,
    ) -> dict[str, Any]:
        scopes: tuple[WATCHLIST_SCOPE, ...] = (scope,) if scope is not None else DEFAULT_SCOPES
        observed_at = self._utcnow()
        per_scope_limit = max(1, int(limit_per_scope or settings.WATCHLIST_MONITOR_BATCH_LIMIT))

        result: dict[str, Any] = {
            "capturedAtUtc": observed_at.isoformat(),
            "limitPerScope": per_scope_limit,
            "scopes": {},
            "summary": {
                "totalScopes": len(scopes),
                "scopesWithDueRows": 0,
                "totalDueBefore": 0,
                "totalDueAfter": 0,
                "totalEvaluated": 0,
                "totalEntryCandidates": 0,
                "totalWaitingForSetup": 0,
                "totalDataStale": 0,
                "totalDataUnavailable": 0,
                "totalMonitorOnly": 0,
                "totalBiasConflict": 0,
                "totalEvaluationBlocked": 0,
                "totalSessionBlocked": 0,
                "totalEntryIntentCount": 0,
                "totalEntrySubmitted": 0,
                "totalEntryFilled": 0,
                "totalEntryRejected": 0,
                "totalEntrySkipped": 0,
            },
        }

        for scope_value in scopes:
            watchlist_service.reconcile_scope_statuses(db, scope=scope_value)
            due_before = self._count_due_rows(db, scope=scope_value, observed_at=observed_at)
            session_status = get_scope_session_status(scope_value, observed_at)
            scope_result: dict[str, Any] = {
                "scope": scope_value,
                "session": session_status.to_dict(),
                "dueCountBefore": due_before,
                "dueCountAfter": due_before,
                "evaluatedCount": 0,
                "summary": {
                    "entryCandidateCount": 0,
                    "waitingForSetupCount": 0,
                    "dataStaleCount": 0,
                    "dataUnavailableCount": 0,
                    "monitorOnlyCount": 0,
                    "inactiveCount": 0,
                    "biasConflictCount": 0,
                    "evaluationBlockedCount": 0,
                },
                "rows": [],
                "sessionBlockedCount": 0,
                "entryExecution": {
                    "candidateCount": 0,
                    "intentCount": 0,
                    "submittedCount": 0,
                    "filledCount": 0,
                    "rejectedCount": 0,
                    "skippedCount": 0,
                    "rows": [],
                },
                "monitoringSnapshot": watchlist_service.get_monitoring_snapshot(db, scope=scope_value, include_inactive=False),
            }
            if due_before > 0 and scope_value == "stocks_only" and not session_status.session_open:
                scope_result["sessionBlockedCount"] = due_before
            elif due_before > 0:
                evaluated_scope = template_evaluation_service.evaluate_scope(
                    db,
                    scope=scope_value,
                    limit=min(per_scope_limit, due_before),
                    force=False,
                    eligible_statuses=ELIGIBLE_DUE_STATUSES,
                )
                scope_result.update(evaluated_scope)
                scope_result["session"] = session_status.to_dict()
                scope_result["sessionBlockedCount"] = 0
                scope_result["dueCountBefore"] = due_before
                entry_execution = self._execute_entry_candidates(db, evaluated_scope["rows"])
                scope_result["entryExecution"] = entry_execution
                scope_result["monitoringSnapshot"] = watchlist_service.get_monitoring_snapshot(db, scope=scope_value, include_inactive=False)
                scope_result["dueCountAfter"] = self._count_due_rows(db, scope=scope_value, observed_at=self._utcnow())
                result["summary"]["scopesWithDueRows"] += 1

            result["scopes"][scope_value] = scope_result
            result["summary"]["totalDueBefore"] += scope_result["dueCountBefore"]
            result["summary"]["totalDueAfter"] += scope_result["dueCountAfter"]
            result["summary"]["totalEvaluated"] += scope_result["evaluatedCount"]
            result["summary"]["totalEntryCandidates"] += scope_result["summary"]["entryCandidateCount"]
            result["summary"]["totalWaitingForSetup"] += scope_result["summary"]["waitingForSetupCount"]
            result["summary"]["totalDataStale"] += scope_result["summary"]["dataStaleCount"]
            result["summary"]["totalDataUnavailable"] += scope_result["summary"]["dataUnavailableCount"]
            result["summary"]["totalMonitorOnly"] += scope_result["summary"]["monitorOnlyCount"]
            result["summary"]["totalBiasConflict"] += scope_result["summary"]["biasConflictCount"]
            result["summary"]["totalEvaluationBlocked"] += scope_result["summary"]["evaluationBlockedCount"]
            result["summary"]["totalSessionBlocked"] += scope_result.get("sessionBlockedCount", 0)
            result["summary"]["totalEntryIntentCount"] += scope_result.get("entryExecution", {}).get("intentCount", 0)
            result["summary"]["totalEntrySubmitted"] += scope_result.get("entryExecution", {}).get("submittedCount", 0)
            result["summary"]["totalEntryFilled"] += scope_result.get("entryExecution", {}).get("filledCount", 0)
            result["summary"]["totalEntryRejected"] += scope_result.get("entryExecution", {}).get("rejectedCount", 0)
            result["summary"]["totalEntrySkipped"] += scope_result.get("entryExecution", {}).get("skippedCount", 0)

        if scope is not None:
            return result["scopes"][scope]
        return result

    async def run_loop(self) -> None:
        self._runtime.enabled = bool(settings.WATCHLIST_MONITOR_ENABLED)
        self._runtime.poll_seconds = max(5, int(settings.WATCHLIST_MONITOR_POLL_SECONDS))
        if not self._runtime.enabled:
            logger.info("Watchlist monitoring orchestrator is disabled.")
            return

        logger.info(
            "Starting watchlist monitoring orchestrator loop (poll=%ss, batch_limit=%s).",
            self._runtime.poll_seconds,
            settings.WATCHLIST_MONITOR_BATCH_LIMIT,
        )

        def _execute_due_run_blocking() -> dict[str, Any]:
            db = SessionLocal()
            try:
                return self.run_due_once(db, limit_per_scope=settings.WATCHLIST_MONITOR_BATCH_LIMIT)
            finally:
                db.close()

        while True:
            self._runtime.last_started_at_utc = self._utcnow().isoformat()
            try:
                run_summary = await asyncio.to_thread(_execute_due_run_blocking)
                self._runtime.last_run_summary = run_summary
                self._runtime.last_error = None
                self._runtime.consecutive_failures = 0
                self._runtime.last_finished_at_utc = self._utcnow().isoformat()
                if run_summary["summary"]["totalEvaluated"] > 0:
                    logger.info(
                        "Watchlist due-run sweep complete: evaluated=%s due_before=%s due_after=%s",
                        run_summary["summary"]["totalEvaluated"],
                        run_summary["summary"]["totalDueBefore"],
                        run_summary["summary"]["totalDueAfter"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Watchlist monitoring orchestrator sweep failed: %s", exc)
                self._runtime.last_error = str(exc)
                self._runtime.consecutive_failures += 1
                self._runtime.last_finished_at_utc = self._utcnow().isoformat()
            await asyncio.sleep(self._runtime.poll_seconds)

    def _execute_entry_candidates(self, db: Session, evaluated_rows: list[dict[str, Any]]) -> dict[str, Any]:
        runtime = runtime_state.get()
        result: dict[str, Any] = {
            "candidateCount": 0,
            "intentCount": 0,
            "submittedCount": 0,
            "filledCount": 0,
            "rejectedCount": 0,
            "skippedCount": 0,
            "rows": [],
        }
        candidate_rows = [
            row for row in evaluated_rows
            if str(row.get("latestDecisionState") or "") == ENTRY_CANDIDATE
        ]
        if not candidate_rows:
            return result

        if not runtime.running:
            for row in candidate_rows:
                result["candidateCount"] += 1
                result["skippedCount"] += 1
                result["rows"].append({
                    "symbol": str(row.get("symbol") or "").upper(),
                    "action": "SKIPPED",
                    "reason": "RUNTIME_STOPPED",
                    "intentId": None,
                    "submittedOrderId": None,
                    "positionId": None,
                    "tradeId": None,
                })
            return result

        stock_symbols = [
            str(row.get("symbol") or "").upper()
            for row in candidate_rows
            if str(row.get("scope") or "") == "stocks_only"
        ]
        crypto_symbols = [
            str(row.get("symbol") or "").upper()
            for row in candidate_rows
            if str(row.get("scope") or "") == "crypto_only"
        ]

        account_cache: dict[str, Any] = {
            "loaded": False,
            "account": None,
            "cashAvailable": 0.0,
            "remainingCash": 0.0,
            "reservedCash": 0.0,
            "error": None,
        }

        if stock_symbols:
            query = (
                db.query(WatchlistMonitorState, WatchlistSymbol)
                .join(WatchlistSymbol, WatchlistSymbol.id == WatchlistMonitorState.watchlist_symbol_id)
                .filter(
                    WatchlistMonitorState.scope == "stocks_only",
                    WatchlistMonitorState.symbol.in_(stock_symbols),
                )
                .order_by(WatchlistSymbol.priority_rank.asc(), WatchlistSymbol.id.asc())
            )
            for monitor_state, symbol_row in query.all():
                result["candidateCount"] += 1
                candidate = self._submit_stock_entry_candidate(
                    db,
                    monitor_state=monitor_state,
                    symbol_row=symbol_row,
                    mode=runtime.stock_mode,
                    account_cache=account_cache,
                )
                result["rows"].append(candidate)
                if candidate.get("intentId"):
                    result["intentCount"] += 1
                action = str(candidate.get("action") or "")
                if action in {"ENTRY_SUBMITTED", "ENTRY_FILLED"}:
                    result["submittedCount"] += 1
                if action == "ENTRY_FILLED":
                    result["filledCount"] += 1
                elif action in {"GATE_REJECTED", "SUBMISSION_REJECTED"}:
                    result["rejectedCount"] += 1
                elif action == "SKIPPED":
                    result["skippedCount"] += 1

        if crypto_symbols:
            query = (
                db.query(WatchlistMonitorState, WatchlistSymbol)
                .join(WatchlistSymbol, WatchlistSymbol.id == WatchlistMonitorState.watchlist_symbol_id)
                .filter(
                    WatchlistMonitorState.scope == "crypto_only",
                    WatchlistMonitorState.symbol.in_(crypto_symbols),
                )
                .order_by(WatchlistSymbol.priority_rank.asc(), WatchlistSymbol.id.asc())
            )
            for monitor_state, symbol_row in query.all():
                result["candidateCount"] += 1
                candidate = self._submit_crypto_entry_candidate(
                    db,
                    monitor_state=monitor_state,
                    symbol_row=symbol_row,
                )
                result["rows"].append(candidate)
                if candidate.get("intentId"):
                    result["intentCount"] += 1
                action = str(candidate.get("action") or "")
                if action in {"ENTRY_SUBMITTED", "ENTRY_FILLED"}:
                    result["submittedCount"] += 1
                if action == "ENTRY_FILLED":
                    result["filledCount"] += 1
                elif action in {"GATE_REJECTED", "SUBMISSION_REJECTED"}:
                    result["rejectedCount"] += 1
                elif action == "SKIPPED":
                    result["skippedCount"] += 1

        return result

    @staticmethod
    def _resolve_account_id(*, mode: str, account: dict[str, Any] | None = None) -> str:
        if account:
            candidate = str(account.get("accountId") or account.get("account_id") or "").strip()
            if candidate:
                return candidate
        creds = tradier_client._credentials_for_mode(mode)
        return str(creds.get("account_id") or mode or "TRADIER").strip() or "TRADIER"

    @staticmethod
    def _intent_mode_matches(intent: OrderIntent, expected_mode: str | None) -> bool:
        if not expected_mode:
            return True
        context = dict(intent.context_json or {})
        actual_mode = str(context.get("mode") or "").upper().strip()
        return not actual_mode or actual_mode == str(expected_mode).upper().strip()

    @staticmethod
    def _entry_block_payload(*, monitor_state: WatchlistMonitorState, symbol_row: WatchlistSymbol, reason: str) -> dict[str, Any]:
        lifecycle = describe_lifecycle(getattr(monitor_state, "monitoring_status", None))
        return {
            "symbol": str(symbol_row.symbol or "").upper(),
            "uploadId": symbol_row.upload_id,
            "priorityRank": symbol_row.priority_rank,
            "setupTemplate": symbol_row.setup_template,
            "exitTemplate": symbol_row.exit_template,
            "action": "SKIPPED",
            "reason": reason,
            "intentId": None,
            "submittedOrderId": None,
            "positionId": None,
            "tradeId": None,
            "lifecycleState": lifecycle.state,
            "lifecycleNote": lifecycle.operator_note,
        }

    def _submit_stock_entry_candidate(
        self,
        db: Session,
        *,
        monitor_state: WatchlistMonitorState,
        symbol_row: WatchlistSymbol,
        mode: str,
        account_cache: dict[str, Any],
    ) -> dict[str, Any]:
        symbol = str(symbol_row.symbol or "").upper()
        payload: dict[str, Any] = {
            "symbol": symbol,
            "uploadId": symbol_row.upload_id,
            "priorityRank": symbol_row.priority_rank,
            "setupTemplate": symbol_row.setup_template,
            "exitTemplate": symbol_row.exit_template,
            "action": None,
            "reason": None,
            "intentId": None,
            "submittedOrderId": None,
            "positionId": None,
            "tradeId": None,
        }

        account_id = self._resolve_account_id(mode=mode)

        if self._has_open_position(db, symbol, account_id=account_id):
            payload["action"] = "SKIPPED"
            payload["reason"] = "OPEN_POSITION_EXISTS"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        if self._has_active_entry_intent(db, symbol, asset_class="stock", account_id=account_id, mode=mode):
            payload["action"] = "SKIPPED"
            payload["reason"] = "ACTIVE_ENTRY_INTENT_EXISTS"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        latest_details = dict((monitor_state.decision_context_json or {}).get("latestEvaluation", {}).get("details", {}) or {})
        current_price = self._safe_float(latest_details.get("currentPrice"))
        if current_price <= 0:
            quote = tradier_client.get_quote_sync(symbol, mode=mode)
            current_price = self._safe_float((quote or {}).get("last") or (quote or {}).get("close"))
        if current_price <= 0:
            payload["action"] = "SKIPPED"
            payload["reason"] = "ENTRY_PRICE_UNAVAILABLE"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        account, cash_available, account_error = self._get_account_snapshot(mode=mode, account_cache=account_cache)
        if account_error:
            payload["action"] = "SKIPPED"
            payload["reason"] = f"ACCOUNT_SNAPSHOT_UNAVAILABLE: {account_error}"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        remaining_cash = self._safe_float(account_cache.get("remainingCash"))
        if remaining_cash <= 0:
            remaining_cash = self._safe_float(cash_available)
        if remaining_cash <= 0:
            payload["action"] = "SKIPPED"
            payload["reason"] = "NO_CASH_AVAILABLE"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        stock_exposure = self._get_open_stock_exposure(db, account_id=account_id)
        positions = self._calculate_stock_positions_compat(
            symbol,
            remaining_cash,
            current_price,
            stock_exposure,
        )
        sized = next((row for row in positions if int(row.get("shares") or 0) > 0), None)
        if sized is None:
            payload["action"] = "SKIPPED"
            payload["reason"] = "POSITION_SIZER_RETURNED_ZERO"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        shares = int(sized.get("shares") or 0)
        estimated_value = self._safe_float(sized.get("estimated_value")) or (current_price * shares)
        if estimated_value <= 0:
            payload["action"] = "SKIPPED"
            payload["reason"] = "ESTIMATED_VALUE_UNAVAILABLE"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        gate_context = {
            "watchlist": {
                "uploadId": symbol_row.upload_id,
                "scope": symbol_row.scope,
                "priorityRank": symbol_row.priority_rank,
                "tier": symbol_row.tier,
                "bias": symbol_row.bias,
                "setupTemplate": symbol_row.setup_template,
                "exitTemplate": symbol_row.exit_template,
                "riskFlags": symbol_row.risk_flags or [],
                "botTimeframes": symbol_row.bot_timeframes or [],
            }
        }
        gate = pre_trade_gate.evaluate_stock_order_sync(
            ticker=symbol,
            shares=shares,
            mode=mode,
            account=account,
            db=db,
            execution_source=ENTRY_EXECUTION_SOURCE,
            decision_context=gate_context,
        )
        gate_payload = gate.to_dict()

        account_id = self._resolve_account_id(mode=mode, account=account)

        if not gate.allowed:
            reject_intent = execution_lifecycle.create_order_intent(
                db,
                account_id=account_id,
                asset_class="stock",
                symbol=symbol,
                side="BUY",
                requested_quantity=shares,
                requested_price=current_price,
                execution_source=ENTRY_EXECUTION_SOURCE,
                context={
                    "mode": mode,
                    "watchlist": gate_context["watchlist"],
                    "estimatedValue": estimated_value,
                    "positionPct": sized.get("position_pct"),
                    "gate": gate_payload,
                },
            )
            execution_lifecycle.mark_rejected_by_gate(
                db,
                reject_intent,
                reason=gate.rejection_reason or "Pre-trade gate rejected the watchlist entry.",
                gate_payload=gate_payload,
            )
            payload.update({
                "action": "GATE_REJECTED",
                "reason": gate.rejection_reason or "Pre-trade gate rejected the watchlist entry.",
                "intentId": reject_intent.intent_id,
            })
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        self._reserve_account_cash(account_cache, estimated_value)

        intent = execution_lifecycle.create_order_intent(
            db,
            account_id=account_id,
            asset_class="stock",
            symbol=symbol,
            side="BUY",
            requested_quantity=shares,
            requested_price=current_price,
            execution_source=ENTRY_EXECUTION_SOURCE,
            context={
                "mode": mode,
                "watchlist": gate_context["watchlist"],
                "estimatedValue": estimated_value,
                "positionPct": sized.get("position_pct"),
                "gate": gate_payload,
            },
        )
        payload["intentId"] = intent.intent_id

        try:
            order_snapshot = tradier_client.place_order_sync(
                ticker=symbol,
                qty=shares,
                side="buy",
                mode=mode,
                order_type="market",
            )
            execution_lifecycle.record_submission(db, intent, order_snapshot)
        except Exception as exc:
            is_network_uncertain = exc.__class__.__name__ in {"RequestException", "Timeout", "ConnectTimeout", "ReadTimeout"}
            if is_network_uncertain:
                reentry_blocked_until = self._utcnow() + timedelta(minutes=5)
                execution_lifecycle.mark_submission_uncertain(
                    db,
                    intent,
                    reason=f"Broker submit timed out before acknowledgement for {symbol}",
                    payload={
                        "error": str(exc),
                        "gate": gate_payload,
                        "reentryBlockedUntilUtc": reentry_blocked_until.isoformat(),
                    },
                )
                payload.update({
                    "action": "SUBMISSION_PENDING",
                    "reason": "BROKER_SUBMIT_ACK_UNCERTAIN",
                    "intentId": intent.intent_id,
                    "reentryBlockedUntilUtc": reentry_blocked_until.isoformat(),
                })
                self._record_entry_execution(db, monitor_state, payload)
                db.commit()
                return payload

            self._release_account_cash(account_cache, estimated_value)
            execution_lifecycle.record_event(
                db,
                intent,
                event_type="ORDER_SUBMISSION_FAILED",
                status="REJECTED",
                message=f"Watchlist entry submission failed for {symbol}: {exc}",
                payload={"error": str(exc), "gate": gate_payload},
            )
            intent.status = "REJECTED"
            intent.rejection_reason = str(exc)
            db.commit()
            db.refresh(intent)
            payload.update({
                "action": "SUBMISSION_REJECTED",
                "reason": str(exc),
                "intentId": intent.intent_id,
            })
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        confirmed_order = self._confirm_stock_order_sync(order_snapshot, mode=mode)
        intent = execution_lifecycle.refresh_from_order_snapshot(db, intent, confirmed_order)
        fill_record = execution_lifecycle.materialize_stock_fill(
            db,
            intent,
            strategy="WATCHLIST_ENTRY",
            stop_loss=current_price * (1 - settings.STOP_LOSS_PCT),
            profit_target=current_price * (1 + settings.PROFIT_TARGET_PCT),
            trailing_stop=current_price * (1 - settings.TRAILING_STOP_PCT),
            current_price=current_price,
        )
        if fill_record is not None:
            payload.update({
                "action": "ENTRY_FILLED",
                "reason": intent.rejection_reason,
                "submittedOrderId": intent.submitted_order_id,
                "positionId": fill_record.get("position_id"),
                "tradeId": fill_record.get("trade_id"),
            })
        else:
            payload.update({
                "action": "ENTRY_SUBMITTED",
                "reason": intent.rejection_reason,
                "submittedOrderId": intent.submitted_order_id,
            })
        self._record_entry_execution(db, monitor_state, payload)
        db.commit()
        return payload

    @staticmethod
    def _parse_iso_datetime(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _crypto_reentry_cooldown_state(
        self,
        monitor_state: WatchlistMonitorState,
        *,
        observed_at: datetime,
    ) -> tuple[bool, datetime | None, str | None]:
        context = dict(monitor_state.decision_context_json or {})
        blocked_until = self._parse_iso_datetime(context.get("reentryBlockedUntilUtc"))
        if blocked_until is not None and blocked_until > observed_at:
            return True, blocked_until, "CRYPTO_REENTRY_COOLDOWN_ACTIVE"
        last_exit_at = self._parse_iso_datetime(context.get("lastExitAtUtc"))
        if (
            str(monitor_state.latest_decision_state or "").upper() == "EXIT_FILLED"
            and last_exit_at is not None
            and (observed_at - last_exit_at).total_seconds() < 60
        ):
            return True, blocked_until, "CRYPTO_EXIT_JUST_FILLED"
        return False, blocked_until, None

    def _submit_crypto_entry_candidate(
        self,
        db: Session,
        *,
        monitor_state: WatchlistMonitorState,
        symbol_row: WatchlistSymbol,
    ) -> dict[str, Any]:
        symbol = str(symbol_row.symbol or "").upper().strip()
        quote_currency = str(symbol_row.quote_currency or "USD").upper().strip()
        pair = f"{symbol}/{quote_currency}"
        resolved_pair = kraken_service.resolve_pair(pair)
        payload: dict[str, Any] = {
            "symbol": pair,
            "uploadId": symbol_row.upload_id,
            "priorityRank": symbol_row.priority_rank,
            "setupTemplate": symbol_row.setup_template,
            "exitTemplate": symbol_row.exit_template,
            "action": None,
            "reason": None,
            "intentId": None,
            "submittedOrderId": None,
            "positionId": None,
            "tradeId": None,
        }

        if resolved_pair is None:
            payload["action"] = "SKIPPED"
            payload["reason"] = "PAIR_UNRESOLVED"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        cooldown_active, blocked_until, cooldown_reason = self._crypto_reentry_cooldown_state(
            monitor_state,
            observed_at=self._utcnow(),
        )
        if cooldown_active:
            payload["action"] = "SKIPPED"
            payload["reason"] = cooldown_reason
            if blocked_until is not None:
                payload["reentryBlockedUntilUtc"] = blocked_until.isoformat()
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        lifecycle = describe_lifecycle(monitor_state.monitoring_status)
        if not lifecycle.allows_entry:
            payload.update({
                "action": "SKIPPED",
                "reason": "MANAGED_ONLY_ENTRY_BLOCKED" if lifecycle.state == "MANAGED_ONLY" else "ENTRY_NOT_ALLOWED",
                "lifecycleState": lifecycle.state,
                "lifecycleNote": lifecycle.operator_note,
            })
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        if self._has_open_crypto_position(pair):
            payload["action"] = "SKIPPED"
            payload["reason"] = "OPEN_POSITION_EXISTS"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        if self._has_active_entry_intent(db, pair, asset_class="crypto", account_id="paper-crypto-ledger", mode="PAPER"):
            payload["action"] = "SKIPPED"
            payload["reason"] = "ACTIVE_ENTRY_INTENT_EXISTS"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        latest_details = dict((monitor_state.decision_context_json or {}).get("latestEvaluation", {}).get("details", {}) or {})
        current_price = self._safe_float(latest_details.get("currentPrice"))
        if current_price <= 0:
            ticker = kraken_service.get_ticker(resolved_pair.rest_pair)
            current_price = self._safe_float((ticker or {}).get("c", [0.0])[0] if ticker else 0.0)
        if current_price <= 0:
            payload["action"] = "SKIPPED"
            payload["reason"] = "ENTRY_PRICE_UNAVAILABLE"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        available_balance = float(getattr(crypto_ledger, "balance", 0.0) or 0.0)
        if available_balance <= 0:
            payload["action"] = "SKIPPED"
            payload["reason"] = "NO_CASH_AVAILABLE"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        crypto_exposure = self._get_open_crypto_exposure()
        positions = self._calculate_crypto_positions_compat(
            pair,
            available_balance,
            current_price,
            crypto_exposure,
        )
        sized = next((row for row in positions if self._safe_float(row.get("amount")) > 0), None)
        if sized is None:
            payload["action"] = "SKIPPED"
            payload["reason"] = "POSITION_SIZER_RETURNED_ZERO"
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        amount = self._safe_float(sized.get("amount"))
        intent = execution_lifecycle.create_order_intent(
            db,
            account_id="paper-crypto-ledger",
            asset_class="crypto",
            symbol=pair,
            side="BUY",
            requested_quantity=amount,
            requested_price=current_price,
            execution_source=ENTRY_EXECUTION_SOURCE,
            context={
                "mode": "PAPER",
                "watchlist": {
                    "uploadId": symbol_row.upload_id,
                    "scope": symbol_row.scope,
                    "priorityRank": symbol_row.priority_rank,
                    "tier": symbol_row.tier,
                    "bias": symbol_row.bias,
                    "setupTemplate": symbol_row.setup_template,
                    "exitTemplate": symbol_row.exit_template,
                    "riskFlags": symbol_row.risk_flags or [],
                    "botTimeframes": symbol_row.bot_timeframes or [],
                },
                "estimatedValue": sized.get("estimated_value"),
                "positionPct": sized.get("position_pct"),
                "ohlcvPair": resolved_pair.rest_pair,
                "displayPair": resolved_pair.display_pair,
            },
        )
        payload["intentId"] = intent.intent_id

        trade = crypto_ledger.execute_trade(
            pair,
            resolved_pair.rest_pair,
            "BUY",
            amount,
            current_price,
        )
        trade_status = str(trade.get("status") or "").upper()
        trade_reason = str(trade.get("reason") or "") or None
        trade_timestamp = trade.get("timestamp")
        event_time = datetime.fromisoformat(str(trade_timestamp).replace("Z", "+00:00")) if trade_timestamp else self._utcnow()

        if trade_status != "FILLED":
            intent.status = "REJECTED"
            intent.rejection_reason = trade_reason or "Crypto paper ledger rejected the watchlist entry."
            execution_lifecycle.record_event(
                db,
                intent,
                event_type="ORDER_SUBMISSION_FAILED",
                status="REJECTED",
                message=f"Crypto watchlist entry failed for {pair}: {intent.rejection_reason}",
                payload=trade,
                event_time=event_time,
            )
            db.commit()
            db.refresh(intent)
            payload.update({
                "action": "SUBMISSION_REJECTED",
                "reason": intent.rejection_reason,
                "tradeId": trade.get("id"),
            })
            self._record_entry_execution(db, monitor_state, payload)
            db.commit()
            return payload

        intent.status = "FILLED"
        intent.submitted_order_id = str(trade.get("id") or intent.submitted_order_id or "") or None
        intent.submitted_at = event_time
        intent.first_fill_at = event_time
        intent.last_fill_at = event_time
        intent.filled_quantity = amount
        intent.avg_fill_price = current_price
        intent.rejection_reason = None
        execution_lifecycle.record_event(
            db,
            intent,
            event_type="ORDER_SUBMITTED",
            status="SUBMITTED",
            message=f"Crypto paper ledger accepted order for {pair}",
            payload=trade,
            event_time=event_time,
        )
        execution_lifecycle.record_event(
            db,
            intent,
            event_type="ORDER_STATUS_UPDATED",
            status="FILLED",
            message=f"Confirmed fill for {pair}: {amount} filled",
            payload=trade,
            event_time=event_time,
        )
        db.commit()
        db.refresh(intent)
        payload.update({
            "action": "ENTRY_FILLED",
            "reason": None,
            "submittedOrderId": intent.submitted_order_id,
            "tradeId": trade.get("id"),
        })
        self._record_entry_execution(db, monitor_state, payload)
        db.commit()
        return payload

    def _record_entry_execution(self, db: Session, monitor_state: WatchlistMonitorState, payload: dict[str, Any]) -> None:
        recorded_at = self._utcnow()
        context = dict(monitor_state.decision_context_json or {})
        lifecycle = describe_lifecycle(monitor_state.monitoring_status)
        context["lifecycleState"] = payload.get("lifecycleState") or lifecycle.state
        context["lifecycleNote"] = payload.get("lifecycleNote") or lifecycle.operator_note
        context["entryExecution"] = {
            "action": payload.get("action"),
            "reason": payload.get("reason"),
            "intentId": payload.get("intentId"),
            "submittedOrderId": payload.get("submittedOrderId"),
            "positionId": payload.get("positionId"),
            "tradeId": payload.get("tradeId"),
            "lifecycleState": payload.get("lifecycleState") or lifecycle.state,
            "lifecycleNote": payload.get("lifecycleNote") or lifecycle.operator_note,
            "reentryBlockedUntilUtc": payload.get("reentryBlockedUntilUtc") or context.get("reentryBlockedUntilUtc"),
            "recordedAtUtc": recorded_at.isoformat(),
        }
        monitor_state.decision_context_json = context
        flag_modified(monitor_state, "decision_context_json")
        action = str(payload.get("action") or "").strip()
        reason = payload.get("reason")
        if reason is not None:
            monitor_state.latest_decision_reason = str(reason)
        if action == "SKIPPED" and str(reason or "").strip() == "OPEN_POSITION_EXISTS":
            monitor_state.latest_decision_state = MONITOR_ONLY
            monitor_state.latest_decision_reason = "Open position exists; symbol is now managed under exit rules."
        elif action in {"ENTRY_FILLED", "ENTRY_SUBMITTED", "GATE_REJECTED", "SUBMISSION_REJECTED", "SUBMISSION_PENDING"}:
            monitor_state.latest_decision_state = action if action != "SUBMISSION_PENDING" else SUBMISSION_PENDING_DECISION_STATE
        elif action == "SKIPPED" and str(reason or "").strip() in {"CRYPTO_REENTRY_COOLDOWN_ACTIVE", "CRYPTO_EXIT_JUST_FILLED"}:
            monitor_state.latest_decision_state = COOLDOWN_DECISION_STATE
        monitor_state.last_decision_at_utc = recorded_at
        db.add(monitor_state)
        db.flush()
        db.query(WatchlistMonitorState).filter(WatchlistMonitorState.id == monitor_state.id).update(
            {
                WatchlistMonitorState.decision_context_json: context,
                WatchlistMonitorState.latest_decision_reason: monitor_state.latest_decision_reason,
                WatchlistMonitorState.latest_decision_state: monitor_state.latest_decision_state,
                WatchlistMonitorState.last_decision_at_utc: monitor_state.last_decision_at_utc,
            },
            synchronize_session=False,
        )
        db.flush()
        db.refresh(monitor_state)

    def _get_account_snapshot(
        self,
        *,
        mode: str,
        account_cache: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, float, str | None]:
        if not account_cache.get("loaded"):
            account_cache["loaded"] = True
            try:
                account = tradier_client.get_account_snapshot(mode)
                cash_available = float(account.get("cash") or account.get("buyingPower") or account.get("portfolioValue") or 0.0)
                account_cache["account"] = account
                account_cache["cashAvailable"] = cash_available
                account_cache["remainingCash"] = cash_available
                account_cache["reservedCash"] = 0.0
                account_cache["error"] = None
            except Exception as exc:
                account_cache["account"] = None
                account_cache["cashAvailable"] = 0.0
                account_cache["remainingCash"] = 0.0
                account_cache["reservedCash"] = 0.0
                account_cache["error"] = str(exc)
        remaining_cash = float(account_cache.get("remainingCash") or account_cache.get("cashAvailable") or 0.0)
        return account_cache.get("account"), remaining_cash, account_cache.get("error")

    @staticmethod
    def _calculate_stock_positions_compat(
        symbol: str,
        remaining_cash: float,
        current_price: float,
        stock_exposure: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates = [{"ticker": symbol}]
        prices = {symbol: current_price}
        try:
            return position_sizer.calculate_stock_positions(
                candidates,
                remaining_cash,
                prices=prices,
                current_open_positions=int(stock_exposure.get("openCount") or 0),
                current_symbol_exposure=dict(stock_exposure.get("symbolExposure") or {}),
            )
        except TypeError as exc:
            message = str(exc)
            if "current_open_positions" not in message and "current_symbol_exposure" not in message:
                raise
            return position_sizer.calculate_stock_positions(
                candidates,
                remaining_cash,
                prices=prices,
            )

    @staticmethod
    def _calculate_crypto_positions_compat(
        pair: str,
        available_balance: float,
        current_price: float,
        crypto_exposure: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates = [{"pair": pair}]
        prices = {pair: current_price}
        try:
            return position_sizer.calculate_crypto_positions(
                candidates,
                available_balance,
                prices=prices,
                current_open_positions=int(crypto_exposure.get("openCount") or 0),
                current_symbol_exposure=dict(crypto_exposure.get("symbolExposure") or {}),
            )
        except TypeError as exc:
            message = str(exc)
            if "current_open_positions" not in message and "current_symbol_exposure" not in message:
                raise
            return position_sizer.calculate_crypto_positions(
                candidates,
                available_balance,
                prices=prices,
            )

    @staticmethod
    def _reserve_account_cash(account_cache: dict[str, Any], amount: float) -> None:
        reserve_amount = max(float(amount or 0.0), 0.0)
        if reserve_amount <= 0:
            return
        remaining = float(account_cache.get("remainingCash") or 0.0)
        account_cache["remainingCash"] = max(remaining - reserve_amount, 0.0)
        account_cache["reservedCash"] = float(account_cache.get("reservedCash") or 0.0) + reserve_amount

    @staticmethod
    def _release_account_cash(account_cache: dict[str, Any], amount: float) -> None:
        release_amount = max(float(amount or 0.0), 0.0)
        if release_amount <= 0:
            return
        reserved = float(account_cache.get("reservedCash") or 0.0)
        released = min(reserved, release_amount)
        account_cache["reservedCash"] = max(reserved - released, 0.0)
        account_cache["remainingCash"] = float(account_cache.get("remainingCash") or 0.0) + released


    @staticmethod
    def _get_open_stock_exposure(db: Session, *, account_id: str | None = None) -> dict[str, Any]:
        query = db.query(Position).filter(Position.is_open.is_(True))
        if account_id:
            query = query.filter(Position.account_id == account_id)
        rows = query.all()
        symbol_exposure: dict[str, float] = {}
        open_count = 0
        for row in rows:
            shares = max(int(row.shares or 0), 0)
            if shares <= 0:
                continue
            symbol = str(row.ticker or '').upper().strip()
            if not symbol:
                continue
            price = float(row.current_price or row.avg_entry_price or 0.0)
            exposure_value = max(price, 0.0) * shares
            symbol_exposure[symbol] = symbol_exposure.get(symbol, 0.0) + exposure_value
            open_count += 1
        return {"openCount": open_count, "symbolExposure": symbol_exposure}

    @staticmethod
    def _get_open_crypto_exposure() -> dict[str, Any]:
        ledger = crypto_ledger.get_ledger()
        positions = dict(ledger.get('positions') or {})
        symbol_exposure: dict[str, float] = {}
        open_count = 0
        prices = kraken_service.get_prices(list(positions.keys())) if positions else {}
        for pair, payload in positions.items():
            try:
                amount = float((payload or {}).get('amount') or 0.0)
            except (AttributeError, TypeError, ValueError):
                amount = 0.0
            if amount <= 0:
                continue
            symbol = str(pair or '').upper().strip()
            if not symbol:
                continue
            try:
                current_price = float((prices.get(pair) or {}).get('last') or prices.get(pair) or 0.0)
            except (AttributeError, TypeError, ValueError):
                current_price = 0.0
            exposure_value = max(current_price, 0.0) * amount
            if exposure_value <= 0:
                try:
                    exposure_value = float((payload or {}).get('total_cost') or 0.0)
                except (AttributeError, TypeError, ValueError):
                    exposure_value = 0.0
            symbol_exposure[symbol] = symbol_exposure.get(symbol, 0.0) + exposure_value
            open_count += 1
        return {"openCount": open_count, "symbolExposure": symbol_exposure}

    @staticmethod
    def _has_open_position(db: Session, symbol: str, *, account_id: str | None = None) -> bool:
        query = db.query(Position).filter(Position.ticker == symbol, Position.is_open.is_(True))
        if account_id:
            query = query.filter(Position.account_id == account_id)
        return query.first() is not None

    def _has_active_entry_intent(
        self,
        db: Session,
        symbol: str,
        *,
        asset_class: str = "stock",
        account_id: str | None = None,
        mode: str | None = None,
    ) -> bool:
        query = (
            db.query(OrderIntent)
            .filter(
                OrderIntent.asset_class == asset_class,
                OrderIntent.symbol == symbol,
                OrderIntent.side == "BUY",
                OrderIntent.execution_source == ENTRY_EXECUTION_SOURCE,
                OrderIntent.status.in_(ACTIVE_ENTRY_INTENT_STATUSES),
            )
            .order_by(OrderIntent.created_at.desc(), OrderIntent.id.desc())
        )
        if account_id:
            query = query.filter(OrderIntent.account_id == account_id)
        for intent in query.all():
            if self._intent_mode_matches(intent, mode):
                return True
        return False

    @staticmethod
    def _has_open_crypto_position(pair: str) -> bool:
        positions = getattr(crypto_ledger, "positions", {}) or {}
        return str(pair or "").upper().strip() in {str(key).upper().strip() for key in positions.keys()}

    @staticmethod
    def _confirm_stock_order_sync(order_snapshot: dict[str, Any], *, mode: str) -> dict[str, Any]:
        snapshot = order_snapshot
        normalized = tradier_client.normalize_order_response(snapshot)
        order_id = normalized.get("id")
        if normalized.get("is_terminal") or normalized.get("filled_quantity", 0) > 0 or not order_id:
            return snapshot

        attempts = max(int(settings.ORDER_FILL_CONFIRM_RETRIES), 0)
        for _ in range(attempts):
            time.sleep(float(settings.ORDER_FILL_CONFIRM_DELAY_SECONDS))
            snapshot = tradier_client.get_order_sync(str(order_id), mode=mode)
            normalized = tradier_client.normalize_order_response(snapshot)
            if normalized.get("is_terminal") or normalized.get("filled_quantity", 0) > 0:
                break
        return snapshot

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _query_due_rows(db: Session, *, scope: WATCHLIST_SCOPE, observed_at: datetime):
        return (
            db.query(WatchlistMonitorState)
            .filter(
                WatchlistMonitorState.scope == scope,
                WatchlistMonitorState.monitoring_status.in_(ELIGIBLE_DUE_STATUSES),
            )
            .filter(
                (WatchlistMonitorState.latest_decision_state == PENDING_EVALUATION)
                | (WatchlistMonitorState.next_evaluation_at_utc.is_(None))
                | (WatchlistMonitorState.next_evaluation_at_utc <= observed_at)
            )
        )

    def _count_due_rows(self, db: Session, *, scope: WATCHLIST_SCOPE, observed_at: datetime) -> int:
        return self._query_due_rows(db, scope=scope, observed_at=observed_at).count()


watchlist_monitoring_orchestrator = WatchlistMonitoringOrchestrator()