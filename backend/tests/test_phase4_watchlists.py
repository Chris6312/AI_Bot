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
from app.models.position import Position
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.models.watchlist_ui_context import WatchlistUiContext
from app.models.watchlist_upload import WatchlistUpload
from app.services.control_plane import discord_decision_guard
from app.services.kraken_service import crypto_ledger, kraken_service
from app.services.market_sessions import calculate_next_scope_evaluation_at, get_scope_session_status
from app.services.tradier_client import tradier_client
from app.services.template_evaluator import (
    DATA_STALE,
    ENTRY_CANDIDATE,
    MONITOR_ONLY,
    template_evaluation_service,
)
from app.services.watchlist_monitoring import watchlist_monitoring_orchestrator
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
