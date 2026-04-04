from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.database import SessionLocal
from sqlalchemy import text


def run():

    db = SessionLocal()

    rows = db.execute(text("""
        SELECT symbol
        FROM positions
        WHERE asset_class='CRYPTO'
        AND quantity > 0
    """)).fetchall()

    for r in rows:

        symbol = r.symbol

        ledger_qty = db.execute(text("""
            SELECT SUM(filled_quantity)
            FROM order_intents
            WHERE symbol = :symbol
            AND side='BUY'
            AND status='FILLED'
        """), {"symbol": symbol}).scalar() or 0

        sold_qty = db.execute(text("""
            SELECT SUM(filled_quantity)
            FROM order_intents
            WHERE symbol = :symbol
            AND side='SELL'
            AND status='FILLED'
        """), {"symbol": symbol}).scalar() or 0

        true_qty = float(ledger_qty) - float(sold_qty)

        print(symbol, "true_qty =", true_qty)

        db.execute(text("""
            UPDATE positions
            SET quantity = :q
            WHERE symbol = :symbol
        """), {"symbol": symbol, "q": true_qty})

    db.commit()

    print("\nrepair complete")


if __name__ == "__main__":
    run()