from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.watchlist_monitor_state import MONITOR_ONLY, PENDING_EVALUATION, WatchlistMonitorState
from app.services.kraken_service import crypto_ledger, kraken_service
from app.services.runtime_state import runtime_state
from app.services.watchlist_service import watchlist_service

logger = logging.getLogger(__name__)

AssetClass = Literal['stock', 'crypto']
REPLAYABLE_CRYPTO_INTENT_STATUSES = {'FILLED', 'CLOSED'}
ACTIVE_ENTRY_INTENT_STATUSES = {'READY', 'SUBMITTED', 'PARTIALLY_FILLED'}
CRYPTO_LEDGER_ACCOUNT_ID = 'paper-crypto-ledger'


@dataclass(slots=True)
class ReplayedCryptoTrade:
    intent_id: str
    pair: str
    ohlcv_pair: str | None
    side: str
    amount: Decimal
    price: Decimal
    timestamp: datetime


class PositionReconciliationService:
    def reconcile_all(
        self,
        db: Session,
        *,
        observed_at: datetime | None = None,
        asset_classes: tuple[AssetClass, ...] = ('crypto', 'stock'),
    ) -> dict[str, Any]:
        observed_at = self._coerce_utc(observed_at)
        results: dict[str, Any] = {}
        for asset_class in asset_classes:
            results[asset_class] = self.reconcile_asset_class(db, asset_class=asset_class, observed_at=observed_at)
        return {
            'observedAtUtc': observed_at.isoformat(),
            'results': results,
        }

    def reconcile_asset_class(
        self,
        db: Session,
        *,
        asset_class: AssetClass,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        observed_at = self._coerce_utc(observed_at)
        if asset_class == 'crypto':
            return self._reconcile_crypto(db, observed_at=observed_at)
        if asset_class == 'stock':
            return self._reconcile_stock(db, observed_at=observed_at)
        raise ValueError(f'Unsupported asset class for reconciliation: {asset_class}')

    def _reconcile_crypto(self, db: Session, *, observed_at: datetime) -> dict[str, Any]:
        replay_trades = self._build_replayable_crypto_trades(db)
        restored = self._restore_crypto_ledger_from_trades(replay_trades)
        cleared_guards = self._clear_stale_open_position_guards(
            db,
            scope='crypto_only',
            open_symbols=self._crypto_open_symbol_aliases(),
            has_open_position=self._has_open_crypto_position,
            observed_at=observed_at,
        )
        db.commit()
        logger.info(
            'Startup crypto reconciliation complete: replayed_trades=%s restored_positions=%s cleared_guards=%s',
            restored['replayedTradeCount'],
            restored['restoredPositionCount'],
            cleared_guards,
        )
        return {
            'assetClass': 'crypto',
            'observedAtUtc': observed_at.isoformat(),
            'replayedTradeCount': restored['replayedTradeCount'],
            'restoredPositionCount': restored['restoredPositionCount'],
            'restoredSymbols': restored['restoredSymbols'],
            'clearedMonitorGuardCount': cleared_guards,
            'externalOpenSymbols': sorted(self._crypto_open_symbol_aliases()),
        }

    def _reconcile_stock(self, db: Session, *, observed_at: datetime) -> dict[str, Any]:
        broker_positions = watchlist_service._get_open_stock_broker_positions()
        mirror_summary = watchlist_service._sync_stock_position_mirror_from_broker(
            db,
            observed_at=observed_at,
            broker_positions=broker_positions,
        )
        open_symbols = set(broker_positions.keys()) | {
            str(row.ticker or '').upper().strip()
            for row in db.query(Position).filter(Position.is_open.is_(True)).all()
            if str(row.ticker or '').strip()
        }
        cleared_guards = self._clear_stale_open_position_guards(
            db,
            scope='stocks_only',
            open_symbols=open_symbols,
            has_open_position=self._has_open_stock_position,
            observed_at=observed_at,
        )
        db.commit()
        logger.info(
            'Startup stock reconciliation complete: inserted=%s updated=%s closed=%s cleared_guards=%s',
            mirror_summary.get('inserted', 0),
            mirror_summary.get('updated', 0),
            mirror_summary.get('closed', 0),
            cleared_guards,
        )
        quantity_truth = [
            self.get_stock_quantity_truth(db, symbol=symbol, broker_positions=broker_positions)
            for symbol in sorted(open_symbols)
        ]
        return {
            'assetClass': 'stock',
            'observedAtUtc': observed_at.isoformat(),
            'mirrorSummary': mirror_summary,
            'clearedMonitorGuardCount': cleared_guards,
            'externalOpenSymbols': sorted(open_symbols),
            'quantityTruth': quantity_truth,
        }

    def _build_replayable_crypto_trades(self, db: Session) -> list[ReplayedCryptoTrade]:
        intents = (
            db.query(OrderIntent)
            .filter(
                OrderIntent.asset_class == 'crypto',
                OrderIntent.account_id == CRYPTO_LEDGER_ACCOUNT_ID,
                OrderIntent.status.in_(REPLAYABLE_CRYPTO_INTENT_STATUSES),
            )
            .order_by(OrderIntent.last_fill_at.asc(), OrderIntent.first_fill_at.asc(), OrderIntent.created_at.asc(), OrderIntent.id.asc())
            .all()
        )
        replay_trades: list[ReplayedCryptoTrade] = []
        for intent in intents:
            quantity = Decimal(str(intent.filled_quantity or 0.0))
            price = Decimal(str(intent.avg_fill_price or 0.0))
            side = str(intent.side or '').upper().strip()
            pair = str(intent.symbol or '').upper().strip()
            if quantity <= 0 or price <= 0 or side not in {'BUY', 'SELL'} or not pair:
                continue
            context = intent.context_json if isinstance(intent.context_json, dict) else {}
            ohlcv_pair = str(context.get('ohlcvPair') or '').strip() or None
            replay_trades.append(
                ReplayedCryptoTrade(
                    intent_id=intent.intent_id,
                    pair=pair,
                    ohlcv_pair=ohlcv_pair,
                    side=side,
                    amount=quantity,
                    price=price,
                    timestamp=self._coerce_utc(intent.last_fill_at or intent.first_fill_at or intent.submitted_at or intent.created_at),
                )
            )
        return replay_trades

    def _restore_crypto_ledger_from_trades(self, trades: list[ReplayedCryptoTrade]) -> dict[str, Any]:
        balance = Decimal(str(crypto_ledger.starting_balance))
        positions: dict[str, dict[str, Any]] = {}

        for trade in trades:
            pair = trade.pair
            total = trade.amount * trade.price
            state = positions.setdefault(
                pair,
                {
                    'amount': Decimal('0'),
                    'total_cost': Decimal('0'),
                    'entry_time_utc': None,
                    'ohlcv_pair': trade.ohlcv_pair or kraken_service.get_ohlcv_pair(pair) or pair.replace('/', ''),
                },
            )

            if trade.side == 'BUY':
                balance -= total
                if Decimal(str(state['amount'])) <= Decimal('0.0000000001') and trade.amount > 0:
                    state['entry_time_utc'] = trade.timestamp.isoformat()
                state['amount'] = Decimal(str(state['amount'])) + trade.amount
                state['total_cost'] = Decimal(str(state['total_cost'])) + total
            else:
                current_amount = Decimal(str(state['amount']))
                sell_amount = min(trade.amount, current_amount)
                if sell_amount > 0:
                    avg_cost = (Decimal(str(state['total_cost'])) / current_amount) if current_amount > 0 else Decimal('0')
                    closed_cost = avg_cost * sell_amount
                    state['amount'] = current_amount - sell_amount
                    state['total_cost'] = Decimal(str(state['total_cost'])) - closed_cost
                    balance += sell_amount * trade.price
                if Decimal(str(state['amount'])) <= Decimal('0.0000000001'):
                    state['amount'] = Decimal('0')
                    state['total_cost'] = Decimal('0')
                    state['entry_time_utc'] = None

            if Decimal(str(state['amount'])) <= Decimal('0.0000000001'):
                positions.pop(pair, None)

        crypto_ledger.trades = []
        crypto_ledger.positions = positions
        crypto_ledger.balance = balance
        restored_symbols = sorted(positions.keys())
        return {
            'replayedTradeCount': len(trades),
            'restoredPositionCount': len(restored_symbols),
            'restoredSymbols': restored_symbols,
        }


    def get_stock_quantity_truth(
        self,
        db: Session,
        *,
        symbol: str,
        broker_positions: dict[str, dict[str, Any]] | None = None,
        pending_orders: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        normalized_symbol = str(symbol or '').upper().strip()
        open_rows = (
            db.query(Position)
            .filter(Position.ticker == normalized_symbol, Position.is_open.is_(True))
            .order_by(Position.id.desc())
            .all()
        )
        db_open_quantity = sum(max(int(row.shares or 0), 0) for row in open_rows)
        if broker_positions is None:
            broker_positions = watchlist_service._get_open_stock_broker_positions()
        broker_position = broker_positions.get(normalized_symbol) or {}
        broker_quantity = max(int(round(float(broker_position.get('shares') or 0.0))), 0)
        normalized_pending_orders = [dict(item) for item in (pending_orders or []) if isinstance(item, dict)]
        pending_exit_quantity = max(
            int(round(sum(float(item.get('remaining_quantity') or 0.0) for item in normalized_pending_orders))),
            0,
        )
        sellable_quantity = max(broker_quantity - pending_exit_quantity, 0)
        quantity_delta = broker_quantity - db_open_quantity
        return {
            'symbol': normalized_symbol,
            'dbOpenQuantity': db_open_quantity,
            'brokerQuantity': broker_quantity,
            'pendingExitQuantity': pending_exit_quantity,
            'sellableQuantity': sellable_quantity,
            'openRowCount': len(open_rows),
            'quantityDelta': quantity_delta,
            'brokerHasPosition': broker_quantity > 0,
            'dbHasOpenPosition': db_open_quantity > 0,
            'driftDetected': quantity_delta != 0,
        }

    def _clear_stale_open_position_guards(
        self,
        db: Session,
        *,
        scope: str,
        open_symbols: set[str],
        has_open_position: Any,
        observed_at: datetime,
    ) -> int:
        cleared = 0
        rows = (
            db.query(WatchlistMonitorState)
            .filter(
                WatchlistMonitorState.scope == scope,
                WatchlistMonitorState.latest_decision_state == MONITOR_ONLY,
            )
            .all()
        )
        for row in rows:
            symbol = str(row.symbol or '').upper().strip()
            if not symbol or symbol in open_symbols or has_open_position(db, symbol):
                continue
            if self._has_active_entry_intent(db, scope=scope, symbol=symbol):
                continue
            row.latest_decision_state = PENDING_EVALUATION
            row.latest_decision_reason = 'Startup reconciliation cleared stale OPEN_POSITION_EXISTS guard.'
            row.last_decision_at_utc = observed_at
            row.next_evaluation_at_utc = observed_at
            context = dict(row.decision_context_json or {})
            entry_execution = dict(context.get('entryExecution') or {})
            entry_execution['action'] = 'RECONCILED'
            entry_execution['reason'] = 'STALE_OPEN_POSITION_GUARD_CLEARED'
            entry_execution['recordedAtUtc'] = observed_at.isoformat()
            context['entryExecution'] = entry_execution
            row.decision_context_json = context
            flag_modified(row, 'decision_context_json')
            cleared += 1
        return cleared

    @staticmethod
    def _has_active_entry_intent(db: Session, *, scope: str, symbol: str) -> bool:
        asset_class = 'crypto' if scope == 'crypto_only' else 'stock'
        candidates = {str(symbol or '').upper().strip()}
        if asset_class == 'crypto' and '/' not in symbol:
            candidates.add(f'{symbol}/USD')
        return (
            db.query(OrderIntent)
            .filter(
                OrderIntent.asset_class == asset_class,
                OrderIntent.side == 'BUY',
                OrderIntent.status.in_(ACTIVE_ENTRY_INTENT_STATUSES),
                OrderIntent.symbol.in_(sorted(candidates)),
            )
            .first()
            is not None
        )

    @staticmethod
    def _has_open_stock_position(db: Session, symbol: str) -> bool:
        return (
            db.query(Position)
            .filter(Position.ticker == symbol, Position.is_open.is_(True))
            .first()
            is not None
        )

    @staticmethod
    def _has_open_crypto_position(db: Session, symbol: str) -> bool:
        del db
        normalized = str(symbol or '').upper().strip()
        aliases = {normalized}
        if '/' not in normalized:
            aliases.add(f'{normalized}/USD')
        return any(alias in PositionReconciliationService._crypto_open_symbol_aliases() for alias in aliases)

    @staticmethod
    def _crypto_open_symbol_aliases() -> set[str]:
        aliases: set[str] = set()
        for pair in (getattr(crypto_ledger, 'positions', {}) or {}).keys():
            normalized = str(pair or '').upper().strip()
            if not normalized:
                continue
            aliases.add(normalized)
            if '/' in normalized:
                aliases.add(normalized.split('/', 1)[0])
        return aliases

    @staticmethod
    def _coerce_utc(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


position_reconciliation_service = PositionReconciliationService()
