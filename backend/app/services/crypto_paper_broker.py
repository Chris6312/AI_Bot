from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from threading import RLock
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.crypto_paper_account import CryptoPaperAccount
from app.models.crypto_paper_fill import CryptoPaperFill
from app.models.crypto_paper_order import CryptoPaperOrder
from app.models.crypto_paper_position import CryptoPaperPosition

logger = logging.getLogger(__name__)

CRYPTO_PAPER_ACCOUNT_KEY = "paper-crypto-ledger"
ZERO = Decimal("0")
DUST = Decimal("0.000000000001")
SCALE = Decimal("0.000000000000000001")


class CryptoPaperBrokerService:
    def __init__(self, *, starting_balance: Decimal | float | str = Decimal("100000")) -> None:
        self.starting_balance = Decimal(str(starting_balance))
        self._lock = RLock()

    @contextmanager
    def _session_scope(self, db: Session | None = None) -> Iterator[tuple[Session, bool]]:
        if db is not None:
            yield db, False
            return

        session = SessionLocal()
        try:
            yield session, True
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _normalize_decimal(value: Decimal | float | str | int | None) -> Decimal:
        return Decimal(str(value or 0)).quantize(SCALE, rounding=ROUND_HALF_UP)

    def ensure_account(self, db: Session) -> CryptoPaperAccount:
        account = (
            db.query(CryptoPaperAccount)
            .filter(CryptoPaperAccount.account_key == CRYPTO_PAPER_ACCOUNT_KEY)
            .one_or_none()
        )
        if account is not None:
            return account
        account = CryptoPaperAccount(
            account_key=CRYPTO_PAPER_ACCOUNT_KEY,
            base_currency="USD",
            cash_balance=self.starting_balance,
            starting_balance=self.starting_balance,
            realized_pnl=ZERO,
        )
        db.add(account)
        db.flush()
        return account

    def reset_account_state(self, db: Session) -> None:
        db.query(CryptoPaperFill).filter(CryptoPaperFill.account_key == CRYPTO_PAPER_ACCOUNT_KEY).delete()
        db.query(CryptoPaperOrder).filter(CryptoPaperOrder.account_key == CRYPTO_PAPER_ACCOUNT_KEY).delete()
        db.query(CryptoPaperPosition).filter(CryptoPaperPosition.account_key == CRYPTO_PAPER_ACCOUNT_KEY).delete()
        db.query(CryptoPaperAccount).filter(CryptoPaperAccount.account_key == CRYPTO_PAPER_ACCOUNT_KEY).delete()
        self.ensure_account(db)
        db.flush()

    def execute_trade(
        self,
        *,
        db: Session | None,
        pair: str,
        ohlcv_pair: str | None,
        side: str,
        amount: Decimal | float | str,
        price: Decimal | float | str,
        source: str | None = None,
        intent_id: str | None = None,
        submitted_at: datetime | None = None,
    ) -> dict[str, Any]:
        pair = str(pair or "").upper().strip()
        side = str(side or "").upper().strip()
        quantity = self._normalize_decimal(amount)
        price_dec = self._normalize_decimal(price)
        if not pair or side not in {"BUY", "SELL"} or quantity <= 0 or price_dec <= 0:
            return {"status": "REJECTED", "reason": "Invalid crypto paper trade request"}

        event_time = self._coerce_utc(submitted_at)

        with self._lock:
            with self._session_scope(db) as (session, _):
                account = self.ensure_account(session)
                position = (
                    session.query(CryptoPaperPosition)
                    .filter(
                        CryptoPaperPosition.account_key == CRYPTO_PAPER_ACCOUNT_KEY,
                        CryptoPaperPosition.symbol == pair,
                        CryptoPaperPosition.is_open.is_(True),
                    )
                    .one_or_none()
                )
                total = self._normalize_decimal(quantity * price_dec)
                current_cash = self._normalize_decimal(account.cash_balance or 0)

                if side == "BUY":
                    if current_cash + DUST < total:
                        return {
                            "status": "REJECTED",
                            "reason": f"Insufficient balance: {self._decimal_to_text(account.cash_balance)} < {self._decimal_to_text(total)}",
                        }

                    account.cash_balance = self._normalize_decimal(current_cash - total)

                    if position is None:
                        position = CryptoPaperPosition(
                            account_key=CRYPTO_PAPER_ACCOUNT_KEY,
                            symbol=pair,
                            ohlcv_pair=ohlcv_pair,
                            quantity=ZERO,
                            avg_price=ZERO,
                            total_cost=ZERO,
                            realized_pnl=ZERO,
                            entry_time_utc=event_time,
                            is_open=True,
                        )
                        session.add(position)
                        session.flush()

                    old_quantity = self._normalize_decimal(position.quantity or 0)
                    old_cost = self._normalize_decimal(position.total_cost or 0)
                    new_quantity = self._normalize_decimal(old_quantity + quantity)
                    new_cost = self._normalize_decimal(old_cost + total)

                    position.quantity = new_quantity
                    position.total_cost = new_cost
                    position.avg_price = self._normalize_decimal(new_cost / new_quantity) if new_quantity > 0 else ZERO
                    if old_quantity <= DUST:
                        position.entry_time_utc = event_time
                    position.closed_at = None
                    position.is_open = True

                else:
                    available = self._normalize_decimal(position.quantity if position is not None else 0)
                    if position is None or available + DUST < quantity:
                        return {"status": "REJECTED", "reason": f"Insufficient {pair} position"}

                    avg_cost = self._normalize_decimal((self._normalize_decimal(position.total_cost or 0) / available) if available > 0 else ZERO)
                    closed_cost = self._normalize_decimal(avg_cost * quantity)
                    realized = self._normalize_decimal(total - closed_cost)
                    new_quantity = self._normalize_decimal(available - quantity)
                    new_total_cost = self._normalize_decimal(self._normalize_decimal(position.total_cost or 0) - closed_cost)

                    account.cash_balance = self._normalize_decimal(current_cash + total)
                    account.realized_pnl = self._normalize_decimal(self._normalize_decimal(account.realized_pnl or 0) + realized)
                    position.realized_pnl = self._normalize_decimal(self._normalize_decimal(position.realized_pnl or 0) + realized)

                    if new_quantity <= DUST:
                        position.quantity = ZERO
                        position.total_cost = ZERO
                        position.avg_price = ZERO
                        position.is_open = False
                        position.closed_at = event_time
                    else:
                        position.quantity = new_quantity
                        position.total_cost = new_total_cost
                        position.avg_price = self._normalize_decimal(new_total_cost / new_quantity) if new_quantity > 0 else ZERO

                order_id = f"paper_{uuid4().hex[:24]}"
                order = CryptoPaperOrder(
                    order_id=order_id,
                    account_key=CRYPTO_PAPER_ACCOUNT_KEY,
                    symbol=pair,
                    ohlcv_pair=ohlcv_pair,
                    side=side,
                    status="FILLED",
                    requested_quantity=quantity,
                    requested_price=price_dec,
                    filled_quantity=quantity,
                    avg_fill_price=price_dec,
                    intent_id=intent_id,
                    source=source,
                    submitted_at=event_time,
                )
                session.add(order)
                session.flush()

                fill = CryptoPaperFill(
                    fill_id=f"fill_{uuid4().hex[:24]}",
                    order_id=order.order_id,
                    account_key=CRYPTO_PAPER_ACCOUNT_KEY,
                    symbol=pair,
                    ohlcv_pair=ohlcv_pair,
                    side=side,
                    quantity=quantity,
                    price=price_dec,
                    notional=total,
                    fee=ZERO,
                    filled_at=event_time,
                )
                session.add(fill)
                session.flush()

                return {
                    "id": order.order_id,
                    "timestamp": event_time.isoformat(),
                    "market": "CRYPTO",
                    "pair": pair,
                    "ohlcvPair": ohlcv_pair,
                    "side": side,
                    "amount": float(quantity),
                    "price": float(price_dec),
                    "total": float(total),
                    "status": "FILLED",
                    "balance": float(account.cash_balance),
                }

    def rebuild_from_replay_trades(self, db: Session, trades: list[Any]) -> dict[str, Any]:
        self.reset_account_state(db)
        restored_symbols: set[str] = set()
        replayed_count = 0

        for trade in trades:
            payload = self.execute_trade(
                db=db,
                pair=str(trade.pair),
                ohlcv_pair=getattr(trade, "ohlcv_pair", None),
                side=str(trade.side),
                amount=getattr(trade, "amount"),
                price=getattr(trade, "price"),
                source="RECONCILIATION_REPLAY",
                intent_id=getattr(trade, "intent_id", None),
                submitted_at=getattr(trade, "timestamp", None),
            )
            if str(payload.get("status") or "").upper() == "FILLED":
                replayed_count += 1

        for row in self.get_positions(db=db):
            symbol = str(row.get("pair") or "").strip()
            if symbol:
                restored_symbols.add(symbol)

        return {
            "replayedTradeCount": replayed_count,
            "restoredPositionCount": len(restored_symbols),
            "restoredSymbols": sorted(restored_symbols),
        }

    def get_positions(self, *, db: Session | None = None, price_lookup: callable | None = None) -> list[dict[str, Any]]:
        with self._session_scope(db) as (session, _):
            rows = (
                session.query(CryptoPaperPosition)
                .filter(
                    CryptoPaperPosition.account_key == CRYPTO_PAPER_ACCOUNT_KEY,
                    CryptoPaperPosition.is_open.is_(True),
                )
                .order_by(CryptoPaperPosition.symbol.asc(), CryptoPaperPosition.id.asc())
                .all()
            )

            positions: list[dict[str, Any]] = []
            for row in rows:
                quantity = self._normalize_decimal(row.quantity or 0)
                if quantity <= DUST:
                    continue

                avg_price = self._normalize_decimal(row.avg_price or 0)
                lookup_price = price_lookup(row.symbol, row.ohlcv_pair, float(avg_price)) if price_lookup else avg_price
                current_price = self._normalize_decimal(lookup_price)
                market_value = self._normalize_decimal(quantity * current_price)
                cost_basis = self._normalize_decimal(row.total_cost or 0)
                pnl = self._normalize_decimal(market_value - cost_basis)
                pnl_pct = self._normalize_decimal((pnl / cost_basis * Decimal("100")) if cost_basis > 0 else ZERO)

                positions.append(
                    {
                        "pair": row.symbol,
                        "ohlcvPair": row.ohlcv_pair,
                        "amount": float(quantity),
                        "avgPrice": float(avg_price),
                        "currentPrice": float(current_price),
                        "marketValue": float(market_value),
                        "costBasis": float(cost_basis),
                        "pnl": float(pnl),
                        "pnlPercent": float(pnl_pct),
                        "entryTimeUtc": self._iso_or_none(row.entry_time_utc),
                        "realizedPnl": float(self._normalize_decimal(row.realized_pnl or 0)),
                    }
                )
            return positions

    def get_ledger(self, *, db: Session | None = None, price_lookup: callable | None = None) -> dict[str, Any]:
        with self._session_scope(db) as (session, _):
            account = self.ensure_account(session)
            positions = self.get_positions(db=session, price_lookup=price_lookup)

            fills = (
                session.query(CryptoPaperFill)
                .filter(CryptoPaperFill.account_key == CRYPTO_PAPER_ACCOUNT_KEY)
                .order_by(CryptoPaperFill.filled_at.asc(), CryptoPaperFill.id.asc())
                .all()
            )

            trades = [
                {
                    "id": row.order_id,
                    "timestamp": self._iso_or_none(row.filled_at),
                    "market": "CRYPTO",
                    "pair": row.symbol,
                    "ohlcvPair": row.ohlcv_pair,
                    "side": row.side,
                    "amount": float(self._normalize_decimal(row.quantity or 0)),
                    "price": float(self._normalize_decimal(row.price or 0)),
                    "total": float(self._normalize_decimal(row.notional or 0)),
                    "status": "FILLED",
                }
                for row in fills
            ]

            market_value = self._normalize_decimal(sum(Decimal(str(item.get("marketValue") or 0)) for item in positions))
            balance = self._normalize_decimal(account.cash_balance or 0)
            equity = self._normalize_decimal(balance + market_value)
            starting_balance = self._normalize_decimal(account.starting_balance or self.starting_balance)
            net_pnl = self._normalize_decimal(equity - starting_balance)
            total_pnl = self._normalize_decimal(sum(Decimal(str(item.get("pnl") or 0)) for item in positions))

            return {
                "balance": float(balance),
                "startingBalance": float(starting_balance),
                "marketValue": float(market_value),
                "equity": float(equity),
                "totalPnL": float(total_pnl),
                "realizedPnL": float(self._normalize_decimal(account.realized_pnl or 0)),
                "netPnL": float(net_pnl),
                "returnPct": float(self._normalize_decimal((net_pnl / starting_balance * Decimal("100")) if starting_balance > 0 else ZERO)),
                "trades": trades,
                "positions": positions,
            }

    @staticmethod
    def _coerce_utc(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _iso_or_none(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _decimal_to_text(value: Decimal | str | float | int | None) -> str:
        dec = Decimal(str(value or 0))
        text = format(dec.normalize(), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".") or "0"
        return text


crypto_paper_broker = CryptoPaperBrokerService()