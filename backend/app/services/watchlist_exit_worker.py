from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.trade import Trade
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.services.execution_lifecycle import execution_lifecycle
from app.services.kraken_service import crypto_ledger, kraken_service
from app.services.market_sessions import get_scope_session_status
from app.services.runtime_state import runtime_state
from app.services.tradier_client import tradier_client
from app.services.watchlist_service import watchlist_service

logger = logging.getLogger(__name__)

ACTIVE_EXIT_INTENT_STATUSES = {"READY", "SUBMITTED", "PARTIALLY_FILLED"}
REPLAYABLE_CRYPTO_INTENT_STATUSES = {"FILLED", "CLOSED"}
ACTIVE_BROKER_EXIT_ORDER_STATUSES = {
    "OPEN",
    "PENDING",
    "SUBMITTED",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "NEW",
}
CRYPTO_STALE_EXIT_INTENT_MINUTES = 3
EXECUTION_SOURCE = "WATCHLIST_EXIT_WORKER"

EXIT_TRIGGER = "TIME_STOP_EXPIRED"
STOP_LOSS_TRIGGER = "STOP_LOSS_BREACH"
TRAILING_STOP_TRIGGER = "TRAILING_STOP_BREACH"
PROFIT_TARGET_TRIGGER = "PROFIT_TARGET_REACHED"
FOLLOW_THROUGH_TRIGGER = "FAILED_FOLLOW_THROUGH"

SUPPORTED_SCOPES = ("stocks_only", "crypto_only")
IMPULSE_TRAIL_STOP_FACTOR = 0.5


@dataclass
class ExitWorkerRuntime:
    enabled: bool = True
    poll_seconds: int = 20
    last_started_at_utc: str | None = None
    last_finished_at_utc: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_run_summary: dict[str, Any] = field(default_factory=dict)


class WatchlistExitWorkerService:
    def __init__(self) -> None:
        self._runtime = ExitWorkerRuntime(
            enabled=bool(settings.WATCHLIST_EXIT_WORKER_ENABLED),
            poll_seconds=max(int(settings.WATCHLIST_EXIT_WORKER_POLL_SECONDS), 5),
        )

    def get_status(self, db: Session) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        snapshot = self._build_scope_snapshot(db, scope="stocks_only", observed_at=observed_at)
        rows = list(snapshot.get("rows") or [])
        status_rows = [self._build_status_row(db, row) for row in rows]
        status_rows = [row for row in status_rows if row.get("exitTrigger") or row.get("exitAlreadyInProgress")]
        candidate_exit_count = sum(1 for row in status_rows if not row.get("exitAlreadyInProgress"))
        blocked_expired_count = sum(
            1
            for row in status_rows
            if row.get("positionState", {}).get("positionExpired") and row.get("reason") == "STOCK_SESSION_CLOSED"
        )
        eligible_expired_count = sum(
            1
            for row in status_rows
            if row.get("positionState", {}).get("positionExpired") and row.get("reason") != "STOCK_SESSION_CLOSED"
        )
        return {
            "scope": "stocks_only",
            "enabled": self._runtime.enabled,
            "pollSeconds": self._runtime.poll_seconds,
            "lastStartedAtUtc": self._runtime.last_started_at_utc,
            "lastFinishedAtUtc": self._runtime.last_finished_at_utc,
            "lastError": self._runtime.last_error,
            "consecutiveFailures": self._runtime.consecutive_failures,
            "summary": {
                "candidateExitCount": candidate_exit_count,
                "expiredPositionCount": int(snapshot.get("summary", {}).get("expiredPositionCount") or 0),
                "eligibleExpiredCount": eligible_expired_count,
                "blockedExpiredCount": blocked_expired_count,
            },
            "rows": status_rows,
        }

    def _build_status_row(self, db: Session, row: dict[str, Any]) -> dict[str, Any]:
        candidate = self._build_candidate_row(db, row, enforce_session_open=True)
        return {
            "symbol": candidate.get("displaySymbol") or candidate.get("symbol") or row.get("symbol"),
            "managedOnly": bool(row.get("managedOnly")),
            "positionState": row.get("positionState", {}),
            "monitoringStatus": candidate.get("monitoringStatus") or row.get("monitoringStatus"),
            "latestDecisionState": row.get("latestDecisionState") or row.get("monitoring", {}).get("latestDecisionState"),
            "exitTrigger": candidate.get("exitTrigger") or self._primary_exit_trigger(row),
            "exitReasons": candidate.get("exitReasons") or self._build_exit_reasons(row),
            "exitAlreadyInProgress": candidate.get("action") == "EXIT_ALREADY_IN_PROGRESS",
            "reason": candidate.get("reason"),
            "brokerExitPending": bool(candidate.get("brokerExitPending")),
            "brokerReservedQuantity": int(candidate.get("brokerReservedQuantity") or 0),
            "brokerAvailableQuantity": float(candidate.get("brokerAvailableQuantity") or 0),
        }

    def run_once(self, db: Session, *, limit: int = 25) -> dict[str, Any]:
        self._runtime.last_started_at_utc = datetime.now(UTC).isoformat()
        try:
            run_summary = self.run_exit_sweep(db, execute=True, limit=limit)
            self._runtime.last_finished_at_utc = datetime.now(UTC).isoformat()
            self._runtime.last_error = None
            self._runtime.consecutive_failures = 0
            self._runtime.last_run_summary = run_summary
            return run_summary
        except Exception as exc:
            self._runtime.last_finished_at_utc = datetime.now(UTC).isoformat()
            self._runtime.last_error = str(exc)
            self._runtime.consecutive_failures += 1
            raise

    def run_exit_sweep(
        self,
        db: Session,
        *,
        execute: bool = False,
        limit: int = 25,
    ) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        runtime = runtime_state.get()
        refreshed_price_count = self._refresh_open_position_prices(
            db,
            mode=runtime.stock_mode,
            skip_existing_exit_signals=not execute,
        )

        due_rows = self._collect_rows(db, observed_at=observed_at, limit=limit)
        rows: list[dict[str, Any]] = []
        summary = {
            "candidateCount": 0,
            "entryCandidateCount": 0,
            "submittedCount": 0,
            "filledCount": 0,
            "blockedCount": 0,
            "skippedCount": 0,
            "closedCount": 0,
            "alreadyInProgressCount": 0,
            "protectiveExitCount": 0,
            "profitTargetCount": 0,
            "followThroughExitCount": 0,
            "scaleOutSubmittedCount": 0,
            "expiredPositionCount": 0,
            "refreshedPriceCount": refreshed_price_count,
        }

        for row in due_rows:
            candidate = self._build_candidate_row(db, row, enforce_session_open=execute)
            action = str(candidate.get("action") or "")
            if not candidate.get("exitTrigger"):
                continue
            if candidate.get("positionState", {}).get("positionExpired"):
                summary["expiredPositionCount"] += 1
            exit_reasons = set(candidate.get("exitReasons") or [])
            if {STOP_LOSS_TRIGGER, TRAILING_STOP_TRIGGER}.intersection(exit_reasons) or candidate.get("exitTrigger") in {
                STOP_LOSS_TRIGGER,
                TRAILING_STOP_TRIGGER,
            }:
                summary["protectiveExitCount"] += 1
            if PROFIT_TARGET_TRIGGER in exit_reasons or candidate.get("exitTrigger") == PROFIT_TARGET_TRIGGER:
                summary["profitTargetCount"] += 1
            if FOLLOW_THROUGH_TRIGGER in exit_reasons or candidate.get("exitTrigger") == FOLLOW_THROUGH_TRIGGER:
                summary["followThroughExitCount"] += 1

            if not execute:
                if action == "BLOCKED":
                    summary["blockedCount"] += 1
                    rows.append(candidate)
                    summary["candidateCount"] += 1
                elif action == "EXIT_ALREADY_IN_PROGRESS":
                    summary["alreadyInProgressCount"] += 1
                    rows.append(candidate)
                    summary["candidateCount"] += 1
                elif action or candidate.get("requestedQuantity"):
                    candidate["action"] = "DRY_RUN_CANDIDATE"
                    rows.append(candidate)
                    summary["candidateCount"] += 1
                continue

            if action == "BLOCKED":
                summary["blockedCount"] += 1
                rows.append(candidate)
                summary["candidateCount"] += 1
                continue
            if action == "EXIT_ALREADY_IN_PROGRESS":
                summary["alreadyInProgressCount"] += 1
                rows.append(candidate)
                summary["candidateCount"] += 1
                continue
            if action == "SKIPPED":
                summary["skippedCount"] += 1
                rows.append(candidate)
                summary["candidateCount"] += 1
                continue

            if str(candidate.get("assetClass") or "").lower() == "crypto":
                result = self._submit_crypto_exit_candidate(db, candidate)
            else:
                result = self._submit_stock_exit_candidate(db, candidate)

            rows.append(result)
            summary["candidateCount"] += 1
            final_action = str(result.get("action") or "").upper()
            if final_action in {"EXIT_SUBMITTED", "SCALE_OUT_SUBMITTED", "EXIT_CLOSED", "EXIT_FILLED"}:
                summary["submittedCount"] += 1
            if final_action in {"EXIT_CLOSED", "EXIT_FILLED"}:
                summary["closedCount"] += 1
                summary["filledCount"] += 1
            if final_action == "SCALE_OUT_SUBMITTED":
                summary["scaleOutSubmittedCount"] += 1
            if final_action == "BLOCKED":
                summary["blockedCount"] += 1
            if final_action == "EXIT_ALREADY_IN_PROGRESS":
                summary["alreadyInProgressCount"] += 1

        return {
            "observedAtUtc": observed_at.isoformat(),
            "execute": execute,
            "summary": summary,
            "rows": rows,
        }

    def _collect_rows(self, db: Session, *, observed_at: datetime, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for scope in SUPPORTED_SCOPES:
            snapshot = self._build_scope_snapshot(db, scope=scope, observed_at=observed_at)
            scope_rows = list(snapshot.get("rows") or [])
            rows.extend(scope_rows[:limit])
        return rows

    def _build_scope_snapshot(self, db: Session, *, scope: str, observed_at: datetime) -> dict[str, Any]:
        try:
            snapshot = watchlist_service.get_exit_readiness_snapshot(db, scope=scope)
            rows = []
            for row in list(snapshot.get("rows") or []):
                normalized = dict(row)
                normalized.setdefault("scope", scope)
                rows.append(normalized)
            snapshot = dict(snapshot)
            snapshot["rows"] = rows
            return snapshot
        except Exception as exc:
            logger.warning(
                "Watchlist exit worker could not build %s exit readiness snapshot: %s",
                scope,
                exc,
            )
            return {"scope": scope, "summary": {}, "rows": []}

    @staticmethod
    def _primary_exit_trigger(row: dict[str, Any]) -> str | None:
        position_state = row.get("positionState", {}) or {}
        exit_reasons = position_state.get("exitReasons") or position_state.get("protectiveExitReasons") or []
        if exit_reasons:
            return str(exit_reasons[0])
        if position_state.get("stopLossBreached"):
            return STOP_LOSS_TRIGGER
        if position_state.get("trailingStopBreached"):
            return TRAILING_STOP_TRIGGER
        if position_state.get("scaleOutReady"):
            return PROFIT_TARGET_TRIGGER
        if position_state.get("followThroughFailed") or position_state.get("failedFollowThrough"):
            return FOLLOW_THROUGH_TRIGGER
        if position_state.get("positionExpired"):
            return EXIT_TRIGGER
        return None

    @staticmethod
    def _build_exit_reasons(row: dict[str, Any]) -> list[str]:
        position_state = row.get("positionState", {}) or {}
        seeded = list(position_state.get("exitReasons") or position_state.get("protectiveExitReasons") or [])
        reasons: list[str] = [str(reason) for reason in seeded if str(reason or "").strip()]
        if position_state.get("stopLossBreached"):
            reasons.append(STOP_LOSS_TRIGGER)
        if position_state.get("trailingStopBreached"):
            reasons.append(TRAILING_STOP_TRIGGER)
        if position_state.get("scaleOutReady"):
            reasons.append(PROFIT_TARGET_TRIGGER)
        if position_state.get("followThroughFailed") or position_state.get("failedFollowThrough"):
            reasons.append(FOLLOW_THROUGH_TRIGGER)
        if position_state.get("positionExpired"):
            reasons.append(EXIT_TRIGGER)
        deduped: list[str] = []
        for reason in reasons:
            if reason not in deduped:
                deduped.append(reason)
        return deduped

    @staticmethod
    def _determine_requested_quantity(*, trigger: str | None, available_quantity: int) -> int:
        if available_quantity <= 0:
            return 0
        if trigger == PROFIT_TARGET_TRIGGER:
            return max(int(available_quantity // 2), 1)
        return int(available_quantity)

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _build_candidate_row(self, db: Session, row: dict[str, Any], *, enforce_session_open: bool = True) -> dict[str, Any]:
        scope = str(row.get("scope") or "stocks_only")
        if scope == "crypto_only":
            return self._build_crypto_candidate_row(db, row)

        mode = runtime_state.get().stock_mode
        symbol = str(row.get("symbol") or "").upper().strip()
        exit_trigger = self._primary_exit_trigger(row)
        exit_reasons = self._build_exit_reasons(row)
        payload = {
            "scope": scope,
            "assetClass": row.get("assetClass") or "stock",
            "symbol": row.get("symbol"),
            "displaySymbol": row.get("symbol"),
            "managedOnly": bool(row.get("managedOnly")),
            "monitoringStatus": row.get("monitoringStatus"),
            "positionId": row.get("positionState", {}).get("positionId"),
            "positionState": row.get("positionState", {}),
            "currentPrice": self._safe_float(row.get("positionState", {}).get("currentPrice")),
            "exitTemplate": row.get("exitTemplate"),
            "exitTrigger": exit_trigger,
            "exitReasons": exit_reasons,
            "action": None,
            "reason": None,
            "brokerQuantity": 0,
            "brokerReservedQuantity": 0,
            "brokerAvailableQuantity": 0,
            "brokerPendingOrders": [],
            "brokerExitPending": False,
            "requestedQuantity": 0,
            "quantityTruth": None,
        }

        if self._has_blocking_exit_intent(db, row):
            payload["action"] = "EXIT_ALREADY_IN_PROGRESS"
            payload["reason"] = "EXIT_INTENT_ALREADY_ACTIVE"
            payload["monitoringStatus"] = "EXIT_PENDING"
            return payload

        if not exit_trigger:
            return payload

        broker_state = self._get_broker_exit_state(symbol, mode=mode)
        payload.update(
            {
                "brokerQuantity": int(broker_state.get("brokerQuantity") or 0),
                "brokerReservedQuantity": int(broker_state.get("reservedQuantity") or 0),
                "brokerAvailableQuantity": int(broker_state.get("availableQuantity") or 0),
                "brokerPendingOrders": list(broker_state.get("pendingOrders") or []),
                "brokerExitPending": bool(broker_state.get("pendingOrders")),
                "requestedQuantity": self._determine_requested_quantity(
                    trigger=exit_trigger,
                    available_quantity=int(broker_state.get("availableQuantity") or 0),
                ),
                "quantityTruth": self._build_stock_quantity_truth(db, symbol=symbol, broker_state=broker_state),
            }
        )

        if payload["brokerExitPending"]:
            payload["action"] = "EXIT_ALREADY_IN_PROGRESS"
            payload["reason"] = "BROKER_EXIT_PENDING"
            payload["monitoringStatus"] = "EXIT_PENDING"
            return payload

        if enforce_session_open and not bool(get_scope_session_status("stocks_only", datetime.now(UTC)).session_open):
            payload["action"] = "BLOCKED"
            payload["reason"] = "STOCK_SESSION_CLOSED"
            payload["monitoringStatus"] = "WAITING_FOR_MARKET_OPEN"
            return payload

        if int(payload["brokerAvailableQuantity"]) <= 0:
            payload["action"] = "SKIPPED"
            payload["reason"] = "NO_SELLABLE_QUANTITY"
            return payload

        return payload

    def _build_stock_quantity_truth(self, db: Session, *, symbol: str, broker_state: dict[str, Any]) -> dict[str, Any]:
        broker_quantity = int(broker_state.get("brokerQuantity") or 0)
        reserved_quantity = int(broker_state.get("reservedQuantity") or 0)
        available_quantity = int(broker_state.get("availableQuantity") or 0)
        db_quantity = int(
            sum(
                int(row.shares or 0)
                for row in db.query(Position).filter(Position.ticker == symbol, Position.is_open.is_(True)).all()
            )
        )
        expected_quantity = db_quantity if db_quantity > 0 else broker_quantity
        return {
            "expectedPositionQty": expected_quantity,
            "brokerReportedQty": broker_quantity,
            "pendingOpenOrdersQty": reserved_quantity,
            "sellableQty": available_quantity,
            "brokerQuantity": broker_quantity,
            "reservedQuantity": reserved_quantity,
            "availableQuantity": available_quantity,
            "dbQuantity": db_quantity,
            "dbOpenQuantity": db_quantity,
            "pendingExitQuantity": reserved_quantity,
            "sellableQuantity": available_quantity,
            "quantityDelta": broker_quantity - expected_quantity,
            "requestedQuantity": available_quantity,
            "quantitySource": "BROKER_AVAILABLE",
            "dbTruthAvailable": expected_quantity > 0,
            "driftDetected": expected_quantity > 0 and broker_quantity != expected_quantity,
        }

    def _get_latest_exit_intent(self, db: Session, row: dict[str, Any]) -> OrderIntent | None:
        symbol = str(row.get("symbol") or "").upper().strip()
        scope = str(row.get("scope") or "")
        query = db.query(OrderIntent).filter(
            OrderIntent.side == "SELL",
            OrderIntent.execution_source == EXECUTION_SOURCE,
        )
        if scope == "crypto_only":
            aliases = sorted(self._crypto_symbol_aliases(symbol))
            query = query.filter(
                OrderIntent.asset_class == "crypto",
                OrderIntent.symbol.in_(aliases),
            )
        else:
            query = query.filter(
                OrderIntent.asset_class == "stock",
                OrderIntent.symbol == symbol,
            )
        return query.order_by(OrderIntent.created_at.desc(), OrderIntent.id.desc()).first()

    def _should_retry_stale_crypto_exit_intent(self, intent: OrderIntent) -> bool:
        status = str(intent.status or "").upper()
        if status not in ACTIVE_EXIT_INTENT_STATUSES:
            return False
        if str(intent.asset_class or "").lower() != "crypto":
            return False
        if str(intent.execution_source or "") != EXECUTION_SOURCE:
            return False
        if str(intent.submitted_order_id or "").strip():
            return False

        reference_time = (
            intent.last_fill_at
            or intent.first_fill_at
            or intent.submitted_at
            or intent.updated_at
            or intent.created_at
        )
        if reference_time is None:
            return True
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=UTC)
        age_seconds = max((datetime.now(UTC) - reference_time).total_seconds(), 0.0)
        return age_seconds >= (CRYPTO_STALE_EXIT_INTENT_MINUTES * 60)

    def _mark_stale_crypto_exit_intent_for_retry(self, db: Session, intent: OrderIntent, *, symbol: str) -> None:
        execution_lifecycle.record_event(
            db,
            intent,
            event_type="EXIT_RETRY_UNSTICKED",
            status="FAILED",
            message=f"Clearing stale crypto exit intent for retry: {symbol}",
            payload={"symbol": symbol, "reason": "STALE_CRYPTO_EXIT_INTENT_WITHOUT_SUBMITTED_ORDER"},
        )
        intent.status = "FAILED"
        intent.rejection_reason = "Stale crypto exit intent cleared for retry"
        db.flush()

    def _has_blocking_exit_intent(self, db: Session, row: dict[str, Any]) -> bool:
        latest_intent = self._get_latest_exit_intent(db, row)
        if latest_intent is None:
            return False

        scope = str(row.get("scope") or "")
        symbol = str(row.get("symbol") or "").upper().strip()
        status = str(latest_intent.status or "").upper()

        if scope == "crypto_only" and self._should_retry_stale_crypto_exit_intent(latest_intent):
            self._mark_stale_crypto_exit_intent_for_retry(db, latest_intent, symbol=symbol)
            db.commit()
            return False

        return status in ACTIVE_EXIT_INTENT_STATUSES

    @staticmethod
    def _call_broker_with_optional_use_cache(callable_obj: Any, *args: Any, use_cache: bool = True, **kwargs: Any) -> Any:
        try:
            return callable_obj(*args, use_cache=use_cache, **kwargs)
        except TypeError:
            return callable_obj(*args, **kwargs)

    @classmethod
    def _get_broker_exit_state(cls, symbol: str, *, mode: str, use_cache: bool = True) -> dict[str, Any]:
        if not tradier_client.is_ready(mode=mode):
            return {
                "brokerQuantity": 0,
                "reservedQuantity": 0,
                "availableQuantity": 0,
                "pendingOrders": [],
            }

        broker_quantity = int(
            cls._call_broker_with_optional_use_cache(
                tradier_client.get_position_quantity_sync,
                symbol,
                mode=mode,
                use_cache=use_cache,
            )
            or 0
        )
        pending_orders = cls._call_broker_with_optional_use_cache(
            tradier_client.get_orders_sync,
            mode=mode,
            symbol=symbol,
            side="SELL",
            statuses=list(ACTIVE_BROKER_EXIT_ORDER_STATUSES),
        ) or []

        reserved_quantity = 0
        normalized_orders: list[dict[str, Any]] = []
        for row in pending_orders:
            remaining_quantity = row.get("remaining_quantity")
            if remaining_quantity is None:
                requested_quantity = int(row.get("requested_quantity") or row.get("quantity") or 0)
                filled_quantity = int(row.get("filled_quantity") or row.get("exec_quantity") or 0)
                remaining_quantity = max(requested_quantity - filled_quantity, 0)
            remaining_quantity = int(remaining_quantity or 0)
            reserved_quantity += remaining_quantity
            normalized_orders.append(
                {
                    "id": row.get("id"),
                    "symbol": row.get("symbol") or symbol,
                    "side": row.get("side") or "SELL",
                    "status": row.get("status"),
                    "requestedQuantity": int(row.get("requested_quantity") or row.get("quantity") or 0),
                    "filledQuantity": int(row.get("filled_quantity") or row.get("exec_quantity") or 0),
                    "remainingQuantity": remaining_quantity,
                }
            )

        available_quantity = max(broker_quantity - reserved_quantity, 0)
        return {
            "brokerQuantity": broker_quantity,
            "reservedQuantity": reserved_quantity,
            "availableQuantity": available_quantity,
            "pendingOrders": normalized_orders,
        }

    @staticmethod
    def _refresh_open_position_prices(db: Session, *, mode: str, skip_existing_exit_signals: bool = False) -> int:
        del skip_existing_exit_signals

        if not tradier_client.is_ready(mode=mode):
            return 0

        positions = (
            db.query(Position)
            .filter(Position.is_open.is_(True))
            .order_by(Position.ticker.asc(), Position.id.asc())
            .all()
        )
        if not positions:
            return 0

        symbols = [str(row.ticker or "").upper().strip() for row in positions if str(row.ticker or "").strip()]
        if not symbols:
            return 0

        try:
            quotes = tradier_client.get_quotes_sync(symbols, mode=mode) or {}
        except Exception:
            logger.exception("Watchlist exit worker failed to refresh open position prices")
            return 0

        blocked_symbols: set[str] = set()
        template_by_symbol: dict[str, str] = {}
        snapshot = watchlist_service.get_exit_readiness_snapshot(db, scope="stocks_only")
        for row in list(snapshot.get("rows") or []):
            position_state = row.get("positionState", {}) or {}
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            template_by_symbol[symbol] = str(row.get("exitTemplate") or "").strip().lower()
            if (
                position_state.get("stopLossBreached")
                or position_state.get("trailingStopBreached")
                or position_state.get("followThroughFailed")
                or position_state.get("positionExpired")
                or position_state.get("timeStopExtended")
            ):
                blocked_symbols.add(symbol)

        refreshed_count = 0
        for position in positions:
            symbol = str(position.ticker or "").upper().strip()
            if symbol in blocked_symbols:
                continue
            exit_template = template_by_symbol.get(symbol, "")
            quote = quotes.get(symbol) or {}
            last_price = quote.get("last")
            if last_price in {None, ""}:
                continue
            try:
                current_price = float(last_price)
            except (TypeError, ValueError):
                continue

            position.current_price = current_price
            prior_peak_price = float(position.peak_price or 0.0)
            trail_pct = settings.TRAILING_STOP_PCT
            if exit_template == "trail_after_impulse" and position.profit_target and current_price >= float(position.profit_target):
                trail_pct = settings.TRAILING_STOP_PCT * IMPULSE_TRAIL_STOP_FACTOR

            if current_price > prior_peak_price:
                position.peak_price = current_price
                position.trailing_stop = max(
                    float(position.trailing_stop or 0.0),
                    round(current_price * (1.0 - trail_pct), 4),
                )

            if position.profit_target and current_price >= float(position.profit_target) and position.trailing_stop is None:
                position.trailing_stop = round(current_price * (1.0 - trail_pct), 4)

            trade = (
                db.query(Trade)
                .filter(Trade.ticker == symbol, Trade.account_id == position.account_id)
                .order_by(Trade.entry_time.desc(), Trade.id.desc())
                .first()
            )
            if trade is not None and current_price > prior_peak_price:
                position.trailing_stop = max(
                    float(position.trailing_stop or 0.0),
                    round(current_price * (1.0 - trail_pct), 4),
                )

            refreshed_count += 1

        db.commit()
        return refreshed_count

    @staticmethod
    def _crypto_symbol_aliases(symbol: str | None) -> set[str]:
        raw = str(symbol or "").strip().upper()
        if not raw:
            return set()

        aliases: set[str] = set()

        def add(text: str) -> None:
            normalized = str(text or "").strip().upper()
            if not normalized:
                return
            aliases.add(normalized)
            compact = "".join(ch for ch in normalized if ch.isalnum())
            if compact:
                aliases.add(compact)
            if "/" in normalized:
                base, _, quote = normalized.partition("/")
                if base:
                    aliases.add(base)
                if base and quote:
                    aliases.add(f"{base}{quote}")
            elif normalized.endswith("USD") and len(normalized) > 3:
                base = normalized[:-3]
                aliases.add(base)
                aliases.add(f"{base}/USD")

        add(raw)

        resolved = kraken_service.resolve_pair(raw)
        if resolved is not None:
            add(getattr(resolved, "display_pair", None) or "")
            add(getattr(resolved, "ws_pair", None) or "")
            add(getattr(resolved, "altname", None) or "")
            add(getattr(resolved, "rest_pair", None) or "")
            add(getattr(resolved, "pair_key", None) or "")

        return {item for item in aliases if item}

    def _find_crypto_ledger_position(self, symbol: str, *, db: Session | None = None) -> dict[str, Any] | None:
        aliases = self._crypto_symbol_aliases(symbol)
        try:
            positions = crypto_ledger.get_positions(db=db)
        except TypeError:
            positions = crypto_ledger.get_positions()
        for row in positions:
            pair = str(row.get("pair") or "").upper().strip()
            if pair in aliases or self._crypto_symbol_aliases(pair).intersection(aliases):
                return row
        return None

    def _resolve_crypto_ohlcv_pair(self, symbol: str | None) -> str | None:
        pair = str(symbol or "").strip()
        if not pair:
            return None
        resolved = kraken_service.resolve_pair(pair)
        if resolved is not None:
            return resolved.pair_key
        try:
            return kraken_service.get_ohlcv_pair(pair)
        except Exception:
            return None

    def _canonical_crypto_display_pair(
        self,
        symbol: str | None,
        *,
        ledger_pair: str | None = None,
    ) -> str:
        candidates = [
            str(symbol or "").strip(),
            str(ledger_pair or "").strip(),
        ]
        for candidate in candidates:
            if not candidate:
                continue

            resolved = kraken_service.resolve_pair(candidate)
            if resolved is not None:
                for value in (
                    getattr(resolved, "display_pair", None),
                    getattr(resolved, "ws_pair", None),
                    getattr(resolved, "altname", None),
                ):
                    text = str(value or "").strip().upper()
                    if text:
                        if "/" not in text and text.endswith("USD"):
                            return f"{text[:-3]}/USD"
                        return text

            try:
                display_pair = kraken_service.get_display_pair(candidate)
            except Exception:
                display_pair = None

            text = str(display_pair or "").strip().upper()
            if text:
                if "/" not in text and text.endswith("USD"):
                    return f"{text[:-3]}/USD"
                return text

        fallback = str(symbol or ledger_pair or "").strip().upper()
        if fallback and "/" not in fallback and fallback.endswith("USD"):
            return f"{fallback[:-3]}/USD"
        return fallback

    def _canonical_crypto_intent_symbol(
        self,
        symbol: str | None,
        *,
        ledger_pair: str | None = None,
    ) -> str:
        return self._canonical_crypto_display_pair(symbol, ledger_pair=ledger_pair)

    def _get_crypto_exit_quantity_truth(self, db: Session, *, symbol: str, ledger_quantity: float) -> dict[str, Any]:
        aliases = sorted(self._crypto_symbol_aliases(symbol))
        db_net_open_quantity = 0.0
        buy_filled_quantity = 0.0
        sell_filled_quantity = 0.0

        intents = (
            db.query(OrderIntent)
            .filter(
                OrderIntent.asset_class == "crypto",
                OrderIntent.account_id == "paper-crypto-ledger",
                OrderIntent.status.in_(REPLAYABLE_CRYPTO_INTENT_STATUSES),
            )
            .order_by(OrderIntent.created_at.asc(), OrderIntent.id.asc())
            .all()
        )

        for intent in intents:
            intent_symbol = str(intent.symbol or "").upper().strip()
            if not intent_symbol:
                continue
            if intent_symbol not in aliases and not self._crypto_symbol_aliases(intent_symbol).intersection(aliases):
                continue

            filled_quantity = self._safe_float(intent.filled_quantity)
            if filled_quantity <= 0:
                continue

            side = str(intent.side or "").upper().strip()
            if side == "BUY":
                buy_filled_quantity += filled_quantity
                db_net_open_quantity += filled_quantity
            elif side == "SELL":
                sell_filled_quantity += filled_quantity
                db_net_open_quantity -= filled_quantity

        db_net_open_quantity = max(round(db_net_open_quantity, 12), 0.0)
        ledger_quantity = max(round(float(ledger_quantity or 0.0), 12), 0.0)
        db_truth_available = buy_filled_quantity > 0 or sell_filled_quantity > 0

        if ledger_quantity > 0 and db_net_open_quantity > 0:
            requested_exit_quantity = min(ledger_quantity, db_net_open_quantity)
            quantity_source = "MIN_LEDGER_DB_NET" if abs(ledger_quantity - db_net_open_quantity) > 1e-8 else "LEDGER"
            reason = "CRYPTO_EXIT_QTY_CLAMPED_TO_DB_NET_OPEN" if quantity_source == "MIN_LEDGER_DB_NET" else None
        elif db_truth_available and ledger_quantity > 0:
            requested_exit_quantity = ledger_quantity
            quantity_source = "LEDGER"
            reason = None
        elif ledger_quantity > 0:
            requested_exit_quantity = 0.0
            quantity_source = "UNVERIFIED_LEDGER_BLOCKED"
            reason = "CRYPTO_EXIT_QTY_BLOCKED_UNVERIFIED_LEDGER"
        else:
            requested_exit_quantity = 0.0
            quantity_source = "NONE"
            reason = None

        drift_detected = (
            ledger_quantity > 0
            and db_net_open_quantity > 0
            and abs(ledger_quantity - db_net_open_quantity) > 1e-8
        )

        return {
            "aliases": aliases,
            "ledgerOpenQuantity": ledger_quantity,
            "dbNetOpenQuantity": db_net_open_quantity,
            "buyFilledQuantity": round(buy_filled_quantity, 12),
            "sellFilledQuantity": round(sell_filled_quantity, 12),
            "requestedExitQuantity": round(requested_exit_quantity, 12),
            "quantitySource": quantity_source,
            "dbTruthAvailable": db_truth_available,
            "driftDetected": drift_detected,
            "reason": reason,
        }

    def _get_recent_insufficient_crypto_rejection(self, db: Session, *, symbol: str) -> OrderIntent | None:
        aliases = sorted(self._crypto_symbol_aliases(symbol))
        cutoff = datetime.now(UTC) - timedelta(minutes=10)
        return (
            db.query(OrderIntent)
            .filter(
                OrderIntent.asset_class == "crypto",
                OrderIntent.account_id == "paper-crypto-ledger",
                OrderIntent.execution_source == EXECUTION_SOURCE,
                OrderIntent.side == "SELL",
                OrderIntent.status == "REJECTED",
                OrderIntent.symbol.in_(aliases),
                OrderIntent.updated_at >= cutoff,
                OrderIntent.rejection_reason.ilike("%Insufficient%position%"),
            )
            .order_by(OrderIntent.updated_at.desc(), OrderIntent.id.desc())
            .first()
        )

    def _build_crypto_candidate_row(self, db: Session, row: dict[str, Any]) -> dict[str, Any]:
        symbol = str(row.get("symbol") or "").upper().strip()
        position_state = row.get("positionState", {}) or {}
        ledger_position = self._find_crypto_ledger_position(symbol, db=db)
        ledger_quantity = self._safe_float((ledger_position or {}).get("amount"))
        quantity_truth = self._get_crypto_exit_quantity_truth(db, symbol=symbol, ledger_quantity=ledger_quantity)
        requested_quantity = self._safe_float(quantity_truth.get("requestedExitQuantity"))
        ledger_pair = (ledger_position or {}).get("pair") or row.get("symbol")
        display_pair = self._canonical_crypto_display_pair(symbol, ledger_pair=ledger_pair)

        payload = {
            "scope": "crypto_only",
            "assetClass": "crypto",
            "symbol": display_pair,
            "displaySymbol": display_pair,
            "managedOnly": bool(row.get("managedOnly")),
            "monitoringStatus": row.get("monitoringStatus") or ("EXIT_PENDING" if position_state.get("protectiveExitPending") else None),
            "positionId": None,
            "positionState": position_state,
            "exitTemplate": row.get("exitTemplate"),
            "exitTrigger": self._primary_exit_trigger(row),
            "exitReasons": self._build_exit_reasons(row),
            "action": None,
            "reason": None,
            "brokerQuantity": ledger_quantity,
            "brokerReservedQuantity": 0,
            "brokerAvailableQuantity": requested_quantity,
            "brokerExitPending": False,
            "brokerPendingOrders": [],
            "requestedQuantity": requested_quantity,
            "quantityTruth": quantity_truth,
            "ledgerPair": ledger_pair,
            "ohlcvPair": (ledger_position or {}).get("ohlcvPair") or self._resolve_crypto_ohlcv_pair(ledger_pair),
            "currentPrice": self._safe_float((ledger_position or {}).get("currentPrice") or position_state.get("currentPrice")),
            "avgEntryPrice": self._safe_float((ledger_position or {}).get("avgPrice") or position_state.get("avgEntryPrice")),
        }

        if self._has_blocking_exit_intent(db, row):
            payload["action"] = "EXIT_ALREADY_IN_PROGRESS"
            payload["reason"] = "EXIT_INTENT_ALREADY_ACTIVE"
            payload["monitoringStatus"] = "EXIT_PENDING"
            return payload

        recent_reject = self._get_recent_insufficient_crypto_rejection(db, symbol=symbol)
        if recent_reject is not None:
            payload["action"] = "BLOCKED"
            payload["reason"] = "RECENT_INSUFFICIENT_POSITION_REJECTION"
            payload["monitoringStatus"] = "EXIT_PENDING"
            payload["lastRejectedReason"] = recent_reject.rejection_reason
            return payload

        if requested_quantity <= 0:
            if not bool(quantity_truth.get("dbTruthAvailable")) and ledger_quantity > 0:
                payload["action"] = "BLOCKED"
                payload["reason"] = "CRYPTO_EXIT_QTY_BLOCKED_UNVERIFIED_LEDGER"
            else:
                payload["action"] = "SKIPPED"
                payload["reason"] = "NO_OPEN_QUANTITY"
            return payload

        if bool(quantity_truth.get("driftDetected")):
            payload["reason"] = str(quantity_truth.get("reason") or "CRYPTO_EXIT_QTY_CLAMPED_TO_TRUTH")

        return payload

    def _submit_stock_exit_candidate(self, db: Session, candidate: dict[str, Any]) -> dict[str, Any]:
        mode = runtime_state.get().stock_mode
        symbol = str(candidate.get("symbol") or "").upper().strip()
        requested_quantity = int(candidate.get("requestedQuantity") or 0)
        if requested_quantity <= 0:
            candidate["action"] = "SKIPPED"
            candidate["reason"] = "NO_SELLABLE_QUANTITY"
            return candidate

        session_state = get_scope_session_status("stocks_only", datetime.now(UTC))
        if not bool(getattr(session_state, "session_open", False)):
            candidate["action"] = "BLOCKED"
            candidate["reason"] = "STOCK_SESSION_CLOSED"
            return candidate

        intent = OrderIntent(
            intent_id=f"intent_{uuid4().hex[:24]}",
            account_id="paper",
            asset_class="stock",
            symbol=symbol,
            side="SELL",
            requested_quantity=requested_quantity,
            requested_price=self._safe_float(candidate.get("currentPrice")),
            status="READY",
            execution_source=EXECUTION_SOURCE,
            position_id=candidate.get("positionId"),
            submitted_at=datetime.now(UTC),
            context_json={
                "exitTrigger": candidate.get("exitTrigger"),
                "exitReasons": candidate.get("exitReasons") or [],
                "quantityTruth": candidate.get("quantityTruth") or {},
            },
        )
        db.add(intent)
        db.flush()

        try:
            response = tradier_client.place_order_sync(
                symbol,
                requested_quantity,
                "sell",
                mode=mode,
                order_type="market",
                duration="day",
            )
        except Exception as exc:
            message = str(exc)
            if "more shares than your current long position" in message.lower():
                broker_state = self._get_broker_exit_state(symbol, mode=mode, use_cache=False)
                candidate["brokerQuantity"] = int(broker_state.get("brokerQuantity") or 0)
                candidate["brokerReservedQuantity"] = int(broker_state.get("reservedQuantity") or 0)
                candidate["brokerAvailableQuantity"] = int(broker_state.get("availableQuantity") or 0)
                candidate["brokerPendingOrders"] = list(broker_state.get("pendingOrders") or [])
                candidate["brokerExitPending"] = bool(candidate["brokerPendingOrders"])
                candidate["quantityTruth"] = self._build_stock_quantity_truth(db, symbol=symbol, broker_state=broker_state)
                candidate["requestedQuantity"] = min(requested_quantity, int(broker_state.get("availableQuantity") or 0))
                candidate["reconciliation"] = {
                    "brokerState": broker_state,
                    "pendingOrders": broker_state.get("pendingOrders") or [],
                }
                if broker_state.get("pendingOrders"):
                    candidate["action"] = "EXIT_ALREADY_IN_PROGRESS"
                    candidate["reason"] = "BROKER_EXIT_PENDING_AFTER_REJECTION"
                    candidate["monitoringStatus"] = "EXIT_PENDING"
                    intent.status = "SKIPPED"
                    execution_lifecycle.record_event(
                        db,
                        intent,
                        event_type="EXIT_RECONCILED",
                        status="SKIPPED",
                        message=f"Reconciled pending stock exit for {symbol} after broker oversell rejection",
                        payload={
                            "brokerState": broker_state,
                            "pendingOrders": broker_state.get("pendingOrders") or [],
                            "quantityTruth": candidate.get("quantityTruth") or {},
                        },
                    )
                else:
                    candidate["action"] = "SKIPPED"
                    candidate["reason"] = "BROKER_POSITION_INSUFFICIENT"
                    intent.status = "SKIPPED"
                    intent.rejection_reason = message
                    execution_lifecycle.record_event(
                        db,
                        intent,
                        event_type="EXIT_RECONCILED",
                        status="SKIPPED",
                        message=f"Skipped stock exit for {symbol} after broker oversell rejection",
                        payload={
                            "brokerState": broker_state,
                            "quantityTruth": candidate.get("quantityTruth") or {},
                        },
                    )
                db.commit()
                return candidate
            intent.status = "FAILED"
            intent.rejection_reason = message
            db.commit()
            raise

        order = response.get("order") or {}
        order_id = str(order.get("id") or "").strip()
        intent.status = "SUBMITTED"
        intent.submitted_order_id = order_id or None

        execution_lifecycle.record_event(
            db,
            intent,
            event_type="EXIT_SUBMITTED",
            status="SUBMITTED",
            message=f"Submitted stock exit for {symbol}",
            payload={"symbol": symbol, "requestedQuantity": requested_quantity, "orderId": order_id},
        )

        final_order = tradier_client.get_order_sync(order_id, mode=mode) if order_id else {"order": order}
        final_order_payload = final_order.get("order") or {}
        final_status = str(final_order_payload.get("status") or "").lower()
        filled_quantity = int(final_order_payload.get("exec_quantity") or final_order_payload.get("quantity") or requested_quantity)
        avg_fill_price = self._safe_float(final_order_payload.get("avg_fill_price") or candidate.get("currentPrice"))

        if requested_quantity < int(candidate.get("brokerQuantity") or requested_quantity):
            intent.status = "FILLED" if final_status == "filled" else "SUBMITTED"
            intent.filled_quantity = filled_quantity
            intent.avg_fill_price = avg_fill_price
            if final_status == "filled":
                intent.first_fill_at = datetime.now(UTC)
                intent.last_fill_at = datetime.now(UTC)
                self._finalize_stock_exit(
                    db,
                    symbol=symbol,
                    filled_quantity=filled_quantity,
                    fill_price=avg_fill_price,
                    trigger=str(candidate.get("exitTrigger") or PROFIT_TARGET_TRIGGER),
                    order_id=order_id or None,
                )
                candidate["action"] = "SCALE_OUT_SUBMITTED"
                candidate["closedShares"] = filled_quantity
                candidate["remainingShares"] = max(int(candidate.get("brokerQuantity") or 0) - filled_quantity, 0)
                db.commit()
                return candidate

        if final_status == "filled":
            intent.status = "CLOSED"
            intent.filled_quantity = filled_quantity
            intent.avg_fill_price = avg_fill_price
            intent.first_fill_at = intent.first_fill_at or datetime.now(UTC)
            intent.last_fill_at = datetime.now(UTC)
            self._finalize_stock_exit(
                db,
                symbol=symbol,
                filled_quantity=filled_quantity,
                fill_price=avg_fill_price,
                trigger=str(candidate.get("exitTrigger") or EXIT_TRIGGER),
                order_id=order_id or None,
            )
            execution_lifecycle.record_event(
                db,
                intent,
                event_type="EXIT_FILLED",
                status="CLOSED",
                message=f"Filled stock exit for {symbol}",
                payload={"symbol": symbol, "filledQuantity": filled_quantity, "avgFillPrice": avg_fill_price},
            )
            candidate["action"] = "EXIT_CLOSED"
            candidate["filledQuantity"] = filled_quantity
            candidate["filledPrice"] = avg_fill_price
            db.commit()
            return candidate

        candidate["action"] = "EXIT_SUBMITTED"
        db.commit()
        return candidate

    def _submit_crypto_exit_candidate(self, db: Session, candidate: dict[str, Any]) -> dict[str, Any]:
        symbol = str(candidate.get("symbol") or "").upper().strip()
        pair = str(candidate.get("ledgerPair") or symbol).upper().strip()
        display_pair = self._canonical_crypto_display_pair(symbol, ledger_pair=pair)
        intent_symbol = self._canonical_crypto_intent_symbol(symbol, ledger_pair=pair)
        ohlcv_pair = candidate.get("ohlcvPair")
        requested_quantity = self._safe_float(candidate.get("requestedQuantity"))
        current_price = self._safe_float(candidate.get("currentPrice"))

        candidate["symbol"] = intent_symbol
        candidate["displaySymbol"] = display_pair

        if requested_quantity <= 0:
            candidate["action"] = "SKIPPED"
            candidate["reason"] = "NO_SELLABLE_QUANTITY"
            return candidate

        intent = OrderIntent(
            intent_id=f"intent_{uuid4().hex[:24]}",
            account_id="paper-crypto-ledger",
            asset_class="crypto",
            symbol=intent_symbol,
            side="SELL",
            requested_quantity=requested_quantity,
            requested_price=current_price,
            status="READY",
            execution_source=EXECUTION_SOURCE,
            submitted_at=datetime.now(UTC),
            context_json={
                "displayPair": display_pair,
                "ledgerPair": pair,
                "ohlcvPair": ohlcv_pair,
                "quantityTruth": candidate.get("quantityTruth"),
            },
        )
        db.add(intent)
        db.flush()

        execution_lifecycle.record_event(
            db,
            intent,
            event_type="EXIT_SUBMITTED",
            status="READY",
            message=f"Submitted crypto exit for {display_pair}",
            payload={
                "symbol": intent_symbol,
                "displayPair": display_pair,
                "ledgerPair": pair,
                "requestedQuantity": requested_quantity,
                "ohlcvPair": ohlcv_pair,
            },
        )

        trade = crypto_ledger.execute_trade(
            db=db,
            pair=pair,
            ohlcv_pair=ohlcv_pair,
            side="SELL",
            amount=requested_quantity,
            price=current_price,
            source=EXECUTION_SOURCE,
            intent_id=intent.intent_id,
        )
        status = str(trade.get("status") or "").upper()
        if status == "FILLED":
            intent.status = "CLOSED"
            intent.filled_quantity = requested_quantity
            intent.avg_fill_price = current_price
            intent.submitted_order_id = trade.get("id")
            intent.first_fill_at = datetime.now(UTC)
            intent.last_fill_at = datetime.now(UTC)
            execution_lifecycle.record_event(
                db,
                intent,
                event_type="EXIT_FILLED",
                status="CLOSED",
                message=f"Filled crypto exit for {display_pair}",
                payload={**trade, "symbol": intent_symbol, "displayPair": display_pair, "ledgerPair": pair},
            )
            self._apply_crypto_cooldown(
                db,
                symbol=symbol,
                fill_payload={**trade, "displayPair": display_pair, "ledgerPair": pair},
            )
            candidate["action"] = "EXIT_CLOSED"
            candidate["filledQuantity"] = requested_quantity
            candidate["filledPrice"] = current_price
            db.commit()
            return candidate

        intent.status = "REJECTED"
        intent.rejection_reason = str(trade.get("reason") or "Crypto exit rejected")
        execution_lifecycle.record_event(
            db,
            intent,
            event_type="EXIT_REJECTED",
            status="REJECTED",
            message=f"Rejected crypto exit for {display_pair}",
            payload={**trade, "symbol": intent_symbol, "displayPair": display_pair, "ledgerPair": pair},
        )
        candidate["action"] = "BLOCKED"
        candidate["reason"] = "CRYPTO_EXIT_REJECTED"
        db.commit()
        return candidate

    def _apply_crypto_cooldown(self, db: Session, *, symbol: str, fill_payload: dict[str, Any]) -> None:
        aliases = sorted(self._crypto_symbol_aliases(symbol))
        monitor_state = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.symbol.in_(aliases))
            .order_by(WatchlistMonitorState.id.desc())
            .first()
        )
        if monitor_state is None:
            return

        blocked_until = datetime.now(UTC) + timedelta(minutes=15)
        context_json = dict(monitor_state.decision_context_json or {})
        entry_execution = dict(context_json.get("entryExecution") or {})
        display_pair = self._canonical_crypto_display_pair(
            symbol,
            ledger_pair=str(fill_payload.get("ledgerPair") or fill_payload.get("pair") or symbol),
        )

        entry_execution.update(
            {
                "action": "EXIT_FILLED",
                "reason": "CRYPTO_LEDGER_EXIT_FILLED",
                "filledQuantity": fill_payload.get("amount"),
                "filledPrice": fill_payload.get("price"),
                "displayPair": display_pair,
            }
        )
        context_json.update(
            {
                "lastExitAtUtc": datetime.now(UTC).isoformat(),
                "lastExitReason": "CRYPTO_LEDGER_EXIT_FILLED",
                "reentryBlockedUntilUtc": blocked_until.isoformat(),
                "cooldownActive": True,
                "entryExecution": entry_execution,
                "exitExecution": {
                    "action": "EXIT_FILLED",
                    "filledQuantity": fill_payload.get("amount"),
                    "filledPrice": fill_payload.get("price"),
                    "displayPair": display_pair,
                },
            }
        )
        monitor_state.monitoring_status = "COOLDOWN"
        monitor_state.latest_decision_state = "EXIT_FILLED"
        monitor_state.latest_decision_reason = "CRYPTO_LEDGER_EXIT_FILLED"
        monitor_state.decision_context_json = context_json
        db.flush()

    def _finalize_stock_exit(
        self,
        db: Session,
        *,
        symbol: str,
        filled_quantity: int,
        fill_price: float,
        trigger: str,
        order_id: str | None = None,
    ) -> None:
        position = (
            db.query(Position)
            .filter(Position.ticker == symbol, Position.is_open.is_(True))
            .order_by(Position.entry_time.desc(), Position.id.desc())
            .first()
        )
        if position is None:
            return

        original_shares = int(position.shares or 0)
        position.current_price = fill_price
        position.peak_price = max(float(position.peak_price or 0.0), float(fill_price or 0.0))
        remaining = max(original_shares - int(filled_quantity), 0)
        if remaining <= 0:
            position.shares = 0
            position.is_open = False
        else:
            position.shares = remaining
            if trigger == PROFIT_TARGET_TRIGGER:
                position.stop_loss = max(float(position.stop_loss or 0.0), float(position.avg_entry_price or 0.0))
                position.trailing_stop = max(
                    float(position.trailing_stop or 0.0),
                    round(float(fill_price or 0.0) * (1.0 - settings.TRAILING_STOP_PCT), 4),
                )

        trade = (
            db.query(Trade)
            .filter(Trade.ticker == symbol, Trade.account_id == position.account_id)
            .order_by(Trade.entry_time.desc(), Trade.id.desc())
            .first()
        )
        if trade is not None:
            if remaining <= 0:
                trade.exit_time = datetime.now(UTC)
                trade.exit_price = fill_price
                trade.exit_order_id = order_id or f"exit-{symbol.lower()}"
                trade.exit_trigger = trigger
                trade.exit_proceeds = float(fill_price or 0.0) * float(filled_quantity or 0)
                trade.gross_pnl = trade.exit_proceeds - float(trade.entry_cost or 0.0)
                trade.net_pnl = trade.gross_pnl
                trade.return_pct = (
                    ((trade.net_pnl / float(trade.entry_cost or 1.0)) * 100.0)
                    if float(trade.entry_cost or 0.0) > 0
                    else 0.0
                )
            else:
                trade.shares = remaining
                context = dict(trade.exit_reasoning or {})
                partial_exits = list(context.get("partialExits") or [])
                partial_exits.append(
                    {
                        "trigger": trigger,
                        "filledQuantity": int(filled_quantity or 0),
                        "filledPrice": float(fill_price or 0.0),
                        "remainingShares": remaining,
                        "recordedAtUtc": datetime.now(UTC).isoformat(),
                    }
                )
                context["partialExits"] = partial_exits
                trade.exit_reasoning = context
        db.flush()

    async def run_cycle(self) -> dict[str, Any]:
        session = SessionLocal()
        try:
            return self.run_once(session)
        finally:
            session.close()

    async def run_loop(self) -> None:
        """
        Backwards-compatible wrapper so existing startup orchestration
        in app.main can call run_loop() safely.
        """
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                logging.getLogger(__name__).exception(
                    "watchlist_exit_worker loop error",
                    exc_info=e,
                )

            await asyncio.sleep(self._runtime.poll_seconds)

    async def run_forever(self) -> None:
        while True:
            if not self._runtime.enabled:
                await asyncio.sleep(self._runtime.poll_seconds)
                continue

            session = SessionLocal()
            try:
                self.run_once(session)
            except Exception:
                logger.exception("Watchlist exit worker run failed")
            finally:
                session.close()

            await asyncio.sleep(self._runtime.poll_seconds)


watchlist_exit_worker = WatchlistExitWorkerService()