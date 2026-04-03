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
from app.models.watchlist_symbol import WatchlistSymbol
from app.services.execution_lifecycle import execution_lifecycle
from app.services.market_sessions import get_scope_session_status
from app.services.runtime_state import runtime_state
from app.services.trade_validator import trade_validator
from app.services.tradier_client import tradier_client
from app.services.watchlist_service import watchlist_service

logger = logging.getLogger(__name__)

ACTIVE_EXIT_INTENT_STATUSES = {'READY', 'SUBMITTED', 'PARTIALLY_FILLED', 'FILLED'}
TERMINAL_EXIT_INTENT_STATUSES = {'REJECTED', 'CLOSED', 'CANCELED', 'CANCELLED', 'ERROR', 'FAILED'}
EXIT_TRIGGER = 'TIME_STOP_EXPIRED'
STOP_LOSS_TRIGGER = 'STOP_LOSS_BREACH'
TRAILING_STOP_TRIGGER = 'TRAILING_STOP_BREACH'
PROFIT_TARGET_TRIGGER = 'PROFIT_TARGET_REACHED'
FOLLOW_THROUGH_TRIGGER = 'FAILED_FOLLOW_THROUGH'
EXECUTION_SOURCE = 'WATCHLIST_EXIT_WORKER'
SUPPORTED_SCOPE = 'stocks_only'
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
                {
                    'symbol': row['symbol'],
                    'managedOnly': bool(row.get('managedOnly')),
                    'positionState': row.get('positionState', {}),
                    'monitoringStatus': row.get('monitoringStatus'),
                    'exitTrigger': self._primary_exit_trigger(row),
                    'exitReasons': self._build_exit_reasons(row),
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
        runtime = runtime_state.get()
        refreshed_price_count = self._refresh_open_position_prices(db, mode=runtime.stock_mode)
        due_rows = self._get_due_rows(db)[: max(1, int(limit))]
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
        broker_quantity = self._safe_broker_quantity(position.ticker, mode)
        trigger = str(candidate.get('exitTrigger') or EXIT_TRIGGER)
        requested_quantity = self._determine_requested_quantity(
            trigger=trigger,
            fallback_quantity=fallback_quantity,
            broker_quantity=broker_quantity,
        )
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
            execution_lifecycle.record_event(
                db,
                exit_intent,
                event_type='EXIT_SUBMISSION_FAILED',
                status='REJECTED',
                message=f'Watchlist exit worker failed for {position.ticker}: {exc}',
                payload={'error': str(exc), 'exitTrigger': candidate.get('exitTrigger') or EXIT_TRIGGER},
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
        try:
            snapshot = watchlist_service.get_exit_readiness_snapshot(db, scope=SUPPORTED_SCOPE, expiring_within_hours=24)
        except Exception as exc:
            logger.warning('Watchlist exit worker could not build exit readiness snapshot: %s', exc)
            return []
        rows = list(snapshot.get('rows', []))
        return [
            row
            for row in rows
            if row.get('positionState', {}).get('positionExpired')
            or row.get('positionState', {}).get('protectiveExitPending')
            or row.get('positionState', {}).get('scaleOutReady')
            or row.get('positionState', {}).get('followThroughFailed')
        ]

    def _build_candidate_row(self, db: Session, row: dict[str, Any]) -> dict[str, Any]:
        payload = {
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
    def _determine_requested_quantity(*, trigger: str, fallback_quantity: int, broker_quantity: int) -> int:
        available_quantity = broker_quantity or fallback_quantity
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
            .filter(WatchlistSymbol.scope == SUPPORTED_SCOPE, WatchlistSymbol.symbol == symbol)
            .order_by(WatchlistSymbol.id.desc())
            .first()
        )

    @staticmethod
    def _refresh_open_position_prices(db: Session, *, mode: str) -> int:
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
        symbols = [str(position.ticker or '').upper().strip() for position in positions if str(position.ticker or '').strip()]
        if not symbols:
            return 0
        try:
            quotes = tradier_client.get_quotes_sync(symbols, mode=mode)
        except Exception:
            return 0

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
        return changed_count


watchlist_exit_worker = WatchlistExitWorkerService()
