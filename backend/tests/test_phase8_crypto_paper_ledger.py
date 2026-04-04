from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.crypto_paper_fill import CryptoPaperFill
from app.models.crypto_paper_order import CryptoPaperOrder
from app.models.crypto_paper_position import CryptoPaperPosition
from app.models.order_intent import OrderIntent
from app.services.crypto_paper_broker import crypto_paper_broker
from app.services.kraken_service import crypto_ledger
from app.services.position_reconciliation import position_reconciliation_service
from tests.test_phase4_watchlists import build_session_factory


def test_crypto_paper_broker_persists_buy_and_sell_without_decimal_clamp(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        buy = crypto_paper_broker.execute_trade(
            db=db,
            pair='BTC/USD',
            ohlcv_pair='XBTUSD',
            side='BUY',
            amount=Decimal('0.07629373987424998'),
            price=Decimal('65432.123456789123'),
            source='TEST',
            intent_id='intent_buy_precise',
        )
        assert buy['status'] == 'FILLED'

        sell = crypto_paper_broker.execute_trade(
            db=db,
            pair='BTC/USD',
            ohlcv_pair='XBTUSD',
            side='SELL',
            amount=Decimal('0.01629373987424998'),
            price=Decimal('66432.123456789123'),
            source='TEST',
            intent_id='intent_sell_precise',
        )
        assert sell['status'] == 'FILLED'

        position = db.query(CryptoPaperPosition).filter(CryptoPaperPosition.symbol == 'BTC/USD').one()
        assert Decimal(str(position.quantity)) == Decimal('0.06')
        assert Decimal(str(position.avg_price)) > Decimal('0')
        assert db.query(CryptoPaperOrder).count() == 2
        assert db.query(CryptoPaperFill).count() == 2


def test_crypto_reconciliation_rebuilds_persisted_ledger_tables(tmp_path) -> None:
    crypto_ledger.trades = []
    crypto_ledger.positions = {}
    crypto_ledger.balance = crypto_ledger.starting_balance
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        filled_at = datetime.now(UTC).replace(microsecond=0)
        db.add(
            OrderIntent(
                intent_id='intent_crypto_tao_buy_phase8',
                account_id='paper-crypto-ledger',
                asset_class='crypto',
                symbol='TAO/USD',
                side='BUY',
                requested_quantity=1.234567890123,
                requested_price=456.789012345678,
                filled_quantity=1.234567890123,
                avg_fill_price=456.789012345678,
                status='FILLED',
                execution_source='WATCHLIST_MONITOR_ENTRY',
                submitted_order_id='paper_replay_1',
                context_json={'ohlcvPair': 'TAOUSD'},
                submitted_at=filled_at,
                first_fill_at=filled_at,
                last_fill_at=filled_at,
            )
        )
        db.commit()

        summary = position_reconciliation_service.reconcile_asset_class(
            db,
            asset_class='crypto',
            observed_at=filled_at + timedelta(minutes=1),
        )

        assert summary['restoredPositionCount'] == 1
        position = db.query(CryptoPaperPosition).filter(CryptoPaperPosition.symbol == 'TAO/USD').one()
        assert Decimal(str(position.quantity)) == Decimal('1.234567890123')
        fills = db.query(CryptoPaperFill).filter(CryptoPaperFill.symbol == 'TAO/USD').all()
        assert len(fills) == 1
