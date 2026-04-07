from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.order_event import OrderEvent
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.trade import Trade
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.models.watchlist_upload import WatchlistUpload
from app.services.kraken_service import crypto_ledger, kraken_service
from app.services.runtime_state import runtime_state
from app.services.tradier_client import tradier_client
from app.services.watchlist_service import watchlist_service


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
            return self._build_crypto_or_cooldown_payload(db, normalized_symbol)
        raise PositionInspectNotFound(f'Unsupported asset class: {asset_class}')
        
    def _create_reconciliation_event(
        session,
        *,
        symbol: str,
        asset_class: str,
        broker_qty: float,
        broker_avg_price: float,
        synced_at,
    ):
        from app.models.audit_event import AuditEvent

        event = AuditEvent(
            event_type="POSITION_RESTORED_FROM_BROKER",
            asset_class=asset_class,
            symbol=symbol,
            details={
                "source": "reconciliation_worker",
                "broker_qty": broker_qty,
                "broker_avg_price": broker_avg_price,
                "synced_at": synced_at.isoformat(),
            },
        )

        session.add(event)

    def _build_stock_payload(self, db: Session, symbol: str) -> dict[str, Any]:
        position = (
            db.query(Position)
            .filter(Position.ticker == symbol, Position.is_open.is_(True))
            .order_by(Position.entry_time.desc(), Position.id.desc())
            .first()
        )
        if position is None:
            raise PositionInspectNotFound(f'No open stock position found for {symbol}.')

        # --- LIVE QUOTE FETCH & PNL RECALCULATION ---
        # Ensures the drawer is perfectly up-to-date, overriding any DB lag
        mode = getattr(runtime_state.get(), 'stock_mode', 'PAPER')
        try:
            quote = tradier_client.get_quote_sync(symbol, mode=mode) or {}
            live_price = quote.get('last') or quote.get('close')
        except Exception:
            live_price = None

        current_price = float(live_price) if live_price is not None else float(position.current_price or position.avg_entry_price or 0.0)
        avg_entry_price = float(position.avg_entry_price or 0.0)
        shares = float(position.shares or 0)
        
        unrealized_pnl = (current_price - avg_entry_price) * shares if shares > 0 else 0.0
        cost_basis = avg_entry_price * shares
        unrealized_pnl_pct = ((unrealized_pnl / cost_basis) * 100.0) if cost_basis > 0 else 0.0
        # --------------------------------------------

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
        broker_exit_orders = self._load_broker_exit_orders(symbol)
        signal_snapshot = {
            'strategy': position.strategy,
            'executionSource': entry_reasoning.get('executionSource') or (intent.execution_source if intent is not None else None),
            'entryReasoning': entry_reasoning,
            'status': 'EXIT_PENDING' if broker_exit_orders else (intent.status if intent is not None else None),
            'brokerExitPending': bool(broker_exit_orders),
        }
        sizing = {
            'accountId': position.account_id,
            'requestedQuantity': float(intent.requested_quantity or 0.0) if intent is not None else float(position.shares or 0),
            'filledQuantity': float(intent.filled_quantity or 0.0) if intent is not None else float(position.shares or 0),
            'requestedPrice': float(intent.requested_price or 0.0) if intent is not None and intent.requested_price is not None else None,
            'avgFillPrice': float(intent.avg_fill_price or 0.0) if intent is not None and intent.avg_fill_price is not None else float(position.avg_entry_price or 0.0),
        }
        _stock_ms = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.scope == 'stocks_only', WatchlistMonitorState.symbol == symbol)
            .order_by(WatchlistMonitorState.id.desc())
            .first()
        )
        _frozen_tpl = str(position.frozen_exit_template or '').strip() or None
        _frozen_hrs = int(position.frozen_max_hold_hours) if position.frozen_max_hold_hours is not None else None
        _db_peak = float(position.peak_price) if position.peak_price is not None else None
        _ms_peak = float(_stock_ms.peak_price_since_entry) if _stock_ms is not None and _stock_ms.peak_price_since_entry is not None else None
        _peak_price = max(p for p in (_db_peak, _ms_peak) if p is not None) if (_db_peak is not None or _ms_peak is not None) else None
        exit_plan = {
            'template': _frozen_tpl,
            'maxHoldHours': _frozen_hrs,
            'stopLoss': float(position.stop_loss or 0.0) if position.stop_loss is not None else None,
            'profitTarget': float(position.profit_target or 0.0) if position.profit_target is not None else None,
            'trailingStop': float(position.trailing_stop or 0.0) if position.trailing_stop is not None else None,
            'peakPrice': _peak_price,
            'protectionMode': _stock_ms.protection_mode_high_water if _stock_ms is not None else None,
            'feeAdjustedBreakEven': None,
            'promotedProtectiveFloor': _stock_ms.promoted_protective_floor if _stock_ms is not None else None,
            'tpTouchedAtUtc': _stock_ms.tp_touched_at_utc.isoformat() if _stock_ms is not None and _stock_ms.tp_touched_at_utc else None,
            'strongerMarginReached': bool(_stock_ms.stronger_margin_promoted_at_utc) if _stock_ms is not None else False,
            'lastConfirmedHigherLow': None,
            'tradeExitTrigger': trade.exit_trigger if trade is not None else None,
        }
        position_snapshot = {
            'accountId': position.account_id,
            'quantityLabel': 'Shares',
            'quantity': int(position.shares or 0),
            'avgEntryPrice': avg_entry_price if position.avg_entry_price is not None else None,
            'currentPrice': current_price,
            'marketValue': current_price * shares,
            'unrealizedPnl': unrealized_pnl,
            'unrealizedPnlPct': unrealized_pnl_pct,
            'entryTimeUtc': position.entry_time.isoformat() if position.entry_time else None,
            'isOpen': bool(position.is_open),
        }
        latest_evaluation = {
            'state': 'EXIT_PENDING' if broker_exit_orders else None,
            'reason': 'Broker already has an open stock exit order for this symbol.' if broker_exit_orders else None,
        }
        exit_worker = self._derive_exit_worker(
            asset_class='stock',
            display_symbol=symbol,
            exit_plan=exit_plan,
            position_snapshot=position_snapshot,
            latest_evaluation=latest_evaluation,
            monitoring_status='ACTIVE',
            lifecycle_state='ACTIVE',
            cooldown_active=False,
            broker_exit_pending=bool(broker_exit_orders),
            managed_only=False,
            lifecycle=events,
        )
        return {
            'assetClass': 'stock',
            'symbol': symbol,
            'displaySymbol': symbol,
            'inspectSource': 'positions_table',
            'positionSnapshot': position_snapshot,
            'signalSnapshot': signal_snapshot,
            'sizing': sizing,
            'timeframeAlignment': {
                'mode': 'legacy_stock_position',
                'configured': [],
                'confirmed': [],
                'items': [],
                'note': 'Legacy stock positions preserve entry reasoning and lifecycle events, but they do not yet store per-timeframe confirmation flags in a normalized shape.',
            },
            'biasExplanation': self._build_bias_explanation(
                bias=signal_snapshot.get('bias'),
                monitoring_status=signal_snapshot.get('monitoringStatus'),
                latest_evaluation=latest_evaluation,
                timeframe_items=[],
            ),
            'exitPlan': exit_plan,
            'latestEvaluation': latest_evaluation,
            'exitWorker': exit_worker,
            'lifecycle': events,
            'rawContext': {
                'entryReasoning': entry_reasoning,
                'intentContext': intent.context_json if intent is not None else {},
                'tradeEntryReasoning': trade.entry_reasoning if trade is not None else {},
                'brokerExitOrders': broker_exit_orders,
            },
        }

    def _load_broker_exit_orders(self, symbol: str) -> list[dict[str, Any]]:
        mode = getattr(runtime_state.get(), 'stock_mode', 'PAPER')
        try:
            return list(
                tradier_client.get_orders_sync(
                    mode=mode,
                    symbol=symbol,
                    side='SELL',
                    statuses=['OPEN', 'PENDING', 'SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED', 'NEW'],
                )
                or []
            )
        except Exception:
            return []

    def _build_crypto_payload(self, db: Session, symbol: str) -> dict[str, Any]:
        current_position = self._find_crypto_position(symbol)
        if current_position is None:
            raise PositionInspectNotFound(f'No open crypto position found for {symbol}.')

        aliases = self._crypto_symbol_aliases(symbol, current_position=current_position)
        intent = self._find_crypto_intent(db, aliases)
        watch_symbol = self._find_crypto_watch_symbol(db, aliases)
        monitor_state = self._find_crypto_monitor_state(db, aliases, watch_symbol)
        live_watchlist_row = self._find_live_crypto_watchlist_row(db, aliases=aliases)
        live_position_state = dict(live_watchlist_row.get('positionState') or {}) if isinstance(live_watchlist_row, dict) else {}
        live_monitoring = dict(live_watchlist_row.get('monitoring') or {}) if isinstance(live_watchlist_row, dict) else {}
        upload = None
        if watch_symbol is not None:
            upload = db.query(WatchlistUpload).filter(WatchlistUpload.upload_id == watch_symbol.upload_id).first()

        intent_context = intent.context_json if intent is not None and isinstance(intent.context_json, dict) else {}
        watchlist_context = dict(intent_context.get('watchlist') or {})
        base_monitor_context = dict(monitor_state.decision_context_json or {}) if monitor_state is not None and isinstance(monitor_state.decision_context_json, dict) else {}
        latest_eval = dict(base_monitor_context.get('latestEvaluation') or {})
        details = dict(latest_eval.get('details') or {})
        if not details and base_monitor_context:
            details = dict(base_monitor_context.get('details') or {})
        if live_position_state:
            details.update({key: value for key, value in live_position_state.items() if value is not None})

        configured_timeframes = list(
            (watch_symbol.bot_timeframes if watch_symbol is not None else None)
            or (watchlist_context.get('botTimeframes') if isinstance(watchlist_context.get('botTimeframes'), list) else None)
            or (monitor_state.required_timeframes_json if monitor_state is not None else None)
            or (base_monitor_context.get('botTimeframes') if isinstance(base_monitor_context.get('botTimeframes'), list) else None)
            or []
        )
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
        cooldown_active = bool(base_monitor_context.get('cooldownActive') or base_monitor_context.get('reentryBlockedUntilUtc'))
        signal_snapshot = {
            'marketRegime': upload.market_regime if upload is not None else watchlist_context.get('marketRegime'),
            'tradeDirection': watch_symbol.trade_direction if watch_symbol is not None else watchlist_context.get('tradeDirection'),
            'bias': watch_symbol.bias if watch_symbol is not None else watchlist_context.get('bias') or base_monitor_context.get('bias'),
            'setupTemplate': watch_symbol.setup_template if watch_symbol is not None else watchlist_context.get('setupTemplate') or base_monitor_context.get('setupTemplate'),
            'priorityRank': watch_symbol.priority_rank if watch_symbol is not None else watchlist_context.get('priorityRank'),
            'tier': watch_symbol.tier if watch_symbol is not None else watchlist_context.get('tier') or base_monitor_context.get('tier'),
            'riskFlags': watch_symbol.risk_flags if watch_symbol is not None else watchlist_context.get('riskFlags') or base_monitor_context.get('riskFlags') or [],
            'executionSource': intent.execution_source if intent is not None else None,
            'displayPair': intent_context.get('displayPair') or current_position.get('pair') or symbol,
            'ohlcvPair': intent_context.get('ohlcvPair') or current_position.get('ohlcvPair'),
            'latestDecisionState': monitor_state.latest_decision_state if monitor_state is not None else latest_eval.get('state'),
            'latestDecisionReason': monitor_state.latest_decision_reason if monitor_state is not None else latest_eval.get('reason'),
            'monitoringStatus': monitor_state.monitoring_status if monitor_state is not None else None,
            'cooldownActive': cooldown_active,
            'reentryBlockedUntilUtc': base_monitor_context.get('reentryBlockedUntilUtc'),
            'lastExitAtUtc': base_monitor_context.get('lastExitAtUtc'),
            'details': details,
        }
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
        stop_loss = self._maybe_float(live_position_state.get('stopLoss'))
        if stop_loss is None:
            stop_loss = self._maybe_float(current_position.get('stopLoss') or (
            round(float(current_position['avgPrice']) * (1.0 - float(settings.STOP_LOSS_PCT)), 8)
            if current_position.get('avgPrice') else None
        ))
        profit_target = self._maybe_float(live_position_state.get('profitTarget'))
        if profit_target is None:
            profit_target = self._maybe_float(current_position.get('profitTarget') or (
            round(float(current_position['avgPrice']) * (1.0 + float(settings.PROFIT_TARGET_PCT)), 8)
            if current_position.get('avgPrice') else None
        ))
        trailing_stop = self._maybe_float(live_position_state.get('trailingStop'))
        if trailing_stop is None:
            trailing_stop = self._maybe_float(current_position.get('trailingStop') or (
            round(float(current_position['avgPrice']) * (1.0 - float(settings.TRAILING_STOP_PCT)), 8)
            if current_position.get('avgPrice') else None
        ))
        current_price = self._maybe_float(current_position.get('currentPrice'))
        stop_distance = (current_price - stop_loss) if current_price is not None and stop_loss is not None else None
        target_distance = (profit_target - current_price) if current_price is not None and profit_target is not None else None
        trailing_distance = (current_price - trailing_stop) if current_price is not None and trailing_stop is not None else None
        frozen_policy = dict(base_monitor_context.get('frozenManagementPolicy') or {})
        exit_plan = {
            'template': (
                watchlist_context.get('exitTemplate')
                or frozen_policy.get('exitTemplate')
                or (watch_symbol.exit_template if watch_symbol is not None else None)
                or base_monitor_context.get('exitTemplate')
            ),
            'maxHoldHours': (
                watchlist_context.get('maxHoldHours')
                or frozen_policy.get('maxHoldHours')
                or (watch_symbol.max_hold_hours if watch_symbol is not None else None)
                or base_monitor_context.get('maxHoldHours')
            ),
            'triggerLevel': self._maybe_float(details.get('triggerLevel')),
            'bounceFloor': self._maybe_float(details.get('bounceFloor')),
            'breakoutLevel': self._maybe_float(details.get('breakoutLevel')),
            'recentHigh': self._maybe_float(details.get('recentHigh')),
            'recentLow': self._maybe_float(details.get('recentLow')),
            'continuityOk': details.get('continuityOk'),
            'continuityGapSeconds': self._maybe_float(details.get('continuityGapSeconds')),
            'marketDataAtUtc': latest_eval.get('marketDataAtUtc') or base_monitor_context.get('marketDataAtUtc'),
            'stopLoss': stop_loss,
            'profitTarget': profit_target,
            'trailingStop': trailing_stop,
            'protectionMode': details.get('protectionMode'),
            'feeAdjustedBreakEven': self._maybe_float(details.get('feeAdjustedBreakEven')),
            'promotedProtectiveFloor': self._maybe_float(details.get('promotedProtectiveFloor')),
            'tpTouchedAtUtc': details.get('tpTouchedAtUtc'),
            'strongerMarginReached': bool(details.get('strongerMarginReached')),
            'lastConfirmedHigherLow': self._maybe_float(details.get('lastConfirmedHigherLow')),
            'stopDistance': stop_distance,
            'targetDistance': target_distance,
            'trailingDistance': trailing_distance,
            'expectedExitThresholds': {
                'stopLoss': stop_loss,
                'profitTarget': profit_target,
                'trailingStop': trailing_stop,
                'triggerLevel': self._maybe_float(details.get('triggerLevel')),
                'bounceFloor': self._maybe_float(details.get('bounceFloor')),
                'breakoutLevel': self._maybe_float(details.get('breakoutLevel')),
            },
        }

        protective_reasons: list[str] = []
        if current_price is not None and stop_loss is not None and current_price <= stop_loss:
            protective_reasons.append('STOP_LOSS_BREACH')
        if current_price is not None and trailing_stop is not None and current_price <= trailing_stop:
            protective_reasons.append('TRAILING_STOP_BREACH')
        if protective_reasons:
            protective_reason_text = ', '.join(protective_reasons)
            signal_snapshot['latestDecisionState'] = 'EXIT_PENDING'
            signal_snapshot['latestDecisionReason'] = protective_reason_text
            latest_eval = dict(latest_eval or {})
            latest_eval['state'] = 'EXIT_PENDING'
            latest_eval['reason'] = protective_reason_text
            latest_eval.setdefault('details', {})
            latest_eval['details'] = {**dict(latest_eval.get('details') or {}), 'protectiveExitReasons': protective_reasons}
        elif live_monitoring:
            if live_monitoring.get('latestDecisionState') and not signal_snapshot.get('latestDecisionState'):
                signal_snapshot['latestDecisionState'] = live_monitoring.get('latestDecisionState')
            if live_monitoring.get('latestDecisionReason') and not signal_snapshot.get('latestDecisionReason'):
                signal_snapshot['latestDecisionReason'] = live_monitoring.get('latestDecisionReason')
        position_snapshot = {
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
        }
        latest_evaluation = ({
            **(latest_eval or {}),
            'state': (latest_eval or {}).get('state') or live_monitoring.get('latestDecisionState'),
            'reason': (latest_eval or {}).get('reason') or live_monitoring.get('latestDecisionReason'),
            'details': {
                **dict((latest_eval or {}).get('details') or {}),
                **{key: value for key, value in live_position_state.items() if value is not None},
                'cooldownActive': cooldown_active,
                'reentryBlockedUntilUtc': base_monitor_context.get('reentryBlockedUntilUtc'),
                'lastExitAtUtc': base_monitor_context.get('lastExitAtUtc'),
                'monitoringStatus': monitor_state.monitoring_status if monitor_state is not None else live_watchlist_row.get('monitoringStatus') if isinstance(live_watchlist_row, dict) else None,
            },
        } if latest_eval or cooldown_active or monitor_state is not None else None)
        exit_worker = self._derive_exit_worker(
            asset_class='crypto',
            display_symbol=current_position.get('pair') or symbol,
            exit_plan=exit_plan,
            position_snapshot=position_snapshot,
            latest_evaluation=latest_evaluation,
            monitoring_status=monitor_state.monitoring_status if monitor_state is not None else signal_snapshot.get('monitoringStatus'),
            lifecycle_state=base_monitor_context.get('lifecycleState') or 'ACTIVE',
            cooldown_active=cooldown_active,
            broker_exit_pending=bool(signal_snapshot.get('brokerExitPending')),
            managed_only=bool(watch_symbol.monitoring_status == 'MANAGED_ONLY' if watch_symbol is not None else False),
            lifecycle=events,
        )
        return {
            'assetClass': 'crypto',
            'symbol': symbol,
            'displaySymbol': current_position.get('pair') or symbol,
            'inspectSource': 'crypto_paper_ledger',
            'positionSnapshot': position_snapshot,
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
            'latestEvaluation': latest_evaluation,
            'exitWorker': exit_worker,
            'lifecycle': events,
            'rawContext': {
                'intentContext': intent_context,
                'watchlist': {
                    'uploadId': watch_symbol.upload_id if watch_symbol is not None else watchlist_context.get('uploadId'),
                    'scope': watch_symbol.scope if watch_symbol is not None else watchlist_context.get('scope') or 'crypto_only',
                    'symbolAliases': sorted(aliases),
                },
                'monitorContext': base_monitor_context,
                'currentPosition': current_position,
            },
        }

    def _build_crypto_or_cooldown_payload(self, db: Session, symbol: str) -> dict[str, Any]:
        current_position = self._find_crypto_position(symbol)
        if current_position is not None:
            return self._build_crypto_payload(db, symbol)
        return self._build_crypto_cooldown_payload(db, symbol)

    def _build_crypto_cooldown_payload(self, db: Session, symbol: str) -> dict[str, Any]:
        aliases = self._crypto_symbol_aliases(symbol)
        watch_symbol = self._find_crypto_watch_symbol(db, aliases)
        monitor_state = self._find_crypto_monitor_state(db, aliases, watch_symbol)
        if monitor_state is None:
            raise PositionInspectNotFound(f'No open crypto position found for {symbol}.')

        upload = None
        if watch_symbol is not None:
            upload = db.query(WatchlistUpload).filter(WatchlistUpload.upload_id == watch_symbol.upload_id).first()
        base_monitor_context = dict(monitor_state.decision_context_json or {}) if isinstance(monitor_state.decision_context_json, dict) else {}
        frozen_policy = dict(base_monitor_context.get('frozenManagementPolicy') or {})
        exit_execution = dict(base_monitor_context.get('exitExecution') or {})
        latest_eval = {
            'state': monitor_state.latest_decision_state,
            'reason': monitor_state.latest_decision_reason,
            'details': {
                'cooldownActive': True,
                'reentryBlockedUntilUtc': base_monitor_context.get('reentryBlockedUntilUtc'),
                'lastExitAtUtc': base_monitor_context.get('lastExitAtUtc'),
                'monitoringStatus': monitor_state.monitoring_status,
            },
        }
        configured_timeframes = list(
            (watch_symbol.bot_timeframes if watch_symbol is not None else None)
            or (monitor_state.required_timeframes_json if monitor_state is not None else None)
            or (base_monitor_context.get('botTimeframes') if isinstance(base_monitor_context.get('botTimeframes'), list) else None)
            or []
        )
        timeframe_items = [{'timeframe': timeframe, 'status': 'configured', 'reason': 'Watchlist requires this timeframe.'} for timeframe in configured_timeframes]
        return {
            'assetClass': 'crypto',
            'symbol': symbol,
            'displaySymbol': (exit_execution.get('displayPair') or (watch_symbol.symbol if watch_symbol is not None else symbol)),
            'inspectSource': 'watchlist_monitor_state',
            'positionSnapshot': {
                'accountId': 'paper-crypto-ledger',
                'quantityLabel': 'Amount',
                'quantity': 0.0,
                'avgEntryPrice': None,
                'currentPrice': None,
                'marketValue': 0.0,
                'costBasis': 0.0,
                'unrealizedPnl': 0.0,
                'unrealizedPnlPct': 0.0,
                'realizedPnl': None,
                'entryTimeUtc': None,
                'isOpen': False,
            },
            'signalSnapshot': {
                'marketRegime': upload.market_regime if upload is not None else None,
                'tradeDirection': watch_symbol.trade_direction if watch_symbol is not None else base_monitor_context.get('tradeDirection'),
                'bias': watch_symbol.bias if watch_symbol is not None else base_monitor_context.get('bias'),
                'setupTemplate': watch_symbol.setup_template if watch_symbol is not None else base_monitor_context.get('setupTemplate'),
                'priorityRank': watch_symbol.priority_rank if watch_symbol is not None else None,
                'tier': watch_symbol.tier if watch_symbol is not None else base_monitor_context.get('tier'),
                'riskFlags': watch_symbol.risk_flags if watch_symbol is not None else base_monitor_context.get('riskFlags') or [],
                'latestDecisionState': monitor_state.latest_decision_state,
                'latestDecisionReason': monitor_state.latest_decision_reason,
                'monitoringStatus': monitor_state.monitoring_status or (watch_symbol.monitoring_status if watch_symbol is not None else None) or base_monitor_context.get('monitoringStatus') or '',
                'lastExitAtUtc': base_monitor_context.get('lastExitAtUtc'),
                'lastExitReason': base_monitor_context.get('lastExitReason'),
                'reentryBlockedUntilUtc': base_monitor_context.get('reentryBlockedUntilUtc'),
                'cooldownActive': True,
                'details': latest_eval['details'],
            },
            'sizing': {
                'accountId': 'paper-crypto-ledger',
                'requestedQuantity': 0.0,
                'filledQuantity': float(exit_execution.get('filledQuantity') or 0.0),
                'requestedPrice': None,
                'avgFillPrice': self._maybe_float(exit_execution.get('filledPrice')),
                'displayPair': watch_symbol.symbol if watch_symbol is not None else symbol,
                'ohlcvPair': base_monitor_context.get('ohlcvPair'),
            },
            'timeframeAlignment': {
                'mode': 'single_timeframe_monitor',
                'configured': configured_timeframes,
                'confirmed': [],
                'items': timeframe_items,
                'note': 'The symbol is flat and in post-exit cooldown. Timeframes remain attached to the watchlist context.',
            },
            'exitPlan': {
                'template': (
                    frozen_policy.get('exitTemplate')
                    or (watch_symbol.exit_template if watch_symbol is not None else None)
                    or base_monitor_context.get('exitTemplate')
                ),
                'maxHoldHours': (
                    frozen_policy.get('maxHoldHours')
                    or (watch_symbol.max_hold_hours if watch_symbol is not None else None)
                    or base_monitor_context.get('maxHoldHours')
                ),
                'stopLoss': None,
                'profitTarget': None,
                'trailingStop': None,
                'expectedExitThresholds': {},
            },
            'latestEvaluation': latest_eval,
            'lifecycle': [],
            'rawContext': {
                'watchlist': {
                    'uploadId': watch_symbol.upload_id if watch_symbol is not None else None,
                    'scope': watch_symbol.scope if watch_symbol is not None else 'crypto_only',
                    'symbolAliases': sorted(aliases),
                },
                'monitorContext': base_monitor_context,
            },
        }

    @staticmethod
    def _find_crypto_position(symbol: str) -> dict[str, Any] | None:
        normalized = str(symbol or '').strip().upper()
        aliases = PositionInspectService._crypto_symbol_aliases(normalized)
        for row in crypto_ledger.get_positions():
            pair = str(row.get('pair') or '').strip().upper()
            if pair in aliases:
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
    def _find_crypto_intent(db: Session, aliases: set[str]) -> OrderIntent | None:
        query = (
            db.query(OrderIntent)
            .filter(OrderIntent.asset_class == 'crypto', OrderIntent.symbol.in_(sorted(aliases)))
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
    def _find_crypto_watch_symbol(db: Session, aliases: set[str]) -> WatchlistSymbol | None:
        base_candidates = sorted({alias for alias in aliases if '/' not in alias and not alias.endswith('USD') or alias == alias[:-3]})
        # keep explicit symbol aliases first
        all_candidates = sorted(aliases)
        query = (
            db.query(WatchlistSymbol)
            .filter(
                WatchlistSymbol.scope == 'crypto_only',
                WatchlistSymbol.asset_class == 'crypto',
                WatchlistSymbol.symbol.in_(all_candidates),
            )
            .order_by(WatchlistSymbol.updated_at.desc(), WatchlistSymbol.created_at.desc(), WatchlistSymbol.id.desc())
        )
        row = query.first()
        if row is not None:
            return row
        if base_candidates:
            return (
                db.query(WatchlistSymbol)
                .filter(
                    WatchlistSymbol.scope == 'crypto_only',
                    WatchlistSymbol.asset_class == 'crypto',
                    WatchlistSymbol.symbol.in_(base_candidates),
                )
                .order_by(WatchlistSymbol.updated_at.desc(), WatchlistSymbol.created_at.desc(), WatchlistSymbol.id.desc())
                .first()
            )
        return None

    @staticmethod
    def _find_crypto_monitor_state(db: Session, aliases: set[str], watch_symbol: WatchlistSymbol | None) -> WatchlistMonitorState | None:
        query = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.scope == 'crypto_only')
        filters = [WatchlistMonitorState.symbol.in_(sorted(aliases))]
        if watch_symbol is not None:
            filters.append(WatchlistMonitorState.watchlist_symbol_id == watch_symbol.id)
        row = query.filter(or_(*filters)).order_by(WatchlistMonitorState.updated_at.desc(), WatchlistMonitorState.id.desc()).first()
        return row

    def _find_live_crypto_watchlist_row(self, db: Session, *, aliases: set[str]) -> dict[str, Any] | None:
        try:
            snapshot = watchlist_service.get_monitoring_snapshot(db, scope='crypto_only', include_inactive=True)
        except Exception:
            return None

        for row in list(snapshot.get('rows') or []):
            row_aliases = self._crypto_symbol_aliases(str(row.get('symbol') or ''))
            if row_aliases.intersection(aliases):
                return row
        return None

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
    def _normalize_protection_mode_label(value: Any) -> str | None:
        raw = str(value or '').strip().upper()
        mapping = {
            'INITIAL_RISK': 'Initial risk',
            'BREAK_EVEN_PROMOTED': 'Break-even promoted',
            'STRUCTURE_TIGHTENED': 'Structure tightened',
            'EXIT_READY': 'Exit ready',
        }
        return mapping.get(raw) or (raw.replace('_', ' ').title() if raw else None)

    @staticmethod
    def _build_bias_explanation(
        *,
        bias: Any,
        monitoring_status: str | None,
        latest_evaluation: dict[str, Any] | None,
        timeframe_items: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        normalized_bias = str(bias or 'unknown').strip().title() or 'Unknown'
        state = str((latest_evaluation or {}).get('state') or '').strip().upper()
        reason = str((latest_evaluation or {}).get('reason') or '').strip() or None
        blocked = state in {'BLOCKED', 'REJECTED'} or str(monitoring_status or '').strip().upper() in {'BLOCKED', 'BIAS_CONFLICT'}
        items: list[dict[str, Any]] = []
        for item in list(timeframe_items or []):
            timeframe = str(item.get('timeframe') or '').strip()
            status = str(item.get('status') or '').strip().title() or 'Unknown'
            item_reason = str(item.get('reason') or '').strip() or None
            if timeframe:
                items.append({'timeframe': timeframe, 'status': status, 'reason': item_reason})
        if blocked:
            entry_permission = 'Blocked'
            unblock_condition = reason or 'Reclaim bullish structure across required timeframes.'
        else:
            entry_permission = 'Allowed'
            unblock_condition = 'No bias block is active right now.'
        return {
            'biasState': normalized_bias,
            'entryPermission': entry_permission,
            'unblockCondition': unblock_condition,
            'timeframes': items,
        }


    def _derive_exit_worker(
        self,
        *,
        asset_class: str,
        display_symbol: str,
        exit_plan: dict[str, Any],
        position_snapshot: dict[str, Any],
        latest_evaluation: dict[str, Any] | None,
        monitoring_status: str | None,
        lifecycle_state: str | None,
        cooldown_active: bool,
        broker_exit_pending: bool,
        managed_only: bool,
        lifecycle: list[dict[str, Any]],
    ) -> dict[str, Any]:
        details = dict((latest_evaluation or {}).get('details') or {})
        state = str((latest_evaluation or {}).get('state') or '').strip().upper()
        reason = str((latest_evaluation or {}).get('reason') or '').strip() or None
        template = str(exit_plan.get('template') or '').strip().lower()
        current_price = self._maybe_float(position_snapshot.get('currentPrice'))
        entry_price = self._maybe_float(position_snapshot.get('avgEntryPrice'))
        unrealized_pnl = self._maybe_float(position_snapshot.get('unrealizedPnl'))
        stop_loss = self._maybe_float(exit_plan.get('stopLoss'))
        profit_target = self._maybe_float(exit_plan.get('profitTarget'))
        trailing_stop = self._maybe_float(exit_plan.get('trailingStop'))
        peak_price = self._maybe_float(exit_plan.get('peakPrice'))
        protection_mode = str(details.get('protectionMode') or exit_plan.get('protectionMode') or 'INITIAL_RISK').strip().upper() or 'INITIAL_RISK'
        fee_adjusted_break_even = self._maybe_float(details.get('feeAdjustedBreakEven') or exit_plan.get('feeAdjustedBreakEven'))
        promoted_protective_floor = self._maybe_float(details.get('promotedProtectiveFloor') or exit_plan.get('promotedProtectiveFloor'))
        tp_touched_at_utc = details.get('tpTouchedAtUtc') or exit_plan.get('tpTouchedAtUtc')
        stronger_margin_reached = bool(details.get('strongerMarginReached') or exit_plan.get('strongerMarginReached'))
        last_confirmed_higher_low = self._maybe_float(details.get('lastConfirmedHigherLow') or exit_plan.get('lastConfirmedHigherLow'))
        hours_since_entry = self._maybe_float(exit_plan.get('hoursSinceEntry') or details.get('hoursSinceEntry') or details.get('hours_open'))
        max_hold_hours = self._maybe_float(exit_plan.get('maxHoldHours'))
        follow_through_failed = bool(details.get('followThroughFailed') or exit_plan.get('followThroughFailed'))

# --- DYNAMIC RECALCULATION OF RUNNER PROTECTION ---
        # Ensures UI instantly reflects promoted rails if price crosses targets
        # between background worker cycles, while strictly respecting already-saved DB state.
        from datetime import datetime, timezone
        dynamic_protection = watchlist_service._build_runner_protection_state(
            asset_class=asset_class,
            exit_template=template,
            observed_at=datetime.now(timezone.utc),
            entry_time=None,
            avg_entry_price=entry_price,
            current_price=current_price,
            profit_target=profit_target,
            trailing_stop=trailing_stop,
            peak_price=peak_price,
            follow_through_failed=follow_through_failed,
        )
        
        dyn_mode = dynamic_protection.get('protectionMode') or 'INITIAL_RISK'
        mode_rank = {'INITIAL_RISK': 0, 'BREAK_EVEN_PROMOTED': 1, 'STRUCTURE_TIGHTENED': 2, 'EXIT_READY': 3}
        
        # We only apply dynamic overrides if the real-time data warrants an UPGRADE to the protection tier
        if mode_rank.get(dyn_mode, 0) > mode_rank.get(protection_mode, 0):
            protection_mode = dyn_mode
            
            # Since we are upgrading, we adopt the dynamic floors if they are higher
            fee_adjusted_break_even = fee_adjusted_break_even if fee_adjusted_break_even is not None else dynamic_protection.get('feeAdjustedBreakEven')
            
            dyn_promoted_floor = dynamic_protection.get('promotedProtectiveFloor')
            if dyn_promoted_floor is not None:
                promoted_protective_floor = max(promoted_protective_floor or 0.0, dyn_promoted_floor) if promoted_protective_floor is not None else dyn_promoted_floor

            dyn_higher_low = dynamic_protection.get('lastConfirmedHigherLow')
            if dyn_higher_low is not None:
                last_confirmed_higher_low = max(last_confirmed_higher_low or 0.0, dyn_higher_low) if last_confirmed_higher_low is not None else dyn_higher_low

            tp_touched_at_utc = tp_touched_at_utc or dynamic_protection.get('tpTouchedAtUtc')
            stronger_margin_reached = stronger_margin_reached or dynamic_protection.get('strongerMarginReached')

        # Always backfill missing fields from dynamic calculation, even if mode wasn't upgraded
        elif mode_rank.get(dyn_mode, 0) == mode_rank.get(protection_mode, 0):
            if fee_adjusted_break_even is None:
                fee_adjusted_break_even = dynamic_protection.get('feeAdjustedBreakEven')
            if promoted_protective_floor is None:
                promoted_protective_floor = dynamic_protection.get('promotedProtectiveFloor')
            if last_confirmed_higher_low is None:
                last_confirmed_higher_low = dynamic_protection.get('lastConfirmedHigherLow')
            if tp_touched_at_utc is None:
                tp_touched_at_utc = dynamic_protection.get('tpTouchedAtUtc')
            if not stronger_margin_reached:
                stronger_margin_reached = dynamic_protection.get('strongerMarginReached')

        # Mutate the passed exit_plan so the outer payload also reflects the live state
        exit_plan['protectionMode'] = protection_mode
        exit_plan['feeAdjustedBreakEven'] = fee_adjusted_break_even
        exit_plan['promotedProtectiveFloor'] = promoted_protective_floor
        exit_plan['tpTouchedAtUtc'] = tp_touched_at_utc
        exit_plan['strongerMarginReached'] = stronger_margin_reached
        exit_plan['lastConfirmedHigherLow'] = last_confirmed_higher_low
        # --- END DYNAMIC RECALCULATION ---

        target_reached = bool(details.get('profitTargetReached')) or bool(tp_touched_at_utc) or (current_price is not None and profit_target is not None and current_price >= profit_target)
        effective_protective_floor = trailing_stop
        candidate_floors = [value for value in (trailing_stop, promoted_protective_floor, fee_adjusted_break_even) if value is not None]
        if candidate_floors:
            effective_protective_floor = max(candidate_floors)
            
        trail_breached = bool(details.get('trailingStopBreached')) or (current_price is not None and effective_protective_floor is not None and current_price <= effective_protective_floor)
        stop_breached = bool(details.get('stopLossBreached')) or (current_price is not None and stop_loss is not None and current_price <= stop_loss)
        impulse_trail_armed = bool(details.get('impulseTrailArmed'))
        scale_out_taken = bool(details.get('scaleOutAlreadyTaken'))
        scale_out_ready = bool(details.get('scaleOutReady')) or (template == 'scale_out_then_trail' and target_reached and not scale_out_taken)
        promoted_floor = promoted_protective_floor
        active_protection_rail = promoted_floor or effective_protective_floor or trailing_stop
        
        execution_status = 'No order pending'
        broker_status = 'Connected'
        logic_state = 'NO_EXIT_SIGNAL'
        logic_summary = 'No exit signal is active right now.'
        why_not = 'No exit condition is currently active.'
        next_trigger = 'Continue monitoring'
        next_trigger_level = None
        next_trigger_distance = None
        current_phase = 'Monitoring'
        phase_transition = 'Will continue monitoring until an exit rule arms.'
        structure_health = 'Neutral'
        signal_conflict = 'None'
        trail_status = 'Configured' if trailing_stop is not None else 'Unavailable'
        volatility_regime = 'Normal'
        risk_state = 'Stable'
        risk_compression = 'Mixed'
        exit_readiness_score = 0.15
        exit_likelihood = 'Low probability of exit in the next review cycle.'
        strategy_biases = []
        exit_sensitivity = 'Balanced'
        active_trigger_label = None
        if cooldown_active or not position_snapshot.get('isOpen'):
            logic_state = 'COOLDOWN_ACTIVE'
            logic_summary = 'Cooldown active'
            why_not = 'The position is flat and the cooldown window is blocking immediate re-entry.'
            next_trigger = 'Cooldown expiry'
            next_trigger_level = details.get('reentryBlockedUntilUtc')
            current_phase = 'Cooldown'
            phase_transition = 'Will transition back to fresh-entry monitoring after the cooldown expires.'
            structure_health = 'N/A'
            trail_status = 'Inactive'
            risk_state = 'Flat'
            risk_compression = 'Not applicable'
            exit_readiness_score = 0.0
            exit_likelihood = 'No exit is pending because the position is already flat.'
        elif broker_exit_pending or state == 'EXIT_PENDING':
            logic_state = 'EXIT_PENDING'
            logic_summary = 'Exit pending'
            why_not = 'An exit has already been prepared or submitted, so the worker is waiting on execution rather than searching for a new signal.'
            next_trigger = 'Broker acknowledgement / fill'
            current_phase = 'Exit Pending'
            phase_transition = 'Will transition to closed once the broker confirms the exit fill.'
            structure_health = 'Exit ready'
            trail_status = 'Locked'
            risk_state = 'De-risking'
            risk_compression = 'Active'
            execution_status = 'Exit pending'
            exit_readiness_score = 1.0
            exit_likelihood = 'Exit is already in motion.'
        elif stop_breached:
            logic_state = 'STOP_LOSS_BREACHED_EXIT_READY'
            logic_summary = 'Stop loss breached'
            why_not = 'Price has crossed the stop-loss rail and the position is waiting for the exit path to finish.'
            next_trigger = 'Exit submission'
            next_trigger_level = stop_loss
            current_phase = 'Protective Exit'
            phase_transition = 'Will transition to exit pending once the protective order is created.'
            structure_health = 'Failed'
            trail_status = 'Overridden'
            risk_state = 'High risk'
            risk_compression = 'Failing'
            exit_readiness_score = 0.98
            exit_likelihood = 'Exit is highly likely on the next worker cycle.'
            active_trigger_label = 'Stop loss'
        elif trail_breached:
            logic_state = 'TRAIL_BREACH_EXIT_READY'
            logic_summary = 'Trailing stop breached'
            why_not = 'Price is below the active trailing rail, so the worker is preparing to close the position.'
            next_trigger = 'Exit submission'
            next_trigger_level = active_protection_rail or trailing_stop
            current_phase = 'Trail Exit'
            phase_transition = 'Will transition to exit pending once the trailing-stop exit is submitted.'
            structure_health = 'Failed'
            trail_status = 'Breached'
            risk_state = 'High risk'
            risk_compression = 'Failing'
            exit_readiness_score = 0.95
            exit_likelihood = 'Exit is highly likely on the next worker cycle.'
            active_trigger_label = 'Trailing stop'
        elif template == 'first_failed_follow_through':
            strategy_biases = ['profit maximization bias', 'delayed exit bias', 'structure confirmation']
            exit_sensitivity = 'Balanced'
            if target_reached and not follow_through_failed:
                logic_state = 'TP_HIT_AWAITING_FOLLOW_THROUGH_FAILURE'
                logic_summary = 'TP hit awaiting weakness'
                why_not = 'Profit target was touched, break-even protection is promoted, and the runner remains open until follow-through fails or the promoted floor breaks.'
                next_trigger = 'Follow-through failure or trail breach'
                next_trigger_level = active_protection_rail or profit_target
                current_phase = 'Break-even promoted' if protection_mode == 'BREAK_EVEN_PROMOTED' else 'Structure tightened' if protection_mode == 'STRUCTURE_TIGHTENED' else 'TP hit awaiting weakness'
                phase_transition = 'Will transition to exit ready when follow-through fails or the promoted protection rail is breached.'
                structure_health = 'Strong'
                trail_status = 'Break-even promoted' if protection_mode == 'BREAK_EVEN_PROMOTED' else 'Structure tightened' if protection_mode == 'STRUCTURE_TIGHTENED' else 'Monitoring follow-through'
                risk_state = 'Protected' if protection_mode in {'BREAK_EVEN_PROMOTED', 'STRUCTURE_TIGHTENED'} else 'Improving'
                risk_compression = 'Active'
                exit_readiness_score = 0.62
                exit_likelihood = 'Moderate probability of exit if momentum fades in the next review cycle.'
                active_trigger_label = 'Break-even floor' if protection_mode == 'BREAK_EVEN_PROMOTED' else 'Promoted protective floor' if protection_mode == 'STRUCTURE_TIGHTENED' else 'Follow-through failure'
            elif target_reached and follow_through_failed:
                logic_state = 'FOLLOW_THROUGH_FAILED_EXIT_READY'
                logic_summary = 'Follow-through failed'
                why_not = 'The profit target was hit, continuation failed, and the runner is now queued for immediate exit.'
                next_trigger = 'Immediate exit submission'
                next_trigger_level = active_protection_rail or profit_target
                current_phase = 'Exit Ready'
                phase_transition = 'Will transition to exit pending once the failed follow-through exit is submitted.'
                structure_health = 'Weakening'
                trail_status = 'Armed'
                risk_state = 'At risk'
                risk_compression = 'Slowing'
                exit_readiness_score = 0.88
                exit_likelihood = 'High probability of exit soon.'
                active_trigger_label = 'Failed follow-through'
            else:
                logic_state = 'AWAITING_PROFIT_TARGET'
                logic_summary = 'Awaiting profit target'
                why_not = 'The runner is still waiting for the first profit-target touch that promotes protection.'
                next_trigger = 'Profit target touch'
                next_trigger_level = profit_target
                current_phase = 'Awaiting Profit Target'
                phase_transition = 'Will transition to runner supervision after the target is reached.'
                structure_health = 'Healthy'
                trail_status = 'Configured'
                risk_state = 'Stable'
                risk_compression = 'Mixed'
                exit_readiness_score = 0.28
                exit_likelihood = 'Low probability of exit before the target is reached.'
                active_trigger_label = 'Profit target'
        elif template == 'scale_out_then_trail':
            strategy_biases = ['partial-profit bias', 'runner management', 'trail protection']
            exit_sensitivity = 'Balanced'
            if scale_out_taken:
                logic_state = 'RUNNER_ACTIVE_TRAIL_PROTECTING'
                logic_summary = 'Runner active trail protecting'
                why_not = 'Partial profits have already been secured and the remaining size is being managed by the trailing rail.'
                next_trigger = 'Trailing stop breach'
                next_trigger_level = trailing_stop
                current_phase = 'Runner'
                phase_transition = 'Will transition to exit ready when the trailing stop is breached.'
                structure_health = 'Healthy'
                trail_status = 'Active'
                risk_state = 'Improving'
                risk_compression = 'Active'
                exit_readiness_score = 0.48
                exit_likelihood = 'Moderate probability of exit if the runner gives back momentum.'
                active_trigger_label = 'Trailing stop'
            elif scale_out_ready:
                logic_state = 'SCALE_OUT_READY'
                logic_summary = 'Scale-out ready'
                why_not = 'The profit target has been reached and the worker is ready to harvest the first partial profit.'
                next_trigger = 'Scale-out submission'
                next_trigger_level = profit_target
                current_phase = 'Scale Out'
                phase_transition = 'Will transition to runner mode after the scale-out fill is recorded.'
                structure_health = 'Healthy'
                trail_status = 'Pending runner activation'
                risk_state = 'Improving'
                risk_compression = 'Active'
                exit_readiness_score = 0.78
                exit_likelihood = 'High probability of scale-out soon.'
                active_trigger_label = 'Scale-out trigger'
            else:
                logic_state = 'AWAITING_SCALE_OUT_TRIGGER'
                logic_summary = 'Awaiting scale-out trigger'
                why_not = 'Price has not yet reached the first profit-taking rail required to begin scaling out.'
                next_trigger = 'Scale-out threshold'
                next_trigger_level = profit_target
                current_phase = 'Pre-Target'
                phase_transition = 'Will transition to scale-out once price reaches the profit target.'
                structure_health = 'Healthy'
                trail_status = 'Configured'
                risk_state = 'Stable'
                risk_compression = 'Mixed'
                exit_readiness_score = 0.32
                exit_likelihood = 'Low to moderate probability of exit before the first target is reached.'
                active_trigger_label = 'Profit target'
        elif template == 'trail_after_impulse':
            strategy_biases = ['let-run bias', 'impulse confirmation', 'trail protection']
            exit_sensitivity = 'Delayed'
            if protection_mode == 'BREAK_EVEN_PROMOTED' and not impulse_trail_armed:
                logic_state = 'BREAK_EVEN_PROMOTED'
                logic_summary = 'Awaiting impulse confirmation'
                why_not = 'Profit target was touched and protection is promoted to fee-adjusted break-even while the worker waits for stronger extension.'
                next_trigger = 'Stronger extension or trail breach'
                next_trigger_level = active_protection_rail or profit_target
                current_phase = 'Break-even promoted'
                phase_transition = 'Will transition to structure tightened on stronger extension or to exit ready if the promoted floor breaks.'
                structure_health = 'Healthy'
                trail_status = 'Break-even promoted'
                risk_state = 'Protected'
                risk_compression = 'Active'
                exit_readiness_score = 0.36
                exit_likelihood = 'Low to moderate probability of exit unless the runner weakens.'
                active_trigger_label = 'Break-even floor'
            elif impulse_trail_armed or protection_mode == 'STRUCTURE_TIGHTENED':
                logic_state = 'TRAIL_ACTIVE_STRUCTURE_HEALTHY'
                logic_summary = 'Trail active'
                why_not = 'The impulse threshold has been reached and the active protection rail is now managing the runner while structure remains intact.'
                next_trigger = 'Follow-through failure or trail breach'
                next_trigger_level = active_protection_rail
                current_phase = 'Structure tightened' if protection_mode == 'STRUCTURE_TIGHTENED' else 'Trail Active'
                phase_transition = 'Will transition to exit ready when price breaches the active protection rail.'
                structure_health = 'Healthy'
                trail_status = 'Structure tightened' if protection_mode == 'STRUCTURE_TIGHTENED' else 'Active'
                risk_state = 'Protected' if protection_mode == 'STRUCTURE_TIGHTENED' else 'Improving'
                risk_compression = 'Tightened' if protection_mode == 'STRUCTURE_TIGHTENED' else 'Active'
                exit_readiness_score = 0.45
                exit_likelihood = 'Moderate probability of exit if the runner pulls back.'
                active_trigger_label = 'Promoted protective floor' if protection_mode == 'STRUCTURE_TIGHTENED' else 'Trailing stop'
            else:
                logic_state = 'AWAITING_IMPULSE_CONFIRMATION'
                logic_summary = 'Awaiting impulse confirmation'
                why_not = 'Price has not moved far enough to activate the impulse-based protection logic yet.'
                next_trigger = 'Impulse confirmation'
                next_trigger_level = profit_target
                current_phase = 'Awaiting Impulse Confirmation'
                phase_transition = 'Will transition to active trailing once the impulse threshold is reached.'
                structure_health = 'Healthy'
                trail_status = 'Configured'
                risk_state = 'Stable'
                risk_compression = 'Mixed'
                exit_readiness_score = 0.24
                exit_likelihood = 'Low probability of exit before the impulse threshold is reached.'
                active_trigger_label = 'Impulse threshold'
        elif template == 'time_stop_with_structure_check':
            strategy_biases = ['time-based governance', 'structure check', 'risk control']
            exit_sensitivity = 'Conservative'
            hours_until_expiry = self._maybe_float(exit_plan.get('hoursUntilExpiry') or details.get('hoursUntilExpiry'))
            if hours_until_expiry is not None and hours_until_expiry <= 0:
                logic_state = 'TIME_STOP_DUE_AWAITING_STRUCTURE_CHECK'
                logic_summary = 'Time stop due'
                why_not = 'The maximum hold window has expired and the worker is waiting on the structure check result.'
                next_trigger = 'Structure check verdict'
                current_phase = 'Time Stop Review'
                phase_transition = 'Will transition either to an extension or to exit ready after the structure check.'
                structure_health = 'Review'
                risk_state = 'At risk'
                risk_compression = 'Slowing'
                exit_readiness_score = 0.76
                exit_likelihood = 'Moderate to high probability of exit soon.'
            else:
                logic_state = 'TIME_STOP_MONITORING'
                logic_summary = 'Time stop monitoring'
                why_not = 'The trade is still inside its allowed hold window, so the worker is monitoring for either a price-based exit or the later time-stop review.'
                next_trigger = 'Time-stop review'
                next_trigger_level = details.get('positionExpiresAtUtc') or exit_plan.get('positionExpiresAtUtc')
                current_phase = 'Hold Window'
                phase_transition = 'Will transition to time-stop review when the hold window expires.'
                structure_health = 'Healthy'
                risk_state = 'Stable'
                risk_compression = 'Mixed'
                exit_readiness_score = 0.22
                exit_likelihood = 'Low probability of exit unless the trade nears expiry or breaches protection.'
        if next_trigger_level is not None and current_price is not None and isinstance(next_trigger_level, (int, float)):
            next_trigger_distance = float(next_trigger_level) - current_price
        elif next_trigger_level is not None and isinstance(next_trigger_level, str):
            next_trigger_distance = None
        distance_from_stop = None
        distance_from_trail = None
        distance_from_target = None
        if current_price is not None and stop_loss is not None and current_price:
            distance_from_stop = ((current_price - stop_loss) / current_price) * 100.0
        if current_price is not None and trailing_stop is not None and current_price:
            distance_from_trail = ((current_price - trailing_stop) / current_price) * 100.0
        if current_price is not None and profit_target is not None and current_price:
            distance_from_target = ((profit_target - current_price) / current_price) * 100.0
        if managed_only and 'managed-only' not in why_not.lower():
            why_not = f'{why_not} Managed-only: symbol removed from fresh-entry eligibility while exit supervision remains active until the position closes.'
        if managed_only:
            execution_status = 'Managed-only supervision'
        position_maturity = 'Unknown'
        hours_since_entry = self._derive_hours_since_entry(position_snapshot.get('entryTimeUtc'))
        if hours_since_entry is not None and max_hold_hours:
            pct = hours_since_entry / max_hold_hours if max_hold_hours else 0.0
            if pct < 0.33:
                position_maturity = 'Early'
            elif pct < 0.8:
                position_maturity = 'Normal'
            else:
                position_maturity = 'Extended'
        current_progress_r = None
        expected_exit_range = None
        if entry_price is not None and stop_loss is not None and current_price is not None:
            risk_per_unit = entry_price - stop_loss
            if risk_per_unit > 0:
                current_progress_r = (current_price - entry_price) / risk_per_unit
                expected_exit_range = {'from': 1.0, 'to': 2.5} if template in {'scale_out_then_trail', 'first_failed_follow_through'} else {'from': 0.8, 'to': 2.0}
        state_history = self._build_exit_state_history(lifecycle, logic_summary)
        return {
            'worker': 'Exit Worker',
            'logicState': logic_state,
            'logicSummary': logic_summary,
            'whyNotExitingYet': why_not,
            'nextExitTrigger': next_trigger,
            'nextTriggerLevel': next_trigger_level,
            'nextTriggerDistance': next_trigger_distance,
            'nextReviewAtUtc': (latest_evaluation or {}).get('nextEvaluationAtUtc') or details.get('nextEvaluationAtUtc'),
            'currentPhase': current_phase,
            'phaseTransitionCondition': phase_transition,
            'monitoringStatus': monitoring_status,
            'lifecycleState': lifecycle_state,
            'cooldownActive': cooldown_active,
            'managedOnly': managed_only,
            'managedOnlyExplanation': 'Managed-only: symbol removed from fresh-entry eligibility while exit supervision remains active until the position closes.' if managed_only else None,
            'activeTriggerLabel': active_trigger_label,
            'structureHealth': structure_health,
            'signalConflict': signal_conflict,
            'trailStatus': trail_status,
            'volatilityRegime': volatility_regime,
            'riskState': risk_state,
            'riskCompression': risk_compression,
            'distanceFromStopPct': distance_from_stop,
            'distanceFromTrailPct': distance_from_trail,
            'distanceFromTargetPct': distance_from_target,
            'unrealizedProfitExposed': unrealized_pnl if unrealized_pnl and unrealized_pnl > 0 else 0.0,
            'exitReadinessScore': round(exit_readiness_score, 2),
            'exitLikelihood': exit_likelihood,
            'expectedExitRangeR': expected_exit_range,
            'currentProgressR': current_progress_r,
            'positionMaturity': position_maturity,
            'strategyBiases': strategy_biases,
            'exitSensitivity': exit_sensitivity,
            'protectionMode': protection_mode,
            'feeAdjustedBreakEven': fee_adjusted_break_even,
            'promotedProtectiveFloor': promoted_floor,
            'activeProtectionRail': active_protection_rail,
            'baseTrailingStop': trailing_stop,
            'tpTouchedAtUtc': tp_touched_at_utc,
            'strongerMarginReached': stronger_margin_reached,
            'lastConfirmedHigherLow': last_confirmed_higher_low,
            'followThroughFailed': follow_through_failed,
            'executionStatus': execution_status,
            'brokerStatus': broker_status,
            'stateHistory': state_history,
            'evaluatedAtUtc': (latest_evaluation or {}).get('evaluatedAtUtc'),
        }

    def _build_exit_state_history(self, lifecycle: list[dict[str, Any]], logic_summary: str) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for event in (lifecycle or [])[-3:]:
            history.append({
                'time': event.get('eventTime'),
                'label': str(event.get('eventType') or '').replace('_', ' ').title(),
                'detail': event.get('message'),
            })
        if not history:
            history.append({'time': None, 'label': logic_summary, 'detail': 'Latest exit-worker verdict.'})
        return history

    @staticmethod
    def _derive_hours_since_entry(entry_time_value: Any) -> float | None:
        if not entry_time_value:
            return None
        try:
            from datetime import datetime, timezone
            raw = str(entry_time_value)
            if raw.endswith('Z'):
                raw = raw[:-1] + '+00:00'
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600.0, 0.0)
        except Exception:
            return None

    @staticmethod
    def _maybe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _crypto_symbol_aliases(symbol: str, current_position: dict[str, Any] | None = None) -> set[str]:
        aliases: set[str] = set()

        def add(raw: Any) -> None:
            text = str(raw or '').strip().upper()
            if not text:
                return
            aliases.add(text)
            compact = text.replace('/', '')
            aliases.add(compact)
            if '/' in text:
                base, quote = text.split('/', 1)
                aliases.add(base)
                aliases.add(f'{base}{quote}')
            elif text.endswith('USD') and len(text) > 3:
                base = text[:-3]
                aliases.add(base)
                aliases.add(f'{base}/USD')

        add(symbol)
        if current_position is not None:
            add(current_position.get('pair'))
            add(current_position.get('ohlcvPair'))
        resolved = kraken_service.resolve_pair(str(symbol or '').strip())
        if resolved is not None:
            add(resolved.display_pair)
            add(resolved.rest_pair)
            add(resolved.altname)
            add(resolved.ws_pair)
            add(resolved.pair_key)
        aliases.discard('')
        return aliases


position_inspect_service = PositionInspectService()