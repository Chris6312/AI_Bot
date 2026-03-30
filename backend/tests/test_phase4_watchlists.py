from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
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
            lambda pair: KrakenPairMetadata(display_pair='BTC/USD', rest_pair='XBTUSD', pair_key='XXBTZUSD', ws_pair='XBT/USD', altname='XBTUSD'),
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
