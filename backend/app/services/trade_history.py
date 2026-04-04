from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.order_intent import OrderIntent
from app.models.trade import Trade

ET = ZoneInfo('America/New_York')


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
                'dateRange': {
                    'fromUtc': self._iso_or_none(active_filters.date_from),
                    'toUtc': self._iso_or_none(active_filters.date_to),
                    'fromEt': self._et_iso_or_none(active_filters.date_from),
                    'toEt': self._et_iso_or_none(active_filters.date_to),
                },
            },
            'filters': {
                'mode': active_filters.mode or 'ALL',
                'assetClass': active_filters.asset_class or 'all',
                'symbol': active_filters.symbol or '',
                'dateFromUtc': self._iso_or_none(active_filters.date_from),
                'dateToUtc': self._iso_or_none(active_filters.date_to),
                'dateFromEt': self._et_iso_or_none(active_filters.date_from),
                'dateToEt': self._et_iso_or_none(active_filters.date_to),
            },
            'generatedAtUtc': self._iso_or_none(datetime.now(UTC)),
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
            buy_price = self._float_or_zero(trade.entry_price)
            sell_price = self._float_or_zero(trade.exit_price)
            quantity = float(trade.shares or 0.0)
            buy_total = self._coalesce_float(trade.entry_cost, buy_price * quantity)
            sell_total = self._coalesce_float(trade.exit_proceeds, sell_price * quantity)
            sold_at = self._ensure_utc(trade.exit_time)
            bought_at = self._ensure_utc(trade.entry_time)
            strategy_snapshot = self._extract_strategy_snapshot(
                entry_reasoning,
                fallback_setup_template=trade.strategy,
            )
            technical_snapshot = self._extract_technical_snapshot(entry_reasoning)
            row = {
                'id': f'stock-trade-{trade.id}',
                'tradeId': trade.trade_id,
                'assetClass': 'stock',
                'mode': mode,
                'symbol': str(trade.ticker or '').upper(),
                'buyIntentId': entry_reasoning.get('intentId'),
                'sellIntentId': None,
                'source': entry_reasoning.get('executionSource') or 'STOCK_EXECUTION_LIFECYCLE',
                'boughtAtUtc': self._iso_or_none(bought_at),
                'boughtAtEt': self._et_iso_or_none(bought_at),
                'buyPrice': buy_price,
                'buyQuantity': quantity,
                'buyTotal': round(buy_total, 8),
                'soldAtUtc': self._iso_or_none(sold_at),
                'soldAtEt': self._et_iso_or_none(sold_at),
                'sellPrice': sell_price,
                'sellQuantity': quantity,
                'sellTotal': round(sell_total, 8),
                'priceDifference': round(sell_price - buy_price, 8),
                'differenceAmount': round(sell_total - buy_total, 8),
                'fees': 0.0,
                'realizedPnl': round(float(trade.net_pnl if trade.net_pnl is not None else trade.gross_pnl or 0.0), 8),
                'holdDurationMinutes': int(trade.duration_minutes or 0) if trade.duration_minutes is not None else None,
                'exitTrigger': trade.exit_trigger,
                'strategySnapshot': strategy_snapshot,
                'technicalSnapshot': technical_snapshot,
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
            fill_time = self._ensure_utc(intent.last_fill_at or intent.first_fill_at or intent.submitted_at or intent.created_at)
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
                        'boughtAtEt': self._et_iso_or_none(fill_time),
                        'boughtAt': fill_time,
                        'context': context,
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
                buy_total = round(matched_quantity * buy_price, 8)
                sell_total = round(matched_quantity * fill_price, 8)
                realized = round(sell_total - buy_total, 8)
                duration_minutes = self._duration_minutes(lot.get('boughtAt'), fill_time)
                buy_context = self._dict_or_empty(lot.get('context'))
                strategy_snapshot = self._extract_strategy_snapshot(
                    buy_context,
                    fallback_setup_template=((buy_context.get('watchlist') or {}).get('setupTemplate') if isinstance(buy_context.get('watchlist'), dict) else None),
                )
                technical_snapshot = self._extract_technical_snapshot(buy_context)
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
                    'boughtAtEt': lot['boughtAtEt'],
                    'buyPrice': buy_price,
                    'buyQuantity': round(matched_quantity, 12),
                    'buyTotal': buy_total,
                    'soldAtUtc': self._iso_or_none(fill_time),
                    'soldAtEt': self._et_iso_or_none(fill_time),
                    'sellPrice': fill_price,
                    'sellQuantity': round(matched_quantity, 12),
                    'sellTotal': sell_total,
                    'priceDifference': round(fill_price - buy_price, 8),
                    'differenceAmount': realized,
                    'fees': 0.0,
                    'realizedPnl': realized,
                    'holdDurationMinutes': duration_minutes,
                    'exitTrigger': context.get('exitTrigger') or context.get('reason'),
                    'strategySnapshot': strategy_snapshot,
                    'technicalSnapshot': technical_snapshot,
                }
                if self._matches_filters(row, filters):
                    rows.append(row)
                lot['remainingQuantity'] = float(lot['remainingQuantity']) - matched_quantity
                remaining_sell -= matched_quantity
                if float(lot['remainingQuantity']) <= 1e-12:
                    symbol_lots.pop(0)
        return rows

    @staticmethod
    def _dict_or_empty(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _extract_strategy_snapshot(
        self,
        source: dict[str, Any],
        *,
        fallback_setup_template: Any = None,
    ) -> dict[str, Any]:
        strategy_snapshot = dict(self._dict_or_empty(source.get('strategySnapshot')))
        watchlist = self._dict_or_empty(source.get('watchlist'))
        if not strategy_snapshot and watchlist:
            strategy_snapshot = {
                'scope': watchlist.get('scope'),
                'priorityRank': watchlist.get('priorityRank'),
                'tier': watchlist.get('tier'),
                'bias': watchlist.get('bias'),
                'setupTemplate': watchlist.get('setupTemplate'),
                'exitTemplate': watchlist.get('exitTemplate'),
                'triggerTimeframe': self._coerce_trigger_timeframe(watchlist.get('triggerTimeframe') or watchlist.get('botTimeframes')),
                'riskFlags': watchlist.get('riskFlags') if isinstance(watchlist.get('riskFlags'), list) else [],
            }
        if fallback_setup_template and not strategy_snapshot.get('setupTemplate'):
            strategy_snapshot['setupTemplate'] = str(fallback_setup_template)
        if 'triggerTimeframe' not in strategy_snapshot:
            strategy_snapshot['triggerTimeframe'] = self._coerce_trigger_timeframe(strategy_snapshot.get('botTimeframes'))
        cleaned = {key: value for key, value in strategy_snapshot.items() if value is not None and key != 'botTimeframes'}
        cleaned['triggerTimeframe'] = self._coerce_trigger_timeframe(cleaned.get('triggerTimeframe'))
        risk_flags = cleaned.get('riskFlags')
        if not isinstance(risk_flags, list):
            cleaned['riskFlags'] = [] if risk_flags is None else [str(risk_flags)]
        return cleaned


    @staticmethod
    def _coerce_trigger_timeframe(value: Any) -> str | None:
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            if not items:
                return None
            order = ['5m', '15m', '1h', '4h', '1d']
            ranked = sorted(items, key=lambda item: order.index(item) if item in order else len(order))
            return ranked[0] if ranked else items[0]
        raw = str(value or '').strip()
        return raw or None

    def _extract_technical_snapshot(self, source: dict[str, Any]) -> dict[str, Any]:
        technical_snapshot = dict(self._dict_or_empty(source.get('technicalSnapshot')))
        if technical_snapshot:
            return {key: value for key, value in technical_snapshot.items() if value is not None}
        return {}

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
    def _ensure_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @classmethod
    def _iso_or_none(cls, value: datetime | None) -> str | None:
        normalized = cls._ensure_utc(value)
        return normalized.isoformat() if normalized is not None else None

    @classmethod
    def _et_iso_or_none(cls, value: datetime | None) -> str | None:
        normalized = cls._ensure_utc(value)
        return normalized.astimezone(ET).isoformat() if normalized is not None else None

    @staticmethod
    def _float_or_zero(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _coalesce_float(primary: Any, fallback: float) -> float:
        try:
            if primary is not None:
                return float(primary)
        except (TypeError, ValueError):
            pass
        return float(fallback)

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
