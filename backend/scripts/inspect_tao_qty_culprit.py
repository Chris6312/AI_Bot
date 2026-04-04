"""
TAO quantity investigation
Find why sell quantity exceeded real position size
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from app.core.database import SessionLocal


SYMBOLS = [
    "TAO/USD",
    "TAOUSD",
    "TAOUSD/USD",
    "TAO",
]


def run():

    db = SessionLocal()

    print("\n==================== ORDER INTENTS ====================\n")

    intents = db.execute(
        text(
            """
            SELECT
                id,
                symbol,
                side,
                status,
                quantity,
                filled_quantity,
                created_at
            FROM order_intents
            WHERE symbol ILIKE '%TAO%'
            ORDER BY created_at DESC
            """
        )
    ).fetchall()

    for row in intents:
        print(dict(row))

    print("\n==================== POSITIONS TABLE ====================\n")

    positions = db.execute(
        text(
            """
            SELECT
                symbol,
                quantity,
                avg_price,
                updated_at
            FROM positions
            WHERE symbol ILIKE '%TAO%'
            """
        )
    ).fetchall()

    for row in positions:
        print(dict(row))

    print("\n==================== EXECUTION AUDIT ====================\n")

    audit = db.execute(
        text(
            """
            SELECT
                symbol,
                side,
                quantity,
                price,
                source,
                created_at
            FROM execution_audit
            WHERE symbol ILIKE '%TAO%'
            ORDER BY created_at DESC
            """
        )
    ).fetchall()

    for row in audit:
        print(dict(row))

    print("\n==================== SUMMARY ====================\n")

    buy_qty = sum(
        float(r.quantity or 0)
        for r in intents
        if r.side == "BUY" and r.status in ("FILLED", "EXECUTED")
    )

    sell_qty = sum(
        float(r.quantity or 0)
        for r in intents
        if r.side == "SELL"
    )

    print(f"total BUY qty:  {buy_qty}")
    print(f"total SELL qty: {sell_qty}")

    db.close()


if __name__ == "__main__":
    run()