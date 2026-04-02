from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.main import app
from app.models.order_intent import OrderIntent
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.models.watchlist_upload import WatchlistUpload
from app.services.execution_lifecycle import execution_lifecycle
from app.services.kraken_service import crypto_ledger


@contextmanager
def build_session_factory(tmp_path) -> Iterator[sessionmaker]:
    db_path = tmp_path / 'phase7b_position_inspect.db'
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


def test_crypto_position_inspect_returns_signal_sizing_and_lifecycle(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        upload = WatchlistUpload(
            upload_id='upl-crypto-1',
            scan_id='scan-crypto-1',
            schema_version='bot_watchlist_v3',
            provider='claude_tradier_mcp',
            scope='crypto_only',
            source='test',
            payload_hash='hash-1',
            generated_at_utc=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            received_at_utc=datetime(2026, 4, 1, 12, 1, tzinfo=UTC),
            watchlist_expires_at_utc=datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
            validation_status='ACCEPTED',
            market_regime='mixed',
            selected_count=1,
            is_active=True,
            validation_result_json={},
            raw_payload_json={},
            bot_payload_json={},
        )
        db.add(upload)
        db.flush()

        row = WatchlistSymbol(
            upload_id=upload.upload_id,
            scope='crypto_only',
            symbol='TAO/USD',
            quote_currency='USD',
            asset_class='crypto',
            enabled=True,
            trade_direction='long',
            priority_rank=1,
            tier='tier_1',
            bias='bullish',
            setup_template='trend_continuation',
            bot_timeframes=['15m', '1h', '4h'],
            exit_template='scale_out_then_trail',
            max_hold_hours=72,
            risk_flags=['high_beta'],
            monitoring_status='ACTIVE',
        )
        db.add(row)
        db.flush()

        monitor_state = WatchlistMonitorState(
            watchlist_symbol_id=row.id,
            upload_id=upload.upload_id,
            scope='crypto_only',
            symbol='TAO/USD',
            monitoring_status='ACTIVE',
            latest_decision_state='ENTRY_CANDIDATE',
            latest_decision_reason='Momentum is continuing above reference trend anchors.',
            decision_context_json={
                'latestEvaluation': {
                    'state': 'ENTRY_CANDIDATE',
                    'reason': 'Momentum is continuing above reference trend anchors.',
                    'evaluatedAtUtc': '2026-04-01T12:05:00+00:00',
                    'marketDataAtUtc': '2026-04-01T12:04:30+00:00',
                    'details': {
                        'currentPrice': 410.5,
                        'continuityOk': True,
                        'recentHigh': 412.0,
                        'recentLow': 389.5,
                    },
                }
            },
            required_timeframes_json=['15m', '1h', '4h'],
            evaluation_interval_seconds=300,
            last_decision_at_utc=datetime(2026, 4, 1, 12, 5, tzinfo=UTC),
            last_evaluated_at_utc=datetime(2026, 4, 1, 12, 5, tzinfo=UTC),
            next_evaluation_at_utc=datetime(2026, 4, 1, 12, 10, tzinfo=UTC),
            last_market_data_at_utc=datetime(2026, 4, 1, 12, 4, 30, tzinfo=UTC),
        )
        db.add(monitor_state)
        db.flush()

        intent = execution_lifecycle.create_order_intent(
            db,
            account_id='paper-crypto-ledger',
            asset_class='crypto',
            symbol='TAO/USD',
            side='BUY',
            requested_quantity=0.5,
            requested_price=400.0,
            execution_source='WATCHLIST_MONITOR_ENTRY',
            context={
                'mode': 'PAPER',
                'estimatedValue': 200.0,
                'positionPct': 0.02,
                'ohlcvPair': 'TAOUSD',
                'displayPair': 'TAO/USD',
            },
        )
        execution_lifecycle.record_event(
            db,
            intent,
            event_type='ORDER_SUBMITTED',
            status='SUBMITTED',
            message='Crypto paper ledger accepted order for TAO/USD',
            payload={'id': 'paper_1'},
            event_time=datetime(2026, 4, 1, 12, 6, tzinfo=UTC),
        )
        execution_lifecycle.record_event(
            db,
            intent,
            event_type='ORDER_STATUS_UPDATED',
            status='FILLED',
            message='Confirmed fill for TAO/USD: 0.5 filled',
            payload={'id': 'paper_1'},
            event_time=datetime(2026, 4, 1, 12, 6, tzinfo=UTC),
        )
        intent.status = 'FILLED'
        intent.filled_quantity = 0.5
        intent.avg_fill_price = 400.0
        intent.first_fill_at = datetime(2026, 4, 1, 12, 6, tzinfo=UTC)
        intent.last_fill_at = datetime(2026, 4, 1, 12, 6, tzinfo=UTC)
        db.commit()

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [
                {
                    'pair': 'TAO/USD',
                    'ohlcvPair': 'TAOUSD',
                    'amount': 0.5,
                    'avgPrice': 400.0,
                    'currentPrice': 410.5,
                    'marketValue': 205.25,
                    'costBasis': 200.0,
                    'pnl': 5.25,
                    'pnlPercent': 2.625,
                    'entryTimeUtc': '2026-04-01T12:06:00+00:00',
                    'realizedPnl': 0.0,
                }
            ],
        )

        def override_get_db():
            local_db = SessionFactory()
            try:
                yield local_db
            finally:
                local_db.close()

        app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(app)
            response = client.get('/api/positions/inspect', params={'asset_class': 'crypto', 'symbol': 'TAO/USD'})
        finally:
            app.dependency_overrides.clear()
            db.close()

        assert response.status_code == 200
        payload = response.json()
        assert payload['assetClass'] == 'crypto'
        assert payload['displaySymbol'] == 'TAO/USD'
        assert payload['signalSnapshot']['setupTemplate'] == 'trend_continuation'
        assert payload['signalSnapshot']['marketRegime'] == 'mixed'
        assert payload['sizing']['filledQuantity'] == 0.5
        assert payload['timeframeAlignment']['configured'] == ['15m', '1h', '4h']
        assert payload['timeframeAlignment']['confirmed'] == ['15m']
        assert payload['exitPlan']['template'] == 'scale_out_then_trail'
        assert [event['eventType'] for event in payload['lifecycle']] == [
            'INTENT_CREATED',
            'ORDER_SUBMITTED',
            'ORDER_STATUS_UPDATED',
        ]
        assert payload['latestEvaluation']['state'] == 'ENTRY_CANDIDATE'


def test_crypto_position_inspect_uses_symbol_aliases_and_intent_watchlist_fallback(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        intent = execution_lifecycle.create_order_intent(
            db,
            account_id='paper-crypto-ledger',
            asset_class='crypto',
            symbol='TAO/USD',
            side='BUY',
            requested_quantity=1.0,
            requested_price=300.0,
            execution_source='WATCHLIST_MONITOR_ENTRY',
            context={
                'mode': 'PAPER',
                'estimatedValue': 300.0,
                'positionPct': 0.1,
                'ohlcvPair': 'TAOUSD',
                'displayPair': 'TAO/USD',
                'watchlist': {
                    'uploadId': 'upl-fallback',
                    'scope': 'crypto_only',
                    'priorityRank': 2,
                    'tier': 'tier_1',
                    'bias': 'bullish',
                    'tradeDirection': 'long',
                    'setupTemplate': 'trend_continuation',
                    'exitTemplate': 'scale_out_then_trail',
                    'riskFlags': ['high_beta'],
                    'botTimeframes': ['15m', '1h', '4h'],
                    'maxHoldHours': 72,
                },
            },
        )
        intent.status = 'FILLED'
        intent.filled_quantity = 1.0
        intent.avg_fill_price = 300.0
        intent.first_fill_at = datetime(2026, 4, 1, 23, 0, tzinfo=UTC)
        intent.last_fill_at = datetime(2026, 4, 1, 23, 0, tzinfo=UTC)

        upload = WatchlistUpload(
            upload_id='upl-fallback',
            scan_id='scan-fallback',
            schema_version='bot_watchlist_v3',
            provider='claude_tradier_mcp',
            scope='crypto_only',
            source='test',
            payload_hash='hash-fallback',
            generated_at_utc=datetime(2026, 4, 1, 22, 0, tzinfo=UTC),
            received_at_utc=datetime(2026, 4, 1, 22, 1, tzinfo=UTC),
            watchlist_expires_at_utc=datetime(2026, 4, 2, 22, 0, tzinfo=UTC),
            validation_status='ACCEPTED',
            market_regime='mixed',
            selected_count=1,
            is_active=True,
            validation_result_json={},
            raw_payload_json={},
            bot_payload_json={},
        )
        db.add(upload)
        db.flush()

        row = WatchlistSymbol(
            upload_id=upload.upload_id,
            scope='crypto_only',
            symbol='TAO',
            quote_currency='USD',
            asset_class='crypto',
            enabled=True,
            trade_direction='long',
            priority_rank=2,
            tier='tier_1',
            bias='bullish',
            setup_template='trend_continuation',
            bot_timeframes=['15m', '1h', '4h'],
            exit_template='scale_out_then_trail',
            max_hold_hours=72,
            risk_flags=['high_beta'],
            monitoring_status='ACTIVE',
        )
        db.add(row)
        db.flush()

        monitor_state = WatchlistMonitorState(
            watchlist_symbol_id=row.id,
            upload_id=upload.upload_id,
            scope='crypto_only',
            symbol='TAO',
            monitoring_status='ACTIVE',
            latest_decision_state='ENTRY_CANDIDATE',
            latest_decision_reason='Trend continuation still intact.',
            decision_context_json={
                'setupTemplate': 'trend_continuation',
                'exitTemplate': 'scale_out_then_trail',
                'botTimeframes': ['15m', '1h', '4h'],
                'tradeDirection': 'long',
                'bias': 'bullish',
                'tier': 'tier_1',
                'riskFlags': ['high_beta'],
                'maxHoldHours': 72,
                'latestEvaluation': {
                    'state': 'ENTRY_CANDIDATE',
                    'reason': 'Trend continuation still intact.',
                    'marketDataAtUtc': '2026-04-01T22:59:30+00:00',
                    'details': {
                        'currentPrice': 305.5844,
                        'continuityOk': True,
                        'recentHigh': 309.1,
                        'recentLow': 295.8,
                    },
                },
            },
            required_timeframes_json=['15m', '1h', '4h'],
            evaluation_interval_seconds=300,
            last_decision_at_utc=datetime(2026, 4, 1, 23, 0, tzinfo=UTC),
            last_evaluated_at_utc=datetime(2026, 4, 1, 23, 0, tzinfo=UTC),
            next_evaluation_at_utc=datetime(2026, 4, 1, 23, 5, tzinfo=UTC),
            last_market_data_at_utc=datetime(2026, 4, 1, 22, 59, 30, tzinfo=UTC),
        )
        db.add(monitor_state)
        db.commit()

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [
                {
                    'pair': 'TAO/USD',
                    'ohlcvPair': 'TAOUSD',
                    'amount': 1.0,
                    'avgPrice': 300.0,
                    'currentPrice': 305.5844,
                    'marketValue': 305.5844,
                    'costBasis': 300.0,
                    'pnl': 5.5844,
                    'pnlPercent': 1.8614667,
                    'entryTimeUtc': '2026-04-01T23:00:00+00:00',
                    'realizedPnl': 0.0,
                }
            ],
        )

        def override_get_db():
            local_db = SessionFactory()
            try:
                yield local_db
            finally:
                local_db.close()

        app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(app)
            response = client.get('/api/positions/inspect', params={'asset_class': 'crypto', 'symbol': 'TAO/USD'})
        finally:
            app.dependency_overrides.clear()
            db.close()

        assert response.status_code == 200
        payload = response.json()
        assert payload['signalSnapshot']['setupTemplate'] == 'trend_continuation'
        assert payload['signalSnapshot']['priorityRank'] == 2
        assert payload['timeframeAlignment']['configured'] == ['15m', '1h', '4h']
        assert payload['exitPlan']['template'] == 'scale_out_then_trail'
        assert payload['exitPlan']['maxHoldHours'] == 72
        assert payload['latestEvaluation']['state'] == 'ENTRY_CANDIDATE'
