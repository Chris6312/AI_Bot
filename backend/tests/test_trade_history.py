from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models.order_intent import OrderIntent
from app.models.trade import Trade
from tests.test_phase4_watchlists import build_session_factory


def test_trade_history_returns_closed_stock_and_crypto_rows(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        entry_time = datetime(2026, 4, 1, 14, 30, tzinfo=UTC)
        exit_time = entry_time + timedelta(hours=2)
        db.add(
            Trade(
                trade_id='trade_stock_1',
                account_id='paper-stock',
                ticker='AAPL',
                direction='LONG',
                strategy='pullback_reclaim',
                entry_time=entry_time,
                entry_price=100.0,
                shares=10,
                entry_cost=1000.0,
                exit_time=exit_time,
                exit_price=110.0,
                exit_proceeds=1100.0,
                gross_pnl=100.0,
                net_pnl=100.0,
                duration_minutes=120,
                entry_reasoning={'mode': 'PAPER', 'executionSource': 'WATCHLIST_MONITOR_ENTRY', 'intentId': 'intent_stock_buy_1'},
                exit_trigger='target_hit',
            )
        )
        db.add_all(
            [
                OrderIntent(
                    intent_id='intent_crypto_buy_1',
                    account_id='paper-crypto-ledger',
                    asset_class='crypto',
                    symbol='SOL/USD',
                    side='BUY',
                    requested_quantity=2.0,
                    requested_price=50.0,
                    filled_quantity=2.0,
                    avg_fill_price=50.0,
                    status='FILLED',
                    execution_source='WATCHLIST_MONITOR_ENTRY',
                    context_json={'mode': 'PAPER', 'displayPair': 'SOL/USD', 'ohlcvPair': 'SOLUSD'},
                    submitted_at=entry_time,
                    first_fill_at=entry_time,
                    last_fill_at=entry_time,
                ),
                OrderIntent(
                    intent_id='intent_crypto_sell_1',
                    account_id='paper-crypto-ledger',
                    asset_class='crypto',
                    symbol='SOL/USD',
                    side='SELL',
                    requested_quantity=2.0,
                    requested_price=60.0,
                    filled_quantity=2.0,
                    avg_fill_price=60.0,
                    status='CLOSED',
                    execution_source='WATCHLIST_MONITOR_EXIT',
                    context_json={'mode': 'PAPER', 'displayPair': 'SOL/USD', 'ohlcvPair': 'SOLUSD', 'exitTrigger': 'profit_target'},
                    submitted_at=exit_time,
                    first_fill_at=exit_time,
                    last_fill_at=exit_time,
                ),
            ]
        )
        db.commit()

        def override_get_db():
            local_db = SessionFactory()
            try:
                yield local_db
            finally:
                local_db.close()

        app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(app)
            response = client.get('/api/trade-history')
        finally:
            app.dependency_overrides.clear()
            db.close()

        assert response.status_code == 200
        payload = response.json()
        assert payload['summary']['totalCount'] == 2
        assert payload['summary']['realizedPnl'] == 120.0
        assert payload['summary']['assetCounts']['stock'] == 1
        assert payload['summary']['assetCounts']['crypto'] == 1
        symbols = sorted(row['symbol'] for row in payload['rows'])
        assert symbols == ['AAPL', 'SOL/USD']
        rows_by_symbol = {row['symbol']: row for row in payload['rows']}
        crypto_row = rows_by_symbol['SOL/USD']
        assert crypto_row['buyQuantity'] == 2.0
        assert crypto_row['realizedPnl'] == 20.0
        assert crypto_row['exitTrigger'] == 'profit_target'
        stock_row = rows_by_symbol['AAPL']
        assert stock_row['buyTotal'] == 1000.0
        assert stock_row['sellTotal'] == 1100.0
        assert stock_row['realizedPnl'] == 100.0


def test_trade_history_filters_by_asset_mode_symbol_and_date(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        sold_at = datetime(2026, 4, 2, 15, 0, tzinfo=UTC)
        db.add(
            Trade(
                trade_id='trade_live_1',
                account_id='live-stock',
                ticker='MSFT',
                direction='LONG',
                strategy='trend_continuation',
                entry_time=sold_at - timedelta(days=1),
                entry_price=200.0,
                shares=5,
                entry_cost=1000.0,
                exit_time=sold_at,
                exit_price=210.0,
                exit_proceeds=1050.0,
                gross_pnl=50.0,
                net_pnl=50.0,
                duration_minutes=60,
                entry_reasoning={'mode': 'LIVE'},
            )
        )
        db.commit()

        def override_get_db():
            local_db = SessionFactory()
            try:
                yield local_db
            finally:
                local_db.close()

        app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(app)
            response = client.get(
                '/api/trade-history',
                params={
                    'asset_class': 'stock',
                    'mode': 'LIVE',
                    'symbol': 'MSF',
                    'date_from': '2026-04-02',
                    'date_to': '2026-04-02',
                },
            )
        finally:
            app.dependency_overrides.clear()
            db.close()

        assert response.status_code == 200
        payload = response.json()
        assert payload['summary']['totalCount'] == 1
        assert payload['rows'][0]['symbol'] == 'MSFT'
        assert payload['rows'][0]['mode'] == 'LIVE'
