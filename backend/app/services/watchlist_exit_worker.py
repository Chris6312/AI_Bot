from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.order_intent import OrderIntent
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.models.position import Position
from app.models.trade import Trade
from app.models.watchlist_symbol import WatchlistSymbol
from app.services.execution_lifecycle import execution_lifecycle
from app.services.kraken_service import crypto_ledger, kraken_service
from app.services.market_sessions import get_scope_session_status
from app.services.runtime_state import runtime_state
from app.services.trade_validator import trade_validator
from app.services.tradier_client import tradier_client
from app.services.watchlist_service import FOLLOW_THROUGH_EXIT_TEMPLATES, watchlist_service

logger = logging.getLogger(__name__)

ACTIVE_EXIT_INTENT_STATUSES = {'READY', 'SUBMITTED', 'PARTIALLY_FILLED', 'FILLED'}
TERMINAL_EXIT_INTENT_STATUSES = {'REJECTED', 'CLOSED', 'CANCELED', 'CANCELLED', 'ERROR', 'FAILED'}
ACTIVE_BROKER_EXIT_ORDER_STATUSES = {'OPEN', 'PENDING', 'SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED', 'NEW'}
EXIT_TRIGGER = 'TIME_STOP_EXPIRED'
STOP_LOSS_TRIGGER = 'STOP_LOSS_BREACH'
TRAILING_STOP_TRIGGER = 'TRAILING_STOP_BREACH'
PROFIT_TARGET_TRIGGER = 'PROFIT_TARGET_REACHED'
FOLLOW_THROUGH_TRIGGER = 'FAILED_FOLLOW_THROUGH'
EXECUTION_SOURCE = 'WATCHLIST_EXIT_WORKER'
SUPPORTED_SCOPES = ('stocks_only', 'crypto_only')
PRIMARY_SCOPE = 'stocks_only'
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
            poll_seconds=max(5, int(settings.WATCHLIST_EXIT_WORKER_POLL_SECONDS)),
        )

    def get_status(self, db: Session) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        due_rows = self._get_due_rows(db)
        expired_rows = [row for row in due_rows if row.get('positionState', {}).get('positionExpired')]
        protective_rows = [row for row in due_rows if row.get('positionState', {}).get('protectiveExitPending')]
        profit_target_rows = [row for row in due_rows if row.get('positionState', {}).get('scaleOutReady')]
        follow_through_rows = [row for row in due_rows if row.get('positionState', {}).get('followThroughFailed')]
        session = get_scope_session_status(PRIMARY_SCOPE, observed_at)
        session_open = bool(getattr(session, 'session_open', False))
        eligible_due = len(due_rows) if session_open else 0
        blocked_due = 0 if session_open else len(due_rows)
        return {
            'scope': PRIMARY_SCOPE,
            'capturedAtUtc': observed_at.isoformat(),
            'mode': {'stock': runtime_state.get().stock_mode, 'crypto': 'PAPER_LEDGER'},
            'runtimeRunning': runtime_state.get().running,
            'brokerReady': tradier_client.is_ready(runtime_state.get().stock_mode),
            'cryptoLedgerReady': True,
            'session': session.to_dict(),
            'enabled': self._runtime.enabled,
            'pollSeconds': self._runtime.poll_seconds,
            'lastStartedAtUtc': self._runtime.last_started_at_utc,
            'lastFinishedAtUtc': self._runtime.last_finished_at_utc,
            'lastError': self._runtime.last_error,
            'consecutiveFailures': self._runtime.consecutive_failures,
            'lastRunSummary': self._runtime.last_run_summary,
            'summary': {
                'cryptoCandidateExitCount': sum(1 for row in due_rows if str(row.get('scope') or '') == 'crypto_only'),
                'stockCandidateExitCount': sum(1 for row in due_rows if str(row.get('scope') or '') == 'stocks_only'),
                'candidateExitCount': len(due_rows),
                'expiredPositionCount': len(expired_rows),
                'protectiveExitCount': len(protective_rows),
                'profitTargetCount': len(profit_target_rows),
                'followThroughExitCount': len(follow_through_rows),
                'eligibleExpiredCount': len(expired_rows) if session_open else 0,
                'blockedExpiredCount': 0 if session_open else len(expired_rows),
                'eligibleProtectiveCount': len(protective_rows) if session_open else 0,
                'blockedProtectiveCount': 0 if session_open else len(protective_rows),
                'eligibleProfitTargetCount': len(profit_target_rows) if session_open else 0,
                'blockedProfitTargetCount': 0 if session_open else len(profit_target_rows),
                'eligibleExitCount': eligible_due,
                'blockedExitCount': blocked_due,
                'managedOnlyExpiredCount': sum(1 for row in expired_rows if row.get('managedOnly')),
                'alreadyInProgressCount': sum(1 for row in due_rows if self._has_active_exit_intent(db, row)),
            },
            'rows': [
                self._build_status_row(db, row)
                for row in due_rows
            ],
        }

    def _build_status_row(self, db: Session, row: dict[str, Any]) -> dict[str, Any]:
        candidate = self._build_candidate_row(db, row)
        return {
            'symbol': row['symbol'],
            'managedOnly': bool(row.get('managedOnly')),
            'positionState': row.get('positionState', {}),
            'monitoringStatus': candidate.get('monitoringStatus') or row.get('monitoringStatus'),
            'exitTrigger': self._primary_exit_trigger(row),
            'exitReasons': self._build_exit_reasons(row),
            'exitAlreadyInProgress': candidate.get('action') == 'EXIT_ALREADY_IN_PROGRESS',
            'reason': candidate.get('reason'),
            'brokerExitPending': bool(candidate.get('brokerExitPending')),
            'brokerReservedQuantity': int(candidate.get('brokerReservedQuantity') or 0),
            'brokerAvailableQuantity': int(candidate.get('brokerAvailableQuantity') or 0),
        }

    def run_once(self, db: Session, *, limit: int | None = None) -> dict[str, Any]:
        run_summary = self.run_exit_sweep(
            db,
            execute=True,
            limit=limit or settings.WATCHLIST_EXIT_WORKER_BATCH_LIMIT,
        )
        self._runtime.last_run_summary = run_summary
        self._runtime.last_error = None
        self._runtime.consecutive_failures = 0
        self._runtime.last_finished_at_utc = datetime.now(UTC).isoformat()
        return run_summary

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
        due_rows = self._get_due_rows(db)[: max(1, int(limit))]
        session = get_scope_session_status(PRIMARY_SCOPE, observed_at)
        result: dict[str, Any] = {
            'scope': 'all',
            'capturedAtUtc': observed_at.isoformat(),
            'executeRequested': bool(execute),
            'mode': {'stock': runtime.stock_mode, 'crypto': 'PAPER_LEDGER'},
            'runtimeRunning': runtime.running,
            'brokerReady': tradier_client.is_ready(runtime.stock_mode),
            'cryptoLedgerReady': True,
            'session': session.to_dict(),
            'summary': {
                'cryptoCandidateExitCount': sum(1 for row in due_rows if str(row.get('scope') or '') == 'crypto_only'),
                'stockCandidateExitCount': sum(1 for row in due_rows if str(row.get('scope') or '') == 'stocks_only'),
                'expiredPositionCount': sum(1 for row in due_rows if row.get('positionState', {}).get('positionExpired')),
                'protectiveExitCount': sum(1 for row in due_rows if row.get('positionState', {}).get('protectiveExitPending')),
                'profitTargetCount': sum(1 for row in due_rows if row.get('positionState', {}).get('scaleOutReady')),
                'followThroughExitCount': sum(1 for row in due_rows if row.get('positionState', {}).get('followThroughFailed')),
                'refreshedPriceCount': refreshed_price_count,
                'candidateCount': 0,
                'submittedCount': 0,
                'closedCount': 0,
                'scaleOutSubmittedCount': 0,
                'alreadyInProgressCount': 0,
                'blockedCount': 0,
                'skippedCount': 0,
            },
            'rows': [],
        }

        for row in due_rows:
            candidate = self._build_candidate_row(db, row)
            if candidate.get('action') == 'EXIT_ALREADY_IN_PROGRESS':
                result['summary']['alreadyInProgressCount'] += 1
                result['summary']['skippedCount'] += 1
                result['rows'].append(candidate)
                continue

            if not execute:
                candidate['action'] = 'DRY_RUN_CANDIDATE'
                result['summary']['candidateCount'] += 1
                result['rows'].append(candidate)
                continue

            if not runtime.running:
                candidate['action'] = 'BLOCKED'
                candidate['reason'] = 'RUNTIME_NOT_RUNNING'
                result['summary']['blockedCount'] += 1
                result['rows'].append(candidate)
                continue

            row_scope = str(row.get('scope') or '')
            if row_scope == 'stocks_only':
                if not session.session_open:
                    candidate['action'] = 'BLOCKED'
                    candidate['reason'] = 'STOCK_SESSION_CLOSED'
                    candidate['monitoringStatus'] = 'WAITING_FOR_MARKET_OPEN'
                    result['summary']['blockedCount'] += 1
                    result['rows'].append(candidate)
                    continue

                if not tradier_client.is_ready(runtime.stock_mode):
                    candidate['action'] = 'BLOCKED'
                    candidate['reason'] = 'BROKER_NOT_READY'
                    result['summary']['blockedCount'] += 1
                    result['rows'].append(candidate)
                    continue

                executed = self._submit_stock_exit(db, candidate, mode=runtime.stock_mode)
            else:
                executed = self._submit_crypto_exit(db, candidate)

            result['rows'].append(executed)
            if executed['action'] in {'EXIT_SUBMITTED', 'SCALE_OUT_SUBMITTED'}:
                result['summary']['submittedCount'] += 1
                if executed['action'] == 'SCALE_OUT_SUBMITTED':
                    result['summary']['scaleOutSubmittedCount'] += 1
            elif executed['action'] == 'EXIT_CLOSED':
                result['summary']['submittedCount'] += 1
                result['summary']['closedCount'] += 1
            else:
                result['summary']['skippedCount'] += 1

        return result

    async def run_loop(self) -> None:
        self._runtime.enabled = bool(settings.WATCHLIST_EXIT_WORKER_ENABLED)
        self._runtime.poll_seconds = max(5, int(settings.WATCHLIST_EXIT_WORKER_POLL_SECONDS))
        if not self._runtime.enabled:
            logger.info('Watchlist exit worker is disabled.')
            return

        logger.info(
            'Starting watchlist exit worker loop (poll=%ss, batch_limit=%s).',
            self._runtime.poll_seconds,
            settings.WATCHLIST_EXIT_WORKER_BATCH_LIMIT,
        )
        def _run_once_blocking() -> dict[str, Any]:
            db = SessionLocal()
            try:
                return self.run_once(db, limit=settings.WATCHLIST_EXIT_WORKER_BATCH_LIMIT)
            finally:
                db.close()

        while True:
            self._runtime.last_started_at_utc = datetime.now(UTC).isoformat()
            try:
                run_summary = await asyncio.to_thread(_run_once_blocking)
                if run_summary['summary']['submittedCount'] > 0 or run_summary['summary']['blockedCount'] > 0:
                    logger.info(
                        'Watchlist exit sweep complete: submitted=%s closed=%s blocked=%s expired=%s',
                        run_summary['summary']['submittedCount'],
                        run_summary['summary']['closedCount'],
                        run_summary['summary']['blockedCount'],
                        run_summary['summary']['expiredPositionCount'],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception('Watchlist exit worker sweep failed: %s', exc)
                self._runtime.last_error = str(exc)
                self._runtime.consecutive_failures += 1
                self._runtime.last_finished_at_utc = datetime.now(UTC).isoformat()
            await asyncio.sleep(self._runtime.poll_seconds)

    def _submit_stock_exit(self, db: Session, candidate: dict[str, Any], *, mode: str) -> dict[str, Any]:
        position_id = candidate.get('positionId')
        symbol = str(candidate.get('symbol') or '').upper().strip()

        if position_id is not None:
            position = db.query(Position).filter(Position.id == position_id).first()
        else:
            # Broker-sync positions have positionId=None; fall back to open DB row
            # matching the symbol so protective exits can still be submitted.
            position = (
                db.query(Position)
                .filter(
                    Position.ticker == symbol,
                    Position.is_open.is_(True),
                )
                .order_by(Position.id.desc())
                .first()
            )

        if position is None or not position.is_open or int(position.shares or 0) <= 0:
            candidate['action'] = 'SKIPPED'
            candidate['reason'] = 'POSITION_NOT_OPEN'
            return candidate

        fallback_quantity = int(position.shares or 0)
        pending_orders = list(candidate.get('brokerPendingOrders') or [])
        broker_quantity = int(candidate.get('brokerQuantity') or 0)
        reserved_quantity = int(candidate.get('brokerReservedQuantity') or 0)
        available_quantity = int(candidate.get('brokerAvailableQuantity') or 0)
        if broker_quantity <= 0 and reserved_quantity <= 0 and available_quantity <= 0 and not pending_orders:
            broker_state = self._get_broker_exit_state(str(position.ticker or '').upper(), mode=mode)
            broker_quantity = int(broker_state.get('brokerQuantity') or 0)
            reserved_quantity = int(broker_state.get('reservedQuantity') or 0)
            available_quantity = int(broker_state.get('availableQuantity') or 0)
            pending_orders = list(broker_state.get('pendingOrders') or [])
        trigger = str(candidate.get('exitTrigger') or EXIT_TRIGGER)
        requested_quantity = self._determine_requested_quantity(
            trigger=trigger,
            available_quantity=available_quantity,
        )
        candidate['fallbackQuantity'] = fallback_quantity
        candidate['brokerQuantity'] = broker_quantity
        candidate['brokerReservedQuantity'] = reserved_quantity
        candidate['brokerAvailableQuantity'] = available_quantity
        candidate['brokerExitPending'] = bool(pending_orders)
        candidate['brokerPendingOrders'] = pending_orders
        candidate['requestedQuantity'] = requested_quantity
        if pending_orders:
            candidate['action'] = 'EXIT_ALREADY_IN_PROGRESS'
            candidate['reason'] = 'BROKER_EXIT_PENDING'
            candidate['monitoringStatus'] = 'EXIT_PENDING'
            return candidate
        if requested_quantity <= 0:
            candidate['action'] = 'SKIPPED'
            candidate['reason'] = 'NO_OPEN_QUANTITY'
            return candidate

        account_id = str(position.account_id or '').strip() or f'{mode.lower()}-watchlist-exit'
        exit_intent = execution_lifecycle.create_exit_intent(
            db,
            account_id=account_id,
            asset_class='stock',
            symbol=str(position.ticker or '').upper(),
            requested_quantity=requested_quantity,
            requested_price=float(position.current_price or position.avg_entry_price or 0.0) or None,
            execution_source=EXECUTION_SOURCE,
            position_id=position.id,
            trade_id=self._resolve_trade_id_for_position(db, position),
            linked_intent_id=str(position.execution_id or '').strip() or None,
            context={
                'exitTrigger': candidate.get('exitTrigger') or EXIT_TRIGGER,
                'exitReasons': candidate.get('exitReasons') or [],
                'mode': mode,
                'scaleOut': trigger == PROFIT_TARGET_TRIGGER,
                'fallbackQuantity': fallback_quantity,
                'brokerQuantity': broker_quantity,
            },
        )
        candidate['intentId'] = exit_intent.intent_id

        try:
            order_snapshot = tradier_client.place_order_sync(
                ticker=str(position.ticker or '').upper(),
                qty=requested_quantity,
                side='sell',
                mode=mode,
                order_type='market',
            )
            execution_lifecycle.record_submission(db, exit_intent, order_snapshot)
            confirmed = self._confirm_stock_order(order_snapshot, mode=mode)
            exit_intent = execution_lifecycle.refresh_from_order_snapshot(db, exit_intent, confirmed)
            exit_record = execution_lifecycle.materialize_stock_exit(
                db,
                exit_intent,
                current_price=float(position.current_price or 0.0) or None,
                exit_trigger=str(candidate.get('exitTrigger') or EXIT_TRIGGER),
            )
            if exit_record and trigger == PROFIT_TARGET_TRIGGER and int(exit_record.get('remaining_shares') or 0) > 0:
                self._arm_profit_target_trailing(db, position_id=position.id, exit_price=float(exit_record.get('exit_price') or 0.0))
        except Exception as exc:
            refreshed_broker_state = self._get_broker_exit_state(str(position.ticker or '').upper(), mode=mode)
            reconciliation_payload = {
                'error': str(exc),
                'exitTrigger': candidate.get('exitTrigger') or EXIT_TRIGGER,
                'brokerState': refreshed_broker_state,
            }
            execution_lifecycle.record_event(
                db,
                exit_intent,
                event_type='EXIT_SUBMISSION_FAILED',
                status='REJECTED',
                message=f'Watchlist exit worker failed for {position.ticker}: {exc}',
                payload=reconciliation_payload,
            )
            exit_intent.status = 'REJECTED'
            exit_intent.rejection_reason = str(exc)
            db.commit()
            db.refresh(exit_intent)
            candidate['intentStatus'] = exit_intent.status
            candidate['reconciliation'] = refreshed_broker_state
            if refreshed_broker_state.get('pendingOrders'):
                candidate['action'] = 'EXIT_ALREADY_IN_PROGRESS'
                candidate['reason'] = 'BROKER_EXIT_PENDING_AFTER_REJECTION'
                candidate['monitoringStatus'] = 'EXIT_PENDING'
                candidate['brokerExitPending'] = True
                return candidate
            candidate['action'] = 'SKIPPED'
            candidate['reason'] = f'EXIT_SUBMISSION_FAILED: {exc}'
            return candidate

        candidate['intentStatus'] = exit_intent.status
        candidate['submittedOrderId'] = exit_intent.submitted_order_id
        if exit_record and int(exit_record.get('remaining_shares') or 0) <= 0:
            candidate['action'] = 'EXIT_CLOSED'
            candidate['closedShares'] = int(exit_record.get('closed_shares') or 0)
            candidate['remainingShares'] = int(exit_record.get('remaining_shares') or 0)
            candidate['exitPrice'] = (
                float(exit_record.get('exit_price') or 0.0)
                if exit_record.get('exit_price') is not None
                else None
            )
            return candidate

        candidate['action'] = 'SCALE_OUT_SUBMITTED' if trigger == PROFIT_TARGET_TRIGGER else 'EXIT_SUBMITTED'
        candidate['closedShares'] = (
            int(exit_record.get('closed_shares') or 0)
            if exit_record
            else int(round(float(exit_intent.filled_quantity or 0.0)))
        )
        candidate['remainingShares'] = (
            int(exit_record.get('remaining_shares') or position.shares or 0)
            if exit_record
            else int(position.shares or 0)
        )
        candidate['exitPrice'] = (
            float(exit_record.get('exit_price') or 0.0)
            if exit_record and exit_record.get('exit_price') is not None
            else None
        )
        return candidate

    def _confirm_stock_order(self, order_snapshot: dict[str, Any], *, mode: str) -> dict[str, Any]:
        snapshot = order_snapshot
        normalized = tradier_client.normalize_order_response(snapshot)
        order_id = normalized.get('id')
        if normalized.get('is_terminal') or normalized.get('filled_quantity', 0) > 0 or not order_id:
            return snapshot

        attempts = max(int(settings.ORDER_FILL_CONFIRM_RETRIES), 0)
        delay_seconds = max(float(settings.ORDER_FILL_CONFIRM_DELAY_SECONDS), 0.0)
        for _ in range(attempts):
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            snapshot = tradier_client.get_order_sync(str(order_id), mode=mode)
            normalized = tradier_client.normalize_order_response(snapshot)
            if normalized.get('is_terminal') or normalized.get('filled_quantity', 0) > 0:
                break
        return snapshot

    def _get_due_rows(self, db: Session) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for scope in SUPPORTED_SCOPES:
            try:
                snapshot = watchlist_service.get_exit_readiness_snapshot(db, scope=scope, expiring_within_hours=24)
            except Exception as exc:
                logger.warning('Watchlist exit worker could not build %s exit readiness snapshot: %s', scope, exc)
                continue
            for row in list(snapshot.get('rows', [])):
                if (
                    row.get('positionState', {}).get('positionExpired')
                    or row.get('positionState', {}).get('protectiveExitPending')
                    or row.get('positionState', {}).get('scaleOutReady')
                    or row.get('positionState', {}).get('followThroughFailed')
                ):
                    row = dict(row)
                    row.setdefault('scope', scope)
                    rows.append(row)
        return rows

    def _build_candidate_row(self, db: Session, row: dict[str, Any]) -> dict[str, Any]:
        scope = str(row.get('scope') or 'stocks_only')
        if scope == 'crypto_only':
            return self._build_crypto_candidate_row(db, row)

        mode = runtime_state.get().stock_mode
        symbol = str(row.get('symbol') or '').upper().strip()
        broker_state = self._get_broker_exit_state(symbol, mode=mode)
        payload = {
            'scope': scope,
            'assetClass': row.get('assetClass') or 'stock',
            'symbol': row.get('symbol'),
            'managedOnly': bool(row.get('managedOnly')),
            'monitoringStatus': row.get('monitoringStatus'),
            'positionId': row.get('positionState', {}).get('positionId'),
            'positionState': row.get('positionState', {}),
            'exitTemplate': row.get('exitTemplate'),
            'exitTrigger': self._primary_exit_trigger(row),
            'exitReasons': self._build_exit_reasons(row),
            'action': None,
            'reason': None,
            'brokerQuantity': int(broker_state.get('brokerQuantity') or 0),
            'brokerReservedQuantity': int(broker_state.get('reservedQuantity') or 0),
            'brokerAvailableQuantity': int(broker_state.get('availableQuantity') or 0),
            'brokerPendingOrders': list(broker_state.get('pendingOrders') or []),
            'brokerExitPending': bool(broker_state.get('pendingOrders')),
            'requestedQuantity': self._determine_requested_quantity(
                trigger=self._primary_exit_trigger(row),
                available_quantity=int(broker_state.get('availableQuantity') or 0),
            ),
        }
        if self._has_active_exit_intent(db, row):
            payload['action'] = 'EXIT_ALREADY_IN_PROGRESS'
            payload['reason'] = 'ACTIVE_EXIT_INTENT_EXISTS'
            payload['monitoringStatus'] = 'EXIT_PENDING'
            return payload
        if payload['brokerExitPending']:
            payload['action'] = 'EXIT_ALREADY_IN_PROGRESS'
            payload['reason'] = 'BROKER_EXIT_PENDING'
            payload['monitoringStatus'] = 'EXIT_PENDING'
        return payload

    def _has_active_exit_intent(self, db: Session, row: dict[str, Any]) -> bool:
        position_id = row.get('positionState', {}).get('positionId')
        scope = str(row.get('scope') or '')
        symbol = str(row.get('symbol') or '').upper().strip()
        query = db.query(OrderIntent).filter(
            OrderIntent.side == 'SELL',
            OrderIntent.status.in_(ACTIVE_EXIT_INTENT_STATUSES),
            OrderIntent.execution_source == EXECUTION_SOURCE,
        )
        if position_id is not None:
            query = query.filter(OrderIntent.position_id == position_id)
        elif scope == 'crypto_only':
            aliases = sorted(self._crypto_symbol_aliases(symbol))
            query = query.filter(OrderIntent.asset_class == 'crypto', OrderIntent.symbol.in_(aliases))
        else:
            return False
        return query.first() is not None

    @staticmethod
    def _resolve_trade_id_for_position(db: Session, position: Position) -> int | None:
        trade = (
            db.query(Trade)
            .filter(
                Trade.ticker == position.ticker,
                Trade.account_id == position.account_id,
            )
            .order_by(Trade.entry_time.desc(), Trade.id.desc())
            .first()
        )
        return trade.id if trade is not None else None

    def _build_crypto_candidate_row(self, db: Session, row: dict[str, Any]) -> dict[str, Any]:
        symbol = str(row.get('symbol') or '').upper().strip()
        position_state = row.get('positionState', {}) or {}
        ledger_position = self._find_crypto_ledger_position(symbol)
        requested_quantity = self._safe_float((ledger_position or {}).get('amount'))
        payload = {
            'scope': 'crypto_only',
            'assetClass': 'crypto',
            'symbol': row.get('symbol'),
            'displaySymbol': (ledger_position or {}).get('pair') or row.get('symbol'),
            'managedOnly': bool(row.get('managedOnly')),
            'monitoringStatus': row.get('monitoringStatus') or ('EXIT_PENDING' if position_state.get('protectiveExitPending') else None),
            'positionId': None,
            'positionState': position_state,
            'exitTemplate': row.get('exitTemplate'),
            'exitTrigger': self._primary_exit_trigger(row),
            'exitReasons': self._build_exit_reasons(row),
            'action': None,
            'reason': None,
            'brokerQuantity': None,
            'brokerReservedQuantity': 0,
            'brokerAvailableQuantity': requested_quantity,
            'brokerExitPending': False,
            'brokerPendingOrders': [],
            'requestedQuantity': requested_quantity,
            'ledgerPair': (ledger_position or {}).get('pair') or row.get('symbol'),
            'ohlcvPair': (ledger_position or {}).get('ohlcvPair') or self._resolve_crypto_ohlcv_pair((ledger_position or {}).get('pair') or row.get('symbol')),
            'currentPrice': self._safe_float((ledger_position or {}).get('currentPrice') or position_state.get('currentPrice')),
            'avgEntryPrice': self._safe_float((ledger_position or {}).get('avgPrice') or position_state.get('avgEntryPrice')),
        }
        if self._has_active_exit_intent(db, row):
            payload['action'] = 'EXIT_ALREADY_IN_PROGRESS'
            payload['reason'] = 'EXIT_INTENT_ALREADY_ACTIVE'
            payload['monitoringStatus'] = 'EXIT_PENDING'
            return payload
        if requested_quantity <= 0:
            payload['action'] = 'SKIPPED'
            payload['reason'] = 'NO_OPEN_QUANTITY'
        return payload

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _crypto_symbol_aliases(symbol: str | None) -> set[str]:
        raw = str(symbol or '').strip().upper()
        if not raw:
            return set()
        compact = ''.join(ch for ch in raw if ch.isalnum())
        aliases = {raw, compact}
        if '/' in raw:
            base, _, quote = raw.partition('/')
            aliases.add(base)
            aliases.add(f'{base}{quote}')
        elif raw.endswith('USD') and len(raw) > 3:
            base = raw[:-3]
            aliases.add(base)
            aliases.add(f'{base}/USD')
        else:
            aliases.add(f'{raw}/USD')
            aliases.add(f'{raw}USD')
        return {item for item in aliases if item}

    @classmethod
    def _find_crypto_ledger_position(cls, symbol: str) -> dict[str, Any] | None:
        aliases = cls._crypto_symbol_aliases(symbol)
        for row in crypto_ledger.get_positions():
            pair = str(row.get('pair') or '').strip().upper()
            if pair in aliases or cls._crypto_symbol_aliases(pair).intersection(aliases):
                return row
        return None

    @staticmethod
    def _resolve_crypto_ohlcv_pair(pair: str | None) -> str | None:
        raw = str(pair or '').strip()
        if not raw:
            return None
        try:
            return kraken_service.get_ohlcv_pair(raw)
        except Exception:
            return None

    @staticmethod
    def _calculate_reentry_blocked_until(
        *,
        reference_time: datetime,
        evaluation_interval_seconds: int | None,
    ) -> datetime:
        interval = int(evaluation_interval_seconds or 0)
        if interval <= 0:
            return reference_time + timedelta(minutes=15)
        return reference_time + timedelta(seconds=max(interval, 60))

    def _finalize_crypto_exit_monitor_state(
        self,
        db: Session,
        *,
        pair: str,
        candidate: dict[str, Any],
        event_time: datetime,
        exit_intent: OrderIntent,
    ) -> None:
        aliases = self._crypto_symbol_aliases(pair)
        watch_symbol = (
            db.query(WatchlistSymbol)
            .filter(
                WatchlistSymbol.scope == 'crypto_only',
                WatchlistSymbol.symbol.in_(sorted(aliases)),
            )
            .order_by(WatchlistSymbol.updated_at.desc(), WatchlistSymbol.created_at.desc(), WatchlistSymbol.id.desc())
            .first()
        )
        query = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.scope == 'crypto_only')
        if watch_symbol is not None:
            query = query.filter(WatchlistMonitorState.watchlist_symbol_id == watch_symbol.id)
        else:
            query = query.filter(WatchlistMonitorState.symbol.in_(sorted(aliases)))
        monitor_state = query.order_by(WatchlistMonitorState.updated_at.desc(), WatchlistMonitorState.id.desc()).first()
        if monitor_state is None:
            return

        blocked_until = self._calculate_reentry_blocked_until(
            reference_time=event_time,
            evaluation_interval_seconds=monitor_state.evaluation_interval_seconds,
        )
        context = dict(monitor_state.decision_context_json or {})
        context['lastExitAtUtc'] = event_time.isoformat()
        context['lastClosedPositionAtUtc'] = event_time.isoformat()
        context['lastExitReason'] = candidate.get('reason') or candidate.get('exitTrigger') or EXIT_TRIGGER
        context['reentryBlockedUntilUtc'] = blocked_until.isoformat()
        context['cooldownActive'] = True
        context['exitExecution'] = {
            'action': 'EXIT_FILLED',
            'reason': candidate.get('reason') or 'CRYPTO_LEDGER_EXIT_FILLED',
            'intentId': exit_intent.intent_id,
            'submittedOrderId': exit_intent.submitted_order_id,
            'tradeId': candidate.get('tradeId'),
            'filledPrice': candidate.get('exitPrice'),
            'filledQuantity': candidate.get('closedAmount'),
            'notional': round(float(candidate.get('closedAmount') or 0.0) * float(candidate.get('exitPrice') or 0.0), 8),
            'executedAtUtc': event_time.isoformat(),
            'source': 'CRYPTO_PAPER_LEDGER',
            'displayPair': pair,
        }
        context['entryExecution'] = {
            'action': 'EXIT_FILLED',
            'reason': candidate.get('reason') or 'CRYPTO_LEDGER_EXIT_FILLED',
            'intentId': exit_intent.intent_id,
            'submittedOrderId': exit_intent.submitted_order_id,
            'tradeId': candidate.get('tradeId'),
            'reentryBlockedUntilUtc': blocked_until.isoformat(),
            'recordedAtUtc': event_time.isoformat(),
        }
        monitor_state.decision_context_json = context
        monitor_state.latest_decision_state = 'EXIT_FILLED'
        monitor_state.latest_decision_reason = context['lastExitReason']
        monitor_state.last_decision_at_utc = event_time
        monitor_state.last_evaluated_at_utc = event_time
        monitor_state.next_evaluation_at_utc = blocked_until
        if monitor_state.monitoring_status not in {'ACTIVE', 'MANAGED_ONLY'}:
            monitor_state.monitoring_status = 'ACTIVE'
        db.add(monitor_state)
        db.flush()

    def _submit_crypto_exit(self, db: Session, candidate: dict[str, Any]) -> dict[str, Any]:
        pair = str(candidate.get('ledgerPair') or candidate.get('displaySymbol') or candidate.get('symbol') or '').upper().strip()
        if not pair:
            candidate['action'] = 'SKIPPED'
            candidate['reason'] = 'PAIR_NOT_RESOLVED'
            return candidate

        amount = self._safe_float(candidate.get('requestedQuantity'))
        if amount <= 0:
            candidate['action'] = 'SKIPPED'
            candidate['reason'] = 'NO_OPEN_QUANTITY'
            return candidate

        current_price = self._safe_float(candidate.get('currentPrice')) or None
        exit_intent = execution_lifecycle.create_exit_intent(
            db,
            account_id='paper-crypto-ledger',
            asset_class='crypto',
            symbol=pair,
            requested_quantity=amount,
            requested_price=current_price,
            execution_source=EXECUTION_SOURCE,
            position_id=None,
            trade_id=None,
            linked_intent_id=None,
            context={
                'exitTrigger': candidate.get('exitTrigger') or EXIT_TRIGGER,
                'exitReasons': candidate.get('exitReasons') or [],
                'mode': 'PAPER_LEDGER',
                'displayPair': pair,
                'ohlcvPair': candidate.get('ohlcvPair'),
            },
        )
        candidate['intentId'] = exit_intent.intent_id
        candidate['monitoringStatus'] = 'EXIT_PENDING'

        try:
            trade = crypto_ledger.execute_trade(
                pair=pair,
                ohlcv_pair=str(candidate.get('ohlcvPair') or self._resolve_crypto_ohlcv_pair(pair) or ''),
                side='SELL',
                amount=amount,
                price=current_price,
            )
            trade_status = str(trade.get('status') or '').upper()
            trade_reason = str(trade.get('reason') or '').strip() or None
            trade_timestamp = trade.get('timestamp')
            event_time = datetime.fromisoformat(str(trade_timestamp).replace('Z', '+00:00')) if trade_timestamp else datetime.now(UTC)
            if trade_status != 'FILLED':
                execution_lifecycle.record_event(
                    db,
                    exit_intent,
                    event_type='EXIT_SUBMISSION_FAILED',
                    status='REJECTED',
                    message=f'Crypto watchlist exit failed for {pair}: {trade_reason or "Crypto paper ledger rejected the exit."}',
                    payload=trade,
                    event_time=event_time,
                )
                exit_intent.status = 'REJECTED'
                exit_intent.rejection_reason = trade_reason or 'Crypto paper ledger rejected the exit.'
                db.commit()
                db.refresh(exit_intent)
                candidate['intentStatus'] = exit_intent.status
                candidate['action'] = 'SKIPPED'
                candidate['reason'] = f'EXIT_SUBMISSION_FAILED: {exit_intent.rejection_reason}'
                return candidate

            exit_intent.status = 'FILLED'
            exit_intent.submitted_order_id = str(trade.get('id') or exit_intent.submitted_order_id or '') or None
            exit_intent.submitted_at = event_time
            exit_intent.first_fill_at = event_time
            exit_intent.last_fill_at = event_time
            exit_intent.filled_quantity = amount
            exit_intent.avg_fill_price = self._safe_float(trade.get('price')) or current_price
            exit_intent.rejection_reason = None
            execution_lifecycle.record_event(
                db,
                exit_intent,
                event_type='ORDER_SUBMITTED',
                status='SUBMITTED',
                message=f'Crypto paper ledger accepted exit order for {pair}',
                payload=trade,
                event_time=event_time,
            )
            execution_lifecycle.record_event(
                db,
                exit_intent,
                event_type='ORDER_STATUS_UPDATED',
                status='FILLED',
                message=f'Confirmed crypto exit fill for {pair}: {amount} filled',
                payload=trade,
                event_time=event_time,
            )
            self._finalize_crypto_exit_monitor_state(
                db,
                pair=pair,
                candidate={**candidate, 'tradeId': trade.get('id')},
                event_time=event_time,
                exit_intent=exit_intent,
            )
            db.commit()
            db.refresh(exit_intent)
            candidate['intentStatus'] = exit_intent.status
            candidate['submittedOrderId'] = exit_intent.submitted_order_id
            candidate['closedAmount'] = amount
            candidate['remainingAmount'] = 0.0
            candidate['exitPrice'] = self._safe_float(trade.get('price')) or current_price
            candidate['action'] = 'EXIT_CLOSED'
            candidate['reason'] = candidate.get('reason') or 'CRYPTO_LEDGER_EXIT_FILLED'
            return candidate
        except Exception as exc:
            execution_lifecycle.record_event(
                db,
                exit_intent,
                event_type='EXIT_SUBMISSION_FAILED',
                status='REJECTED',
                message=f'Crypto watchlist exit failed for {pair}: {exc}',
                payload={'error': str(exc), 'exitTrigger': candidate.get('exitTrigger') or EXIT_TRIGGER},
            )
            exit_intent.status = 'REJECTED'
            exit_intent.rejection_reason = str(exc)
            db.commit()
            db.refresh(exit_intent)
            candidate['intentStatus'] = exit_intent.status
            candidate['action'] = 'SKIPPED'
            candidate['reason'] = f'EXIT_SUBMISSION_FAILED: {exc}'
            return candidate

    @staticmethod
    def _safe_broker_quantity(symbol: str, mode: str) -> int:
        if not tradier_client.is_ready(mode):
            return 0
        try:
            result = tradier_client.get_position_quantity_sync(
                symbol,
                mode=mode,
                timeout=1.5,
                use_cache=True,
            )
        except TypeError:
            try:
                result = tradier_client.get_position_quantity_sync(symbol, mode=mode)
            except Exception:
                return 0
        except Exception:
            return 0
        try:
            return int(result or 0)
        except Exception:
            return 0

    @staticmethod
    def _safe_broker_sell_orders(symbol: str, mode: str) -> list[dict[str, Any]]:
        if not tradier_client.is_ready(mode):
            return []
        try:
            orders = tradier_client.get_orders_sync(
                mode=mode,
                symbol=symbol,
                side='SELL',
                statuses=sorted(ACTIVE_BROKER_EXIT_ORDER_STATUSES),
                timeout=1.5,
                use_cache=True,
            )
        except TypeError:
            try:
                orders = tradier_client.get_orders_sync(
                    mode=mode,
                    symbol=symbol,
                    side='SELL',
                    statuses=sorted(ACTIVE_BROKER_EXIT_ORDER_STATUSES),
                    timeout=1.5,
                )
            except TypeError:
                try:
                    orders = tradier_client.get_orders_sync(
                        mode=mode,
                        symbol=symbol,
                        side='SELL',
                        statuses=sorted(ACTIVE_BROKER_EXIT_ORDER_STATUSES),
                    )
                except Exception:
                    return []
            except Exception:
                return []
        except Exception:
            return []
        try:
            return list(orders or [])
        except Exception:
            return []

    @classmethod
    def _get_broker_exit_state(cls, symbol: str, *, mode: str) -> dict[str, Any]:
        broker_quantity = cls._safe_broker_quantity(symbol, mode)
        pending_orders = cls._safe_broker_sell_orders(symbol, mode)
        reserved_quantity = int(round(sum(float(order.get('remaining_quantity') or 0.0) for order in pending_orders)))
        available_quantity = max(broker_quantity - reserved_quantity, 0)
        return {
            'symbol': symbol,
            'brokerQuantity': broker_quantity,
            'reservedQuantity': reserved_quantity,
            'availableQuantity': available_quantity,
            'pendingOrders': pending_orders,
        }

    @staticmethod
    def _build_exit_reasons(row: dict[str, Any]) -> list[str]:
        position_state = row.get('positionState', {}) if isinstance(row, dict) else {}
        reasons = list(position_state.get('protectiveExitReasons') or [])
        if position_state.get('followThroughFailed'):
            reasons.append(FOLLOW_THROUGH_TRIGGER)
        if position_state.get('scaleOutReady'):
            reasons.append(PROFIT_TARGET_TRIGGER)
        if position_state.get('positionExpired'):
            reasons.append(EXIT_TRIGGER)
        ordered: list[str] = []
        for reason in reasons:
            if reason and reason not in ordered:
                ordered.append(str(reason))
        return ordered

    @classmethod
    def _primary_exit_trigger(cls, row: dict[str, Any]) -> str:
        reasons = cls._build_exit_reasons(row)
        for preferred in (STOP_LOSS_TRIGGER, TRAILING_STOP_TRIGGER, FOLLOW_THROUGH_TRIGGER, PROFIT_TARGET_TRIGGER, EXIT_TRIGGER):
            if preferred in reasons:
                return preferred
        return reasons[0] if reasons else EXIT_TRIGGER

    @staticmethod
    def _determine_requested_quantity(*, trigger: str, available_quantity: int) -> int:
        if available_quantity <= 0:
            return 0
        if str(trigger).upper() != PROFIT_TARGET_TRIGGER:
            return available_quantity
        if available_quantity <= 1:
            return 1
        return max(1, available_quantity // 2)

    @staticmethod
    def _arm_profit_target_trailing(db: Session, *, position_id: int, exit_price: float) -> None:
        position = db.query(Position).filter(Position.id == position_id).first()
        if position is None or not position.is_open:
            return
        changed = False
        avg_entry = float(position.avg_entry_price or 0.0)
        if avg_entry > 0 and float(position.stop_loss or 0.0) < avg_entry:
            position.stop_loss = avg_entry
            changed = True
        peak_price = max(float(position.peak_price or 0.0), float(exit_price or 0.0), float(position.current_price or 0.0))
        if peak_price > float(position.peak_price or 0.0):
            position.peak_price = peak_price
            changed = True
        trailing_candidate = round(peak_price * (1.0 - float(settings.TRAILING_STOP_PCT)), 4)
        if position.trailing_stop is None or trailing_candidate > float(position.trailing_stop or 0.0):
            position.trailing_stop = trailing_candidate
            changed = True
        if changed:
            db.commit()


    @staticmethod
    def _resolve_watchlist_row(db: Session, *, symbol: str) -> WatchlistSymbol | None:
        return (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.scope == PRIMARY_SCOPE, WatchlistSymbol.symbol == symbol)
            .order_by(WatchlistSymbol.id.desc())
            .first()
        )

    @staticmethod
    def _position_has_local_exit_signal(db: Session, position: Position) -> bool:
        current_price = float(position.current_price or 0.0)
        stop_loss = float(position.stop_loss or 0.0)
        trailing_stop = float(position.trailing_stop or 0.0)
        if current_price > 0 and stop_loss > 0 and current_price <= stop_loss:
            return True
        if current_price > 0 and trailing_stop > 0 and current_price <= trailing_stop:
            return True
        watchlist_row = WatchlistExitWorkerService._resolve_watchlist_row(db, symbol=str(position.ticker or '').upper().strip())
        exit_template = str(watchlist_row.exit_template or '').strip().lower() if watchlist_row is not None else ''
        if exit_template in FOLLOW_THROUGH_EXIT_TEMPLATES:
            entry_time = position.entry_time
            if entry_time is not None and entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=UTC)
            hours_since_entry = None
            if entry_time is not None:
                hours_since_entry = (datetime.now(UTC) - entry_time).total_seconds() / 3600.0
            avg_entry_price = float(position.avg_entry_price or 0.0)
            max_hold_hours = int(watchlist_row.max_hold_hours) if watchlist_row is not None and watchlist_row.max_hold_hours is not None else None
            follow_through_window_hours = watchlist_service._resolve_follow_through_window_hours(max_hold_hours)
            if (
                avg_entry_price > 0
                and current_price > 0
                and current_price < avg_entry_price
                and hours_since_entry is not None
                and follow_through_window_hours is not None
                and 1.0 <= hours_since_entry <= follow_through_window_hours
            ):
                return True
        return False

    @staticmethod
    def _refresh_open_position_prices(db: Session, *, mode: str, skip_existing_exit_signals: bool = False) -> int:
        if not tradier_client.is_ready(mode):
            return 0
        positions = (
            db.query(Position)
            .filter(Position.is_open.is_(True))
            .order_by(Position.id.asc())
            .all()
        )
        if not positions:
            return 0
        if skip_existing_exit_signals:
            positions = [position for position in positions if not WatchlistExitWorkerService._position_has_local_exit_signal(db, position)]
        if not positions:
            return 0
        symbols = [str(position.ticker or '').upper().strip() for position in positions if str(position.ticker or '').strip()]
        if not symbols:
            return 0
        try:
            quotes = tradier_client.get_quotes_sync(symbols, mode=mode)
        except Exception:
            return 0

        refreshed_count = 0
        changed_count = 0
        for position in positions:
            symbol = str(position.ticker or '').upper().strip()
            if not symbol:
                continue
            quote = quotes.get(symbol, {})
            if not isinstance(quote, dict):
                continue
            market_timestamp = trade_validator._extract_market_timestamp(quote)
            if market_timestamp is None:
                continue
            current_price = 0.0
            for key in ('last', 'close', 'bid', 'ask'):
                try:
                    value = float(quote.get(key) or 0.0)
                except (TypeError, ValueError):
                    value = 0.0
                if value > 0:
                    current_price = value
                    break
            if current_price <= 0:
                continue
            refreshed_count += 1

            changed = False
            if float(position.current_price or 0.0) != current_price:
                position.current_price = current_price
                changed = True

            peak_price = max(float(position.peak_price or 0.0), current_price)
            if float(position.peak_price or 0.0) != peak_price:
                position.peak_price = peak_price
                changed = True

            if position.trailing_stop is not None:
                trailing_candidate = round(peak_price * (1.0 - float(settings.TRAILING_STOP_PCT)), 4)
                if trailing_candidate > float(position.trailing_stop or 0.0):
                    position.trailing_stop = trailing_candidate
                    changed = True

            avg_entry_price = float(position.avg_entry_price or 0.0)
            shares = int(position.shares or 0)
            if avg_entry_price > 0 and shares > 0:
                unrealized_pnl = round((current_price - avg_entry_price) * shares, 4)
                unrealized_pnl_pct = round(((current_price / avg_entry_price) - 1.0) * 100.0, 4)
                if float(position.unrealized_pnl or 0.0) != unrealized_pnl:
                    position.unrealized_pnl = unrealized_pnl
                    changed = True
                if float(position.unrealized_pnl_pct or 0.0) != unrealized_pnl_pct:
                    position.unrealized_pnl_pct = unrealized_pnl_pct
                    changed = True

            watchlist_row = WatchlistExitWorkerService._resolve_watchlist_row(db, symbol=symbol)
            exit_template = str(watchlist_row.exit_template or '').strip().lower() if watchlist_row is not None else ''
            profit_target = float(position.profit_target or 0.0)
            if exit_template == 'trail_after_impulse' and profit_target > 0 and current_price >= profit_target:
                impulse_reference = max(float(position.peak_price or 0.0), current_price)
                tightened_pct = float(settings.TRAILING_STOP_PCT) * IMPULSE_TRAIL_STOP_FACTOR
                impulse_candidate = round(impulse_reference * (1.0 - tightened_pct), 4)
                if position.trailing_stop is None or impulse_candidate > float(position.trailing_stop or 0.0):
                    position.trailing_stop = impulse_candidate
                    changed = True

            if changed:
                changed_count += 1

        if changed_count > 0:
            db.commit()
        return refreshed_count


watchlist_exit_worker = WatchlistExitWorkerService()
