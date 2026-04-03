from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models.order_intent import OrderIntent
from app.models.trade import Trade


@dataclass
class TradeHistoryFilters:
    mode: str | None = None
    asset_class: str | None = None
    symbol: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


class TradeHistoryService:
    def get_closed_trade_history(
        self,
        db: Session,
        *,
        filters: TradeHistoryFilters | None = None,
    ) -> dict[str, Any]:
        active_filters = filters or TradeHistoryFilters()
        rows = [
            *self._build_stock_rows(db, active_filters),
            *self._build_crypto_rows(db, active_filters),
        ]
        rows.sort(key=lambda row: self._sort_ts(row.get('soldAtUtc')), reverse=True)
        realized_total = round(sum(float(row.get('realizedPnl') or 0.0) for row in rows), 8)
        win_count = sum(1 for row in rows if float(row.get('realizedPnl') or 0.0) > 0)
        loss_count = sum(1 for row in rows if float(row.get('realizedPnl') or 0.0) < 0)
        return {
            'rows': rows,
            'summary': {
                'totalCount': len(rows),
                'realizedPnl': realized_total,
                'winCount': win_count,
                'lossCount': loss_count,
                'assetCounts': {
                    'stock': sum(1 for row in rows if row.get('assetClass') == 'stock'),
                    'crypto': sum(1 for row in rows if row.get('assetClass') == 'crypto'),
                },
                'modeCounts': {
                    'PAPER': sum(1 for row in rows if row.get('mode') == 'PAPER'),
                    'LIVE': sum(1 for row in rows if row.get('mode') == 'LIVE'),
                },
            },
        }

    def _build_stock_rows(self, db: Session, filters: TradeHistoryFilters) -> list[dict[str, Any]]:
        trades = (
            db.query(Trade)
            .filter(Trade.exit_time.is_not(None))
            .order_by(Trade.exit_time.desc(), Trade.id.desc())
            .all()
        )
        rows: list[dict[str, Any]] = []
        for trade in trades:
            entry_reasoning = trade.entry_reasoning if isinstance(trade.entry_reasoning, dict) else {}
            mode = self._normalize_mode(entry_reasoning.get('mode') or trade.account_id)
            row = {
                'id': f'stock-trade-{trade.id}',
                'tradeId': trade.trade_id,
                'assetClass': 'stock',
                'mode': mode,
                'symbol': str(trade.ticker or '').upper(),
                'buyIntentId': entry_reasoning.get('intentId'),
                'sellIntentId': None,
                'source': entry_reasoning.get('executionSource') or 'STOCK_EXECUTION_LIFECYCLE',
                'boughtAtUtc': self._iso_or_none(trade.entry_time),
                'buyPrice': self._float_or_none(trade.entry_price),
                'buyQuantity': float(trade.shares or 0.0),
                'buyTotal': self._float_or_none(trade.entry_cost),
                'soldAtUtc': self._iso_or_none(trade.exit_time),
                'sellPrice': self._float_or_none(trade.exit_price),
                'sellQuantity': float(trade.shares or 0.0),
                'sellTotal': self._float_or_none(trade.exit_proceeds),
                'unitDiff': round(float((trade.exit_price or 0.0) - (trade.entry_price or 0.0)), 8),
                'fees': 0.0,
                'realizedPnl': round(float(trade.net_pnl if trade.net_pnl is not None else trade.gross_pnl or 0.0), 8),
                'holdDurationMinutes': int(trade.duration_minutes or 0) if trade.duration_minutes is not None else None,
                'exitTrigger': trade.exit_trigger,
            }
            if self._matches_filters(row, filters):
                rows.append(row)
        return rows

    def _build_crypto_rows(self, db: Session, filters: TradeHistoryFilters) -> list[dict[str, Any]]:
        intents = (
            db.query(OrderIntent)
            .filter(OrderIntent.asset_class == 'crypto')
            .filter(OrderIntent.status.in_(['FILLED', 'CLOSED']))
            .order_by(OrderIntent.last_fill_at.asc(), OrderIntent.first_fill_at.asc(), OrderIntent.created_at.asc(), OrderIntent.id.asc())
            .all()
        )
        open_lots: dict[str, list[dict[str, Any]]] = {}
        rows: list[dict[str, Any]] = []
        for intent in intents:
            symbol = str(intent.symbol or '').upper().strip()
            if not symbol:
                continue
            context = intent.context_json if isinstance(intent.context_json, dict) else {}
            mode = self._normalize_mode(context.get('mode') or intent.account_id)
            fill_quantity = float(intent.filled_quantity or intent.requested_quantity or 0.0)
            fill_price = float(intent.avg_fill_price or intent.requested_price or 0.0)
            fill_time = intent.last_fill_at or intent.first_fill_at or intent.submitted_at or intent.created_at
            if fill_quantity <= 0 or fill_price <= 0 or fill_time is None:
                continue

            if str(intent.side or '').upper() == 'BUY':
                open_lots.setdefault(symbol, []).append(
                    {
                        'intentId': intent.intent_id,
                        'symbol': symbol,
                        'displaySymbol': context.get('displayPair') or symbol,
                        'mode': mode,
                        'source': intent.execution_source,
                        'remainingQuantity': fill_quantity,
                        'buyQuantity': fill_quantity,
                        'buyPrice': fill_price,
                        'buyTotal': round(fill_quantity * fill_price, 8),
                        'boughtAtUtc': self._iso_or_none(fill_time),
                        'boughtAt': fill_time,
                    }
                )
                continue

            if str(intent.side or '').upper() != 'SELL':
                continue

            remaining_sell = fill_quantity
            symbol_lots = open_lots.setdefault(symbol, [])
            while remaining_sell > 1e-12 and symbol_lots:
                lot = symbol_lots[0]
                matched_quantity = min(float(lot['remainingQuantity']), remaining_sell)
                buy_price = float(lot['buyPrice'])
                sell_total = round(matched_quantity * fill_price, 8)
                buy_total = round(matched_quantity * buy_price, 8)
                realized = round(sell_total - buy_total, 8)
                duration_minutes = self._duration_minutes(lot.get('boughtAt'), fill_time)
                row = {
                    'id': f"crypto-{lot['intentId']}-{intent.intent_id}-{len(rows) + 1}",
                    'tradeId': None,
                    'assetClass': 'crypto',
                    'mode': mode,
                    'symbol': lot.get('displaySymbol') or symbol,
                    'buyIntentId': lot['intentId'],
                    'sellIntentId': intent.intent_id,
                    'source': intent.execution_source,
                    'boughtAtUtc': lot['boughtAtUtc'],
                    'buyPrice': buy_price,
                    'buyQuantity': round(matched_quantity, 12),
                    'buyTotal': buy_total,
                    'soldAtUtc': self._iso_or_none(fill_time),
                    'sellPrice': fill_price,
                    'sellQuantity': round(matched_quantity, 12),
                    'sellTotal': sell_total,
                    'unitDiff': round(fill_price - buy_price, 8),
                    'fees': 0.0,
                    'realizedPnl': realized,
                    'holdDurationMinutes': duration_minutes,
                    'exitTrigger': context.get('exitTrigger') or context.get('reason'),
                }
                if self._matches_filters(row, filters):
                    rows.append(row)
                lot['remainingQuantity'] = float(lot['remainingQuantity']) - matched_quantity
                remaining_sell -= matched_quantity
                if float(lot['remainingQuantity']) <= 1e-12:
                    symbol_lots.pop(0)
        return rows

    def _matches_filters(self, row: dict[str, Any], filters: TradeHistoryFilters) -> bool:
        if filters.asset_class and str(row.get('assetClass') or '').lower() != filters.asset_class.lower():
            return False
        if filters.mode and str(row.get('mode') or '').upper() != filters.mode.upper():
            return False
        if filters.symbol and filters.symbol.upper() not in str(row.get('symbol') or '').upper():
            return False
        sold_at = self._parse_iso(row.get('soldAtUtc'))
        if filters.date_from and (sold_at is None or sold_at < filters.date_from):
            return False
        if filters.date_to and (sold_at is None or sold_at > filters.date_to):
            return False
        return True

    @staticmethod
    def _normalize_mode(value: Any) -> str:
        raw = str(value or '').strip().upper()
        if 'LIVE' in raw:
            return 'LIVE'
        return 'PAPER'

    @staticmethod
    def _iso_or_none(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _sort_ts(self, value: Any) -> float:
        parsed = self._parse_iso(value)
        return parsed.timestamp() if parsed is not None else 0.0

    @staticmethod
    def _duration_minutes(start: datetime | None, end: datetime | None) -> int | None:
        if start is None or end is None:
            return None
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        return max(int((end - start).total_seconds() // 60), 0)


trade_history_service = TradeHistoryService()
