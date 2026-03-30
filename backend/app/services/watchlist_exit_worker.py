from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.trade import Trade
from app.services.execution_lifecycle import execution_lifecycle
from app.services.market_sessions import get_scope_session_status
from app.services.runtime_state import runtime_state
from app.services.tradier_client import tradier_client
from app.services.watchlist_service import watchlist_service

logger = logging.getLogger(__name__)

ACTIVE_EXIT_INTENT_STATUSES = {'READY', 'SUBMITTED', 'PARTIALLY_FILLED', 'FILLED'}
TERMINAL_EXIT_INTENT_STATUSES = {'REJECTED', 'CLOSED', 'CANCELED', 'CANCELLED', 'ERROR', 'FAILED'}
EXIT_TRIGGER = 'TIME_STOP_EXPIRED'
EXECUTION_SOURCE = 'WATCHLIST_EXIT_WORKER'
SUPPORTED_SCOPE = 'stocks_only'


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
        session = get_scope_session_status(SUPPORTED_SCOPE, observed_at)
        session_open = bool(getattr(session, 'session_open', False))
        eligible_due = len(due_rows) if session_open else 0
        blocked_due = 0 if session_open else len(due_rows)
        return {
            'scope': SUPPORTED_SCOPE,
            'capturedAtUtc': observed_at.isoformat(),
            'mode': runtime_state.get().stock_mode,
            'runtimeRunning': runtime_state.get().running,
            'brokerReady': tradier_client.is_ready(runtime_state.get().stock_mode),
            'session': session.to_dict(),
            'enabled': self._runtime.enabled,
            'pollSeconds': self._runtime.poll_seconds,
            'lastStartedAtUtc': self._runtime.last_started_at_utc,
            'lastFinishedAtUtc': self._runtime.last_finished_at_utc,
            'lastError': self._runtime.last_error,
            'consecutiveFailures': self._runtime.consecutive_failures,
            'lastRunSummary': self._runtime.last_run_summary,
            'summary': {
                'expiredPositionCount': len(due_rows),
                'eligibleExpiredCount': eligible_due,
                'blockedExpiredCount': blocked_due,
                'managedOnlyExpiredCount': sum(1 for row in due_rows if row.get('managedOnly')),
                'alreadyInProgressCount': sum(1 for row in due_rows if self._has_active_exit_intent(db, row)),
            },
            'rows': [
                {
                    'symbol': row['symbol'],
                    'managedOnly': bool(row.get('managedOnly')),
                    'positionState': row.get('positionState', {}),
                    'monitoringStatus': row.get('monitoringStatus'),
                    'exitAlreadyInProgress': self._has_active_exit_intent(db, row),
                }
                for row in due_rows
            ],
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
        due_rows = self._get_due_rows(db)[: max(1, int(limit))]
        runtime = runtime_state.get()
        session = get_scope_session_status(SUPPORTED_SCOPE, observed_at)
        result: dict[str, Any] = {
            'scope': SUPPORTED_SCOPE,
            'capturedAtUtc': observed_at.isoformat(),
            'executeRequested': bool(execute),
            'mode': runtime.stock_mode,
            'runtimeRunning': runtime.running,
            'brokerReady': tradier_client.is_ready(runtime.stock_mode),
            'session': session.to_dict(),
            'summary': {
                'expiredPositionCount': len(due_rows),
                'candidateCount': 0,
                'submittedCount': 0,
                'closedCount': 0,
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

            if not session.session_open:
                candidate['action'] = 'BLOCKED'
                candidate['reason'] = 'STOCK_SESSION_CLOSED'
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
            result['rows'].append(executed)
            if executed['action'] == 'EXIT_SUBMITTED':
                result['summary']['submittedCount'] += 1
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
        while True:
            self._runtime.last_started_at_utc = datetime.now(UTC).isoformat()
            db = SessionLocal()
            try:
                run_summary = self.run_once(db, limit=settings.WATCHLIST_EXIT_WORKER_BATCH_LIMIT)
                if run_summary['summary']['submittedCount'] > 0 or run_summary['summary']['blockedCount'] > 0:
                    logger.info(
                        'Watchlist exit sweep complete: submitted=%s closed=%s blocked=%s expired=%s',
                        run_summary['summary']['submittedCount'],
                        run_summary['summary']['closedCount'],
                        run_summary['summary']['blockedCount'],
                        run_summary['summary']['expiredPositionCount'],
                    )
            except asyncio.CancelledError:
                db.close()
                raise
            except Exception as exc:
                logger.exception('Watchlist exit worker sweep failed: %s', exc)
                self._runtime.last_error = str(exc)
                self._runtime.consecutive_failures += 1
                self._runtime.last_finished_at_utc = datetime.now(UTC).isoformat()
            finally:
                db.close()
            await asyncio.sleep(self._runtime.poll_seconds)

    def _submit_stock_exit(self, db: Session, candidate: dict[str, Any], *, mode: str) -> dict[str, Any]:
        position = db.query(Position).filter(Position.id == candidate['positionId']).first()
        if position is None or not position.is_open or int(position.shares or 0) <= 0:
            candidate['action'] = 'SKIPPED'
            candidate['reason'] = 'POSITION_NOT_OPEN'
            return candidate

        fallback_quantity = int(position.shares or 0)
        broker_quantity = self._safe_broker_quantity(position.ticker, mode)
        requested_quantity = broker_quantity or fallback_quantity
        candidate['fallbackQuantity'] = fallback_quantity
        candidate['brokerQuantity'] = broker_quantity
        candidate['requestedQuantity'] = requested_quantity
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
                'exitTrigger': EXIT_TRIGGER,
                'mode': mode,
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
                exit_trigger=EXIT_TRIGGER,
            )
        except Exception as exc:
            execution_lifecycle.record_event(
                db,
                exit_intent,
                event_type='EXIT_SUBMISSION_FAILED',
                status='REJECTED',
                message=f'Watchlist exit worker failed for {position.ticker}: {exc}',
                payload={'error': str(exc), 'exitTrigger': EXIT_TRIGGER},
            )
            exit_intent.status = 'REJECTED'
            exit_intent.rejection_reason = str(exc)
            db.commit()
            db.refresh(exit_intent)
            candidate['action'] = 'SKIPPED'
            candidate['reason'] = f'EXIT_SUBMISSION_FAILED: {exc}'
            candidate['intentStatus'] = exit_intent.status
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

        candidate['action'] = 'EXIT_SUBMITTED'
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
        snapshot = watchlist_service.get_exit_readiness_snapshot(db, scope=SUPPORTED_SCOPE, expiring_within_hours=24)
        rows = list(snapshot.get('rows', []))
        return [row for row in rows if row.get('positionState', {}).get('positionExpired')]

    def _build_candidate_row(self, db: Session, row: dict[str, Any]) -> dict[str, Any]:
        payload = {
            'symbol': row.get('symbol'),
            'managedOnly': bool(row.get('managedOnly')),
            'monitoringStatus': row.get('monitoringStatus'),
            'positionId': row.get('positionState', {}).get('positionId'),
            'positionState': row.get('positionState', {}),
            'exitTemplate': row.get('exitTemplate'),
            'action': None,
            'reason': None,
        }
        if self._has_active_exit_intent(db, row):
            payload['action'] = 'EXIT_ALREADY_IN_PROGRESS'
            payload['reason'] = 'ACTIVE_EXIT_INTENT_EXISTS'
        return payload

    def _has_active_exit_intent(self, db: Session, row: dict[str, Any]) -> bool:
        position_id = row.get('positionState', {}).get('positionId')
        if position_id is None:
            return False
        return (
            db.query(OrderIntent)
            .filter(
                OrderIntent.position_id == position_id,
                OrderIntent.side == 'SELL',
                OrderIntent.status.in_(ACTIVE_EXIT_INTENT_STATUSES),
                OrderIntent.execution_source == EXECUTION_SOURCE,
            )
            .first()
            is not None
        )

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

    @staticmethod
    def _safe_broker_quantity(symbol: str, mode: str) -> int:
        try:
            return int(tradier_client.get_position_quantity_sync(symbol, mode=mode) or 0)
        except Exception:
            return 0


watchlist_exit_worker = WatchlistExitWorkerService()
