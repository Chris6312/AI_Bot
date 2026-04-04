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
                entry_reasoning={
                    'mode': 'PAPER',
                    'executionSource': 'WATCHLIST_MONITOR_ENTRY',
                    'intentId': 'intent_stock_buy_1',
                    'strategySnapshot': {
                        'setupTemplate': 'pullback_reclaim',
                        'exitTemplate': 'first_failed_follow_through',
                        'bias': 'bullish',
                        'botTimeframes': ['5m', '15m'],
                    },
                    'technicalSnapshot': {
                        'currentPrice': 100.0,
                        'changePct': 1.25,
                        'sma5': 99.2,
                        'sma10': 98.7,
                        'signalStrength': 0.9,
                        'distanceFromSma10Pct': 1.3171,
                        'breakoutDistancePct': 0.4049,
                    },
                },
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
                    context_json={
                        'mode': 'PAPER',
                        'displayPair': 'SOL/USD',
                        'ohlcvPair': 'SOLUSD',
                        'strategySnapshot': {
                            'setupTemplate': 'pullback_reclaim',
                            'exitTemplate': 'profit_target',
                            'bias': 'bullish',
                            'botTimeframes': ['5m'],
                        },
                        'technicalSnapshot': {
                            'currentPrice': 50.0,
                            'changePct': 2.1,
                            'sma5': 49.5,
                            'sma10': 48.8,
                            'continuityOk': True,
                            'signalStrength': 1.0,
                            'distanceFromSma10Pct': 2.459,
                            'breakoutDistancePct': 0.0,
                        },
                    },
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
        assert payload['filters']['mode'] == 'ALL'
        symbols = sorted(row['symbol'] for row in payload['rows'])
        assert symbols == ['AAPL', 'SOL/USD']
        rows_by_symbol = {row['symbol']: row for row in payload['rows']}
        crypto_row = rows_by_symbol['SOL/USD']
        assert crypto_row['buyQuantity'] == 2.0
        assert crypto_row['realizedPnl'] == 20.0
        assert crypto_row['differenceAmount'] == 20.0
        assert crypto_row['exitTrigger'] == 'profit_target'
        assert crypto_row['boughtAtEt'].endswith('-04:00')
        stock_row = rows_by_symbol['AAPL']
        assert stock_row['buyTotal'] == 1000.0
        assert stock_row['sellTotal'] == 1100.0
        assert stock_row['priceDifference'] == 10.0
        assert stock_row['differenceAmount'] == 100.0
        assert stock_row['realizedPnl'] == 100.0
        assert stock_row['soldAtEt'].endswith('-04:00')
        assert stock_row['strategySnapshot']['setupTemplate'] == 'pullback_reclaim'
        assert stock_row['strategySnapshot']['botTimeframes'] == ['5m', '15m']
        assert stock_row['technicalSnapshot']['sma5'] == 99.2
        assert crypto_row['strategySnapshot']['setupTemplate'] == 'pullback_reclaim'
        assert crypto_row['technicalSnapshot']['continuityOk'] is True



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
        assert payload['summary']['dateRange']['fromEt'].startswith('2026-04-02T00:00:00')
        assert payload['summary']['dateRange']['toEt'].startswith('2026-04-02T23:59:59.999999')
        assert payload['rows'][0]['symbol'] == 'MSFT'
        assert payload['rows'][0]['mode'] == 'LIVE'



def test_trade_history_preserves_normalized_signal_fields(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        entry_time = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
        exit_time = entry_time + timedelta(minutes=45)
        db.add(
            Trade(
                trade_id='trade_stock_metrics_1',
                account_id='paper-stock',
                ticker='NVDA',
                direction='LONG',
                strategy='range_breakout',
                entry_time=entry_time,
                entry_price=101.0,
                shares=10,
                entry_cost=1010.0,
                exit_time=exit_time,
                exit_price=99.0,
                exit_proceeds=990.0,
                gross_pnl=-20.0,
                net_pnl=-20.0,
                duration_minutes=45,
                entry_reasoning={
                    'mode': 'PAPER',
                    'strategySnapshot': {'setupTemplate': 'range_breakout'},
                    'technicalSnapshot': {
                        'currentPrice': 101.0,
                        'sma10': 99.0,
                        'breakoutLevel': 100.0,
                        'signalStrength': 0.7,
                        'distanceFromSma10Pct': 2.0202,
                        'breakoutDistancePct': 1.0,
                    },
                },
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
            response = client.get('/api/trade-history')
        finally:
            app.dependency_overrides.clear()
            db.close()

        assert response.status_code == 200
        row = next(item for item in response.json()['rows'] if item['symbol'] == 'NVDA')
        assert row['technicalSnapshot']['signalStrength'] == 0.7
        assert row['technicalSnapshot']['distanceFromSma10Pct'] == 2.0202
        assert row['technicalSnapshot']['breakoutDistancePct'] == 1.0
