from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.order_event import OrderEvent
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.trade import Trade


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
        return {
            'position_id': position.id,
            'trade_id': trade.id,
            'filled_shares': filled_shares,
            'avg_fill_price': entry_price,
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


execution_lifecycle = ExecutionLifecycleService()
