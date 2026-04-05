from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.crypto_paper_account import CryptoPaperAccount
from app.models.crypto_paper_fill import CryptoPaperFill
from app.models.crypto_paper_order import CryptoPaperOrder
from app.models.crypto_paper_position import CryptoPaperPosition
from app.models.order_event import OrderEvent
from app.models.order_intent import OrderIntent
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.services.crypto_paper_broker import CRYPTO_PAPER_ACCOUNT_KEY, SCALE, ZERO, crypto_paper_broker
from app.services.execution_lifecycle import execution_lifecycle
from app.services.kraken_service import crypto_ledger
from app.services.watchlist_service import watchlist_service

ADMIN_EXECUTION_SOURCE = "CRYPTO_PAPER_LEDGER_ADMIN"
AUDIT_SYMBOL = "CRYPTO_PAPER"
PENDING_INTENT_STATUSES = {
    "READY",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "SUBMISSION_PENDING",
    "PENDING",
    "OPEN",
    "NEW",
    "ACCEPTED",
}
PENDING_ORDER_STATUSES = {
    "READY",
    "SUBMITTED",
    "PARTIALLY_FILLED",
    "PENDING",
    "OPEN",
    "NEW",
    "ACCEPTED",
}


class CryptoPaperLedgerAdminValidationError(ValueError):
    pass


class CryptoPaperLedgerAdminConflict(RuntimeError):
    pass


class CryptoPaperLedgerAdminService:
    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _normalize_cash_balance(value: Decimal | float | str | int) -> Decimal:
        try:
            normalized = Decimal(str(value)).quantize(SCALE, rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CryptoPaperLedgerAdminValidationError("Cash balance must be a valid non-negative number.") from exc
        if normalized < ZERO:
            raise CryptoPaperLedgerAdminValidationError("Cash balance must be zero or greater.")
        return normalized

    @staticmethod
    def _crypto_symbol_aliases(symbol: str | None) -> set[str]:
        raw = str(symbol or "").strip().upper()
        if not raw:
            return set()
        compact = "".join(ch for ch in raw if ch.isalnum())
        aliases = {raw, compact}
        if "/" in raw:
            base, _, quote = raw.partition("/")
            aliases.add(base)
            aliases.add(f"{base}{quote}")
        elif raw.endswith("USD") and len(raw) > 3:
            base = raw[:-3]
            aliases.add(base)
            aliases.add(f"{base}/USD")
        return {alias for alias in aliases if alias}

    def _matching_crypto_monitor_states(self, db: Session, *, symbols: list[str] | None = None, full_scope: bool = False) -> list[WatchlistMonitorState]:
        query = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.scope == "crypto_only")
            .order_by(WatchlistMonitorState.id.asc())
        )
        rows = query.all()
        if full_scope or not symbols:
            return rows if full_scope else []

        aliases: set[str] = set()
        for symbol in symbols:
            aliases.update(self._crypto_symbol_aliases(symbol))

        matched: list[WatchlistMonitorState] = []
        for row in rows:
            if self._crypto_symbol_aliases(row.symbol).intersection(aliases):
                matched.append(row)
        return matched

    def _clear_monitor_states(self, db: Session, *, symbols: list[str] | None = None, full_scope: bool = False, reason: str) -> dict[str, Any]:
        rows = self._matching_crypto_monitor_states(db, symbols=symbols, full_scope=full_scope)
        now = self._utcnow()
        cleared_symbols: list[str] = []

        for row in rows:
            context = dict(row.decision_context_json or {})
            context.pop("reentryBlockedUntilUtc", None)
            context.pop("cooldownActive", None)
            context.pop("lastExitAtUtc", None)
            context.pop("lastExitReason", None)
            context.pop("entryExecution", None)
            context.pop("exitExecution", None)
            context.pop("lifecycleState", None)
            context.pop("lifecycleNote", None)
            row.decision_context_json = context
            flag_modified(row, "decision_context_json")
            row.latest_decision_state = "PENDING_EVALUATION"
            row.latest_decision_reason = reason
            row.last_decision_at_utc = now
            row.next_evaluation_at_utc = now
            cleared_symbols.append(str(row.symbol or "").upper())

        db.flush()
        return {
            "clearedMonitorStates": len(rows),
            "symbols": sorted({symbol for symbol in cleared_symbols if symbol}),
        }

    def _record_admin_event(self, db: Session, *, event_type: str, message: str, payload: dict[str, Any]) -> None:
        now = self._utcnow()
        intent = OrderIntent(
            intent_id=f"intent_{uuid4().hex[:24]}",
            account_id=CRYPTO_PAPER_ACCOUNT_KEY,
            asset_class="crypto",
            symbol=AUDIT_SYMBOL,
            side="ADMIN",
            requested_quantity=0.0,
            requested_price=None,
            filled_quantity=0.0,
            avg_fill_price=None,
            status="CLOSED",
            execution_source=ADMIN_EXECUTION_SOURCE,
            context_json=payload,
            submitted_at=now,
            first_fill_at=now,
            last_fill_at=now,
        )
        db.add(intent)
        db.flush()
        execution_lifecycle.record_event(
            db,
            intent,
            event_type=event_type,
            status="CLOSED",
            message=message,
            payload=payload,
            event_time=now,
        )

    @staticmethod
    def _refresh_runtime_cache(db: Session) -> None:
        refresh_cache = getattr(crypto_ledger, "_refresh_cache", None)
        if callable(refresh_cache):
            refresh_cache(db=db, include_trades=True)

    def _reconcile_crypto_monitoring(self, db: Session, *, symbols: list[str] | None = None, full_scope: bool = False, reason: str) -> dict[str, Any]:
        self._refresh_runtime_cache(db)
        watchlist_service.reconcile_scope_statuses(db, scope="crypto_only")
        return self._clear_monitor_states(db, symbols=symbols, full_scope=full_scope, reason=reason)

    @staticmethod
    def _reset_runtime_ledger_state(*, cash_balance: Decimal) -> None:
        ledger_lock = getattr(crypto_ledger, "_ledger_lock", None)

        def apply_reset() -> None:
            crypto_ledger.starting_balance = cash_balance
            crypto_ledger.balance = cash_balance
            crypto_ledger.trades = []
            crypto_ledger.positions = {}

        if ledger_lock is None:
            apply_reset()
            return

        with ledger_lock:
            apply_reset()

    def _cancel_pending(self, db: Session) -> dict[str, Any]:
        intents = (
            db.query(OrderIntent)
            .filter(
                OrderIntent.account_id == CRYPTO_PAPER_ACCOUNT_KEY,
                OrderIntent.asset_class == "crypto",
                OrderIntent.status.in_(sorted(PENDING_INTENT_STATUSES)),
            )
            .order_by(OrderIntent.created_at.asc(), OrderIntent.id.asc())
            .all()
        )
        orders = (
            db.query(CryptoPaperOrder)
            .filter(
                CryptoPaperOrder.account_key == CRYPTO_PAPER_ACCOUNT_KEY,
                CryptoPaperOrder.status.in_(sorted(PENDING_ORDER_STATUSES)),
            )
            .order_by(CryptoPaperOrder.created_at.asc(), CryptoPaperOrder.id.asc())
            .all()
        )

        canceled_intent_ids: list[str] = []
        for intent in intents:
            intent.status = "CANCELED"
            intent.rejection_reason = "Canceled by crypto paper admin control"
            execution_lifecycle.record_event(
                db,
                intent,
                event_type="CRYPTO_PAPER_ADMIN_CANCELED",
                status="CANCELED",
                message=f"Canceled pending crypto paper intent for {intent.symbol}",
                payload={"symbol": intent.symbol, "executionSource": intent.execution_source},
                event_time=self._utcnow(),
            )
            canceled_intent_ids.append(intent.intent_id)

        canceled_order_ids: list[str] = []
        for order in orders:
            order.status = "CANCELED"
            canceled_order_ids.append(order.order_id)

        db.flush()
        return {
            "canceledPendingIntents": len(intents),
            "canceledPendingOrders": len(orders),
            "intentIds": canceled_intent_ids,
            "orderIds": canceled_order_ids,
        }

    def cancel_pending(self, db: Session) -> dict[str, Any]:
        try:
            summary = self._cancel_pending(db)
            payload = {
                "success": True,
                "assetClass": "crypto",
                "mode": "PAPER",
                "message": "Canceled pending crypto paper orders and intents.",
                **summary,
            }
            self._record_admin_event(
                db,
                event_type="CRYPTO_PAPER_ADMIN_CANCEL_PENDING",
                message=payload["message"],
                payload=payload,
            )
            self._refresh_runtime_cache(db)
            db.commit()
            return payload
        except Exception:
            db.rollback()
            raise

    def _flatten_positions_impl(self, db: Session, *, clear_all_monitor_state: bool = False) -> dict[str, Any]:
        rows = (
            db.query(CryptoPaperPosition)
            .filter(
                CryptoPaperPosition.account_key == CRYPTO_PAPER_ACCOUNT_KEY,
                CryptoPaperPosition.is_open.is_(True),
            )
            .order_by(CryptoPaperPosition.symbol.asc(), CryptoPaperPosition.id.asc())
            .all()
        )

        flattened_rows: list[dict[str, Any]] = []
        flattened_symbols: list[str] = []
        for row in rows:
            quantity = Decimal(str(row.quantity or 0))
            if quantity <= ZERO:
                continue
            avg_price = Decimal(str(row.avg_price or 0))
            execution_price = avg_price if avg_price > ZERO else Decimal("0.00000001")
            result = crypto_paper_broker.execute_trade(
                db=db,
                pair=str(row.symbol or "").upper().strip(),
                ohlcv_pair=str(row.ohlcv_pair or "").upper().strip() or None,
                side="SELL",
                amount=quantity,
                price=execution_price,
                source=ADMIN_EXECUTION_SOURCE,
            )
            flattened_rows.append(
                {
                    "symbol": str(row.symbol or "").upper().strip(),
                    "quantity": float(quantity),
                    "price": float(execution_price),
                    "status": str(result.get("status") or "FILLED").upper(),
                }
            )
            flattened_symbols.append(str(row.symbol or "").upper().strip())

        cleared = self._reconcile_crypto_monitoring(
            db,
            symbols=flattened_symbols,
            full_scope=clear_all_monitor_state,
            reason="CRYPTO_PAPER_ADMIN_POSITION_RESET",
        )
        db.flush()
        return {
            "flattenedPositions": len(flattened_rows),
            "flattenedQuantity": round(sum(float(item["quantity"]) for item in flattened_rows), 12),
            "rows": flattened_rows,
            **cleared,
        }

    def flatten_positions(self, db: Session) -> dict[str, Any]:
        try:
            canceled = self._cancel_pending(db)
            flattened = self._flatten_positions_impl(db)
            payload = {
                "success": True,
                "assetClass": "crypto",
                "mode": "PAPER",
                "message": "Flattened open crypto paper positions and cleared related monitor state.",
                **canceled,
                **flattened,
            }
            self._record_admin_event(
                db,
                event_type="CRYPTO_PAPER_ADMIN_FLATTEN",
                message=payload["message"],
                payload=payload,
            )
            self._refresh_runtime_cache(db)
            db.commit()
            return payload
        except Exception:
            db.rollback()
            raise

    def _delete_history_impl(self, db: Session) -> dict[str, Any]:
        open_position_count = (
            db.query(CryptoPaperPosition)
            .filter(
                CryptoPaperPosition.account_key == CRYPTO_PAPER_ACCOUNT_KEY,
                CryptoPaperPosition.is_open.is_(True),
            )
            .count()
        )
        if open_position_count > 0:
            raise CryptoPaperLedgerAdminConflict("Cannot delete crypto paper history while open crypto paper positions remain.")

        intent_ids = [
            row.intent_id
            for row in db.query(OrderIntent.intent_id)
            .filter(
                OrderIntent.account_id == CRYPTO_PAPER_ACCOUNT_KEY,
                OrderIntent.asset_class == "crypto",
            )
            .all()
        ]
        deleted_events = 0
        if intent_ids:
            deleted_events = (
                db.query(OrderEvent)
                .filter(OrderEvent.intent_id.in_(intent_ids))
                .delete(synchronize_session=False)
            )

        deleted_intents = (
            db.query(OrderIntent)
            .filter(
                OrderIntent.account_id == CRYPTO_PAPER_ACCOUNT_KEY,
                OrderIntent.asset_class == "crypto",
            )
            .delete(synchronize_session=False)
        )
        deleted_fills = (
            db.query(CryptoPaperFill)
            .filter(CryptoPaperFill.account_key == CRYPTO_PAPER_ACCOUNT_KEY)
            .delete(synchronize_session=False)
        )
        deleted_orders = (
            db.query(CryptoPaperOrder)
            .filter(CryptoPaperOrder.account_key == CRYPTO_PAPER_ACCOUNT_KEY)
            .delete(synchronize_session=False)
        )

        account = crypto_paper_broker.ensure_account(db)
        account.realized_pnl = ZERO
        db.flush()
        return {
            "deletedTrades": int(deleted_fills),
            "deletedOrders": int(deleted_orders),
            "deletedIntents": int(deleted_intents),
            "deletedEvents": int(deleted_events),
        }

    def delete_history(self, db: Session) -> dict[str, Any]:
        try:
            summary = self._delete_history_impl(db)
            payload = {
                "success": True,
                "assetClass": "crypto",
                "mode": "PAPER",
                "message": "Deleted crypto paper trade and order history.",
                **summary,
            }
            self._record_admin_event(
                db,
                event_type="CRYPTO_PAPER_ADMIN_DELETE_HISTORY",
                message=payload["message"],
                payload=payload,
            )
            self._refresh_runtime_cache(db)
            db.commit()
            return payload
        except Exception:
            db.rollback()
            raise

    def set_cash_balance(
        self,
        db: Session,
        *,
        cash_balance: Decimal | float | str | int,
        reset_starting_balance: bool = False,
        clear_realized_pnl: bool = False,
        audit_event_type: str = "CRYPTO_PAPER_ADMIN_SET_CASH",
        audit_message: str = "Updated crypto paper cash balance.",
        write_audit_event: bool = True,
    ) -> dict[str, Any]:
        try:
            account = crypto_paper_broker.ensure_account(db)
            new_cash_balance = self._normalize_cash_balance(cash_balance)
            old_cash_balance = Decimal(str(account.cash_balance or 0))
            old_starting_balance = Decimal(str(account.starting_balance or 0))
            old_realized_pnl = Decimal(str(account.realized_pnl or 0))

            account.cash_balance = new_cash_balance
            if reset_starting_balance:
                account.starting_balance = new_cash_balance
            if clear_realized_pnl:
                account.realized_pnl = ZERO
            db.flush()

            payload = {
                "success": True,
                "assetClass": "crypto",
                "mode": "PAPER",
                "message": audit_message,
                "oldCashBalance": float(old_cash_balance),
                "newCashBalance": float(new_cash_balance),
                "oldStartingBalance": float(old_starting_balance),
                "newStartingBalance": float(Decimal(str(account.starting_balance or 0))),
                "oldRealizedPnl": float(old_realized_pnl),
                "newRealizedPnl": float(Decimal(str(account.realized_pnl or 0))),
            }
            if write_audit_event:
                self._record_admin_event(
                    db,
                    event_type=audit_event_type,
                    message=audit_message,
                    payload=payload,
                )
            self._refresh_runtime_cache(db)
            db.commit()
            return payload
        except Exception:
            db.rollback()
            raise

    def fresh_start(self, db: Session, *, cash_balance: Decimal | float | str | int) -> dict[str, Any]:
        try:
            account = crypto_paper_broker.ensure_account(db)
            old_cash_balance = float(Decimal(str(account.cash_balance or 0)))
            new_cash_balance = self._normalize_cash_balance(cash_balance)

            canceled = self._cancel_pending(db)
            flattened = self._flatten_positions_impl(db, clear_all_monitor_state=True)
            deleted = self._delete_history_impl(db)
            account = crypto_paper_broker.ensure_account(db)
            account.realized_pnl = ZERO
            account.cash_balance = new_cash_balance
            account.starting_balance = new_cash_balance
            db.flush()
            self._reset_runtime_ledger_state(cash_balance=new_cash_balance)
            self._reconcile_crypto_monitoring(
                db,
                full_scope=True,
                reason="CRYPTO_PAPER_ADMIN_POSITION_RESET",
            )

            payload = {
                "success": True,
                "assetClass": "crypto",
                "mode": "PAPER",
                "message": "Crypto paper account reset complete.",
                "oldCashBalance": old_cash_balance,
                "newCashBalance": float(Decimal(str(account.cash_balance or 0))),
                "cancelPending": canceled,
                "flattenPositions": flattened,
                "deleteHistory": deleted,
                "clearedMonitorStates": int(flattened.get("clearedMonitorStates") or 0),
                "canceledPendingOrders": int(canceled.get("canceledPendingOrders") or 0),
                "canceledPendingIntents": int(canceled.get("canceledPendingIntents") or 0),
                "flattenedPositions": int(flattened.get("flattenedPositions") or 0),
                "deletedTrades": int(deleted.get("deletedTrades") or 0),
                "deletedOrders": int(deleted.get("deletedOrders") or 0),
                "deletedIntents": int(deleted.get("deletedIntents") or 0),
            }
            self._record_admin_event(
                db,
                event_type="CRYPTO_PAPER_ADMIN_FRESH_START",
                message=payload["message"],
                payload=payload,
            )
            self._refresh_runtime_cache(db)
            db.commit()
            return payload
        except Exception:
            db.rollback()
            raise


crypto_paper_ledger_admin = CryptoPaperLedgerAdminService()
