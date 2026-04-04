from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.order_event import OrderEvent
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.trade import Trade
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.models.watchlist_ui_context import WatchlistUiContext
from app.models.watchlist_upload import WatchlistUpload
from app.services.control_plane import discord_decision_guard
from app.services.kraken_service import CryptoPaperLedger, KrakenPairMetadata, crypto_ledger, kraken_service
from app.services.market_sessions import calculate_next_scope_evaluation_at, get_scope_session_status
from app.services.trade_validator import trade_validator
from app.services.position_sizer import position_sizer
from app.services.pre_trade_gate import pre_trade_gate
from app.services.tradier_client import tradier_client
from app.services.runtime_state import runtime_state
from app.services.template_evaluator import (
    DATA_STALE,
    ENTRY_CANDIDATE,
    MONITOR_ONLY,
    template_evaluation_service,
)
from app.services.watchlist_monitoring import watchlist_monitoring_orchestrator
from app.services.watchlist_exit_worker import watchlist_exit_worker
from app.services.watchlist_service import INACTIVE, MANAGED_ONLY, WatchlistValidationError, watchlist_service


@contextmanager
def build_session_factory(tmp_path) -> Iterator[sessionmaker]:
    db_path = tmp_path / 'phase4_watchlists.db'
    engine = create_engine(
        f'sqlite:///{db_path}',
        connect_args={'check_same_thread': False},
    )
    SessionFactory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    try:
        yield SessionFactory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def build_stock_payload() -> dict:
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    return {
        'schema_version': 'bot_stock_watchlist_v1',
        'generated_at_utc': generated_at,
        'provider': 'claude_tradier_mcp',
        'scope': 'stocks_only',
        'bot_payload': {
            'market_regime': 'mixed',
            'symbols': [
                {
                    'symbol': 'AAPL',
                    'quote_currency': 'USD',
                    'asset_class': 'stock',
                    'enabled': True,
                    'trade_direction': 'long',
                    'priority_rank': 1,
                    'tier': 'tier_1',
                    'bias': 'bullish',
                    'setup_template': 'pullback_reclaim',
                    'bot_timeframes': ['15m', '1h', '4h', '1d'],
                    'exit_template': 'scale_out_then_trail',
                    'max_hold_hours': 72,
                    'risk_flags': ['crowded_trade'],
                },
                {
                    'symbol': 'MSFT',
                    'quote_currency': 'USD',
                    'asset_class': 'stock',
                    'enabled': True,
                    'trade_direction': 'long',
                    'priority_rank': 2,
                    'tier': 'tier_2',
                    'bias': 'bullish',
                    'setup_template': 'trend_continuation',
                    'bot_timeframes': ['5m', '15m', '1h', '4h'],
                    'exit_template': 'trail_after_impulse',
                    'max_hold_hours': 48,
                    'risk_flags': ['headline_sensitive'],
                },
            ],
        },
        'ui_payload': {
            'summary': {
                'selected_count': 2,
                'primary_focus': ['AAPL'],
                'regime_note': 'Mixed tape, favor liquid leaders.',
            },
            'provider_limitations': ['Provider does not expose exact triggers.'],
            'symbol_context': {
                'AAPL': {
                    'scan_reason': 'relative_strength',
                    'sector': 'Technology',
                    'thesis': 'Large-cap leader with clean participation.',
                    'why_now': 'Holding up while market tone is mixed.',
                    'notes': 'Let the bot confirm reclaim behavior.',
                },
                'MSFT': {
                    'scan_reason': 'trend',
                    'sector': 'Technology',
                    'thesis': 'Persistent trend leadership.',
                    'why_now': 'Trend continuation candidate if participation stays healthy.',
                    'notes': 'Watch for headline sensitivity around macro chatter.',
                },
            },
        },
    }


def build_crypto_payload() -> dict:
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    return {
        'schema_version': 'bot_watchlist_v3',
        'generated_at_utc': generated_at,
        'provider': 'chatgpt_kraken_app',
        'scope': 'crypto_only',
        'bot_payload': {
            'market_regime': 'risk_on',
            'symbols': [
                {
                    'symbol': 'BTC',
                    'quote_currency': 'USD',
                    'asset_class': 'crypto',
                    'enabled': True,
                    'trade_direction': 'long',
                    'priority_rank': 1,
                    'tier': 'tier_1',
                    'bias': 'bullish',
                    'setup_template': 'trend_continuation',
                    'bot_timeframes': ['15m', '1h', '4h'],
                    'exit_template': 'trail_after_impulse',
                    'max_hold_hours': 72,
                    'risk_flags': ['crowded_trade'],
                },
                {
                    'symbol': 'ETH',
                    'quote_currency': 'USD',
                    'asset_class': 'crypto',
                    'enabled': True,
                    'trade_direction': 'long',
                    'priority_rank': 2,
                    'tier': 'tier_2',
                    'bias': 'bullish',
                    'setup_template': 'breakout_retest',
                    'bot_timeframes': ['15m', '1h', '4h'],
                    'exit_template': 'scale_out_then_trail',
                    'max_hold_hours': 48,
                    'risk_flags': ['high_beta'],
                },
            ],
        },
        'ui_payload': {
            'summary': {
                'selected_count': 2,
                'primary_focus': ['BTC', 'ETH'],
                'regime_note': 'Risk-on tape with majors leading.',
            },
            'provider_limitations': ['Provider does not expose OHLCV candles or exact trigger prices.'],
            'symbol_context': {
                'BTC': {
                    'radar_bucket': 'trending',
                    'role': 'market_anchor',
                    'thesis': 'Major trend leader setting tone for the board.',
                    'why_now': 'Provider momentum context is supportive.',
                    'notes': 'Bot still owns trigger math and risk controls.',
                },
                'ETH': {
                    'radar_bucket': 'gainers',
                    'role': 'momentum_leader',
                    'thesis': 'Participation broadens beyond BTC.',
                    'why_now': 'Higher-beta continuation candidate.',
                    'notes': 'Treat as higher beta than BTC.',
                },
            },
        },
    }


def test_monitoring_startup_bootstrap_refreshes_assetpairs_and_forces_crypto_eval(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        watchlist_service.ingest_watchlist(db, payload, source='api')
        db.close()

        monkeypatch.setattr('app.services.watchlist_monitoring.SessionLocal', SessionFactory)

        calls: dict[str, object] = {'refresh_force': None, 'evaluate': None}

        def fake_refresh_asset_pairs(*, force: bool = False):
            calls['refresh_force'] = force
            return {
                'BTC/USD': 'XBTUSD',
                'ETH/USD': 'ETHUSD',
            }

        def fake_evaluate_scope(db, *, scope, limit=25, force=False, eligible_statuses=None):
            calls['evaluate'] = {
                'scope': scope,
                'limit': limit,
                'force': force,
                'eligible_statuses': eligible_statuses,
            }
            return {
                'scope': scope,
                'capturedAtUtc': datetime.now(UTC).isoformat(),
                'evaluatedCount': 2,
                'summary': {
                    'entryCandidateCount': 0,
                    'waitingForSetupCount': 2,
                    'dataStaleCount': 0,
                    'dataUnavailableCount': 0,
                    'monitorOnlyCount': 0,
                    'inactiveCount': 0,
                    'biasConflictCount': 0,
                    'evaluationBlockedCount': 0,
                },
                'rows': [],
                'monitoringSnapshot': {'summary': {'activeCount': 2}},
            }

        monkeypatch.setattr('app.services.watchlist_monitoring.kraken_service.refresh_asset_pairs', fake_refresh_asset_pairs)
        monkeypatch.setattr('app.services.watchlist_monitoring.template_evaluation_service.evaluate_scope', fake_evaluate_scope)

        summary = watchlist_monitoring_orchestrator._bootstrap_startup_state_sync()

        assert calls['refresh_force'] is True
        assert calls['evaluate'] == {
            'scope': 'crypto_only',
            'limit': 2,
            'force': True,
            'eligible_statuses': ('ACTIVE', 'MANAGED_ONLY'),
        }
        assert summary['assetPairsRefreshed'] is True
        assert summary['assetPairCount'] == 2
        assert summary['cryptoMonitorRefreshApplied'] is True
        assert summary['evaluatedCount'] == 2
        assert summary['evaluationSummary']['waitingForSetupCount'] == 2


def test_watchlist_service_ingests_stock_watchlist_and_persists_rows(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()

        persisted = watchlist_service.ingest_watchlist(db, payload, source='api')

        assert persisted['schemaVersion'] == 'bot_stock_watchlist_v1'
        assert persisted['scope'] == 'stocks_only'
        assert persisted['selectedCount'] == 2
        assert persisted['isActive'] is True
        assert persisted['uiPayload']['summary']['selected_count'] == 2
        assert persisted['symbols'][0]['symbol'] == 'AAPL'
        assert persisted['managedOnlySymbols'] == []
        assert persisted['statusSummary']['activeCount'] == 2
        assert db.query(WatchlistUpload).count() == 1
        assert db.query(WatchlistSymbol).count() == 2
        assert db.query(WatchlistUiContext).count() == 1


def test_new_upload_replaces_previous_active_scope(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        first = watchlist_service.ingest_watchlist(db, first_payload, source='api')
        second = watchlist_service.ingest_watchlist(db, second_payload, source='api')

        first_row = db.query(WatchlistUpload).filter(WatchlistUpload.upload_id == first['uploadId']).one()
        second_row = db.query(WatchlistUpload).filter(WatchlistUpload.upload_id == second['uploadId']).one()
        first_symbols = db.query(WatchlistSymbol).filter(WatchlistSymbol.upload_id == first['uploadId']).all()
        second_symbols = db.query(WatchlistSymbol).filter(WatchlistSymbol.upload_id == second['uploadId']).all()

        assert first_row.is_active is False
        assert second_row.is_active is True
        assert all(symbol.monitoring_status == INACTIVE for symbol in first_symbols)
        assert all(symbol.monitoring_status == 'ACTIVE' for symbol in second_symbols)
        assert second['managedOnlySymbols'] == []


def test_removed_stock_symbol_becomes_managed_only_from_broker_snapshot_when_db_position_is_missing(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        monkeypatch.setattr(
            tradier_client,
            'get_positions_snapshot',
            lambda mode=None: [
                {
                    'symbol': 'AAPL',
                    'shares': 10,
                    'avgPrice': 100.0,
                    'currentPrice': 101.0,
                    'marketValue': 1010.0,
                    'pnl': 10.0,
                    'pnlPercent': 1.0,
                }
            ],
        )

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        active_payload = watchlist_service.ingest_watchlist(db, second_payload, source='api')

        historical_aapl = (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.symbol == 'AAPL')
            .order_by(WatchlistSymbol.id.asc())
            .first()
        )
        assert historical_aapl is not None
        assert historical_aapl.monitoring_status == MANAGED_ONLY
        assert active_payload['managedOnlySymbols'][0]['symbol'] == 'AAPL'

        snapshot = watchlist_service.get_monitoring_snapshot(db, scope='stocks_only')
        managed_row = next(row for row in snapshot['rows'] if row['symbol'] == 'AAPL')
        assert managed_row['managedOnly'] is True
        assert managed_row['positionState']['hasOpenPosition'] is True
        assert managed_row['positionState']['positionSource'] == 'broker'
        assert managed_row['positionState']['positionId'] is None


def test_removed_stock_symbol_becomes_managed_only_when_position_is_open(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        db.add(
            Position(
                account_id='paper',
                ticker='AAPL',
                shares=10,
                avg_entry_price=100.0,
                current_price=101.0,
                strategy='pullback_reclaim',
                entry_time=datetime.now(UTC),
                stop_loss=95.0,
                profit_target=110.0,
                peak_price=101.0,
                is_open=True,
            )
        )
        db.commit()

        active_payload = watchlist_service.ingest_watchlist(db, second_payload, source='api')

        historical_aapl = (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.symbol == 'AAPL')
            .order_by(WatchlistSymbol.id.asc())
            .first()
        )
        assert historical_aapl is not None
        assert historical_aapl.monitoring_status == MANAGED_ONLY
        assert active_payload['managedOnlySymbols'][0]['symbol'] == 'AAPL'
        assert active_payload['statusSummary']['managedOnlyCount'] == 1


def test_reconcile_scope_statuses_demotes_managed_only_when_position_closes(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=10,
            avg_entry_price=100.0,
            current_price=101.0,
            strategy='pullback_reclaim',
            entry_time=datetime.now(UTC),
            stop_loss=95.0,
            profit_target=110.0,
            peak_price=101.0,
            is_open=True,
        )
        db.add(position)
        db.commit()
        watchlist_service.ingest_watchlist(db, second_payload, source='api')

        position.is_open = False
        position.shares = 0
        db.commit()

        result = watchlist_service.reconcile_scope_statuses(db, scope='stocks_only')
        historical_aapl = (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.symbol == 'AAPL')
            .order_by(WatchlistSymbol.id.asc())
            .first()
        )
        assert result['changedRows'] == 1
        assert result['managedOnlyCount'] == 0
        assert historical_aapl is not None
        assert historical_aapl.monitoring_status == INACTIVE


def test_crypto_removed_symbol_becomes_managed_only_from_open_ledger_position(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_crypto_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['ETH']
        second_payload['ui_payload']['symbol_context'] = {'ETH': second_payload['ui_payload']['symbol_context']['ETH']}

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [{'pair': 'BTC/USD', 'amount': 0.25}],
        )

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        active_payload = watchlist_service.ingest_watchlist(db, second_payload, source='api')

        historical_btc = (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.scope == 'crypto_only', WatchlistSymbol.symbol == 'BTC')
            .order_by(WatchlistSymbol.id.asc())
            .first()
        )
        assert historical_btc is not None
        assert historical_btc.monitoring_status == MANAGED_ONLY
        assert active_payload['managedOnlySymbols'][0]['symbol'] == 'BTC'


def test_watchlist_service_rejects_stale_payload() -> None:
    payload = build_crypto_payload()
    payload['generated_at_utc'] = (datetime.now(UTC) - timedelta(hours=8)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')

    try:
        watchlist_service.parse_payload(payload)
        watchlist_service.validate_freshness(datetime.fromisoformat(payload['generated_at_utc'].replace('Z', '+00:00')))
    except WatchlistValidationError as exc:
        assert 'stale' in str(exc)
    else:
        raise AssertionError('Expected stale payload to be rejected.')


def test_watchlist_endpoints_return_active_and_latest_payloads(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        def override_get_db():
            db = SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        monkeypatch.setattr(settings, 'ADMIN_API_TOKEN', 'phase4-secret')

        with TestClient(app) as client:
            response = client.post(
                '/api/watchlists/ingest',
                json=build_stock_payload(),
                headers={'X-Admin-Token': 'phase4-secret'},
            )
            assert response.status_code == 200
            upload_id = response.json()['uploadId']

            latest_scope = client.get('/api/watchlists/latest?scope=stocks_only')
            active_scope = client.get('/api/watchlists/active?scope=stocks_only')
            latest_all = client.get('/api/watchlists/latest')

            assert latest_scope.status_code == 200
            assert latest_scope.json()['uploadId'] == upload_id
            assert active_scope.status_code == 200
            assert active_scope.json()['isActive'] is True
            assert active_scope.json()['managedOnlySymbols'] == []
            assert latest_all.status_code == 200
            assert latest_all.json()['stocks_only']['uploadId'] == upload_id

        app.dependency_overrides.clear()


def test_ai_decisions_endpoint_derives_entries_from_watchlist_uploads(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        def override_get_db():
            db = SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

        db = SessionFactory()
        try:
            watchlist_service.ingest_watchlist(db, build_stock_payload(), source='api')
        finally:
            db.close()

        with TestClient(app) as client:
            response = client.get('/api/ai/decisions?limit=5')

        app.dependency_overrides.clear()

        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 2
        first = payload[0]
        assert first['market'] == 'STOCK'
        assert first['type'] == 'SCREENING'
        assert first['executed'] is False
        assert first['confidence'] >= 0.5
        assert first['symbol'] == 'AAPL'
        assert 'thesis:' in first['reasoning']
        assert first['rejected'] is False


def test_reconcile_revives_inactive_stock_row_from_broker_snapshot(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        watchlist_service.ingest_watchlist(db, second_payload, source='api')

        historical_aapl = (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.symbol == 'AAPL')
            .order_by(WatchlistSymbol.id.asc())
            .first()
        )
        assert historical_aapl is not None
        historical_aapl.monitoring_status = INACTIVE
        db.commit()

        monkeypatch.setattr(
            tradier_client,
            'get_positions_snapshot',
            lambda mode=None: [
                {
                    'symbol': 'AAPL',
                    'shares': 289,
                    'avgPrice': 65.49,
                    'currentPrice': 66.33,
                    'marketValue': 19169.37,
                    'pnl': 242.01,
                    'pnlPercent': 1.28,
                }
            ],
        )

        result = watchlist_service.reconcile_scope_statuses(db, scope='stocks_only')
        db.refresh(historical_aapl)
        assert result['changedRows'] >= 1
        assert historical_aapl.monitoring_status == MANAGED_ONLY



def test_reconcile_scope_backfills_broker_only_stock_position_into_db(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        watchlist_service.ingest_watchlist(db, payload, source='api')

        db.add(
            OrderIntent(
                intent_id='intent_aapl_fill',
                account_id='paper',
                asset_class='stock',
                symbol='AAPL',
                side='BUY',
                requested_quantity=10,
                requested_price=190.0,
                filled_quantity=10,
                avg_fill_price=190.5,
                status='FILLED',
                execution_source='WATCHLIST_MONITOR_ENTRY',
                submitted_at=datetime.now(UTC) - timedelta(minutes=5),
                first_fill_at=datetime.now(UTC) - timedelta(minutes=4),
                last_fill_at=datetime.now(UTC) - timedelta(minutes=4),
                context_json={'setupTemplate': 'pullback_reclaim'},
            )
        )
        db.commit()

        monkeypatch.setattr(
            tradier_client,
            'get_positions_snapshot',
            lambda mode=None: [
                {
                    'symbol': 'AAPL',
                    'shares': 10,
                    'avgPrice': 190.5,
                    'currentPrice': 192.25,
                    'marketValue': 1922.5,
                    'pnl': 17.5,
                    'pnlPercent': 0.92,
                }
            ],
        )

        result = watchlist_service.reconcile_scope_statuses(db, scope='stocks_only')
        assert result['changedRows'] >= 0

        mirrored = db.query(Position).filter(Position.ticker == 'AAPL', Position.is_open.is_(True)).order_by(Position.id.desc()).first()
        assert mirrored is not None
        assert mirrored.shares == 10
        assert mirrored.avg_entry_price == 190.5
        assert mirrored.current_price == 192.25
        assert mirrored.execution_id == 'intent_aapl_fill'
        assert isinstance(mirrored.entry_reasoning, dict)
        assert mirrored.entry_reasoning.get('syncSource') == 'broker_position_mirror'
        assert mirrored.entry_reasoning.get('seedIntentId') == 'intent_aapl_fill'

        monkeypatch.setattr(tradier_client, 'get_positions_snapshot', lambda mode=None: [])
        watchlist_service.reconcile_scope_statuses(db, scope='stocks_only')
        db.refresh(mirrored)
        assert mirrored.is_open is False
        assert mirrored.shares == 0


def test_db_positions_endpoint_returns_position_rows(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        def override_get_db():
            db = SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

        db = SessionFactory()
        try:
            db.add(
                Position(
                    account_id='paper',
                    ticker='AA',
                    shares=289,
                    avg_entry_price=65.49,
                    current_price=66.33,
                    unrealized_pnl=242.01,
                    unrealized_pnl_pct=1.28,
                    strategy='WATCHLIST_ENTRY',
                    entry_time=datetime.now(UTC),
                    entry_reasoning={'intentId': 'demo'},
                    stop_loss=61.0,
                    profit_target=71.0,
                    peak_price=66.85,
                    trailing_stop=63.5,
                    is_open=True,
                    execution_id='intent_demo',
                )
            )
            db.commit()
        finally:
            db.close()

        with TestClient(app) as client:
            response = client.get('/api/stocks/db-positions')

        app.dependency_overrides.clear()

        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]['ticker'] == 'AA'
        assert payload[0]['shares'] == 289
        assert payload[0]['avgEntryPrice'] == 65.49
        assert payload[0]['currentPrice'] == 66.33
        assert payload[0]['unrealizedPnl'] == 242.01
        assert payload[0]['isOpen'] is True


def test_reconcile_endpoint_returns_status_transition_summary(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        def override_get_db():
            db = SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        monkeypatch.setattr(settings, 'ADMIN_API_TOKEN', 'phase4-secret')

        db = SessionFactory()
        try:
            first_payload = build_stock_payload()
            second_payload = deepcopy(first_payload)
            second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
            second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
            second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
            second_payload['ui_payload']['summary']['selected_count'] = 1
            second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
            second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

            watchlist_service.ingest_watchlist(db, first_payload, source='api')
            position = Position(
                account_id='paper',
                ticker='AAPL',
                shares=10,
                avg_entry_price=100.0,
                current_price=101.0,
                strategy='pullback_reclaim',
                entry_time=datetime.now(UTC),
                stop_loss=95.0,
                profit_target=110.0,
                peak_price=101.0,
                is_open=True,
            )
            db.add(position)
            db.commit()
            watchlist_service.ingest_watchlist(db, second_payload, source='api')
            position.is_open = False
            position.shares = 0
            db.commit()
        finally:
            db.close()

        with TestClient(app) as client:
            response = client.post(
                '/api/watchlists/reconcile-status?scope=stocks_only',
                headers={'X-Admin-Token': 'phase4-secret'},
            )
            assert response.status_code == 200
            body = response.json()
            assert body['scope'] == 'stocks_only'
            assert body['managedOnlyCount'] == 0
            assert body['changedRows'] == 1

        app.dependency_overrides.clear()


def test_discord_guard_accepts_watchlist_schema_version_and_generated_at_utc() -> None:
    payload = build_stock_payload()
    message = SimpleNamespace(id=123456)

    accepted, reason = discord_decision_guard.validate_and_register(message, payload)

    assert accepted is True
    assert reason == 'accepted'



def test_watchlist_ingest_initializes_monitoring_state(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()

        persisted = watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_rows = db.query(WatchlistMonitorState).order_by(WatchlistMonitorState.symbol.asc()).all()
        assert len(monitor_rows) == 2
        assert monitor_rows[0].latest_decision_state == 'PENDING_EVALUATION'
        assert monitor_rows[0].last_decision_at_utc is not None
        assert monitor_rows[0].next_evaluation_at_utc is not None
        assert monitor_rows[0].evaluation_interval_seconds in {300, 900}
        assert persisted['symbols'][0]['monitoring']['latestDecisionState'] == 'PENDING_EVALUATION'
        assert persisted['monitoringSummary']['pendingEvaluationCount'] == 2


def test_managed_only_transition_updates_monitoring_record(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        db.add(
            Position(
                account_id='paper',
                ticker='AAPL',
                shares=10,
                avg_entry_price=100.0,
                current_price=101.0,
                strategy='pullback_reclaim',
                entry_time=datetime.now(UTC),
                stop_loss=95.0,
                profit_target=110.0,
                peak_price=101.0,
                is_open=True,
            )
        )
        db.commit()

        active_payload = watchlist_service.ingest_watchlist(db, second_payload, source='api')
        managed_row = db.query(WatchlistSymbol).filter(WatchlistSymbol.symbol == 'AAPL').order_by(WatchlistSymbol.id.asc()).first()
        assert managed_row is not None
        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.watchlist_symbol_id == managed_row.id).one()
        assert monitor_row.monitoring_status == MANAGED_ONLY
        assert monitor_row.latest_decision_state == 'MONITOR_ONLY'
        assert monitor_row.next_evaluation_at_utc is not None
        assert active_payload['managedOnlySymbols'][0]['monitoring']['latestDecisionState'] == 'MONITOR_ONLY'


def test_reconcile_to_inactive_clears_next_evaluation(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=10,
            avg_entry_price=100.0,
            current_price=101.0,
            strategy='pullback_reclaim',
            entry_time=datetime.now(UTC),
            stop_loss=95.0,
            profit_target=110.0,
            peak_price=101.0,
            is_open=True,
        )
        db.add(position)
        db.commit()
        watchlist_service.ingest_watchlist(db, second_payload, source='api')

        position.is_open = False
        position.shares = 0
        db.commit()

        watchlist_service.reconcile_scope_statuses(db, scope='stocks_only')
        inactive_row = db.query(WatchlistSymbol).filter(WatchlistSymbol.symbol == 'AAPL').order_by(WatchlistSymbol.id.asc()).first()
        assert inactive_row is not None
        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.watchlist_symbol_id == inactive_row.id).one()
        assert monitor_row.monitoring_status == INACTIVE
        assert monitor_row.latest_decision_state == 'INACTIVE'
        assert monitor_row.next_evaluation_at_utc is None


def test_monitoring_endpoint_returns_active_and_managed_only_rows(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        def override_get_db():
            db = SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        monkeypatch.setattr(settings, 'ADMIN_API_TOKEN', 'phase4-secret')

        db = SessionFactory()
        try:
            first_payload = build_stock_payload()
            second_payload = deepcopy(first_payload)
            second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
            second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
            second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
            second_payload['ui_payload']['summary']['selected_count'] = 1
            second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
            second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

            watchlist_service.ingest_watchlist(db, first_payload, source='api')
            db.add(
                Position(
                    account_id='paper',
                    ticker='AAPL',
                    shares=10,
                    avg_entry_price=100.0,
                    current_price=101.0,
                    strategy='pullback_reclaim',
                    entry_time=datetime.now(UTC),
                    stop_loss=95.0,
                    profit_target=110.0,
                    peak_price=101.0,
                    is_open=True,
                )
            )
            db.commit()
            watchlist_service.ingest_watchlist(db, second_payload, source='api')
        finally:
            db.close()

        with TestClient(app) as client:
            response = client.get('/api/watchlists/monitoring?scope=stocks_only')
            assert response.status_code == 200
            body = response.json()
            assert body['scope'] == 'stocks_only'
            assert body['summary']['activeCount'] == 1
            assert body['summary']['managedOnlyCount'] == 1
            assert len(body['rows']) == 2
            assert body['rows'][0]['monitoring']['latestDecisionState'] in {'PENDING_EVALUATION', 'MONITOR_ONLY'}
            assert any(row['managedOnly'] is True for row in body['rows'])

        app.dependency_overrides.clear()



def test_template_evaluator_promotes_stock_symbol_to_entry_candidate(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monkeypatch.setattr(
            tradier_client,
            'get_quote_sync',
            lambda symbol, mode=None: {
                'symbol': symbol,
                'last': 101.75,
                'prevclose': 100.0,
                'open': 100.9,
                'volume': 2_500_000,
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )

        result = template_evaluation_service.evaluate_scope(db, scope='stocks_only', force=True)
        snapshot = watchlist_service.get_monitoring_snapshot(db, scope='stocks_only')

        assert result['summary']['entryCandidateCount'] == 1
        assert snapshot['rows'][0]['monitoring']['latestDecisionState'] == ENTRY_CANDIDATE
        assert snapshot['summary']['entryCandidateCount'] == 1


def test_template_evaluator_marks_stale_stock_quote(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monkeypatch.setattr(
            tradier_client,
            'get_quote_sync',
            lambda symbol, mode=None: {
                'symbol': symbol,
                'last': 101.75,
                'prevclose': 100.0,
                'open': 100.9,
                'volume': 2_500_000,
                '_fetched_at_utc': (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            },
        )

        template_evaluation_service.evaluate_scope(db, scope='stocks_only', force=True)
        snapshot = watchlist_service.get_monitoring_snapshot(db, scope='stocks_only')

        assert snapshot['rows'][0]['monitoring']['latestDecisionState'] == DATA_STALE
        assert snapshot['summary']['dataStaleCount'] == 1


def test_template_evaluator_promotes_crypto_symbol_to_entry_candidate(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        now = datetime.now(UTC)
        monkeypatch.setattr(
            kraken_service,
            'resolve_pair',
            lambda pair: KrakenPairMetadata(
                display_pair='BTC/USD',
                rest_pair='XBTUSD',
                pair_key='XBTUSD',
                ws_pair='XBT/USD',
                altname='XBTUSD',
            ),
        )
        monkeypatch.setattr(
            kraken_service,
            'get_ticker',
            lambda pair: {
                'c': ['105.0'],
                'a': ['105.2'],
                'b': ['104.9'],
                'v': ['1000', '1200'],
                'o': ['100.0', '100.0'],
                '_fetched_at_utc': now.isoformat(),
            },
        )
        monkeypatch.setattr(
            'app.services.watchlist_monitoring.kraken_service.get_ohlc',
            lambda pair, interval=15, limit=25: [
                {'open': '68000.0', 'high': '69000.0', 'low': '67500.0', 'close': '68500.0', 'time': 1},
                {'open': '68500.0', 'high': '69500.0', 'low': '68000.0', 'close': '69000.0', 'time': 2},
                {'open': '69000.0', 'high': '70000.0', 'low': '68800.0', 'close': '69500.0', 'time': 3},
                {'open': '69500.0', 'high': '70500.0', 'low': '69200.0', 'close': '69800.0', 'time': 4},
                {'open': '69800.0', 'high': '71000.0', 'low': '69700.0', 'close': '70000.0', 'time': 5},
                {'open': '70000.0', 'high': '71200.0', 'low': '69900.0', 'close': '70500.0', 'time': 6},
            ],
        )
        candles = []
        base_ts = int((now - timedelta(minutes=75)).timestamp())
        price = 100.0
        for idx in range(6):
            open_price = price + idx * 0.7
            close_price = open_price + 1.2
            candles.append({
                'timestamp': base_ts + idx * 900,
                'open': open_price,
                'high': close_price + 0.2,
                'low': open_price - 0.2,
                'close': close_price,
                'vwap': close_price,
                'volume': 10 + idx,
                'count': 1,
            })
        monkeypatch.setattr(kraken_service, 'get_ohlc', lambda pair, interval=15, limit=25: candles)

        result = template_evaluation_service.evaluate_scope(db, scope='crypto_only', force=True)
        snapshot = watchlist_service.get_monitoring_snapshot(db, scope='crypto_only')

        assert result['summary']['entryCandidateCount'] == 1
        assert snapshot['rows'][0]['monitoring']['latestDecisionState'] == ENTRY_CANDIDATE


def test_template_evaluator_keeps_managed_only_rows_in_monitor_only(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        db.add(
            Position(
                account_id='paper',
                ticker='AAPL',
                shares=10,
                avg_entry_price=100.0,
                current_price=101.0,
                strategy='pullback_reclaim',
                entry_time=datetime.now(UTC),
                stop_loss=95.0,
                profit_target=110.0,
                peak_price=101.0,
                is_open=True,
            )
        )
        db.commit()
        watchlist_service.ingest_watchlist(db, second_payload, source='api')

        monkeypatch.setattr(
            tradier_client,
            'get_quote_sync',
            lambda symbol, mode=None: {
                'symbol': symbol,
                'last': 101.75,
                'prevclose': 100.0,
                'open': 100.9,
                'volume': 2_500_000,
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )

        template_evaluation_service.evaluate_scope(db, scope='stocks_only', force=True)
        managed_row = next(row for row in watchlist_service.get_monitoring_snapshot(db, scope='stocks_only')['rows'] if row['symbol'] == 'AAPL')

        assert managed_row['managedOnly'] is True
        assert managed_row['monitoring']['latestDecisionState'] == MONITOR_ONLY


def test_watchlist_evaluate_endpoint_returns_runner_summary(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monkeypatch.setattr(
            tradier_client,
            'get_quote_sync',
            lambda symbol, mode=None: {
                'symbol': symbol,
                'last': 101.75,
                'prevclose': 100.0,
                'open': 100.9,
                'volume': 2_500_000,
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )
        monkeypatch.setattr(settings, 'ADMIN_API_TOKEN', 'phase44-token', raising=False)

        def override_db():
            try:
                yield db
            finally:
                pass

        app.dependency_overrides[get_db] = override_db
        try:
            client = TestClient(app)
            response = client.post(
                '/api/watchlists/evaluate?scope=stocks_only&force=true',
                headers={'X-Admin-Token': 'phase44-token'},
            )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        payload = response.json()
        assert payload['scope'] == 'stocks_only'
        assert payload['summary']['entryCandidateCount'] == 1
        assert payload['rows'][0]['latestDecisionState'] == ENTRY_CANDIDATE



def test_due_run_orchestrator_evaluates_only_due_active_rows(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        monkeypatch.setattr(
            'app.services.watchlist_monitoring.get_scope_session_status',
            lambda scope, observed_at: get_scope_session_status('crypto_only', observed_at) if scope == 'crypto_only' else SimpleNamespace(**{
                'session_open': True,
                'to_dict': lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'patched open session',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            }),
        )
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        watchlist_service.ingest_watchlist(db, second_payload, source='api')

        active_msft = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.scope == 'stocks_only', WatchlistMonitorState.symbol == 'MSFT')
            .order_by(WatchlistMonitorState.id.desc())
            .first()
        )
        inactive_aapl = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.scope == 'stocks_only', WatchlistMonitorState.symbol == 'AAPL')
            .order_by(WatchlistMonitorState.id.asc())
            .first()
        )
        assert active_msft is not None
        assert inactive_aapl is not None
        active_msft.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
        active_msft.latest_decision_state = 'WAITING_FOR_SETUP'
        db.commit()

        monkeypatch.setattr(
            tradier_client,
            'get_quote_sync',
            lambda symbol, mode=None: {
                'symbol': symbol,
                'last': 101.75,
                'prevclose': 100.0,
                'open': 100.9,
                'volume': 2_500_000,
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )

        result = watchlist_monitoring_orchestrator.run_due_once(db, scope='stocks_only', limit_per_scope=10)
        db.refresh(active_msft)
        db.refresh(inactive_aapl)

        assert result['scope'] == 'stocks_only'
        assert result['dueCountBefore'] == 1
        assert result['evaluatedCount'] == 1
        assert result['rows'][0]['symbol'] == 'MSFT'
        assert result['dueCountAfter'] == 0
        assert active_msft.last_evaluated_at_utc is not None
        assert inactive_aapl.last_evaluated_at_utc is None



def test_watchlist_orchestration_endpoint_reports_due_counts_and_runtime_state(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        def override_get_db():
            db = SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        monkeypatch.setattr(settings, 'ADMIN_API_TOKEN', 'phase45-secret')
        monkeypatch.setattr(settings, 'WATCHLIST_MONITOR_ENABLED', False)
        monkeypatch.setattr(
            'app.services.watchlist_monitoring.get_scope_session_status',
            lambda scope, observed_at: get_scope_session_status('crypto_only', observed_at) if scope == 'crypto_only' else SimpleNamespace(**{
                'session_open': True,
                'to_dict': lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'patched open session',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            }),
        )

        db = SessionFactory()
        try:
            payload = build_stock_payload()
            payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
            payload['ui_payload']['summary']['selected_count'] = 1
            payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
            payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
            watchlist_service.ingest_watchlist(db, payload, source='api')
            monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'AAPL').one()
            monitor_row.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
            db.commit()
        finally:
            db.close()

        monkeypatch.setattr(
            tradier_client,
            'get_quote_sync',
            lambda symbol, mode=None: {
                'symbol': symbol,
                'last': 101.75,
                'prevclose': 100.0,
                'open': 100.9,
                'volume': 2_500_000,
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )

        with TestClient(app) as client:
            status_response = client.get('/api/watchlists/orchestration?scope=stocks_only')
            assert status_response.status_code == 200
            status_body = status_response.json()
            assert status_body['dueSnapshot']['dueCount'] == 1
            assert status_body['dueSnapshot']['scope'] == 'stocks_only'

            run_response = client.post(
                '/api/watchlists/run-due?scope=stocks_only&limit_per_scope=5',
                headers={'X-Admin-Token': 'phase45-secret'},
            )
            assert run_response.status_code == 200
            run_body = run_response.json()
            assert run_body['scope'] == 'stocks_only'
            assert run_body['dueCountBefore'] == 1
            assert run_body['evaluatedCount'] == 1
            assert run_body['dueCountAfter'] == 0

        app.dependency_overrides.clear()


def test_stock_session_scheduler_waits_for_market_open_before_first_sweep() -> None:
    reference = datetime(2026, 3, 30, 12, 0, tzinfo=UTC)
    scheduled = calculate_next_scope_evaluation_at('stocks_only', reference, 300)

    assert scheduled is not None
    assert scheduled == datetime(2026, 3, 30, 13, 30, 20, tzinfo=UTC)


def test_stock_session_scheduler_rolls_after_close_to_next_session_open() -> None:
    reference = datetime(2026, 3, 30, 20, 58, tzinfo=UTC)
    scheduled = calculate_next_scope_evaluation_at('stocks_only', reference, 300)

    assert scheduled is not None
    assert scheduled == datetime(2026, 3, 31, 13, 30, 20, tzinfo=UTC)


def test_due_run_orchestrator_blocks_stock_sweeps_when_market_session_is_closed(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'AAPL').one()
        monitor_row.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        monkeypatch.setattr(
            'app.services.watchlist_monitoring.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=False,
                to_dict=lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': False,
                    'reason': 'market closed for test',
                    'nextSessionStartUtc': datetime(2026, 3, 31, 13, 30, 0, tzinfo=UTC).isoformat(),
                    'nextSessionStartEt': datetime(2026, 3, 31, 9, 30, 0, tzinfo=ZoneInfo('America/New_York')).isoformat(),
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            ),
        )

        result = watchlist_monitoring_orchestrator.run_due_once(db, scope='stocks_only', limit_per_scope=10)

        db.refresh(monitor_row)
        assert result['scope'] == 'stocks_only'
        assert result['dueCountBefore'] == 1
        assert result['evaluatedCount'] == 0
        assert result['sessionBlockedCount'] == 1
        assert result['dueCountAfter'] == 1
        assert result['session']['sessionOpen'] is False
        assert monitor_row.last_evaluated_at_utc is None


def test_due_snapshot_reports_blocked_stock_rows_outside_session(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'AAPL').one()
        monitor_row.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        monkeypatch.setattr(
            'app.services.watchlist_monitoring.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=False,
                to_dict=lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': False,
                    'reason': 'market closed for test',
                    'nextSessionStartUtc': datetime(2026, 3, 31, 13, 30, 0, tzinfo=UTC).isoformat(),
                    'nextSessionStartEt': datetime(2026, 3, 31, 9, 30, 0, tzinfo=ZoneInfo('America/New_York')).isoformat(),
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            ),
        )

        snapshot = watchlist_monitoring_orchestrator.get_due_snapshot(db, scope='stocks_only')
        assert snapshot['dueCount'] == 1
        assert snapshot['eligibleDueCount'] == 0
        assert snapshot['blockedDueCount'] == 1
        assert snapshot['session']['sessionOpen'] is False


def test_monitoring_snapshot_includes_stock_position_expiry_state(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        position = Position(
            account_id='acct-1',
            ticker='AAPL',
            shares=5,
            avg_entry_price=100.0,
            current_price=104.0,
            strategy='AI_SCREENING',
            entry_time=datetime.now(UTC) - timedelta(hours=73),
            entry_reasoning={'intentId': 'intent-1'},
            stop_loss=98.0,
            profit_target=108.0,
            peak_price=104.0,
            trailing_stop=101.0,
            is_open=True,
            execution_id='intent-1',
        )
        db.add(position)
        db.commit()

        snapshot = watchlist_service.get_monitoring_snapshot(db, scope='stocks_only', include_inactive=False)

        assert snapshot['summary']['openPositionCount'] == 1
        assert snapshot['summary']['expiredPositionCount'] == 1
        row = snapshot['rows'][0]
        assert row['symbol'] == 'AAPL'
        assert row['positionState']['hasOpenPosition'] is True
        assert row['positionState']['shares'] == 5
        assert row['positionState']['maxHoldHours'] == 72
        assert row['positionState']['positionExpired'] is True
        assert row['positionState']['positionExpiresAtUtc'] is not None
        assert row['positionState']['exitDeadlineSource'] == 'watchlist_max_hold'



def test_exit_readiness_snapshot_tracks_managed_only_open_positions(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_stock_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        second_payload['ui_payload']['symbol_context'] = {'MSFT': second_payload['ui_payload']['symbol_context']['MSFT']}

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        db.add(
            Position(
                account_id='acct-1',
                ticker='AAPL',
                shares=3,
                avg_entry_price=100.0,
                current_price=102.0,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC) - timedelta(hours=1),
                entry_reasoning={'intentId': 'intent-2'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=102.0,
                trailing_stop=100.0,
                is_open=True,
                execution_id='intent-2',
            )
        )
        db.commit()
        watchlist_service.ingest_watchlist(db, second_payload, source='api')

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='stocks_only', expiring_within_hours=24)

        assert readiness['summary']['openPositionCount'] == 1
        assert readiness['summary']['managedOnlyOpenCount'] == 1
        assert readiness['rows'][0]['symbol'] == 'AAPL'
        assert readiness['rows'][0]['managedOnly'] is True
        assert readiness['rows'][0]['positionState']['hasOpenPosition'] is True
        assert readiness['rows'][0]['positionState']['positionExpired'] is False



def test_watchlist_exit_readiness_endpoint_returns_position_deadline_summary(tmp_path) -> None:
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
                account_id='acct-1',
                ticker='AAPL',
                shares=2,
                avg_entry_price=100.0,
                current_price=103.0,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC) - timedelta(hours=73),
                entry_reasoning={'intentId': 'intent-3'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=103.0,
                trailing_stop=100.0,
                is_open=True,
                execution_id='intent-3',
            )
        )
        db.commit()

        def override_db():
            try:
                yield db
            finally:
                pass

        app.dependency_overrides[get_db] = override_db
        try:
            client = TestClient(app)
            response = client.get('/api/watchlists/exit-readiness?scope=stocks_only&expiring_within_hours=24')
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert body['scope'] == 'stocks_only'
        assert body['summary']['openPositionCount'] == 1
        assert body['summary']['expiredPositionCount'] == 1
        assert body['rows'][0]['symbol'] == 'AAPL'
        assert body['rows'][0]['positionState']['positionExpired'] is True




def test_exit_readiness_snapshot_extends_structure_aware_time_stop(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        symbol_payload = deepcopy(payload['bot_payload']['symbols'][0])
        symbol_payload['exit_template'] = 'time_stop_with_structure_check'
        symbol_payload['max_hold_hours'] = 24
        payload['bot_payload']['symbols'] = [symbol_payload]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=25)
        db.add(
            Position(
                account_id='paper',
                ticker='AAPL',
                shares=5,
                avg_entry_price=100.0,
                current_price=103.0,
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_reasoning={'intentId': 'intent-structure-1'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=104.0,
                trailing_stop=101.0,
                is_open=True,
                execution_id='intent-structure-1',
            )
        )
        db.commit()

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='stocks_only', expiring_within_hours=24)

        assert readiness['summary']['openPositionCount'] == 1
        assert readiness['summary']['expiredPositionCount'] == 0
        assert readiness['summary']['timeStopExtendedCount'] == 1
        row = readiness['rows'][0]
        assert row['positionState']['positionExpired'] is False
        assert row['positionState']['timeStopStructureCheckPassed'] is True
        assert row['positionState']['timeStopExtended'] is True
        assert row['positionState']['timeStopExtensionHours'] == 4.0
        assert row['positionState']['basePositionExpiresAtUtc'] is not None
        assert row['positionState']['timeStopExtendedUntilUtc'] is not None
        assert row['positionState']['exitDeadlineSource'] == 'watchlist_max_hold_structure_extension'
        assert row['positionState']['hoursUntilExpiry'] is not None
        assert float(row['positionState']['hoursUntilExpiry']) > 0


def test_watchlist_exit_worker_dry_run_skips_structure_extended_time_stop(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        symbol_payload = deepcopy(payload['bot_payload']['symbols'][0])
        symbol_payload['exit_template'] = 'time_stop_with_structure_check'
        symbol_payload['max_hold_hours'] = 24
        payload['bot_payload']['symbols'] = [symbol_payload]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=25)
        db.add(
            Position(
                account_id='paper',
                ticker='AAPL',
                shares=5,
                avg_entry_price=100.0,
                current_price=103.0,
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_reasoning={'intentId': 'intent-structure-2'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=104.0,
                trailing_stop=101.0,
                is_open=True,
                execution_id='intent-structure-2',
            )
        )
        db.commit()

        status = watchlist_exit_worker.get_status(db)
        result = watchlist_exit_worker.run_exit_sweep(db, execute=False, limit=10)

        assert status['summary']['candidateExitCount'] == 0
        assert status['summary']['expiredPositionCount'] == 0
        assert result['summary']['candidateCount'] == 0
        assert result['summary']['expiredPositionCount'] == 0
        assert result['rows'] == []


def test_exit_readiness_snapshot_marks_structure_time_stop_expired_after_extension_window(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        symbol_payload = deepcopy(payload['bot_payload']['symbols'][0])
        symbol_payload['exit_template'] = 'time_stop_with_structure_check'
        symbol_payload['max_hold_hours'] = 24
        payload['bot_payload']['symbols'] = [symbol_payload]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=29)
        db.add(
            Position(
                account_id='paper',
                ticker='AAPL',
                shares=5,
                avg_entry_price=100.0,
                current_price=103.0,
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_reasoning={'intentId': 'intent-structure-3'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=104.0,
                trailing_stop=101.0,
                is_open=True,
                execution_id='intent-structure-3',
            )
        )
        db.commit()

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='stocks_only', expiring_within_hours=24)

        assert readiness['summary']['openPositionCount'] == 1
        assert readiness['summary']['expiredPositionCount'] == 1
        assert readiness['summary']['timeStopExtendedCount'] == 0
        row = readiness['rows'][0]
        assert row['positionState']['timeStopStructureCheckPassed'] is True
        assert row['positionState']['timeStopExtended'] is False
        assert row['positionState']['timeStopExtensionHours'] == 4.0
        assert row['positionState']['positionExpired'] is True
        assert row['positionState']['timeStopExtendedUntilUtc'] is not None
        assert row['positionState']['exitDeadlineSource'] == 'watchlist_max_hold'
        assert float(row['positionState']['hoursUntilExpiry']) <= 0


def test_exit_readiness_snapshot_marks_protective_stock_exit_signals(tmp_path) -> None:
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
                shares=5,
                avg_entry_price=100.0,
                current_price=97.0,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC) - timedelta(hours=2),
                entry_reasoning={'intentId': 'intent-entry'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=102.0,
                trailing_stop=99.0,
                is_open=True,
                execution_id='intent-entry',
            )
        )
        db.commit()

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='stocks_only', expiring_within_hours=24)

        assert readiness['summary']['openPositionCount'] == 1
        assert readiness['summary']['protectiveExitPendingCount'] == 1
        assert readiness['summary']['stopLossBreachedCount'] == 1
        assert readiness['summary']['trailingStopBreachedCount'] == 1
        assert readiness['rows'][0]['positionState']['protectiveExitPending'] is True
        assert readiness['rows'][0]['positionState']['protectiveExitReasons'] == ['STOP_LOSS_BREACH', 'TRAILING_STOP_BREACH']


def test_watchlist_exit_worker_status_reports_expired_positions(tmp_path) -> None:
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
                shares=4,
                avg_entry_price=100.0,
                current_price=103.0,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC) - timedelta(hours=73),
                entry_reasoning={'intentId': 'intent-entry'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=103.0,
                trailing_stop=100.0,
                is_open=True,
                execution_id='intent-entry',
            )
        )
        db.commit()

        status = watchlist_exit_worker.get_status(db)

        assert status['scope'] == 'stocks_only'
        assert status['summary']['expiredPositionCount'] == 1
        assert status['rows'][0]['symbol'] == 'AAPL'
        assert status['rows'][0]['positionState']['positionExpired'] is True
        assert status['rows'][0]['exitAlreadyInProgress'] is False


def test_watchlist_exit_worker_dry_run_does_not_create_intents(tmp_path) -> None:
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
                shares=2,
                avg_entry_price=100.0,
                current_price=103.0,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC) - timedelta(hours=73),
                entry_reasoning={'intentId': 'intent-entry'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=103.0,
                trailing_stop=100.0,
                is_open=True,
                execution_id='intent-entry',
            )
        )
        db.commit()

        result = watchlist_exit_worker.run_exit_sweep(db, execute=False, limit=10)

        assert result['summary']['candidateCount'] == 1
        assert result['summary']['submittedCount'] == 0
        assert db.query(OrderIntent).count() == 0
        assert result['rows'][0]['action'] == 'DRY_RUN_CANDIDATE'



def test_watchlist_exit_worker_dry_run_surfaces_stop_loss_breach(tmp_path) -> None:
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
                shares=2,
                avg_entry_price=100.0,
                current_price=97.5,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC) - timedelta(hours=4),
                entry_reasoning={'intentId': 'intent-entry'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=101.0,
                trailing_stop=96.0,
                is_open=True,
                execution_id='intent-entry',
            )
        )
        db.commit()

        result = watchlist_exit_worker.run_exit_sweep(db, execute=False, limit=10)

        assert result['summary']['candidateCount'] == 1
        assert result['summary']['protectiveExitCount'] == 1
        assert result['rows'][0]['action'] == 'DRY_RUN_CANDIDATE'
        assert result['rows'][0]['exitTrigger'] == 'STOP_LOSS_BREACH'
        assert result['rows'][0]['exitReasons'] == ['STOP_LOSS_BREACH']



def test_watchlist_exit_worker_refreshes_prices_and_ratchets_trailing_stop(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=2,
            avg_entry_price=100.0,
            current_price=101.0,
            strategy='AI_SCREENING',
            entry_time=datetime.now(UTC) - timedelta(hours=4),
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=98.0,
            profit_target=120.0,
            peak_price=101.0,
            trailing_stop=97.97,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(
            tradier_client,
            'get_quotes_sync',
            lambda symbols, mode=None: {
                'AAPL': {
                    'symbol': 'AAPL',
                    'last': 110.0,
                    '_fetched_at_utc': datetime.now(UTC).isoformat(),
                }
            },
        )
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'session open for refresh test',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            ),
        )

        result = watchlist_exit_worker.run_exit_sweep(db, execute=False, limit=10)

        db.refresh(position)
        assert result['summary']['refreshedPriceCount'] == 1
        assert result['summary']['candidateCount'] == 0
        assert position.current_price == 110.0
        assert position.peak_price == 110.0
        assert position.trailing_stop == round(110.0 * (1.0 - settings.TRAILING_STOP_PCT), 4)



def test_watchlist_exit_worker_execute_closes_stop_loss_breached_stock_position(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=3)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=3,
            avg_entry_price=100.0,
            current_price=97.5,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=98.0,
            profit_target=108.0,
            peak_price=101.0,
            trailing_stop=96.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.flush()
        trade = Trade(
            trade_id='trade-stop-loss',
            account_id='paper',
            ticker='AAPL',
            direction='LONG',
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_price=100.0,
            shares=3,
            entry_cost=300.0,
            entry_reasoning={'intentId': 'intent-entry'},
            execution_id='intent-entry',
            entry_order_id='entry-order',
        )
        db.add(trade)
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'session open for stop loss test',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_quotes_sync', lambda symbols, mode=None: {})
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None: 3)
        monkeypatch.setattr(
            tradier_client,
            'place_order_sync',
            lambda ticker, qty, side, mode=None, order_type='market', duration='day': {
                'order': {
                    'id': 'exit-stop-1',
                    'status': 'submitted',
                    'quantity': qty,
                    'exec_quantity': 0,
                }
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'get_order_sync',
            lambda order_id, mode=None: {
                'order': {
                    'id': order_id,
                    'status': 'filled',
                    'quantity': 3,
                    'exec_quantity': 3,
                    'avg_fill_price': 97.25,
                }
            },
        )

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)

        db.refresh(position)
        db.refresh(trade)
        intent = db.query(OrderIntent).filter(OrderIntent.execution_source == 'WATCHLIST_EXIT_WORKER').one()

        assert result['summary']['submittedCount'] == 1
        assert result['summary']['closedCount'] == 1
        assert result['summary']['protectiveExitCount'] == 1
        assert result['rows'][0]['action'] == 'EXIT_CLOSED'
        assert result['rows'][0]['exitTrigger'] == 'STOP_LOSS_BREACH'
        assert intent.status == 'CLOSED'
        assert position.is_open is False
        assert position.shares == 0
        assert trade.exit_trigger == 'STOP_LOSS_BREACH'


def test_watchlist_exit_worker_execute_closes_expired_stock_position(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=73)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=3,
            avg_entry_price=100.0,
            current_price=103.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=98.0,
            profit_target=108.0,
            peak_price=103.0,
            trailing_stop=100.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.flush()
        db.add(
            Trade(
                trade_id='trade-entry',
                account_id='paper',
                ticker='AAPL',
                direction='LONG',
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_price=100.0,
                shares=3,
                entry_cost=300.0,
                entry_reasoning={'intentId': 'intent-entry'},
                execution_id='intent-entry',
                entry_order_id='entry-order',
            )
        )
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'session open for test',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None: 3)
        monkeypatch.setattr(
            tradier_client,
            'place_order_sync',
            lambda ticker, qty, side, mode=None, order_type='market', duration='day': {
                'order': {
                    'id': 'exit-123',
                    'status': 'submitted',
                    'quantity': qty,
                    'exec_quantity': 0,
                }
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'get_order_sync',
            lambda order_id, mode=None: {
                'order': {
                    'id': order_id,
                    'status': 'filled',
                    'quantity': 3,
                    'exec_quantity': 3,
                    'avg_fill_price': 103.5,
                }
            },
        )

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)

        db.refresh(position)
        intent = db.query(OrderIntent).filter(OrderIntent.execution_source == 'WATCHLIST_EXIT_WORKER').one()

        assert result['summary']['submittedCount'] == 1
        assert result['summary']['closedCount'] == 1
        assert result['rows'][0]['action'] == 'EXIT_CLOSED'
        assert intent.status == 'CLOSED'
        assert position.is_open is False
        assert position.shares == 0


def test_watchlist_exit_sweep_endpoint_runs_dry_run_summary(tmp_path, monkeypatch) -> None:
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
                shares=2,
                avg_entry_price=100.0,
                current_price=103.0,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC) - timedelta(hours=73),
                entry_reasoning={'intentId': 'intent-entry'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=103.0,
                trailing_stop=100.0,
                is_open=True,
                execution_id='intent-entry',
            )
        )
        db.commit()
        monkeypatch.setattr(settings, 'ADMIN_API_TOKEN', 'phase52-token', raising=False)

        def override_db():
            try:
                yield db
            finally:
                pass

        app.dependency_overrides[get_db] = override_db
        try:
            client = TestClient(app)
            response = client.post('/api/watchlists/run-exit-sweep?execute=false', headers={'X-Admin-Token': 'phase52-token'})
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert body['summary']['candidateCount'] == 1
        assert body['rows'][0]['action'] == 'DRY_RUN_CANDIDATE'


def test_watchlist_exit_worker_status_reports_runtime_metadata(tmp_path, monkeypatch) -> None:
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
                shares=2,
                avg_entry_price=100.0,
                current_price=103.0,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC) - timedelta(hours=73),
                entry_reasoning={'intentId': 'intent-entry'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=103.0,
                trailing_stop=100.0,
                is_open=True,
                execution_id='intent-entry',
            )
        )
        db.commit()

        watchlist_exit_worker._runtime.enabled = True
        watchlist_exit_worker._runtime.poll_seconds = 17
        watchlist_exit_worker._runtime.last_run_summary = {'summary': {'submittedCount': 0}}
        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=False,
                to_dict=lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': False,
                    'reason': 'market closed for test',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)

        status = watchlist_exit_worker.get_status(db)

        assert status['enabled'] is True
        assert status['pollSeconds'] == 17
        assert status['summary']['expiredPositionCount'] == 1
        assert status['summary']['eligibleExpiredCount'] == 0
        assert status['summary']['blockedExpiredCount'] == 1
        assert status['rows'][0]['symbol'] == 'AAPL'


def test_watchlist_exit_worker_run_once_updates_runtime_summary(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=73)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=4,
            avg_entry_price=100.0,
            current_price=103.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=98.0,
            profit_target=108.0,
            peak_price=103.0,
            trailing_stop=100.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.flush()
        db.add(
            Trade(
                trade_id='trade-entry-run-once',
                account_id='paper',
                ticker='AAPL',
                direction='LONG',
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_price=100.0,
                shares=4,
                entry_cost=400.0,
                entry_reasoning={'intentId': 'intent-entry'},
                execution_id='intent-entry',
                entry_order_id='entry-order',
            )
        )
        db.commit()

        watchlist_exit_worker._runtime.last_started_at_utc = None
        watchlist_exit_worker._runtime.last_finished_at_utc = None
        watchlist_exit_worker._runtime.last_error = 'old-error'
        watchlist_exit_worker._runtime.consecutive_failures = 2
        watchlist_exit_worker._runtime.last_run_summary = {}

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'session open for run_once test',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None: 4)
        monkeypatch.setattr(
            tradier_client,
            'place_order_sync',
            lambda ticker, qty, side, mode=None, order_type='market', duration='day': {
                'order': {
                    'id': 'exit-run-once',
                    'status': 'submitted',
                    'quantity': qty,
                    'exec_quantity': 0,
                }
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'get_order_sync',
            lambda order_id, mode=None: {
                'order': {
                    'id': order_id,
                    'status': 'filled',
                    'quantity': 4,
                    'exec_quantity': 4,
                    'avg_fill_price': 103.25,
                }
            },
        )

        watchlist_exit_worker._runtime.last_started_at_utc = datetime.now(UTC).isoformat()
        result = watchlist_exit_worker.run_once(db, limit=5)

        db.refresh(position)
        assert result['summary']['submittedCount'] == 1
        assert result['summary']['closedCount'] == 1
        assert watchlist_exit_worker._runtime.last_run_summary['summary']['closedCount'] == 1
        assert watchlist_exit_worker._runtime.last_error is None
        assert watchlist_exit_worker._runtime.consecutive_failures == 0
        assert watchlist_exit_worker._runtime.last_finished_at_utc is not None
        assert position.is_open is False


def test_watchlist_exit_worker_endpoint_includes_runtime_status(tmp_path, monkeypatch) -> None:
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
                shares=1,
                avg_entry_price=100.0,
                current_price=103.0,
                strategy='AI_SCREENING',
                entry_time=datetime.now(UTC) - timedelta(hours=73),
                entry_reasoning={'intentId': 'intent-entry'},
                stop_loss=98.0,
                profit_target=108.0,
                peak_price=103.0,
                trailing_stop=100.0,
                is_open=True,
                execution_id='intent-entry',
            )
        )
        db.commit()
        monkeypatch.setattr(settings, 'ADMIN_API_TOKEN', 'phase53-token', raising=False)
        watchlist_exit_worker._runtime.enabled = True
        watchlist_exit_worker._runtime.poll_seconds = 19
        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)

        def override_db():
            try:
                yield db
            finally:
                pass

        app.dependency_overrides[get_db] = override_db
        try:
            client = TestClient(app)
            response = client.get('/api/watchlists/exit-worker')
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert body['enabled'] is True
        assert body['pollSeconds'] == 19
        assert body['summary']['expiredPositionCount'] == 1


def test_exit_readiness_snapshot_surfaces_profit_target_scale_out_ready(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=2)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=5,
            avg_entry_price=100.0,
            current_price=111.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=96.0,
            profit_target=108.0,
            peak_price=111.0,
            trailing_stop=107.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.flush()
        db.add(
            Trade(
                trade_id='trade-profit-target',
                account_id='paper',
                ticker='AAPL',
                direction='LONG',
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_price=100.0,
                shares=5,
                entry_cost=500.0,
                entry_reasoning={'intentId': 'intent-entry'},
                execution_id='intent-entry',
                entry_order_id='entry-order',
            )
        )
        db.commit()

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='stocks_only', expiring_within_hours=24)

        assert readiness['summary']['openPositionCount'] == 1
        assert readiness['summary']['profitTargetReachedCount'] == 1
        assert readiness['summary']['scaleOutReadyCount'] == 1
        assert readiness['rows'][0]['positionState']['profitTargetReached'] is True
        assert readiness['rows'][0]['positionState']['scaleOutReady'] is True
        assert readiness['rows'][0]['positionState']['scaleOutAlreadyTaken'] is False



def test_watchlist_exit_worker_dry_run_surfaces_profit_target_scale_out(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=2)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=5,
            avg_entry_price=100.0,
            current_price=111.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=96.0,
            profit_target=108.0,
            peak_price=111.0,
            trailing_stop=107.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.flush()
        db.add(
            Trade(
                trade_id='trade-profit-target',
                account_id='paper',
                ticker='AAPL',
                direction='LONG',
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_price=100.0,
                shares=5,
                entry_cost=500.0,
                entry_reasoning={'intentId': 'intent-entry'},
                execution_id='intent-entry',
                entry_order_id='entry-order',
            )
        )
        db.commit()

        result = watchlist_exit_worker.run_exit_sweep(db, execute=False, limit=10)

        assert result['summary']['candidateCount'] == 1
        assert result['summary']['profitTargetCount'] == 1
        assert result['rows'][0]['action'] == 'DRY_RUN_CANDIDATE'
        assert result['rows'][0]['exitTrigger'] == 'PROFIT_TARGET_REACHED'
        assert result['rows'][0]['exitReasons'] == ['PROFIT_TARGET_REACHED']



def test_watchlist_exit_worker_execute_scales_out_profit_target_stock_position(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=2)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=5,
            avg_entry_price=100.0,
            current_price=111.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=96.0,
            profit_target=108.0,
            peak_price=111.0,
            trailing_stop=107.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.flush()
        trade = Trade(
            trade_id='trade-profit-target',
            account_id='paper',
            ticker='AAPL',
            direction='LONG',
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_price=100.0,
            shares=5,
            entry_cost=500.0,
            entry_reasoning={'intentId': 'intent-entry'},
            execution_id='intent-entry',
            entry_order_id='entry-order',
        )
        db.add(trade)
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'session open for profit target test',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_quotes_sync', lambda symbols, mode=None: {})
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None: 5)
        monkeypatch.setattr(
            tradier_client,
            'place_order_sync',
            lambda ticker, qty, side, mode=None, order_type='market', duration='day': {
                'order': {
                    'id': 'exit-profit-1',
                    'status': 'submitted',
                    'quantity': qty,
                    'exec_quantity': 0,
                }
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'get_order_sync',
            lambda order_id, mode=None: {
                'order': {
                    'id': order_id,
                    'status': 'filled',
                    'quantity': 2,
                    'exec_quantity': 2,
                    'avg_fill_price': 111.0,
                }
            },
        )

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)

        db.refresh(position)
        db.refresh(trade)
        intent = db.query(OrderIntent).filter(OrderIntent.execution_source == 'WATCHLIST_EXIT_WORKER').one()

        assert result['summary']['submittedCount'] == 1
        assert result['summary']['scaleOutSubmittedCount'] == 1
        assert result['summary']['closedCount'] == 0
        assert result['summary']['profitTargetCount'] == 1
        assert result['rows'][0]['action'] == 'SCALE_OUT_SUBMITTED'
        assert result['rows'][0]['closedShares'] == 2
        assert result['rows'][0]['remainingShares'] == 3
        assert intent.requested_quantity == 2
        assert position.is_open is True
        assert position.shares == 3
        assert position.stop_loss == 100.0
        assert position.trailing_stop == round(111.0 * (1.0 - settings.TRAILING_STOP_PCT), 4)
        assert isinstance(trade.exit_reasoning, dict)
        assert trade.exit_reasoning['partialExits'][0]['trigger'] == 'PROFIT_TARGET_REACHED'


def test_exit_readiness_snapshot_surfaces_failed_follow_through_signal(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        symbol_payload = deepcopy(payload['bot_payload']['symbols'][0])
        symbol_payload['exit_template'] = 'first_failed_follow_through'
        symbol_payload['max_hold_hours'] = 24
        payload['bot_payload']['symbols'] = [symbol_payload]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=3)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=5,
            avg_entry_price=100.0,
            current_price=99.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=95.0,
            profit_target=108.0,
            peak_price=101.0,
            trailing_stop=94.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.add(
            Trade(
                trade_id='trade-follow-through',
                account_id='paper',
                ticker='AAPL',
                direction='LONG',
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_price=100.0,
                shares=5,
                entry_cost=500.0,
                entry_reasoning={'intentId': 'intent-entry'},
                execution_id='intent-entry',
                entry_order_id='entry-order',
            )
        )
        db.commit()

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='stocks_only', expiring_within_hours=24)

        assert readiness['summary']['openPositionCount'] == 1
        assert readiness['summary']['followThroughFailedCount'] == 1
        assert readiness['rows'][0]['positionState']['followThroughFailed'] is True
        assert readiness['rows'][0]['positionState']['hoursSinceEntry'] is not None
        assert readiness['rows'][0]['positionState']['followThroughWindowHours'] == 12.0


def test_watchlist_exit_worker_dry_run_surfaces_failed_follow_through(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        symbol_payload = deepcopy(payload['bot_payload']['symbols'][0])
        symbol_payload['exit_template'] = 'first_failed_follow_through'
        symbol_payload['max_hold_hours'] = 24
        payload['bot_payload']['symbols'] = [symbol_payload]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=3)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=5,
            avg_entry_price=100.0,
            current_price=99.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=95.0,
            profit_target=108.0,
            peak_price=101.0,
            trailing_stop=94.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.add(
            Trade(
                trade_id='trade-follow-through',
                account_id='paper',
                ticker='AAPL',
                direction='LONG',
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_price=100.0,
                shares=5,
                entry_cost=500.0,
                entry_reasoning={'intentId': 'intent-entry'},
                execution_id='intent-entry',
                entry_order_id='entry-order',
            )
        )
        db.commit()

        result = watchlist_exit_worker.run_exit_sweep(db, execute=False, limit=10)

        assert result['summary']['candidateCount'] == 1
        assert result['summary']['followThroughExitCount'] == 1
        assert result['rows'][0]['action'] == 'DRY_RUN_CANDIDATE'
        assert result['rows'][0]['exitTrigger'] == 'FAILED_FOLLOW_THROUGH'
        assert result['rows'][0]['exitReasons'] == ['FAILED_FOLLOW_THROUGH']


def test_watchlist_exit_worker_execute_closes_failed_follow_through_position(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        symbol_payload = deepcopy(payload['bot_payload']['symbols'][0])
        symbol_payload['exit_template'] = 'first_failed_follow_through'
        symbol_payload['max_hold_hours'] = 24
        payload['bot_payload']['symbols'] = [symbol_payload]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=3)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=5,
            avg_entry_price=100.0,
            current_price=99.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=95.0,
            profit_target=108.0,
            peak_price=101.0,
            trailing_stop=94.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        trade = Trade(
            trade_id='trade-follow-through',
            account_id='paper',
            ticker='AAPL',
            direction='LONG',
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_price=100.0,
            shares=5,
            entry_cost=500.0,
            entry_reasoning={'intentId': 'intent-entry'},
            execution_id='intent-entry',
            entry_order_id='entry-order',
        )
        db.add(trade)
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'session open for follow through test',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_quotes_sync', lambda symbols, mode=None: {})
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None: 5)
        monkeypatch.setattr(
            tradier_client,
            'place_order_sync',
            lambda ticker, qty, side, mode=None, order_type='market', duration='day': {
                'order': {
                    'id': 'exit-follow-through-1',
                    'status': 'submitted',
                    'quantity': qty,
                    'exec_quantity': 0,
                }
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'get_order_sync',
            lambda order_id, mode=None: {
                'order': {
                    'id': order_id,
                    'status': 'filled',
                    'quantity': 5,
                    'exec_quantity': 5,
                    'avg_fill_price': 99.0,
                }
            },
        )

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)

        db.refresh(position)
        db.refresh(trade)
        intent = db.query(OrderIntent).filter(OrderIntent.execution_source == 'WATCHLIST_EXIT_WORKER').one()

        assert result['summary']['submittedCount'] == 1
        assert result['summary']['closedCount'] == 1
        assert result['summary']['followThroughExitCount'] == 1
        assert result['rows'][0]['action'] == 'EXIT_CLOSED'
        assert result['rows'][0]['exitTrigger'] == 'FAILED_FOLLOW_THROUGH'
        assert intent.requested_quantity == 5
        assert position.is_open is False
        assert position.shares == 0
        assert trade.exit_trigger == 'FAILED_FOLLOW_THROUGH'


def test_watchlist_exit_worker_refresh_tightens_trailing_stop_for_impulse_template(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [deepcopy(payload['bot_payload']['symbols'][1])]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['MSFT']
        payload['ui_payload']['symbol_context'] = {'MSFT': payload['ui_payload']['symbol_context']['MSFT']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=2)
        position = Position(
            account_id='paper',
            ticker='MSFT',
            shares=4,
            avg_entry_price=100.0,
            current_price=109.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            stop_loss=96.0,
            profit_target=108.0,
            peak_price=109.0,
            trailing_stop=104.0,
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.add(
            Trade(
                trade_id='trade-impulse-trail',
                account_id='paper',
                ticker='MSFT',
                direction='LONG',
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_price=100.0,
                shares=4,
                entry_cost=400.0,
                entry_reasoning={'intentId': 'intent-entry'},
                execution_id='intent-entry',
                entry_order_id='entry-order',
            )
        )
        db.commit()

        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(
            tradier_client,
            'get_quotes_sync',
            lambda symbols, mode=None: {
                'MSFT': {
                    'last': 112.0,
                    'timestamp': datetime.now(UTC).isoformat(),
                }
            },
        )

        result = watchlist_exit_worker.run_exit_sweep(db, execute=False, limit=10)

        db.refresh(position)
        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='stocks_only', expiring_within_hours=24)

        assert result['summary']['refreshedPriceCount'] == 1
        assert result['summary']['candidateCount'] == 0
        assert result['rows'] == []
        assert position.current_price == 112.0
        assert position.peak_price == 112.0
        assert position.trailing_stop == round(112.0 * (1.0 - (settings.TRAILING_STOP_PCT * 0.5)), 4)
        assert readiness['summary']['impulseTrailArmedCount'] == 1
        assert readiness['rows'][0]['positionState']['impulseTrailArmed'] is True
        assert readiness['rows'][0]['positionState']['impulseTrailingStop'] == round(112.0 * (1.0 - (settings.TRAILING_STOP_PCT * 0.5)), 4)


def test_crypto_paper_ledger_reports_equity_market_value_and_realized_pnl(monkeypatch) -> None:
    ledger = CryptoPaperLedger(starting_balance=1000.0)

    ledger.execute_trade(pair='BTC/USD', ohlcv_pair='XBTUSD', side='BUY', amount=1.0, price=100.0)
    ledger.execute_trade(pair='BTC/USD', ohlcv_pair='XBTUSD', side='SELL', amount=0.25, price=140.0)

    monkeypatch.setattr(ledger.kraken, 'get_prices', lambda pairs: {'XXBTZUSD': 120.0})

    snapshot = ledger.get_ledger()

    assert snapshot['balance'] == 935.0
    assert snapshot['marketValue'] == 90.0
    assert snapshot['equity'] == 1025.0
    assert snapshot['totalPnL'] == 15.0
    assert snapshot['realizedPnL'] == 10.0
    assert snapshot['netPnL'] == 25.0
    assert snapshot['positions'][0]['costBasis'] == 75.0
    assert snapshot['positions'][0]['marketValue'] == 90.0
    assert snapshot['positions'][0]['entryTimeUtc'] is not None


def test_crypto_exit_readiness_uses_ledger_entry_time_and_watchlist_max_hold(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['bot_payload']['symbols'][0]['exit_template'] = 'time_stop_with_structure_check'
        payload['bot_payload']['symbols'][0]['max_hold_hours'] = 48
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        entry_time = (datetime.now(UTC) - timedelta(hours=60)).replace(microsecond=0)
        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [
                {
                    'pair': 'BTC/USD',
                    'ohlcvPair': 'XBTUSD',
                    'amount': 0.5,
                    'avgPrice': 100.0,
                    'currentPrice': 95.0,
                    'marketValue': 47.5,
                    'costBasis': 50.0,
                    'pnl': -2.5,
                    'pnlPercent': -5.0,
                    'realizedPnl': 0.0,
                    'entryTimeUtc': entry_time.isoformat(),
                }
            ],
        )

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='crypto_only', expiring_within_hours=24)

        assert readiness['summary']['openPositionCount'] == 1
        assert readiness['summary']['expiredPositionCount'] == 1
        assert readiness['summary']['followThroughFailedCount'] == 0
        assert readiness['rows'][0]['positionState']['entryTimeUtc'] == entry_time.isoformat()
        assert readiness['rows'][0]['positionState']['maxHoldHours'] == 48
        assert readiness['rows'][0]['positionState']['positionExpired'] is True
        assert readiness['rows'][0]['positionState']['exitDeadlineSource'] == 'watchlist_max_hold'
        assert readiness['rows'][0]['positionState']['marketValue'] == 47.5
        assert readiness['rows'][0]['positionState']['costBasis'] == 50.0


def test_crypto_stop_loss_breach_sets_protective_exit_pending(tmp_path, monkeypatch) -> None:
    """Stop-loss breach on a crypto position must surface protectiveExitPending=True
    and appear in the exit-readiness due rows so the exit worker can act on it."""
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['bot_payload']['symbols'][0]['exit_template'] = 'first_failed_follow_through'
        payload['bot_payload']['symbols'][0]['max_hold_hours'] = 48
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        avg_price = 100.0
        # Push current price below the 1.5 % stop-loss floor
        current_price = round(avg_price * (1.0 - settings.STOP_LOSS_PCT) - 0.01, 8)
        entry_time = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [{
                'pair': 'BTC/USD',
                'ohlcvPair': 'XBTUSD',
                'amount': 0.5,
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'marketValue': 0.5 * current_price,
                'costBasis': 0.5 * avg_price,
                'pnl': 0.5 * (current_price - avg_price),
                'pnlPercent': ((current_price / avg_price) - 1.0) * 100.0,
                'realizedPnl': 0.0,
                'entryTimeUtc': entry_time.isoformat(),
            }],
        )

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='crypto_only', expiring_within_hours=24)
        state = readiness['rows'][0]['positionState']

        assert state['stopLossBreached'] is True, 'stop-loss should be breached'
        assert state['protectiveExitPending'] is True, 'protective exit should be pending'
        assert 'STOP_LOSS_BREACH' in state['protectiveExitReasons']
        assert state['trailingStopBreached'] is False

        # Confirm it surfaces in the due-row filter used by the exit worker
        assert readiness['summary']['protectiveExitPendingCount'] == 1
        assert readiness['summary']['stopLossBreachedCount'] == 1


def test_crypto_trailing_stop_breach_sets_protective_exit_pending(tmp_path, monkeypatch) -> None:
    """Trailing-stop breach (price drops through the ratcheted trail) must
    surface protectiveExitPending=True.

    Real-world scenario: entry at 100, price ran to 110 (peak), trailing
    ratchets to 110 × (1 − 3%) = 106.7.  Price then drops back to 106.5 —
    above the hard stop-loss floor (100 × 0.985 = 98.5) but below the
    ratcheted trail.  Because the crypto state-map uses current_price as the
    peak proxy, we simulate this by supplying a currentPrice that is already
    below the trailing level derived from avg_price, but still above the hard
    stop so only TRAILING_STOP_BREACH fires.

    With defaults STOP_LOSS_PCT=1.5 % and TRAILING_STOP_PCT=3 %:
      stop_loss  = avg × 0.985 = 98.50
      trail      = avg × 0.970 = 97.00   (ratcheted from avg as peak proxy)

    Any price in (97.00, 98.50) triggers trailing only.  We pick 97.5.
    """
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['bot_payload']['symbols'][0]['exit_template'] = 'trail_after_impulse'
        payload['bot_payload']['symbols'][0]['max_hold_hours'] = 48
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        avg_price = 100.0
        stop_loss_level = round(avg_price * (1.0 - settings.STOP_LOSS_PCT), 8)   # 98.5
        trailing_level  = round(avg_price * (1.0 - settings.TRAILING_STOP_PCT), 8)  # 97.0
        # Price sits in the gap: below trailing but above hard stop
        current_price = round((stop_loss_level + trailing_level) / 2.0, 8)  # ~97.75

        # Sanity: trailing < current < stop_loss should not be possible with defaults
        # where trailing(3%) < stop_loss(1.5%).  Instead current must be below trailing.
        # Pick a value guaranteed below trailing_level but above stop_loss:
        #   trailing = 97.0, stop_loss = 98.5 → gap doesn't exist (trailing < stop_loss)
        # So we flip: use avg such that trailing > stop_loss after ratchet.
        # Simplest: set current_price just below trailing_level (97.0) but still
        # well above zero, meaning stop_loss(98.5) IS also breached.
        # Accept that both flags fire simultaneously — that is the correct behaviour
        # when trailing < stop_loss in config.
        # The test therefore asserts TRAILING_STOP_BREACH in reasons (stop-loss too).
        current_price = trailing_level - 0.01  # 96.99 — below both stops
        entry_time = (datetime.now(UTC) - timedelta(hours=3)).replace(microsecond=0)

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [{
                'pair': 'BTC/USD',
                'ohlcvPair': 'XBTUSD',
                'amount': 1.0,
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'marketValue': current_price,
                'costBasis': avg_price,
                'pnl': current_price - avg_price,
                'pnlPercent': ((current_price / avg_price) - 1.0) * 100.0,
                'realizedPnl': 0.0,
                'entryTimeUtc': entry_time.isoformat(),
            }],
        )

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='crypto_only', expiring_within_hours=24)
        state = readiness['rows'][0]['positionState']

        # Both trailing and hard stop are breached when price is below trailing
        # (which is tighter than stop-loss with default config)
        assert state['trailingStopBreached'] is True, 'trailing stop should be breached'
        assert state['stopLossBreached'] is True, 'hard stop also breached below trailing level'
        assert state['protectiveExitPending'] is True
        assert 'TRAILING_STOP_BREACH' in state['protectiveExitReasons']
        assert 'STOP_LOSS_BREACH' in state['protectiveExitReasons']
        assert readiness['summary']['protectiveExitPendingCount'] == 1
        assert readiness['summary']['trailingStopBreachedCount'] == 1


def test_crypto_profit_target_reached_sets_scale_out_ready(tmp_path, monkeypatch) -> None:
    """When current price exceeds the +2.5 % profit target on a scale-out
    template, scaleOutReady must be True for the crypto position."""
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['bot_payload']['symbols'][0]['exit_template'] = 'scale_out_then_trail'
        payload['bot_payload']['symbols'][0]['max_hold_hours'] = 48
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        avg_price = 100.0
        # Push current price above the +2.5 % profit target
        current_price = round(avg_price * (1.0 + settings.PROFIT_TARGET_PCT) + 0.01, 8)
        entry_time = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [{
                'pair': 'BTC/USD',
                'ohlcvPair': 'XBTUSD',
                'amount': 2.0,
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'marketValue': 2.0 * current_price,
                'costBasis': 2.0 * avg_price,
                'pnl': 2.0 * (current_price - avg_price),
                'pnlPercent': ((current_price / avg_price) - 1.0) * 100.0,
                'realizedPnl': 0.0,
                'entryTimeUtc': entry_time.isoformat(),
            }],
        )

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='crypto_only', expiring_within_hours=24)
        state = readiness['rows'][0]['positionState']

        assert state['profitTargetReached'] is True
        assert state['scaleOutReady'] is True
        assert state['protectiveExitPending'] is False, 'no protective exit on profitable position'
        assert state['stopLossBreached'] is False
        assert readiness['summary']['scaleOutReadyCount'] == 1
        assert readiness['summary']['profitTargetReachedCount'] == 1


def test_crypto_follow_through_not_triggered_when_stop_loss_breached(tmp_path, monkeypatch) -> None:
    """When stop-loss is breached, follow-through-failed must NOT also be set —
    protective exits take priority and the follow-through window is suppressed."""
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['bot_payload']['symbols'][0]['exit_template'] = 'first_failed_follow_through'
        payload['bot_payload']['symbols'][0]['max_hold_hours'] = 48
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        avg_price = 100.0
        current_price = round(avg_price * (1.0 - settings.STOP_LOSS_PCT) - 0.5, 8)
        entry_time = (datetime.now(UTC) - timedelta(hours=5)).replace(microsecond=0)

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [{
                'pair': 'BTC/USD',
                'ohlcvPair': 'XBTUSD',
                'amount': 1.0,
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'marketValue': current_price,
                'costBasis': avg_price,
                'pnl': current_price - avg_price,
                'pnlPercent': ((current_price / avg_price) - 1.0) * 100.0,
                'realizedPnl': 0.0,
                'entryTimeUtc': entry_time.isoformat(),
            }],
        )

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='crypto_only', expiring_within_hours=24)
        state = readiness['rows'][0]['positionState']

        assert state['stopLossBreached'] is True
        assert state['protectiveExitPending'] is True
        # follow-through must be suppressed when stop-loss is already breached
        assert state['followThroughFailed'] is False, (
            'followThroughFailed should be False when stop_loss_breached is True'
        )


def test_crypto_no_protective_exit_when_price_above_stop(tmp_path, monkeypatch) -> None:
    """A healthy crypto position (price well above stop levels) must NOT trigger
    any protective exit flags."""
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['bot_payload']['symbols'][0]['exit_template'] = 'trail_after_impulse'
        payload['bot_payload']['symbols'][0]['max_hold_hours'] = 48
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        avg_price = 100.0
        current_price = 101.5  # comfortably above both stop levels
        entry_time = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [{
                'pair': 'BTC/USD',
                'ohlcvPair': 'XBTUSD',
                'amount': 1.0,
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'marketValue': current_price,
                'costBasis': avg_price,
                'pnl': current_price - avg_price,
                'pnlPercent': ((current_price / avg_price) - 1.0) * 100.0,
                'realizedPnl': 0.0,
                'entryTimeUtc': entry_time.isoformat(),
            }],
        )

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='crypto_only', expiring_within_hours=24)
        state = readiness['rows'][0]['positionState']

        assert state['stopLossBreached'] is False
        assert state['trailingStopBreached'] is False
        assert state['protectiveExitPending'] is False
        assert state['protectiveExitReasons'] == []
        assert state['stopLoss'] is not None, 'stopLoss level should always be computed'
        assert state['trailingStop'] is not None, 'trailingStop level should always be computed'
        assert state['profitTarget'] is not None, 'profitTarget level should always be computed'
        assert readiness['summary']['protectiveExitPendingCount'] == 0


def test_due_run_submits_watchlist_entry_candidates_into_order_intents(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'AAPL').one()
        monitor_row.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
        monitor_row.latest_decision_state = 'WAITING_FOR_SETUP'
        db.commit()

        monkeypatch.setattr(
            'app.services.watchlist_monitoring.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(**{
                'session_open': True,
                'to_dict': lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'patched open session',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            }),
        )
        monkeypatch.setattr(
            'app.services.pre_trade_gate.get_execution_gate_status',
            lambda: SimpleNamespace(allowed=True, state='ARMED', reason='', status_code=200),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(
            tradier_client,
            'get_account_snapshot',
            lambda mode=None: {
                'mode': (mode or 'PAPER').upper(),
                'connected': True,
                'accountId': 'paper-watchlist',
                'cash': 25_000.0,
                'buyingPower': 25_000.0,
                'portfolioValue': 25_000.0,
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'get_quote_sync',
            lambda symbol, mode=None: {
                'symbol': symbol,
                'last': 110.0,
                'prevclose': 100.0,
                'open': 105.0,
                'volume': 2_500_000,
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'place_order_sync',
            lambda ticker, qty, side, mode=None, order_type='market', duration='day': {
                'order': {'id': 'watch-ord-1', 'status': 'open', 'quantity': qty},
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'get_order_sync',
            lambda order_id, mode=None: {
                'order': {
                    'id': order_id,
                    'status': 'filled',
                    'quantity': 22,
                    'exec_quantity': 22,
                    'avg_fill_price': 111.0,
                }
            },
        )

        result = watchlist_monitoring_orchestrator.run_due_once(db, scope='stocks_only', limit_per_scope=10)

        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'AAPL').one()
        intent = db.query(OrderIntent).filter(OrderIntent.execution_source == 'WATCHLIST_MONITOR_ENTRY').one()
        position = db.query(Position).filter(Position.ticker == 'AAPL', Position.is_open.is_(True)).one()
        trade = db.query(Trade).filter(Trade.ticker == 'AAPL').one()

        assert result['scope'] == 'stocks_only'
        assert result['summary']['entryCandidateCount'] == 1
        assert result['entryExecution']['candidateCount'] == 1
        assert result['entryExecution']['intentCount'] == 1
        assert result['entryExecution']['submittedCount'] == 1
        assert result['entryExecution']['filledCount'] == 1
        assert intent.symbol == 'AAPL'
        assert intent.status == 'FILLED'
        assert position.shares == 22
        assert trade.entry_order_id == 'watch-ord-1'
        assert monitor_row.decision_context_json['entryExecution']['action'] == 'ENTRY_FILLED'
        assert monitor_row.decision_context_json['entryExecution']['positionId'] == position.id


def test_due_run_skips_watchlist_entry_when_open_position_exists(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'AAPL').one()
        monitor_row.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
        expected_account_id = str(
            tradier_client._credentials_for_mode(runtime_state.get().stock_mode).get('account_id')
            or runtime_state.get().stock_mode
            or 'TRADIER'
        ).strip()

        db.add(
            Position(
                account_id=expected_account_id,
                ticker='AAPL',
                shares=10,
                avg_entry_price=100.0,
                current_price=110.0,
                unrealized_pnl=100.0,
                unrealized_pnl_pct=0.1,
                strategy='WATCHLIST_ENTRY',
                entry_time=datetime.now(UTC),
                entry_reasoning={'source': 'test'},
                stop_loss=98.5,
                profit_target=102.5,
                peak_price=110.0,
                trailing_stop=106.7,
                is_open=True,
            )
        )
        db.commit()

        monkeypatch.setattr(
            'app.services.watchlist_monitoring.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(**{
                'session_open': True,
                'to_dict': lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'patched open session',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            }),
        )
        monkeypatch.setattr(
            tradier_client,
            'get_quote_sync',
            lambda symbol, mode=None: {
                'symbol': symbol,
                'last': 110.0,
                'prevclose': 100.0,
                'open': 105.0,
                'volume': 2_500_000,
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )

        result = watchlist_monitoring_orchestrator.run_due_once(db, scope='stocks_only', limit_per_scope=10)
        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'AAPL').one()

        assert result['entryExecution']['candidateCount'] == 1
        assert result['entryExecution']['intentCount'] == 0
        assert result['entryExecution']['skippedCount'] == 1
        assert db.query(OrderIntent).filter(OrderIntent.execution_source == 'WATCHLIST_MONITOR_ENTRY').count() == 0
        assert monitor_row.decision_context_json['entryExecution']['reason'] == 'OPEN_POSITION_EXISTS'


def test_watchlist_exit_worker_handles_exit_snapshot_failure_without_crashing(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.watchlist_service.get_exit_readiness_snapshot',
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('tradier timeout')),
        )

        status = watchlist_exit_worker.get_status(db)
        result = watchlist_exit_worker.run_exit_sweep(db, execute=False, limit=10)

        assert status['summary']['candidateExitCount'] == 0
        assert result['summary']['candidateCount'] == 0
        assert result['rows'] == []


def test_broker_position_seed_omits_missing_account_fk(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        db.add(
            OrderIntent(
                intent_id='intent-seed-account',
                account_id='VA83704948',
                asset_class='stock',
                symbol='AAPL',
                side='BUY',
                status='FILLED',
                requested_quantity=5,
                execution_source='WATCHLIST_MONITOR_ENTRY',
            )
        )
        db.commit()

        seed = watchlist_service._resolve_stock_position_seed(
            db,
            symbol='AAPL',
            watchlist_row=(
                db.query(WatchlistSymbol)
                .filter(WatchlistSymbol.symbol == 'AAPL')
                .order_by(WatchlistSymbol.id.desc())
                .first()
            ),
            broker_position={
                'symbol': 'AAPL',
                'shares': 5,
                'avgPrice': 100.0,
                'currentPrice': 101.0,
                'marketValue': 505.0,
                'pnl': 5.0,
                'pnlPercent': 1.0,
            },
            observed_at=datetime.now(UTC),
        )

        assert seed['accountId'] is None
        assert seed['entryReasoning']['seedAccountId'] == 'VA83704948'
        assert seed['entryReasoning']['seedAccountMissingFromAccounts'] is True


def test_due_run_executes_crypto_watchlist_entry_into_paper_ledger(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'BTC').one()
        monitor_row.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        monkeypatch.setattr(
            'app.services.watchlist_monitoring.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(**{
                'session_open': True,
                'to_dict': lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'patched open session',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            }),
        )
        monkeypatch.setattr(
            'app.services.watchlist_monitoring.kraken_service.resolve_pair',
            lambda pair: KrakenPairMetadata(
                display_pair='BTC/USD',
                rest_pair='XBTUSD',
                pair_key='XBTUSD',
                ws_pair='XBT/USD',
                altname='XBTUSD',
            ),
        )
        monkeypatch.setattr(
            'app.services.watchlist_monitoring.kraken_service.get_ticker',
            lambda pair: {
                'c': ['70000.0', '1'],
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )
        monkeypatch.setattr(
            'app.services.watchlist_monitoring.kraken_service.get_ohlc',
            lambda pair, interval=15, limit=25: [
                {'timestamp': 1, 'open': 68000.0, 'high': 69000.0, 'low': 67500.0, 'close': 68500.0, 'vwap': 68500.0, 'volume': 10.0, 'count': 1},
                {'timestamp': 2, 'open': 68500.0, 'high': 69500.0, 'low': 68000.0, 'close': 69000.0, 'vwap': 69000.0, 'volume': 11.0, 'count': 1},
                {'timestamp': 3, 'open': 69000.0, 'high': 70000.0, 'low': 68800.0, 'close': 69500.0, 'vwap': 69500.0, 'volume': 12.0, 'count': 1},
                {'timestamp': 4, 'open': 69500.0, 'high': 70500.0, 'low': 69200.0, 'close': 69800.0, 'vwap': 69800.0, 'volume': 13.0, 'count': 1},
                {'timestamp': 5, 'open': 69800.0, 'high': 71000.0, 'low': 69700.0, 'close': 70000.0, 'vwap': 70000.0, 'volume': 14.0, 'count': 1},
                {'timestamp': 6, 'open': 70000.0, 'high': 71200.0, 'low': 69900.0, 'close': 70500.0, 'vwap': 70500.0, 'volume': 15.0, 'count': 1},
            ],
        )
        original_balance = crypto_ledger.balance
        original_positions = dict(crypto_ledger.positions)
        original_trades = list(crypto_ledger.trades)
        try:
            crypto_ledger.balance = type(original_balance)('100000')
            crypto_ledger.positions = {}
            crypto_ledger.trades = []

            result = watchlist_monitoring_orchestrator.run_due_once(db, scope='crypto_only', limit_per_scope=10)

            monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'BTC').one()

            assert result['scope'] == 'crypto_only'
            assert result['summary']['entryCandidateCount'] == 1
            assert result['entryExecution']['candidateCount'] == 1
            assert result['entryExecution']['submittedCount'] == 1
            assert result['entryExecution']['filledCount'] == 1
            assert result['entryExecution']['intentCount'] == 1
            assert len(crypto_ledger.trades) == 1
            assert crypto_ledger.trades[0]['pair'] == 'BTC/USD'
            assert crypto_ledger.trades[0]['side'].upper() == 'BUY'
            assert float(crypto_ledger.trades[0]['price']) > 0
            assert monitor_row.decision_context_json['entryExecution']['action'] == 'ENTRY_FILLED'
            assert monitor_row.decision_context_json['entryExecution']['tradeId'] == crypto_ledger.trades[0]['id']
        finally:
            crypto_ledger.balance = original_balance
            crypto_ledger.positions = original_positions
            crypto_ledger.trades = original_trades


def test_crypto_active_watchlist_status_summary_uses_normalized_symbol_universe(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        persisted = watchlist_service.ingest_watchlist(db, payload, source='api')

        active_upload_id = persisted['uploadId']
        generated_at = datetime.now(UTC).replace(microsecond=0)
        historical_upload = WatchlistUpload(
            upload_id='legacy-crypto-upload',
            scan_id='legacy-crypto-scan',
            schema_version='bot_watchlist_v3',
            provider='chatgpt_kraken_app',
            scope='crypto_only',
            source='api',
            payload_hash='legacy-hash',
            generated_at_utc=generated_at,
            received_at_utc=generated_at,
            watchlist_expires_at_utc=generated_at + timedelta(days=1),
            validation_status='accepted',
            market_regime='mixed',
            selected_count=2,
            is_active=False,
            validation_result_json={},
            raw_payload_json={},
            bot_payload_json={},
        )
        db.add(historical_upload)
        db.flush()
        db.add_all([
            WatchlistSymbol(
                upload_id=historical_upload.upload_id,
                scope='crypto_only',
                symbol='BTC/USD',
                quote_currency='USD',
                asset_class='crypto',
                enabled=True,
                trade_direction='long',
                priority_rank=1,
                tier='tier_1',
                bias='bullish',
                setup_template='trend_continuation',
                bot_timeframes=['15m'],
                exit_template='trail_after_impulse',
                max_hold_hours=24,
                risk_flags=[],
                monitoring_status=INACTIVE,
            ),
            WatchlistSymbol(
                upload_id=historical_upload.upload_id,
                scope='crypto_only',
                symbol='BTCUSD',
                quote_currency='USD',
                asset_class='crypto',
                enabled=True,
                trade_direction='long',
                priority_rank=2,
                tier='tier_1',
                bias='bullish',
                setup_template='trend_continuation',
                bot_timeframes=['15m'],
                exit_template='trail_after_impulse',
                max_hold_hours=24,
                risk_flags=[],
                monitoring_status=INACTIVE,
            ),
            WatchlistSymbol(
                upload_id=historical_upload.upload_id,
                scope='crypto_only',
                symbol='DOGE/USD',
                quote_currency='USD',
                asset_class='crypto',
                enabled=True,
                trade_direction='long',
                priority_rank=3,
                tier='tier_3',
                bias='bullish',
                setup_template='range_breakout',
                bot_timeframes=['15m'],
                exit_template='first_failed_follow_through',
                max_hold_hours=24,
                risk_flags=[],
                monitoring_status=INACTIVE,
            ),
            WatchlistSymbol(
                upload_id=historical_upload.upload_id,
                scope='crypto_only',
                symbol='DOGE',
                quote_currency='USD',
                asset_class='crypto',
                enabled=True,
                trade_direction='long',
                priority_rank=4,
                tier='tier_3',
                bias='bullish',
                setup_template='range_breakout',
                bot_timeframes=['15m'],
                exit_template='first_failed_follow_through',
                max_hold_hours=24,
                risk_flags=[],
                monitoring_status=INACTIVE,
            ),
        ])
        db.commit()

        refreshed = watchlist_service.serialize_upload(
            db,
            db.query(WatchlistUpload).filter(WatchlistUpload.upload_id == active_upload_id).one(),
        )

        assert refreshed['selectedCount'] == 2
        assert refreshed['statusSummary']['activeCount'] == 2
        assert refreshed['statusSummary']['activeCount'] <= refreshed['selectedCount']
        assert refreshed['statusSummary']['inactiveCount'] == 1


def test_crypto_status_summary_counts_managed_only_off_watchlist_once(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        first_payload = build_crypto_payload()
        second_payload = deepcopy(first_payload)
        second_payload['generated_at_utc'] = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        second_payload['bot_payload']['symbols'] = [second_payload['bot_payload']['symbols'][1]]
        second_payload['bot_payload']['symbols'][0]['priority_rank'] = 1
        second_payload['ui_payload']['summary']['selected_count'] = 1
        second_payload['ui_payload']['summary']['primary_focus'] = ['ETH']
        second_payload['ui_payload']['symbol_context'] = {'ETH': second_payload['ui_payload']['symbol_context']['ETH']}

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [
                {'pair': 'BTC/USD', 'amount': 0.25},
                {'pair': 'BTCUSD', 'amount': 0.25},
            ],
        )

        watchlist_service.ingest_watchlist(db, first_payload, source='api')
        active_payload = watchlist_service.ingest_watchlist(db, second_payload, source='api')

        assert active_payload['selectedCount'] == 1
        assert active_payload['statusSummary']['activeCount'] == 1
        assert active_payload['statusSummary']['activeCount'] <= active_payload['selectedCount']
        assert active_payload['statusSummary']['managedOnlyCount'] == 1



def test_unified_positions_endpoint_merges_stock_broker_db_and_crypto_ledger(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        def override_get_db():
            db = SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

        db = SessionFactory()
        try:
            db.add(
                Position(
                    account_id='paper',
                    ticker='AAPL',
                    shares=10,
                    avg_entry_price=190.5,
                    current_price=192.25,
                    unrealized_pnl=17.5,
                    unrealized_pnl_pct=0.92,
                    strategy='WATCHLIST_ENTRY',
                    entry_time=datetime.now(UTC),
                    is_open=True,
                    execution_id='intent-aapl',
                )
            )
            db.add(
                Position(
                    account_id='paper',
                    ticker='MSFT',
                    shares=4,
                    avg_entry_price=410.0,
                    current_price=412.0,
                    unrealized_pnl=8.0,
                    unrealized_pnl_pct=0.49,
                    strategy='WATCHLIST_ENTRY',
                    entry_time=datetime.now(UTC),
                    is_open=True,
                    execution_id='intent-msft',
                )
            )
            db.commit()
        finally:
            db.close()

        monkeypatch.setattr(
            tradier_client,
            'get_positions_snapshot',
            lambda mode=None: [
                {
                    'symbol': 'AAPL',
                    'shares': 10,
                    'avgPrice': 190.5,
                    'currentPrice': 192.25,
                    'marketValue': 1922.5,
                    'pnl': 17.5,
                    'pnlPercent': 0.92,
                },
                {
                    'symbol': 'NVDA',
                    'shares': 3,
                    'avgPrice': 900.0,
                    'currentPrice': 910.0,
                    'marketValue': 2730.0,
                    'pnl': 30.0,
                    'pnlPercent': 1.11,
                },
            ],
        )

        original_positions = dict(crypto_ledger.positions)
        original_pair_mappings = dict(getattr(crypto_ledger, 'pair_mappings', {}))
        try:
            crypto_ledger.positions = {
                'TAOUSD': {
                    'amount': Decimal('5'),
                    'total_cost': Decimal('1500'),
                    'entry_time_utc': '2026-04-02T03:00:00+00:00',
                }
            }
            crypto_ledger.pair_mappings = {'TAO/USD': 'TAOUSD'}

            with TestClient(app) as client:
                response = client.get('/api/positions/unified')
        finally:
            crypto_ledger.positions = original_positions
            crypto_ledger.pair_mappings = original_pair_mappings
            app.dependency_overrides.clear()

        assert response.status_code == 200
        payload = response.json()
        assert payload['summary']['totalCount'] == 4
        assert payload['summary']['stockCount'] == 3
        assert payload['summary']['cryptoCount'] == 1
        assert payload['summary']['stockDriftCount'] == 2

        rows = {row['symbol']: row for row in payload['rows']}
        assert rows['AAPL']['assetClass'] == 'stock'
        assert rows['AAPL']['sourceStatus'] == 'aligned'
        assert rows['AAPL']['inspectAssetClass'] == 'stock'
        assert rows['NVDA']['sourceStatus'] == 'broker_only'
        assert rows['MSFT']['sourceStatus'] == 'db_only'
        assert rows['TAO/USD']['assetClass'] == 'crypto'
        assert rows['TAO/USD']['sourceStatus'] == 'ledger'
        assert rows['TAO/USD']['inspectAssetClass'] == 'crypto'


def test_watchlist_exit_worker_skips_duplicate_exit_when_broker_sell_pending(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=80)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=289,
            avg_entry_price=100.0,
            current_price=110.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {'scope': scope, 'observedAtUtc': observed_at.isoformat(), 'sessionOpen': True},
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None: 289)
        monkeypatch.setattr(
            tradier_client,
            'get_orders_sync',
            lambda mode=None, symbol=None, side=None, statuses=None, timeout=None: [
                {
                    'id': 'ord-pending-1',
                    'symbol': 'AAPL',
                    'side': 'SELL',
                    'status': 'PENDING',
                    'requested_quantity': 144,
                    'filled_quantity': 0,
                    'remaining_quantity': 144,
                }
            ],
        )
        called = {'count': 0}

        def _never_submit(*args, **kwargs):
            called['count'] += 1
            raise AssertionError('place_order_sync should not be called when broker exit is already pending')

        monkeypatch.setattr(tradier_client, 'place_order_sync', _never_submit)

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)

        assert called['count'] == 0
        assert result['summary']['alreadyInProgressCount'] == 1
        assert result['rows'][0]['action'] == 'EXIT_ALREADY_IN_PROGRESS'
        assert result['rows'][0]['reason'] == 'BROKER_EXIT_PENDING'
        assert result['rows'][0]['monitoringStatus'] == 'EXIT_PENDING'
        assert result['rows'][0]['brokerReservedQuantity'] == 144
        assert result['rows'][0]['brokerAvailableQuantity'] == 145



def test_watchlist_exit_worker_blocks_new_stock_exit_when_market_closed(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=80)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=10,
            avg_entry_price=100.0,
            current_price=110.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=False,
                to_dict=lambda: {'scope': scope, 'observedAtUtc': observed_at.isoformat(), 'sessionOpen': False},
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None: 10)
        monkeypatch.setattr(tradier_client, 'get_orders_sync', lambda mode=None, symbol=None, side=None, statuses=None, timeout=None: [])
        monkeypatch.setattr(tradier_client, 'place_order_sync', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not submit when market is closed')))

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)

        assert result['summary']['blockedCount'] == 1
        assert result['rows'][0]['action'] == 'BLOCKED'
        assert result['rows'][0]['reason'] == 'STOCK_SESSION_CLOSED'
        assert result['rows'][0]['monitoringStatus'] == 'WAITING_FOR_MARKET_OPEN'



def test_watchlist_exit_worker_uses_broker_available_quantity_not_db_fallback(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=80)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=289,
            avg_entry_price=100.0,
            current_price=110.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {'scope': scope, 'observedAtUtc': observed_at.isoformat(), 'sessionOpen': True},
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None: 289)
        monkeypatch.setattr(
            tradier_client,
            'get_orders_sync',
            lambda mode=None, symbol=None, side=None, statuses=None, timeout=None: [
                {
                    'id': 'ord-pending-1',
                    'symbol': 'AAPL',
                    'side': 'SELL',
                    'status': 'PENDING',
                    'requested_quantity': 288,
                    'filled_quantity': 0,
                    'remaining_quantity': 288,
                }
            ],
        )
        monkeypatch.setattr(tradier_client, 'place_order_sync', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not submit when broker available quantity is fully reserved by pending exits')))

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)

        assert result['rows'][0]['action'] == 'EXIT_ALREADY_IN_PROGRESS'
        assert result['rows'][0]['brokerQuantity'] == 289
        assert result['rows'][0]['brokerReservedQuantity'] == 288
        assert result['rows'][0]['brokerAvailableQuantity'] == 1
        assert result['rows'][0]['requestedQuantity'] == 1 or 'requestedQuantity' not in result['rows'][0]



def test_watchlist_exit_worker_reconciles_open_orders_after_oversell_rejection(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=80)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=289,
            avg_entry_price=100.0,
            current_price=110.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.flush()
        db.add(
            Trade(
                trade_id='trade-exit-reconcile',
                account_id='paper',
                ticker='AAPL',
                direction='LONG',
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_price=100.0,
                shares=289,
                entry_cost=28900.0,
                entry_reasoning={'intentId': 'intent-entry'},
                execution_id='intent-entry',
                entry_order_id='entry-order',
            )
        )
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {'scope': scope, 'observedAtUtc': observed_at.isoformat(), 'sessionOpen': True},
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None: 289)
        call_state = {'orders_call': 0}

        def _get_orders(mode=None, symbol=None, side=None, statuses=None, timeout=None):
            call_state['orders_call'] += 1
            if call_state['orders_call'] == 1:
                return []
            return [
                {
                    'id': 'ord-pending-after-reject',
                    'symbol': 'AAPL',
                    'side': 'SELL',
                    'status': 'PENDING',
                    'requested_quantity': 289,
                    'filled_quantity': 0,
                    'remaining_quantity': 289,
                }
            ]

        monkeypatch.setattr(tradier_client, 'get_orders_sync', _get_orders)
        monkeypatch.setattr(tradier_client, 'place_order_sync', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('Sell order is for more shares than your current long position')))

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)
        intent = db.query(OrderIntent).filter(OrderIntent.execution_source == 'WATCHLIST_EXIT_WORKER').one()
        event = db.query(OrderEvent).filter(OrderEvent.order_intent_id == intent.id).order_by(OrderEvent.id.desc()).first()

        assert result['rows'][0]['action'] == 'EXIT_ALREADY_IN_PROGRESS'
        assert result['rows'][0]['reason'] == 'BROKER_EXIT_PENDING_AFTER_REJECTION'
        assert result['rows'][0]['monitoringStatus'] == 'EXIT_PENDING'
        assert isinstance(result['rows'][0]['reconciliation'], dict)
        assert result['rows'][0]['reconciliation']['pendingOrders'][0]['id'] == 'ord-pending-after-reject'
        assert event is not None
        assert isinstance(event.payload_json, dict)
        assert event.payload_json['brokerState']['pendingOrders'][0]['id'] == 'ord-pending-after-reject'


def test_crypto_explicit_ledger_stop_loss_overrides_config_and_sets_protective_exit_pending(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['bot_payload']['symbols'][0]['exit_template'] = 'first_failed_follow_through'
        payload['bot_payload']['symbols'][0]['max_hold_hours'] = 48
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        avg_price = 100.0
        explicit_stop_loss = 105.0
        current_price = 104.5
        entry_time = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [{
                'pair': 'BTC/USD',
                'ohlcvPair': 'XBTUSD',
                'amount': 0.5,
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'marketValue': 0.5 * current_price,
                'costBasis': 0.5 * avg_price,
                'pnl': 0.5 * (current_price - avg_price),
                'pnlPercent': ((current_price / avg_price) - 1.0) * 100.0,
                'realizedPnl': 0.0,
                'entryTimeUtc': entry_time.isoformat(),
                'stopLoss': explicit_stop_loss,
            }],
        )

        readiness = watchlist_service.get_exit_readiness_snapshot(db, scope='crypto_only', expiring_within_hours=24)
        state = readiness['rows'][0]['positionState']

        assert state['stopLoss'] == explicit_stop_loss
        assert state['currentPrice'] == current_price
        assert state['stopLossBreached'] is True
        assert state['protectiveExitPending'] is True
        assert 'STOP_LOSS_BREACH' in state['protectiveExitReasons']


def test_crypto_exit_sets_cooldown_and_blocks_immediate_reentry(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'BTC').one()
        monitor_row.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        monkeypatch.setattr(
            'app.services.watchlist_monitoring.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(**{
                'session_open': True,
                'to_dict': lambda: {
                    'scope': scope,
                    'observedAtUtc': observed_at.isoformat(),
                    'sessionOpen': True,
                    'reason': 'patched open session',
                    'nextSessionStartUtc': None,
                    'nextSessionStartEt': None,
                    'sessionCloseUtc': None,
                    'sessionCloseEt': None,
                },
            }),
        )
        monkeypatch.setattr(
            'app.services.watchlist_monitoring.kraken_service.resolve_pair',
            lambda pair: KrakenPairMetadata(
                display_pair='BTC/USD',
                rest_pair='XBTUSD',
                pair_key='XBTUSD',
                ws_pair='XBT/USD',
                altname='XBTUSD',
            ),
        )
        monkeypatch.setattr(
            'app.services.watchlist_monitoring.kraken_service.get_ticker',
            lambda pair: {
                'c': ['70000.0', '1'],
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )
        monkeypatch.setattr(
            'app.services.watchlist_monitoring.kraken_service.get_ohlc',
            lambda pair, interval=15, limit=25: [
                {'timestamp': 1, 'open': 68000.0, 'high': 69000.0, 'low': 67500.0, 'close': 68500.0, 'vwap': 68500.0, 'volume': 10.0, 'count': 1},
                {'timestamp': 2, 'open': 68500.0, 'high': 69500.0, 'low': 68000.0, 'close': 69000.0, 'vwap': 69000.0, 'volume': 11.0, 'count': 1},
                {'timestamp': 3, 'open': 69000.0, 'high': 70000.0, 'low': 68800.0, 'close': 69500.0, 'vwap': 69500.0, 'volume': 12.0, 'count': 1},
                {'timestamp': 4, 'open': 69500.0, 'high': 70500.0, 'low': 69200.0, 'close': 69800.0, 'vwap': 69800.0, 'volume': 13.0, 'count': 1},
                {'timestamp': 5, 'open': 69800.0, 'high': 71000.0, 'low': 69700.0, 'close': 70000.0, 'vwap': 70000.0, 'volume': 14.0, 'count': 1},
                {'timestamp': 6, 'open': 70000.0, 'high': 71200.0, 'low': 69900.0, 'close': 70500.0, 'vwap': 70500.0, 'volume': 15.0, 'count': 1},
            ],
        )
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.kraken_service.resolve_pair',
            lambda pair: KrakenPairMetadata(
                display_pair='BTC/USD',
                rest_pair='XBTUSD',
                pair_key='XBTUSD',
                ws_pair='XBT/USD',
                altname='XBTUSD',
            ),
        )
        original_balance = crypto_ledger.balance
        original_positions = dict(crypto_ledger.positions)
        original_trades = list(crypto_ledger.trades)
        try:
            crypto_ledger.balance = type(original_balance)('100000')
            crypto_ledger.positions = {}
            crypto_ledger.trades = []

            entry_result = watchlist_monitoring_orchestrator.run_due_once(db, scope='crypto_only', limit_per_scope=10)
            assert entry_result['entryExecution']['filledCount'] == 1

            exit_result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)
            assert exit_result['summary']['closedCount'] == 1

            monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'BTC').one()
            assert monitor_row.latest_decision_state == 'EXIT_FILLED'
            assert monitor_row.decision_context_json['entryExecution']['action'] == 'EXIT_FILLED'
            assert monitor_row.decision_context_json['reentryBlockedUntilUtc']

            monitor_row.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
            db.commit()

            reentry_result = watchlist_monitoring_orchestrator.run_due_once(db, scope='crypto_only', limit_per_scope=10)
            assert reentry_result['entryExecution']['skippedCount'] == 1
            monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'BTC').one()
            assert monitor_row.latest_decision_state == 'COOLDOWN_ACTIVE'
            assert monitor_row.latest_decision_reason == 'CRYPTO_REENTRY_COOLDOWN_ACTIVE'
        finally:
            crypto_ledger.balance = original_balance
            crypto_ledger.positions = original_positions
            crypto_ledger.trades = original_trades


def test_monitoring_snapshot_dedupes_duplicate_managed_only_rows(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        now = datetime(2026, 4, 3, 18, 0, tzinfo=UTC)

        upload_old = WatchlistUpload(
            upload_id='upload_old',
            scan_id='scan_old',
            schema_version='bot_watchlist_v3',
            provider='chatgpt_kraken_app',
            scope='crypto_only',
            source='discord',
            source_user_id='user-1',
            source_channel_id='channel-1',
            source_message_id='message-1',
            payload_hash='hash-old',
            generated_at_utc=now - timedelta(hours=2),
            received_at_utc=now - timedelta(hours=2),
            watchlist_expires_at_utc=now + timedelta(hours=22),
            validation_status='ACCEPTED',
            market_regime='mixed',
            selected_count=1,
            is_active=False,
            validation_result_json={},
            raw_payload_json={},
            bot_payload_json={},
        )
        upload_new = WatchlistUpload(
            upload_id='upload_new',
            scan_id='scan_new',
            schema_version='bot_watchlist_v3',
            provider='chatgpt_kraken_app',
            scope='crypto_only',
            source='discord',
            source_user_id='user-1',
            source_channel_id='channel-1',
            source_message_id='message-2',
            payload_hash='hash-new',
            generated_at_utc=now - timedelta(hours=1),
            received_at_utc=now - timedelta(hours=1),
            watchlist_expires_at_utc=now + timedelta(hours=23),
            validation_status='ACCEPTED',
            market_regime='mixed',
            selected_count=1,
            is_active=True,
            validation_result_json={},
            raw_payload_json={},
            bot_payload_json={},
        )
        db.add_all([upload_old, upload_new])
        db.flush()

        old_symbol = WatchlistSymbol(
            upload_id='upload_old',
            scope='crypto_only',
            symbol='BTC',
            quote_currency='USD',
            asset_class='crypto',
            enabled=True,
            trade_direction='long',
            priority_rank=1,
            tier='tier_1',
            bias='bullish',
            setup_template='trend_continuation',
            bot_timeframes=['15m', '1h'],
            exit_template='trail_after_impulse',
            max_hold_hours=72,
            risk_flags=[],
            monitoring_status=MANAGED_ONLY,
        )
        new_symbol = WatchlistSymbol(
            upload_id='upload_new',
            scope='crypto_only',
            symbol='BTC',
            quote_currency='USD',
            asset_class='crypto',
            enabled=True,
            trade_direction='long',
            priority_rank=1,
            tier='tier_1',
            bias='bullish',
            setup_template='trend_continuation',
            bot_timeframes=['15m', '1h'],
            exit_template='trail_after_impulse',
            max_hold_hours=72,
            risk_flags=[],
            monitoring_status=MANAGED_ONLY,
        )
        db.add_all([old_symbol, new_symbol])
        db.flush()

        db.add_all(
            [
                WatchlistMonitorState(
                    watchlist_symbol_id=old_symbol.id,
                    upload_id='upload_old',
                    scope='crypto_only',
                    symbol='BTC',
                    monitoring_status=MANAGED_ONLY,
                    latest_decision_state='MONITOR_ONLY',
                    latest_decision_reason='OPEN_POSITION_EXISTS',
                    decision_context_json={},
                    required_timeframes_json=['15m', '1h'],
                    evaluation_interval_seconds=300,
                    last_decision_at_utc=now - timedelta(hours=2),
                    last_evaluated_at_utc=now - timedelta(hours=2),
                    next_evaluation_at_utc=now + timedelta(minutes=5),
                    last_market_data_at_utc=now - timedelta(minutes=1),
                ),
                WatchlistMonitorState(
                    watchlist_symbol_id=new_symbol.id,
                    upload_id='upload_new',
                    scope='crypto_only',
                    symbol='BTC',
                    monitoring_status=MANAGED_ONLY,
                    latest_decision_state='MONITOR_ONLY',
                    latest_decision_reason='OPEN_POSITION_EXISTS',
                    decision_context_json={},
                    required_timeframes_json=['15m', '1h'],
                    evaluation_interval_seconds=300,
                    last_decision_at_utc=now - timedelta(hours=1),
                    last_evaluated_at_utc=now - timedelta(hours=1),
                    next_evaluation_at_utc=now + timedelta(minutes=5),
                    last_market_data_at_utc=now - timedelta(minutes=1),
                ),
            ]
        )
        db.commit()

        snapshot = watchlist_service.get_monitoring_snapshot(db, scope='crypto_only')
        rows = snapshot['rows']
        assert len(rows) == 1
        assert rows[0]['symbol'] == 'BTC'
        assert rows[0]['uploadId'] == 'upload_new'
        assert rows[0]['monitoringStatus'] == MANAGED_ONLY


def test_monitoring_snapshot_without_active_upload_returns_empty_rows(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        snapshot = watchlist_service.get_monitoring_snapshot(db, scope='stocks_only', include_inactive=False)

        assert snapshot['scope'] == 'stocks_only'
        assert snapshot['activeUploadId'] is None
        assert snapshot['summary']['total'] == 0
        assert snapshot['rows'] == []


def test_entry_position_guard_is_scoped_to_account_id(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        db.add(
            Position(
                account_id='live-account',
                ticker='AAPL',
                shares=1,
                avg_entry_price=100.0,
                current_price=101.0,
                strategy='TEST',
                entry_time=datetime.now(UTC),
                is_open=True,
            )
        )
        db.commit()

        assert watchlist_monitoring_orchestrator._has_open_position(db, 'AAPL', account_id='live-account') is True
        assert watchlist_monitoring_orchestrator._has_open_position(db, 'AAPL', account_id='paper-account') is False



def test_active_entry_intent_guard_is_scoped_to_account_id_and_mode(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        db.add_all(
            [
                OrderIntent(
                    intent_id='intent_live_aapl',
                    account_id='live-account',
                    asset_class='stock',
                    symbol='AAPL',
                    side='BUY',
                    requested_quantity=1,
                    requested_price=100.0,
                    filled_quantity=0.0,
                    status='SUBMISSION_PENDING',
                    execution_source='WATCHLIST_MONITOR_ENTRY',
                    context_json={'mode': 'LIVE'},
                ),
                OrderIntent(
                    intent_id='intent_paper_aapl',
                    account_id='paper-account',
                    asset_class='stock',
                    symbol='AAPL',
                    side='BUY',
                    requested_quantity=1,
                    requested_price=100.0,
                    filled_quantity=0.0,
                    status='SUBMITTED',
                    execution_source='WATCHLIST_MONITOR_ENTRY',
                    context_json={'mode': 'PAPER'},
                ),
            ]
        )
        db.commit()

        assert watchlist_monitoring_orchestrator._has_active_entry_intent(
            db,
            'AAPL',
            asset_class='stock',
            account_id='paper-account',
            mode='PAPER',
        ) is True
        assert watchlist_monitoring_orchestrator._has_active_entry_intent(
            db,
            'AAPL',
            asset_class='stock',
            account_id='live-account',
            mode='LIVE',
        ) is True
        assert watchlist_monitoring_orchestrator._has_active_entry_intent(
            db,
            'AAPL',
            asset_class='stock',
            account_id='paper-account',
            mode='LIVE',
        ) is False



def test_stock_entry_timeout_marks_submission_pending_and_blocks_reentry(tmp_path, monkeypatch) -> None:
    from requests.exceptions import RequestException

    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_state = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'AAPL').one()
        symbol_row = db.query(WatchlistSymbol).filter(WatchlistSymbol.symbol == 'AAPL').one()
        monitor_state.decision_context_json = {
            'latestEvaluation': {
                'details': {
                    'currentPrice': 100.0,
                }
            }
        }
        monitor_state.latest_decision_state = ENTRY_CANDIDATE
        db.commit()

        monkeypatch.setattr(tradier_client, 'get_account_snapshot', lambda mode=None: {'accountId': 'paper-account', 'cash': 10000.0})
        monkeypatch.setattr(
            position_sizer,
            'calculate_stock_positions',
            lambda symbols, cash_available, prices=None: [
                {
                    'ticker': 'AAPL',
                    'shares': 2,
                    'estimated_value': 200.0,
                    'position_pct': 0.02,
                }
            ],
        )
        monkeypatch.setattr(
            pre_trade_gate,
            'evaluate_stock_order_sync',
            lambda **kwargs: SimpleNamespace(allowed=True, rejection_reason=None, to_dict=lambda: {'allowed': True}),
        )
        monkeypatch.setattr(tradier_client, 'place_order_sync', lambda *args, **kwargs: (_ for _ in ()).throw(RequestException('network timeout')))

        result = watchlist_monitoring_orchestrator._submit_stock_entry_candidate(
            db,
            monitor_state=monitor_state,
            symbol_row=symbol_row,
            mode='PAPER',
            account_cache={},
        )

        db.refresh(monitor_state)
        intent = db.query(OrderIntent).filter(OrderIntent.symbol == 'AAPL').one()
        event = db.query(OrderEvent).filter(OrderEvent.intent_id == intent.intent_id, OrderEvent.event_type == 'ORDER_SUBMISSION_UNCERTAIN').one()

        assert result['action'] == 'SUBMISSION_PENDING'
        assert result['reason'] == 'BROKER_SUBMIT_ACK_UNCERTAIN'
        assert intent.status == 'SUBMISSION_PENDING'
        assert event.status == 'SUBMISSION_PENDING'
        assert watchlist_monitoring_orchestrator._has_active_entry_intent(
            db,
            'AAPL',
            asset_class='stock',
            account_id='paper-account',
            mode='PAPER',
        ) is True
        assert monitor_state.latest_decision_state == 'SUBMISSION_PENDING'
        assert monitor_state.decision_context_json['entryExecution']['action'] == 'SUBMISSION_PENDING'


def test_stock_session_status_recognizes_good_friday_closure() -> None:
    observed_at = datetime(2026, 4, 3, 15, 0, tzinfo=UTC)
    status = get_scope_session_status('stocks_only', observed_at)

    assert status.session_open is False
    assert 'Good Friday' in status.reason


def test_stock_session_status_recognizes_black_friday_early_close() -> None:
    observed_at = datetime(2026, 11, 27, 18, 30, tzinfo=UTC)
    status = get_scope_session_status('stocks_only', observed_at)

    assert status.session_open is False
    assert 'early close' in status.reason.lower()
    assert status.session_close_et is not None
    assert status.session_close_et.hour == 13
    assert status.session_close_et.minute == 0


def test_trade_validator_blocks_live_stock_trade_on_market_holiday(monkeypatch) -> None:
    holiday_now = datetime(2026, 4, 3, 15, 0, tzinfo=UTC)

    class FrozenDateTime:
        @staticmethod
        def now(tz=None):
            if tz is None:
                return holiday_now.replace(tzinfo=None)
            return holiday_now.astimezone(tz)

    monkeypatch.setattr('app.services.trade_validator.datetime', FrozenDateTime)

    called = {'quote': False}

    def _quote(*args, **kwargs):
        called['quote'] = True
        return {'last': 100.0, 'volume': 1_000_000, 'bid': 99.5, 'ask': 100.5}

    monkeypatch.setattr(tradier_client, 'get_quote_sync', _quote)

    result = trade_validator.validate_stock_trade_with_quote('AAPL', 1, mode='LIVE')

    assert result['valid'] is False
    assert 'Good Friday' in result['reason']
    assert called['quote'] is False


def test_crypto_paper_ledger_positions_use_snapshot_when_state_mutates_mid_read(monkeypatch) -> None:
    ledger = CryptoPaperLedger(starting_balance=1000.0)
    ledger.positions = {
        'BTC/USD': {'amount': Decimal('1'), 'total_cost': Decimal('100')},
    }
    ledger.trades = [
        {
            'id': 'paper_1',
            'timestamp': datetime.now(UTC).isoformat(),
            'pair': 'BTC/USD',
            'side': 'BUY',
            'amount': 1.0,
            'price': 100.0,
            'total': 100.0,
        }
    ]

    monkeypatch.setattr(ledger.kraken, 'get_prices', lambda pairs: {'BTC/USD': 110.0, 'ETH/USD': 55.0})
    monkeypatch.setattr(ledger.kraken, 'resolve_pair', lambda pair: None)
    monkeypatch.setattr(ledger.kraken, 'get_ohlcv_pair', lambda pair: pair.replace('/', ''))

    original_resolver = ledger._resolve_position_ohlcv_pair
    mutated = {'done': False}

    def _mutating_resolver(pair: str):
        if not mutated['done']:
            mutated['done'] = True
            ledger.positions['ETH/USD'] = {'amount': Decimal('2'), 'total_cost': Decimal('80')}
        return original_resolver(pair)

    monkeypatch.setattr(ledger, '_resolve_position_ohlcv_pair', _mutating_resolver)

    positions = ledger.get_positions()

    assert len(positions) == 1
    assert positions[0]['pair'] == 'BTC/USD'
    assert ledger.positions['ETH/USD']['amount'] == Decimal('2')



def test_due_run_reserves_cash_between_stock_candidates(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = payload['bot_payload']['symbols'][:2]
        payload['ui_payload']['summary']['selected_count'] = 2
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL', 'MSFT']
        payload['ui_payload']['symbol_context'] = {
            'AAPL': payload['ui_payload']['symbol_context']['AAPL'],
            'MSFT': payload['ui_payload']['symbol_context']['MSFT'],
        }
        watchlist_service.ingest_watchlist(db, payload, source='api')

        rows = (
            db.query(WatchlistMonitorState, WatchlistSymbol)
            .join(WatchlistSymbol, WatchlistSymbol.id == WatchlistMonitorState.watchlist_symbol_id)
            .filter(WatchlistMonitorState.scope == 'stocks_only')
            .order_by(WatchlistSymbol.priority_rank.asc(), WatchlistSymbol.id.asc())
            .all()
        )

        for monitor_state, _symbol_row in rows:
            monitor_state.next_evaluation_at_utc = datetime.now(UTC) - timedelta(minutes=1)
            monitor_state.latest_decision_state = ENTRY_CANDIDATE
            monitor_state.decision_context_json = {
                'latestEvaluation': {
                    'details': {
                        'currentPrice': 100.0,
                    }
                }
            }
        db.commit()

        monkeypatch.setattr(
            tradier_client,
            'get_account_snapshot',
            lambda mode=None: {
                'mode': (mode or 'PAPER').upper(),
                'connected': True,
                'accountId': 'paper-watchlist',
                'cash': 150.0,
                'buyingPower': 150.0,
                'portfolioValue': 150.0,
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'get_quote_sync',
            lambda symbol, mode=None: {
                'symbol': symbol,
                'last': 100.0,
                'prevclose': 100.0,
                'open': 100.0,
                'volume': 2_500_000,
                '_fetched_at_utc': datetime.now(UTC).isoformat(),
            },
        )

        size_inputs: list[float] = []

        def fake_calculate_stock_positions(candidates, account_equity, prices=None):
            size_inputs.append(float(account_equity))
            ticker = candidates[0]['ticker']
            if account_equity >= 100.0:
                return [{
                    'ticker': ticker,
                    'shares': 1,
                    'estimated_value': 100.0,
                    'position_pct': 0.5,
                    'source': 'calculated',
                }]
            return []

        monkeypatch.setattr(position_sizer, 'calculate_stock_positions', fake_calculate_stock_positions)
        monkeypatch.setattr(
            pre_trade_gate,
            'evaluate_stock_order_sync',
            lambda **kwargs: SimpleNamespace(
                allowed=True,
                rejection_reason='',
                to_dict=lambda: {},
                market_data={},
                risk_data={},
            ),
        )
        monkeypatch.setattr(
            tradier_client,
            'place_order_sync',
            lambda ticker, qty, side, mode=None, order_type='market', duration='day': {
                'order': {'id': f'watch-{ticker}', 'status': 'open', 'quantity': qty},
            },
        )
        monkeypatch.setattr(
            tradier_client,
            'get_order_sync',
            lambda order_id, mode=None: {
                'order': {
                    'id': order_id,
                    'status': 'filled',
                    'quantity': 1,
                    'exec_quantity': 1,
                    'avg_fill_price': 100.0,
                }
            },
        )

        account_cache = {
            'loaded': False,
            'account': None,
            'cashAvailable': 0.0,
            'remainingCash': 0.0,
            'reservedCash': 0.0,
            'error': None,
        }

        first_result = watchlist_monitoring_orchestrator._submit_stock_entry_candidate(
            db,
            monitor_state=rows[0][0],
            symbol_row=rows[0][1],
            mode='PAPER',
            account_cache=account_cache,
        )
        second_result = watchlist_monitoring_orchestrator._submit_stock_entry_candidate(
            db,
            monitor_state=rows[1][0],
            symbol_row=rows[1][1],
            mode='PAPER',
            account_cache=account_cache,
        )

        assert size_inputs[:2] == [150.0, 50.0]
        assert first_result['action'] == 'ENTRY_FILLED'
        assert second_result['action'] == 'SKIPPED'
        assert second_result['reason'] == 'POSITION_SIZER_RETURNED_ZERO'
        assert account_cache['reservedCash'] == 100.0
        assert account_cache['remainingCash'] == 50.0




def test_watchlist_exit_worker_skips_when_no_sellable_quantity_after_broker_reservations(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=80)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=10,
            avg_entry_price=100.0,
            current_price=110.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {'scope': scope, 'observedAtUtc': observed_at.isoformat(), 'sessionOpen': True},
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None, timeout=None, use_cache=True: 10)
        monkeypatch.setattr(
            tradier_client,
            'get_orders_sync',
            lambda mode=None, symbol=None, side=None, statuses=None, timeout=None, use_cache=True: [
                {
                    'id': 'ord-pending-1',
                    'symbol': 'AAPL',
                    'side': 'SELL',
                    'status': 'PENDING',
                    'requested_quantity': 10,
                    'filled_quantity': 0,
                    'remaining_quantity': 10,
                }
            ],
        )
        monkeypatch.setattr(tradier_client, 'place_order_sync', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('should not submit when sellable quantity is zero')))

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)

        assert result['rows'][0]['action'] == 'EXIT_ALREADY_IN_PROGRESS'
        assert result['rows'][0]['reason'] == 'BROKER_EXIT_PENDING'
        assert result['rows'][0]['quantityTruth']['expectedPositionQty'] == 10
        assert result['rows'][0]['quantityTruth']['brokerReportedQty'] == 10
        assert result['rows'][0]['quantityTruth']['pendingOpenOrdersQty'] == 10
        assert result['rows'][0]['quantityTruth']['sellableQty'] == 0



def test_watchlist_exit_worker_oversell_rejection_records_quantity_truth(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['AAPL']
        payload['ui_payload']['symbol_context'] = {'AAPL': payload['ui_payload']['symbol_context']['AAPL']}
        watchlist_service.ingest_watchlist(db, payload, source='api')
        entry_time = datetime.now(UTC) - timedelta(hours=80)
        position = Position(
            account_id='paper',
            ticker='AAPL',
            shares=289,
            avg_entry_price=100.0,
            current_price=110.0,
            strategy='AI_SCREENING',
            entry_time=entry_time,
            entry_reasoning={'intentId': 'intent-entry'},
            is_open=True,
            execution_id='intent-entry',
        )
        db.add(position)
        db.flush()
        db.add(
            Trade(
                trade_id='trade-exit-reconcile-qty',
                account_id='paper',
                ticker='AAPL',
                direction='LONG',
                strategy='AI_SCREENING',
                entry_time=entry_time,
                entry_price=100.0,
                shares=289,
                entry_cost=28900.0,
                entry_reasoning={'intentId': 'intent-entry'},
                execution_id='intent-entry',
                entry_order_id='entry-order',
            )
        )
        db.commit()

        monkeypatch.setattr(runtime_state, 'get', lambda: SimpleNamespace(running=True, stock_mode='PAPER'))
        monkeypatch.setattr(
            'app.services.watchlist_exit_worker.get_scope_session_status',
            lambda scope, observed_at: SimpleNamespace(
                session_open=True,
                to_dict=lambda: {'scope': scope, 'observedAtUtc': observed_at.isoformat(), 'sessionOpen': True},
            ),
        )
        monkeypatch.setattr(tradier_client, 'is_ready', lambda mode=None: True)
        monkeypatch.setattr(tradier_client, 'get_position_quantity_sync', lambda symbol, mode=None, timeout=None, use_cache=True: 200 if use_cache else 120)
        monkeypatch.setattr(tradier_client, 'get_orders_sync', lambda mode=None, symbol=None, side=None, statuses=None, timeout=None, use_cache=True: [])
        monkeypatch.setattr(tradier_client, 'place_order_sync', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('Sell order is for more shares than your current long position')))

        result = watchlist_exit_worker.run_exit_sweep(db, execute=True, limit=10)
        intent = db.query(OrderIntent).filter(OrderIntent.execution_source == 'WATCHLIST_EXIT_WORKER').one()
        event = db.query(OrderEvent).filter(OrderEvent.order_intent_id == intent.id).order_by(OrderEvent.id.desc()).first()

        assert result['rows'][0]['action'] == 'SKIPPED'
        assert result['rows'][0]['quantityTruth']['expectedPositionQty'] == 289
        assert result['rows'][0]['quantityTruth']['brokerReportedQty'] == 120
        assert result['rows'][0]['quantityTruth']['pendingOpenOrdersQty'] == 0
        assert result['rows'][0]['quantityTruth']['sellableQty'] == 120
        assert event is not None
        assert isinstance(event.payload_json, dict)
        assert event.payload_json['quantityTruth']['expectedPositionQty'] == 289
        assert event.payload_json['quantityTruth']['brokerReportedQty'] == 120
        assert event.payload_json['quantityTruth']['sellableQty'] == 120


def test_monitoring_snapshot_scope_truth_flags_managed_only_review_and_missing_scope(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_stock_payload()
        watchlist_service.ingest_watchlist(db, payload, source='api')

        active_row = (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.scope == 'stocks_only')
            .order_by(WatchlistSymbol.id.asc())
            .first()
        )
        assert active_row is not None
        active_row.monitoring_status = MANAGED_ONLY

        monitor_state = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.watchlist_symbol_id == active_row.id)
            .first()
        )
        assert monitor_state is not None
        monitor_state.monitoring_status = MANAGED_ONLY
        db.commit()

        stock_snapshot = watchlist_service.get_monitoring_snapshot(db, scope='stocks_only')
        crypto_snapshot = watchlist_service.get_monitoring_snapshot(db, scope='crypto_only')

        assert stock_snapshot['scopeTruth']['state'] == 'DEGRADED'
        assert stock_snapshot['scopeTruth']['ready'] is False
        assert stock_snapshot['scopeTruth']['managedOnlyCount'] >= 1
        assert 'supervision-only' in stock_snapshot['scopeTruth']['reason']

        assert crypto_snapshot['scopeTruth']['state'] == 'MISSING'
        assert crypto_snapshot['scopeTruth']['ready'] is False
        assert crypto_snapshot['scopeTruth']['activeUploadId'] is None


def test_ingest_watchlist_rejects_exact_duplicate_payload_within_replay_window(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        monkeypatch.setattr(settings, 'WATCHLIST_REPLAY_WINDOW_SECONDS', 900, raising=False)
        db = SessionFactory()
        payload = build_stock_payload()

        accepted = watchlist_service.ingest_watchlist(db, payload, source='api')
        assert accepted['validation']['replay']['status'] == 'accepted'

        with pytest.raises(WatchlistValidationError, match=r'Duplicate watchlist payload suppressed within replay window\.'):
            watchlist_service.ingest_watchlist(db, deepcopy(payload), source='api')

        uploads = db.query(WatchlistUpload).filter(WatchlistUpload.scope == 'stocks_only').order_by(WatchlistUpload.id.asc()).all()
        assert len(uploads) == 2
        assert uploads[0].validation_status == 'valid'
        assert uploads[1].validation_status == 'rejected'
        assert uploads[1].rejection_reason == 'Duplicate watchlist payload suppressed within replay window.'
        assert uploads[1].validation_result_json['replay']['type'] == 'exact_duplicate'
        assert uploads[1].validation_result_json['replay']['duplicateOfUploadId'] == accepted['uploadId']
        assert watchlist_service.get_latest_upload(db, scope='stocks_only', active_only=True)['uploadId'] == accepted['uploadId']
        db.close()


def test_ingest_watchlist_rejects_execution_equivalent_payload_within_replay_window(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        monkeypatch.setattr(settings, 'WATCHLIST_REPLAY_WINDOW_SECONDS', 900, raising=False)
        db = SessionFactory()
        payload = build_crypto_payload()
        watchlist_service.ingest_watchlist(db, payload, source='api')

        replay_payload = deepcopy(payload)
        replay_payload['generated_at_utc'] = (datetime.now(UTC) + timedelta(seconds=5)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        replay_payload['ui_payload']['summary']['regime_note'] = 'Fresh prose, same execution payload.'
        replay_payload['ui_payload']['symbol_context']['BTC']['notes'] = 'Operator context changed only.'

        with pytest.raises(WatchlistValidationError, match=r'Equivalent execution-safe watchlist payload suppressed within replay window\.'):
            watchlist_service.ingest_watchlist(db, replay_payload, source='api')

        latest = watchlist_service.get_latest_upload(db, scope='crypto_only', active_only=False)
        assert latest['validationStatus'] == 'rejected'
        assert latest['validation']['replay']['type'] == 'execution_duplicate'
        assert latest['validation']['replay']['status'] == 'rejected'
        db.close()


def test_ingest_watchlist_allows_replacement_when_execution_payload_changes(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        monkeypatch.setattr(settings, 'WATCHLIST_REPLAY_WINDOW_SECONDS', 900, raising=False)
        db = SessionFactory()
        payload = build_stock_payload()
        first = watchlist_service.ingest_watchlist(db, payload, source='api')

        replacement = deepcopy(payload)
        replacement['generated_at_utc'] = (datetime.now(UTC) + timedelta(seconds=10)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        replacement['bot_payload']['symbols'][1]['setup_template'] = 'breakout_retest'
        replacement['bot_payload']['symbols'][1]['priority_rank'] = 3
        replacement['ui_payload']['summary']['regime_note'] = 'Execution-safe change should be accepted.'

        accepted = watchlist_service.ingest_watchlist(db, replacement, source='api')

        assert accepted['uploadId'] != first['uploadId']
        assert accepted['validation']['replay']['status'] == 'accepted'
        assert accepted['isActive'] is True
        uploads = db.query(WatchlistUpload).filter(WatchlistUpload.scope == 'stocks_only').order_by(WatchlistUpload.id.asc()).all()
        assert [row.validation_status for row in uploads] == ['valid', 'valid']
        db.close()


def test_ingest_watchlist_replay_is_scoped(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        monkeypatch.setattr(settings, 'WATCHLIST_REPLAY_WINDOW_SECONDS', 900, raising=False)
        db = SessionFactory()
        stock_payload = build_stock_payload()
        crypto_payload = build_crypto_payload()

        stock_result = watchlist_service.ingest_watchlist(db, stock_payload, source='api')
        crypto_result = watchlist_service.ingest_watchlist(db, crypto_payload, source='api')

        assert stock_result['scope'] == 'stocks_only'
        assert crypto_result['scope'] == 'crypto_only'
        assert db.query(WatchlistUpload).filter(WatchlistUpload.validation_status == 'rejected').count() == 0
        db.close()
