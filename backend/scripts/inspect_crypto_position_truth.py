from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import SessionLocal
from app.models.order_intent import OrderIntent


SYMBOLS = ["TAO/USD", "HYPE/USD", "BTC/USD"]


def qty(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def run() -> None:
    db = SessionLocal()
    try:
        for symbol in SYMBOLS:
            buys = (
                db.query(OrderIntent)
                .filter(
                    OrderIntent.symbol == symbol,
                    OrderIntent.side == "BUY",
                    OrderIntent.status == "FILLED",
                )
                .all()
            )

            sells = (
                db.query(OrderIntent)
                .filter(
                    OrderIntent.symbol == symbol,
                    OrderIntent.side == "SELL",
                    OrderIntent.status == "FILLED",
                )
                .all()
            )

            buy_qty = sum((qty(o.filled_quantity) for o in buys), Decimal("0"))
            sell_qty = sum((qty(o.filled_quantity) for o in sells), Decimal("0"))
            open_qty = buy_qty - sell_qty

            print("\n==============================")
            print(symbol)
            print("BUY total :", buy_qty)
            print("SELL total:", sell_qty)
            print("OPEN QTY  :", open_qty)
            print("BUY count :", len(buys))
            print("SELL count:", len(sells))
    finally:
        db.close()


if __name__ == "__main__":
    run()