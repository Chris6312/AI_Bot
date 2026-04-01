from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.order_event import OrderEvent
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.trade import Trade
from app.services.discord_notifications import discord_notifications


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class ExecutionLifecycleService:
    def create_order_intent(
        self,
        db: Session,
        *,
        account_id: str,
        asset_class: str,
        symbol: str,
        side: str,
        requested_quantity: float,
        requested_price: float | None,
        execution_source: str,
        context: dict[str, Any] | None = None,
    ) -> OrderIntent:
        intent = OrderIntent(
            intent_id=f"intent_{uuid.uuid4().hex[:24]}",
            account_id=account_id,
            asset_class=asset_class,
            symbol=symbol,
            side=side.upper(),
            requested_quantity=float(requested_quantity),
            requested_price=requested_price,
            filled_quantity=0.0,
            status='READY',
            execution_source=execution_source,
            context_json=context or {},
        )
        db.add(intent)
        db.flush()
        self.record_event(
            db,
            intent,
            event_type='INTENT_CREATED',
            status='READY',
            message=f"Prepared {intent.side} intent for {intent.symbol}",
            payload={'requestedQuantity': intent.requested_quantity, 'requestedPrice': intent.requested_price},
        )
        db.commit()
        db.refresh(intent)
        return intent

    def create_exit_intent(
        self,
        db: Session,
        *,
        account_id: str,
        asset_class: str,
        symbol: str,
        requested_quantity: float,
        requested_price: float | None,
        execution_source: str,
        position_id: int | None,
        trade_id: int | None,
        linked_intent_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> OrderIntent:
        merged_context = dict(context or {})
        merged_context.update(
            {
                'intentRole': 'EXIT',
                'linkedIntentId': linked_intent_id,
                'linkedPositionId': position_id,
                'linkedTradeId': trade_id,
            }
        )
        intent = self.create_order_intent(
            db,
            account_id=account_id,
            asset_class=asset_class,
            symbol=symbol,
            side='SELL',
            requested_quantity=requested_quantity,
            requested_price=requested_price,
            execution_source=execution_source,
            context=merged_context,
        )
        intent.position_id = position_id
        intent.trade_id = trade_id
        db.commit()
        db.refresh(intent)
        self.record_event(
            db,
            intent,
            event_type='EXIT_INTENT_LINKED',
            status=intent.status,
            message=f"Linked exit intent for {intent.symbol}",
            payload={
                'linkedIntentId': linked_intent_id,
                'positionId': position_id,
                'tradeId': trade_id,
            },
        )
        db.commit()
        db.refresh(intent)
        return intent


    def mark_rejected_by_gate(
        self,
        db: Session,
        intent: OrderIntent,
        *,
        reason: str,
        gate_payload: dict[str, Any] | None = None,
    ) -> OrderIntent:
        intent.status = 'REJECTED'
        intent.rejection_reason = reason
        self.record_event(
            db,
            intent,
            event_type='PRE_TRADE_GATE_REJECTED',
            status='REJECTED',
            message=f'Pre-trade gate rejected {intent.symbol}: {reason}',
            payload=gate_payload or {},
        )
        db.commit()
        db.refresh(intent)
        return intent

    def record_event(
        self,
        db: Session,
        intent: OrderIntent,
        *,
        event_type: str,
        status: str,
        message: str,
        payload: dict[str, Any] | None = None,
        event_time: datetime | None = None,
    ) -> OrderEvent:
        event = OrderEvent(
            intent_id=intent.intent_id,
            event_type=event_type,
            status=status,
            message=message,
            payload_json=payload or {},
            event_time=event_time or _utcnow(),
        )
        db.add(event)
        db.flush()
        return event

    def record_submission(self, db: Session, intent: OrderIntent, order_snapshot: dict[str, Any]) -> OrderIntent:
        normalized = self.normalize_order_snapshot(order_snapshot)
        intent.status = 'SUBMITTED'
        intent.submitted_order_id = normalized['order_id'] or intent.submitted_order_id
        intent.submitted_at = normalized['updated_at'] or _utcnow()
        self.record_event(
            db,
            intent,
            event_type='ORDER_SUBMITTED',
            status=intent.status,
            message=f"Broker accepted order for {intent.symbol}",
            payload=normalized['raw'],
            event_time=intent.submitted_at,
        )
        db.commit()
        db.refresh(intent)
        return intent

    def refresh_from_order_snapshot(
        self,
        db: Session,
        intent: OrderIntent,
        order_snapshot: dict[str, Any],
    ) -> OrderIntent:
        normalized = self.normalize_order_snapshot(order_snapshot)
        status = self._resolve_status(intent, normalized)
        filled_quantity = normalized['filled_quantity']
        avg_fill_price = normalized['avg_fill_price'] or intent.avg_fill_price or intent.requested_price
        event_time = normalized['updated_at'] or _utcnow()

        intent.status = status
        intent.submitted_order_id = normalized['order_id'] or intent.submitted_order_id
        intent.filled_quantity = filled_quantity
        intent.avg_fill_price = avg_fill_price
        intent.rejection_reason = normalized['rejection_reason']

        if intent.submitted_at is None and normalized['order_id']:
            intent.submitted_at = event_time
        if filled_quantity > 0:
            if intent.first_fill_at is None:
                intent.first_fill_at = event_time
            intent.last_fill_at = event_time

        self.record_event(
            db,
            intent,
            event_type='ORDER_STATUS_UPDATED',
            status=status,
            message=self._status_message(intent, status, normalized),
            payload=normalized['raw'],
            event_time=event_time,
        )
        db.commit()
        db.refresh(intent)
        return intent

    def materialize_stock_fill(
        self,
        db: Session,
        intent: OrderIntent,
        *,
        strategy: str,
        stop_loss: float,
        profit_target: float,
        trailing_stop: float | None,
        current_price: float,
    ) -> dict[str, Any] | None:
        filled_shares = int(round(float(intent.filled_quantity or 0.0)))
        if filled_shares <= 0:
            return None

        if intent.position_id and intent.trade_id:
            return {
                'position_id': intent.position_id,
                'trade_id': intent.trade_id,
                'filled_shares': filled_shares,
                'avg_fill_price': float(intent.avg_fill_price or current_price or 0.0),
            }

        entry_price = float(intent.avg_fill_price or current_price or intent.requested_price or 0.0)
        entry_time = intent.last_fill_at or intent.first_fill_at or intent.submitted_at or _utcnow()

        position = Position(
            account_id=intent.account_id,
            ticker=intent.symbol,
            shares=filled_shares,
            avg_entry_price=entry_price,
            current_price=float(current_price or entry_price),
            strategy=strategy,
            entry_time=entry_time,
            entry_reasoning={
                'intentId': intent.intent_id,
                'executionSource': intent.execution_source,
                'requestedQuantity': intent.requested_quantity,
                'filledQuantity': intent.filled_quantity,
            },
            stop_loss=float(stop_loss),
            profit_target=float(profit_target),
            peak_price=entry_price,
            trailing_stop=float(trailing_stop) if trailing_stop is not None else None,
            is_open=True,
            execution_id=intent.intent_id,
        )
        db.add(position)
        db.flush()

        trade = Trade(
            trade_id=f"trade_{intent.intent_id}",
            account_id=intent.account_id,
            ticker=intent.symbol,
            direction='LONG',
            strategy=strategy,
            entry_time=entry_time,
            entry_price=entry_price,
            shares=filled_shares,
            entry_cost=entry_price * filled_shares,
            entry_reasoning={
                'intentId': intent.intent_id,
                'executionSource': intent.execution_source,
                'requestedQuantity': intent.requested_quantity,
                'filledQuantity': intent.filled_quantity,
                'status': intent.status,
            },
            execution_id=intent.intent_id,
            entry_order_id=intent.submitted_order_id,
        )
        db.add(trade)
        db.flush()

        intent.position_id = position.id
        intent.trade_id = trade.id
        self.record_event(
            db,
            intent,
            event_type='POSITION_OPENED',
            status=intent.status,
            message=f"Opened position for {intent.symbol} using confirmed fill quantity {filled_shares}",
            payload={
                'positionId': position.id,
                'tradeId': trade.id,
                'filledShares': filled_shares,
                'avgFillPrice': entry_price,
            },
            event_time=entry_time,
        )
        db.commit()
        db.refresh(intent)
        discord_notifications.send_trade_alert(
            asset_class='stock',
            side='BUY',
            symbol=intent.symbol,
            quantity=filled_shares,
            price=entry_price,
            execution_source=intent.execution_source,
            account_id=intent.account_id,
            extra={
                'mode': (intent.context_json or {}).get('mode'),
                'reason': (intent.context_json or {}).get('setupTemplate') or (intent.context_json or {}).get('strategy'),
            },
        )
        return {
            'position_id': position.id,
            'trade_id': trade.id,
            'filled_shares': filled_shares,
            'avg_fill_price': entry_price,
        }

    def materialize_stock_exit(
        self,
        db: Session,
        intent: OrderIntent,
        *,
        current_price: float | None,
        exit_trigger: str,
    ) -> dict[str, Any] | None:
        filled_shares = int(round(float(intent.filled_quantity or 0.0)))
        if filled_shares <= 0:
            return None

        position = self._resolve_position(db, intent)
        trade = self._resolve_trade(db, intent)
        if position is None or trade is None:
            self.record_event(
                db,
                intent,
                event_type='EXIT_RECONCILE_SKIPPED',
                status=intent.status,
                message=f"Could not reconcile exit for {intent.symbol} because no linked position/trade was found",
                payload={'positionId': intent.position_id, 'tradeId': intent.trade_id},
            )
            db.commit()
            db.refresh(intent)
            return None

        open_shares = int(position.shares or 0)
        if open_shares <= 0 or not position.is_open:
            self.record_event(
                db,
                intent,
                event_type='EXIT_RECONCILE_SKIPPED',
                status=intent.status,
                message=f"Position for {intent.symbol} is already flat",
                payload={'positionId': position.id, 'tradeId': trade.id},
            )
            db.commit()
            db.refresh(intent)
            return None

        applied_shares = min(filled_shares, open_shares)
        if applied_shares <= 0:
            return None

        exit_price = float(
            intent.avg_fill_price
            or current_price
            or position.current_price
            or position.avg_entry_price
            or intent.requested_price
            or 0.0
        )
        exit_time = intent.last_fill_at or intent.first_fill_at or intent.submitted_at or _utcnow()
        remaining_shares = max(open_shares - applied_shares, 0)

        position.current_price = exit_price
        position.shares = remaining_shares
        if remaining_shares <= 0:
            position.is_open = False
            position.unrealized_pnl = 0.0
            position.unrealized_pnl_pct = 0.0
        else:
            unrealized = (exit_price - float(position.avg_entry_price or 0.0)) * remaining_shares
            position.unrealized_pnl = unrealized
            position.unrealized_pnl_pct = (
                (unrealized / (float(position.avg_entry_price or 0.0) * remaining_shares) * 100.0)
                if position.avg_entry_price and remaining_shares > 0
                else 0.0
            )

        partial_history = self._build_partial_exit_history(trade, intent, applied_shares, exit_price, exit_time, remaining_shares)
        trade.exit_reasoning = partial_history

        if remaining_shares <= 0:
            entry_cost_closed = float(trade.entry_price or 0.0) * applied_shares
            exit_proceeds = exit_price * applied_shares
            gross_pnl = exit_proceeds - entry_cost_closed
            trade.exit_time = exit_time
            trade.exit_price = exit_price
            trade.exit_proceeds = exit_proceeds
            trade.exit_trigger = exit_trigger
            trade.exit_order_id = intent.submitted_order_id
            trade.gross_pnl = gross_pnl
            trade.net_pnl = gross_pnl
            trade.return_pct = (gross_pnl / entry_cost_closed * 100.0) if entry_cost_closed else 0.0
            trade.duration_minutes = self._duration_minutes(trade.entry_time, exit_time)
            intent.status = 'CLOSED'
            event_type = 'POSITION_CLOSED'
            message = f"Closed {intent.symbol} using confirmed exit quantity {applied_shares}"
        else:
            event_type = 'POSITION_REDUCED'
            message = (
                f"Reduced {intent.symbol} by confirmed exit quantity {applied_shares}; "
                f"{remaining_shares} shares remain open"
            )

        self.record_event(
            db,
            intent,
            event_type=event_type,
            status=intent.status,
            message=message,
            payload={
                'positionId': position.id,
                'tradeId': trade.id,
                'closedShares': applied_shares,
                'remainingShares': remaining_shares,
                'exitPrice': exit_price,
                'exitTrigger': exit_trigger,
            },
            event_time=exit_time,
        )
        db.commit()
        db.refresh(intent)
        discord_notifications.send_trade_alert(
            asset_class='stock',
            side='SELL',
            symbol=intent.symbol,
            quantity=applied_shares,
            price=exit_price,
            execution_source=intent.execution_source,
            account_id=intent.account_id,
            status='FILLED',
            extra={
                'mode': (intent.context_json or {}).get('mode'),
                'trigger': exit_trigger,
                'remainingShares': remaining_shares,
                'pnl': trade.gross_pnl if remaining_shares <= 0 else None,
            },
        )
        return {
            'position_id': position.id,
            'trade_id': trade.id,
            'closed_shares': applied_shares,
            'remaining_shares': remaining_shares,
            'exit_price': exit_price,
            'status': intent.status,
        }

    def serialize_intent(self, intent: OrderIntent, *, db: Session | None = None) -> dict[str, Any]:
        events: list[OrderEvent] = []
        if db is not None:
            events = (
                db.query(OrderEvent)
                .filter(OrderEvent.intent_id == intent.intent_id)
                .order_by(OrderEvent.event_time.asc(), OrderEvent.id.asc())
                .all()
            )
        return {
            'intentId': intent.intent_id,
            'accountId': intent.account_id,
            'assetClass': intent.asset_class,
            'symbol': intent.symbol,
            'side': intent.side,
            'requestedQuantity': float(intent.requested_quantity or 0.0),
            'requestedPrice': float(intent.requested_price or 0.0) if intent.requested_price is not None else None,
            'filledQuantity': float(intent.filled_quantity or 0.0),
            'avgFillPrice': float(intent.avg_fill_price or 0.0) if intent.avg_fill_price is not None else None,
            'status': intent.status,
            'executionSource': intent.execution_source,
            'submittedOrderId': intent.submitted_order_id,
            'positionId': intent.position_id,
            'tradeId': intent.trade_id,
            'rejectionReason': intent.rejection_reason,
            'submittedAt': intent.submitted_at.isoformat() if intent.submitted_at else None,
            'firstFillAt': intent.first_fill_at.isoformat() if intent.first_fill_at else None,
            'lastFillAt': intent.last_fill_at.isoformat() if intent.last_fill_at else None,
            'context': intent.context_json or {},
            'events': [
                {
                    'eventType': event.event_type,
                    'status': event.status,
                    'message': event.message,
                    'eventTime': event.event_time.isoformat() if event.event_time else None,
                    'payload': event.payload_json or {},
                }
                for event in events
            ],
        }

    def normalize_order_snapshot(self, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        raw = snapshot or {}
        order = raw.get('order') if isinstance(raw.get('order'), dict) else raw
        status = str(order.get('status') or raw.get('status') or 'UNKNOWN').upper()
        quantity = _as_float(order.get('quantity') or raw.get('quantity') or raw.get('qty'))
        filled_quantity = _as_float(
            order.get('exec_quantity')
            or order.get('filled_quantity')
            or order.get('filled_qty')
            or raw.get('exec_quantity')
            or raw.get('filled_quantity')
            or raw.get('filled_qty')
        )
        avg_fill_price = _as_float(
            order.get('avg_fill_price')
            or order.get('avg_execution_price')
            or order.get('avg_price')
            or order.get('last_fill_price')
            or raw.get('avg_fill_price')
            or raw.get('avg_execution_price')
            or raw.get('avg_price')
            or raw.get('last_fill_price')
        )
        rejection_reason = (
            order.get('reason_description')
            or order.get('reason')
            or raw.get('reason_description')
            or raw.get('reason')
        )
        order_id = str(order.get('id') or raw.get('id') or '') or None
        updated_at = _utcnow()
        return {
            'order_id': order_id,
            'status': status,
            'requested_quantity': quantity,
            'filled_quantity': filled_quantity,
            'avg_fill_price': avg_fill_price,
            'rejection_reason': str(rejection_reason) if rejection_reason else None,
            'updated_at': updated_at,
            'raw': raw,
        }

    def _resolve_status(self, intent: OrderIntent, normalized: dict[str, Any]) -> str:
        raw_status = normalized['status']
        requested_quantity = normalized['requested_quantity'] or float(intent.requested_quantity or 0.0)
        filled_quantity = normalized['filled_quantity']

        if raw_status in {'REJECTED', 'ERROR', 'FAILED'}:
            return 'REJECTED'
        if raw_status in {'CANCELED', 'CANCELLED'}:
            return 'CANCELED'
        if filled_quantity > 0 and requested_quantity > 0 and filled_quantity + 1e-9 < requested_quantity:
            return 'PARTIALLY_FILLED'
        if filled_quantity > 0 or raw_status == 'FILLED':
            return 'FILLED'
        if raw_status in {'OPEN', 'PENDING', 'SUBMITTED', 'ACCEPTED', 'PLACED'}:
            return 'SUBMITTED'
        return raw_status or intent.status or 'SUBMITTED'

    def _status_message(self, intent: OrderIntent, status: str, normalized: dict[str, Any]) -> str:
        if status == 'FILLED':
            return f"Confirmed fill for {intent.symbol}: {normalized['filled_quantity']} filled"
        if status == 'PARTIALLY_FILLED':
            return (
                f"Partial fill for {intent.symbol}: {normalized['filled_quantity']} of "
                f"{intent.requested_quantity} filled"
            )
        if status == 'REJECTED':
            return normalized['rejection_reason'] or f"Order rejected for {intent.symbol}"
        if status == 'CANCELED':
            return f"Order canceled for {intent.symbol}"
        return f"Order pending for {intent.symbol}"

    def _resolve_position(self, db: Session, intent: OrderIntent) -> Position | None:
        position_id = intent.position_id or (intent.context_json or {}).get('linkedPositionId')
        if position_id:
            return db.query(Position).filter(Position.id == position_id).first()
        return (
            db.query(Position)
            .filter(Position.account_id == intent.account_id, Position.ticker == intent.symbol, Position.is_open.is_(True))
            .order_by(Position.id.desc())
            .first()
        )

    def _resolve_trade(self, db: Session, intent: OrderIntent) -> Trade | None:
        trade_id = intent.trade_id or (intent.context_json or {}).get('linkedTradeId')
        if trade_id:
            return db.query(Trade).filter(Trade.id == trade_id).first()
        return (
            db.query(Trade)
            .filter(Trade.account_id == intent.account_id, Trade.ticker == intent.symbol)
            .order_by(Trade.id.desc())
            .first()
        )

    def _build_partial_exit_history(
        self,
        trade: Trade,
        intent: OrderIntent,
        applied_shares: int,
        exit_price: float,
        exit_time: datetime,
        remaining_shares: int,
    ) -> dict[str, Any]:
        reasoning = trade.exit_reasoning if isinstance(trade.exit_reasoning, dict) else {}
        partial_exits = list(reasoning.get('partialExits', []))
        partial_exits.append(
            {
                'intentId': intent.intent_id,
                'orderId': intent.submitted_order_id,
                'closedShares': applied_shares,
                'remainingShares': remaining_shares,
                'exitPrice': exit_price,
                'eventTime': exit_time.isoformat(),
                'status': intent.status,
                'trigger': (intent.context_json or {}).get('exitTrigger'),
            }
        )
        reasoning.update(
            {
                'partialExits': partial_exits,
                'lastExitIntentId': intent.intent_id,
                'lastRemainingShares': remaining_shares,
            }
        )
        return reasoning

    def _duration_minutes(self, start: datetime | None, end: datetime | None) -> int | None:
        if start is None or end is None:
            return None
        try:
            return max(int((end - start).total_seconds() // 60), 0)
        except TypeError:
            return None


execution_lifecycle = ExecutionLifecycleService()
