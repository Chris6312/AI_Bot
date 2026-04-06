from __future__ import annotations

import pytest
from datetime import UTC, datetime, timedelta

from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.watchlist_monitor_state import MONITOR_ONLY, PENDING_EVALUATION, WatchlistMonitorState
from app.services.kraken_service import crypto_ledger
from app.services.position_reconciliation import position_reconciliation_service
from app.services.watchlist_service import watchlist_service
from tests.test_phase4_watchlists import build_crypto_payload, build_session_factory, build_stock_payload


@pytest.fixture(autouse=True)
def mock_tradier_positions_for_reconciliation(monkeypatch):
    """
    Prevent the tests from hitting the live Tradier sandbox API
    during background reconciliation triggers.
    """
    monkeypatch.setattr('app.services.tradier_client.tradier_client.get_positions_snapshot', lambda mode=None, include_quotes=False: [])


def _reset_crypto_ledger() -> None:
    crypto_ledger.trades = []
    crypto_ledger.positions = {}
    crypto_ledger.balance = crypto_ledger.starting_balance

def test_startup_reconciliation_restores_crypto_ledger_from_filled_intents(tmp_path) -> None:
    _reset_crypto_ledger()
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['bot_payload']['symbols'][0]['symbol'] = 'TAO'
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['TAO']
        payload['ui_payload']['symbol_context'] = {'TAO': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        filled_at = datetime.now(UTC).replace(microsecond=0)
        db.add(
            OrderIntent(
                intent_id='intent_crypto_tao_buy',
                account_id='paper-crypto-ledger',
                asset_class='crypto',
                symbol='TAO/USD',
                side='BUY',
                requested_quantity=1.25,
                requested_price=420.0,
                filled_quantity=1.25,
                avg_fill_price=420.0,
                status='FILLED',
                execution_source='WATCHLIST_MONITOR_ENTRY',
                submitted_order_id='paper_1',
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

        assert summary['replayedTradeCount'] == 1
        assert summary['restoredPositionCount'] == 1
        assert summary['restoredSymbols'] == ['TAO/USD']
        assert summary['externalOpenSymbols'] == ['TAO', 'TAO/USD']
        assert 'TAO/USD' in crypto_ledger.positions
        assert round(float(crypto_ledger.positions['TAO/USD']['amount']), 8) == 1.25
        assert round(float(crypto_ledger.positions['TAO/USD']['total_cost']), 8) == 525.0
        assert len(crypto_ledger.trades) == 0
        restored_positions = crypto_ledger.get_positions()
        assert len(restored_positions) == 1
        assert restored_positions[0]['pair'] == 'TAO/USD'
        assert restored_positions[0]['entryTimeUtc'] == filled_at.isoformat()

    _reset_crypto_ledger()



def test_startup_reconciliation_clears_stale_crypto_open_position_guard(tmp_path) -> None:
    _reset_crypto_ledger()
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['bot_payload']['symbols'][0]['symbol'] = 'TAO'
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['TAO']
        payload['ui_payload']['symbol_context'] = {'TAO': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'TAO').one()
        monitor_row.latest_decision_state = MONITOR_ONLY
        monitor_row.latest_decision_reason = 'Open position exists; symbol is now managed under exit rules.'
        monitor_row.decision_context_json = {
            'entryExecution': {
                'action': 'SKIPPED',
                'reason': 'OPEN_POSITION_EXISTS',
            }
        }
        db.commit()

        observed_at = datetime.now(UTC).replace(microsecond=0)
        summary = position_reconciliation_service.reconcile_asset_class(db, asset_class='crypto', observed_at=observed_at)
        db.refresh(monitor_row)

        assert summary['restoredPositionCount'] == 0
        assert summary['clearedMonitorGuardCount'] == 1
        assert monitor_row.latest_decision_state == PENDING_EVALUATION
        assert 'cleared stale OPEN_POSITION_EXISTS guard' in str(monitor_row.latest_decision_reason)
        assert monitor_row.decision_context_json['entryExecution']['action'] == 'RECONCILED'
        assert monitor_row.decision_context_json['entryExecution']['reason'] == 'STALE_OPEN_POSITION_GUARD_CLEARED'

    _reset_crypto_ledger()



def test_stock_reconciliation_uses_same_service_entrypoint(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        broker_snapshot = {
            'AAPL': {
                'symbol': 'AAPL',
                'shares': 5,
                'avgPrice': 180.0,
                'currentPrice': 181.5,
                'marketValue': 907.5,
                'pnl': 7.5,
                'pnlPercent': 0.83,
            }
        }
        monkeypatch.setattr(watchlist_service, '_get_open_stock_broker_positions', lambda: broker_snapshot)

        observed_at = datetime.now(UTC).replace(microsecond=0)
        summary = position_reconciliation_service.reconcile_asset_class(db, asset_class='stock', observed_at=observed_at)

        rows = db.query(Position).filter(Position.is_open.is_(True), Position.ticker == 'AAPL').all()
        assert summary['mirrorSummary']['inserted'] == 1
        assert summary['externalOpenSymbols'] == ['AAPL']
        assert len(rows) == 1
        assert int(rows[0].shares or 0) == 5
        assert float(rows[0].avg_entry_price or 0.0) == 180.0


def test_startup_reconciliation_does_not_resurrect_closed_crypto_trade_history(tmp_path, monkeypatch) -> None:
    _reset_crypto_ledger()
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        filled_at = datetime.now(UTC).replace(microsecond=0)
        db.add_all(
            [
                OrderIntent(
                    intent_id='intent_crypto_tao_buy_hist',
                    account_id='paper-crypto-ledger',
                    asset_class='crypto',
                    symbol='TAO/USD',
                    side='BUY',
                    requested_quantity=2.0,
                    requested_price=300.0,
                    filled_quantity=2.0,
                    avg_fill_price=300.0,
                    status='CLOSED',
                    execution_source='WATCHLIST_MONITOR_ENTRY',
                    submitted_order_id='paper_hist_buy',
                    context_json={'ohlcvPair': 'TAOUSD'},
                    submitted_at=filled_at,
                    first_fill_at=filled_at,
                    last_fill_at=filled_at,
                ),
                OrderIntent(
                    intent_id='intent_crypto_tao_sell_hist',
                    account_id='paper-crypto-ledger',
                    asset_class='crypto',
                    symbol='TAO/USD',
                    side='SELL',
                    requested_quantity=2.0,
                    requested_price=320.0,
                    filled_quantity=2.0,
                    avg_fill_price=320.0,
                    status='CLOSED',
                    execution_source='WATCHLIST_MONITOR_EXIT',
                    submitted_order_id='paper_hist_sell',
                    context_json={'ohlcvPair': 'TAOUSD'},
                    submitted_at=filled_at + timedelta(minutes=5),
                    first_fill_at=filled_at + timedelta(minutes=5),
                    last_fill_at=filled_at + timedelta(minutes=5),
                ),
            ]
        )
        db.commit()

        summary = position_reconciliation_service.reconcile_asset_class(
            db,
            asset_class='crypto',
            observed_at=filled_at + timedelta(minutes=10),
        )

        assert summary['replayedTradeCount'] == 2
        assert summary['restoredPositionCount'] == 0
        assert summary['restoredSymbols'] == []
        assert crypto_ledger.positions == {}
        assert crypto_ledger.trades == []
        assert crypto_ledger.get_ledger()['trades'] == []

    _reset_crypto_ledger()


def test_stock_reconciliation_exposes_quantity_truth_with_pending_exit_reservations(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        db.add(
            Position(
                account_id='paper',
                ticker='AAPL',
                shares=10,
                avg_entry_price=180.0,
                current_price=181.5,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC).replace(microsecond=0),
                entry_reasoning={'intentId': 'intent-entry'},
                is_open=True,
                execution_id='intent-entry',
            )
        )
        db.commit()

        broker_snapshot = {
            'AAPL': {
                'symbol': 'AAPL',
                'shares': 8,
                'avgPrice': 180.0,
                'currentPrice': 181.5,
                'marketValue': 1452.0,
                'pnl': 12.0,
                'pnlPercent': 0.83,
            }
        }
        quantity_truth = position_reconciliation_service.get_stock_quantity_truth(
            db,
            symbol='AAPL',
            broker_positions=broker_snapshot,
            pending_orders=[{'remaining_quantity': 3}],
        )

        assert quantity_truth['dbOpenQuantity'] == 10
        assert quantity_truth['brokerQuantity'] == 8
        assert quantity_truth['pendingExitQuantity'] == 3
        assert quantity_truth['sellableQuantity'] == 5
        assert quantity_truth['quantityDelta'] == -2
        assert quantity_truth['driftDetected'] is True
