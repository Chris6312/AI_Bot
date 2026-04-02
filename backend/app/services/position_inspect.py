from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.order_event import OrderEvent
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.trade import Trade
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.models.watchlist_upload import WatchlistUpload
from app.services.kraken_service import crypto_ledger


@dataclass
class PositionInspectNotFound(Exception):
    message: str


class PositionInspectService:
    """Builds a normalized inspect payload for stock and crypto positions."""

    def get_inspect_payload(self, db: Session, *, asset_class: str, symbol: str) -> dict[str, Any]:
        normalized_asset = str(asset_class or '').strip().lower()
        normalized_symbol = str(symbol or '').strip().upper()
        if normalized_asset == 'stock':
            return self._build_stock_payload(db, normalized_symbol)
        if normalized_asset == 'crypto':
            return self._build_crypto_payload(db, normalized_symbol)
        raise PositionInspectNotFound(f'Unsupported asset class: {asset_class}')

    def _build_stock_payload(self, db: Session, symbol: str) -> dict[str, Any]:
        position = (
            db.query(Position)
            .filter(Position.ticker == symbol, Position.is_open.is_(True))
            .order_by(Position.entry_time.desc(), Position.id.desc())
            .first()
        )
        if position is None:
            raise PositionInspectNotFound(f'No open stock position found for {symbol}.')

        entry_reasoning = position.entry_reasoning if isinstance(position.entry_reasoning, dict) else {}
        linked_intent_id = str(entry_reasoning.get('intentId') or position.execution_id or '').strip() or None
        intent = self._find_intent(db, asset_class='stock', symbol=symbol, intent_id=linked_intent_id)
        trade = None
        if intent is not None and intent.trade_id is not None:
            trade = db.query(Trade).filter(Trade.id == intent.trade_id).first()
        if trade is None:
            trade = (
                db.query(Trade)
                .filter(Trade.ticker == symbol)
                .order_by(Trade.entry_time.desc(), Trade.id.desc())
                .first()
            )

        events = self._load_events(db, intent)
        signal_snapshot = {
            'strategy': position.strategy,
            'executionSource': entry_reasoning.get('executionSource') or (intent.execution_source if intent is not None else None),
            'entryReasoning': entry_reasoning,
            'status': intent.status if intent is not None else None,
        }
        sizing = {
            'accountId': position.account_id,
            'requestedQuantity': float(intent.requested_quantity or 0.0) if intent is not None else float(position.shares or 0),
            'filledQuantity': float(intent.filled_quantity or 0.0) if intent is not None else float(position.shares or 0),
            'requestedPrice': float(intent.requested_price or 0.0) if intent is not None and intent.requested_price is not None else None,
            'avgFillPrice': float(intent.avg_fill_price or 0.0) if intent is not None and intent.avg_fill_price is not None else float(position.avg_entry_price or 0.0),
        }
        exit_plan = {
            'template': None,
            'stopLoss': float(position.stop_loss or 0.0) if position.stop_loss is not None else None,
            'profitTarget': float(position.profit_target or 0.0) if position.profit_target is not None else None,
            'trailingStop': float(position.trailing_stop or 0.0) if position.trailing_stop is not None else None,
            'peakPrice': float(position.peak_price or 0.0) if position.peak_price is not None else None,
            'tradeExitTrigger': trade.exit_trigger if trade is not None else None,
        }
        return {
            'assetClass': 'stock',
            'symbol': symbol,
            'displaySymbol': symbol,
            'inspectSource': 'positions_table',
            'positionSnapshot': {
                'accountId': position.account_id,
                'quantityLabel': 'Shares',
                'quantity': int(position.shares or 0),
                'avgEntryPrice': float(position.avg_entry_price or 0.0) if position.avg_entry_price is not None else None,
                'currentPrice': float(position.current_price or 0.0) if position.current_price is not None else None,
                'marketValue': (float(position.current_price or 0.0) * float(position.shares or 0.0)) if position.current_price is not None else None,
                'unrealizedPnl': float(position.unrealized_pnl or 0.0) if position.unrealized_pnl is not None else None,
                'unrealizedPnlPct': float(position.unrealized_pnl_pct or 0.0) if position.unrealized_pnl_pct is not None else None,
                'entryTimeUtc': position.entry_time.isoformat() if position.entry_time else None,
                'isOpen': bool(position.is_open),
            },
            'signalSnapshot': signal_snapshot,
            'sizing': sizing,
            'timeframeAlignment': {
                'mode': 'legacy_stock_position',
                'configured': [],
                'confirmed': [],
                'items': [],
                'note': 'Legacy stock positions preserve entry reasoning and lifecycle events, but they do not yet store per-timeframe confirmation flags in a normalized shape.',
            },
            'exitPlan': exit_plan,
            'latestEvaluation': None,
            'lifecycle': events,
            'rawContext': {
                'entryReasoning': entry_reasoning,
                'intentContext': intent.context_json if intent is not None else {},
                'tradeEntryReasoning': trade.entry_reasoning if trade is not None else {},
            },
        }

    def _build_crypto_payload(self, db: Session, symbol: str) -> dict[str, Any]:
        current_position = self._find_crypto_position(symbol)
        if current_position is None:
            raise PositionInspectNotFound(f'No open crypto position found for {symbol}.')

        watch_symbol = (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.scope == 'crypto_only', WatchlistSymbol.asset_class == 'crypto', WatchlistSymbol.symbol == symbol)
            .order_by(WatchlistSymbol.created_at.desc(), WatchlistSymbol.id.desc())
            .first()
        )
        monitor_state = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.scope == 'crypto_only', WatchlistMonitorState.symbol == symbol)
            .order_by(WatchlistMonitorState.updated_at.desc(), WatchlistMonitorState.id.desc())
            .first()
        )
        intent = self._find_intent(db, asset_class='crypto', symbol=symbol)
        upload = None
        if watch_symbol is not None:
            upload = db.query(WatchlistUpload).filter(WatchlistUpload.upload_id == watch_symbol.upload_id).first()

        latest_eval = dict((monitor_state.decision_context_json or {}).get('latestEvaluation', {}) or {}) if monitor_state is not None else {}
        details = dict(latest_eval.get('details') or {})
        configured_timeframes = list((watch_symbol.bot_timeframes if watch_symbol is not None else None) or (monitor_state.required_timeframes_json if monitor_state is not None else []) or [])
        monitoring_timeframe = self._infer_monitoring_timeframe(configured_timeframes)
        timeframe_items = []
        for timeframe in configured_timeframes:
            status = 'configured'
            reason = 'Watchlist requires this timeframe.'
            if timeframe == monitoring_timeframe and latest_eval:
                status = 'confirmed' if str(latest_eval.get('state') or '').upper() == 'ENTRY_CANDIDATE' else 'evaluated'
                reason = str(latest_eval.get('reason') or 'Primary monitoring timeframe evaluated on the latest pass.')
            timeframe_items.append({'timeframe': timeframe, 'status': status, 'reason': reason})

        events = self._load_events(db, intent)
        signal_snapshot = {
            'marketRegime': upload.market_regime if upload is not None else None,
            'tradeDirection': watch_symbol.trade_direction if watch_symbol is not None else None,
            'bias': watch_symbol.bias if watch_symbol is not None else None,
            'setupTemplate': watch_symbol.setup_template if watch_symbol is not None else None,
            'priorityRank': watch_symbol.priority_rank if watch_symbol is not None else None,
            'tier': watch_symbol.tier if watch_symbol is not None else None,
            'riskFlags': watch_symbol.risk_flags if watch_symbol is not None else [],
            'latestDecisionState': monitor_state.latest_decision_state if monitor_state is not None else None,
            'latestDecisionReason': monitor_state.latest_decision_reason if monitor_state is not None else None,
            'details': details,
        }
        intent_context = intent.context_json if intent is not None and isinstance(intent.context_json, dict) else {}
        sizing = {
            'accountId': intent.account_id if intent is not None else 'paper-crypto-ledger',
            'requestedQuantity': float(intent.requested_quantity or 0.0) if intent is not None else float(current_position.get('amount') or 0.0),
            'filledQuantity': float(intent.filled_quantity or 0.0) if intent is not None else float(current_position.get('amount') or 0.0),
            'requestedPrice': float(intent.requested_price or 0.0) if intent is not None and intent.requested_price is not None else None,
            'avgFillPrice': float(intent.avg_fill_price or 0.0) if intent is not None and intent.avg_fill_price is not None else float(current_position.get('avgPrice') or 0.0),
            'estimatedValue': self._maybe_float(intent_context.get('estimatedValue')),
            'positionPct': self._maybe_float(intent_context.get('positionPct')),
            'displayPair': intent_context.get('displayPair') or current_position.get('pair'),
            'ohlcvPair': intent_context.get('ohlcvPair') or current_position.get('ohlcvPair'),
        }
        exit_plan = {
            'template': watch_symbol.exit_template if watch_symbol is not None else None,
            'maxHoldHours': watch_symbol.max_hold_hours if watch_symbol is not None else None,
            'triggerLevel': self._maybe_float(details.get('triggerLevel')),
            'bounceFloor': self._maybe_float(details.get('bounceFloor')),
            'breakoutLevel': self._maybe_float(details.get('breakoutLevel')),
            'recentHigh': self._maybe_float(details.get('recentHigh')),
            'recentLow': self._maybe_float(details.get('recentLow')),
            'continuityOk': details.get('continuityOk'),
            'continuityGapSeconds': self._maybe_float(details.get('continuityGapSeconds')),
            'marketDataAtUtc': latest_eval.get('marketDataAtUtc'),
        }
        return {
            'assetClass': 'crypto',
            'symbol': symbol,
            'displaySymbol': current_position.get('pair') or symbol,
            'inspectSource': 'crypto_paper_ledger',
            'positionSnapshot': {
                'accountId': 'paper-crypto-ledger',
                'quantityLabel': 'Amount',
                'quantity': self._maybe_float(current_position.get('amount')),
                'avgEntryPrice': self._maybe_float(current_position.get('avgPrice')),
                'currentPrice': self._maybe_float(current_position.get('currentPrice')),
                'marketValue': self._maybe_float(current_position.get('marketValue')),
                'costBasis': self._maybe_float(current_position.get('costBasis')),
                'unrealizedPnl': self._maybe_float(current_position.get('pnl')),
                'unrealizedPnlPct': self._maybe_float(current_position.get('pnlPercent')),
                'realizedPnl': self._maybe_float(current_position.get('realizedPnl')),
                'entryTimeUtc': current_position.get('entryTimeUtc'),
                'isOpen': True,
            },
            'signalSnapshot': signal_snapshot,
            'sizing': sizing,
            'timeframeAlignment': {
                'mode': 'single_timeframe_monitor',
                'configured': configured_timeframes,
                'confirmed': [monitoring_timeframe] if monitoring_timeframe and latest_eval else [],
                'items': timeframe_items,
                'note': (
                    'The current runner confirms one monitoring timeframe per cycle and preserves the remaining watchlist timeframes as required context.'
                    if configured_timeframes
                    else 'No watchlist timeframes were available for this position.'
                ),
            },
            'exitPlan': exit_plan,
            'latestEvaluation': latest_eval or None,
            'lifecycle': events,
            'rawContext': {
                'intentContext': intent_context,
                'watchlist': {
                    'uploadId': watch_symbol.upload_id if watch_symbol is not None else None,
                    'scope': watch_symbol.scope if watch_symbol is not None else 'crypto_only',
                },
                'monitorContext': monitor_state.decision_context_json if monitor_state is not None else {},
            },
        }

    @staticmethod
    def _find_crypto_position(symbol: str) -> dict[str, Any] | None:
        normalized = str(symbol or '').strip().upper()
        for row in crypto_ledger.get_positions():
            pair = str(row.get('pair') or '').strip().upper()
            if pair == normalized:
                return row
        return None

    @staticmethod
    def _find_intent(db: Session, *, asset_class: str, symbol: str, intent_id: str | None = None) -> OrderIntent | None:
        if intent_id:
            exact = db.query(OrderIntent).filter(OrderIntent.intent_id == intent_id).first()
            if exact is not None:
                return exact
        query = (
            db.query(OrderIntent)
            .filter(OrderIntent.asset_class == asset_class, OrderIntent.symbol == symbol)
            .order_by(
                OrderIntent.last_fill_at.desc(),
                OrderIntent.first_fill_at.desc(),
                OrderIntent.submitted_at.desc(),
                OrderIntent.created_at.desc(),
                OrderIntent.id.desc(),
            )
        )
        preferred = query.filter(OrderIntent.side == 'BUY', OrderIntent.status.in_(['FILLED', 'PARTIALLY_FILLED'])).first()
        return preferred or query.first()

    @staticmethod
    def _load_events(db: Session, intent: OrderIntent | None) -> list[dict[str, Any]]:
        if intent is None:
            return []

        rows = (
            db.query(OrderEvent)
            .filter(OrderEvent.intent_id == intent.intent_id)
            .order_by(OrderEvent.event_time.asc(), OrderEvent.id.asc())
            .all()
        )

        events: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()

        for row in rows:
            event_time = (
                row.event_time
                or intent.created_at
                or intent.submitted_at
                or intent.first_fill_at
                or intent.last_fill_at
            )
            payload = row.payload_json if isinstance(row.payload_json, dict) else {}
            key = (row.event_type, row.status, row.message, event_time.isoformat() if event_time else None)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                {
                    'eventType': row.event_type,
                    'status': row.status,
                    'message': row.message,
                    'eventTime': event_time.isoformat() if event_time else None,
                    'payload': payload,
                }
            )

        has_intent_created = any(event['eventType'] == 'INTENT_CREATED' for event in events)
        synthetic_intent_time = (
            intent.created_at
            or intent.submitted_at
            or intent.first_fill_at
            or intent.last_fill_at
        )
        if not has_intent_created and synthetic_intent_time is not None:
            events.append(
                {
                    'eventType': 'INTENT_CREATED',
                    'status': intent.status,
                    'message': f'Prepared {intent.side} intent for {intent.symbol}',
                    'eventTime': synthetic_intent_time.isoformat(),
                    'payload': {
                        'requestedQuantity': float(intent.requested_quantity or 0.0),
                        'requestedPrice': float(intent.requested_price) if intent.requested_price is not None else None,
                    },
                }
            )

        priority = {
            'INTENT_CREATED': 0,
            'ORDER_SUBMITTED': 1,
            'ORDER_ACCEPTED': 2,
            'ORDER_STATUS_UPDATED': 3,
            'ORDER_PARTIALLY_FILLED': 4,
            'ORDER_FILLED': 5,
            'EXIT_SUBMITTED': 6,
            'POSITION_EXITED': 7,
            'ORDER_CLOSED': 8,
        }
        events.sort(key=lambda event: (priority.get(str(event.get('eventType') or ''), 99), event.get('eventTime') or ''))
        return events

    @staticmethod
    def _infer_monitoring_timeframe(configured_timeframes: list[str]) -> str | None:
        if not configured_timeframes:
            return None
        order = ['1m', '5m', '15m', '1h', '4h', '1d']
        ranked = sorted(configured_timeframes, key=lambda item: order.index(item) if item in order else len(order))
        return ranked[0] if ranked else configured_timeframes[0]

    @staticmethod
    def _maybe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


position_inspect_service = PositionInspectService()
