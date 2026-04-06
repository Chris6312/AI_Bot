from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

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
from app.services.position_inspect import position_inspect_service
from app.services.watchlist_service import watchlist_service
from tests.test_phase4_watchlists import build_crypto_payload


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
        assert payload['signalSnapshot']['executionSource'] == 'WATCHLIST_MONITOR_ENTRY'
        assert payload['signalSnapshot']['priorityRank'] == 1
        assert payload['signalSnapshot']['riskFlags'] == ['high_beta']
        assert payload['signalSnapshot']['cooldownActive'] is False
        assert payload['sizing']['filledQuantity'] == 0.5
        assert payload['sizing']['displayPair'] == 'TAO/USD'
        assert payload['sizing']['ohlcvPair'] == 'TAOUSD'
        assert payload['timeframeAlignment']['configured'] == ['15m', '1h', '4h']
        assert payload['timeframeAlignment']['confirmed'] == ['15m']
        assert payload['exitPlan']['template'] == 'scale_out_then_trail'
        assert payload['exitPlan']['expectedExitThresholds']['stopLoss'] is not None
        assert payload['exitPlan']['expectedExitThresholds']['profitTarget'] is not None
        assert payload['exitPlan']['stopDistance'] is not None
        assert [event['eventType'] for event in payload['lifecycle']] == [
            'INTENT_CREATED',
            'ORDER_SUBMITTED',
            'ORDER_STATUS_UPDATED',
        ]
        assert payload['latestEvaluation']['state'] == 'ENTRY_CANDIDATE'
        assert payload['latestEvaluation']['details']['cooldownActive'] is False
        assert payload['exitWorker']['logicState'] == 'SCALE_OUT_READY'
        assert payload['exitWorker']['nextExitTrigger'] == 'Scale-out submission'


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


def test_crypto_position_inspect_surfaces_protective_exit_pending_when_stop_loss_breached(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        upload = WatchlistUpload(
            upload_id='upl-crypto-stop-1',
            scan_id='scan-crypto-stop-1',
            schema_version='bot_watchlist_v3',
            provider='claude_tradier_mcp',
            scope='crypto_only',
            source='test',
            payload_hash='hash-stop-1',
            generated_at_utc=datetime(2026, 4, 3, 16, 0, tzinfo=UTC),
            received_at_utc=datetime(2026, 4, 3, 16, 1, tzinfo=UTC),
            watchlist_expires_at_utc=datetime(2026, 4, 4, 16, 0, tzinfo=UTC),
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
            symbol='2Z',
            quote_currency='USD',
            asset_class='crypto',
            enabled=True,
            trade_direction='long',
            priority_rank=1,
            tier='tier_1',
            bias='bullish',
            setup_template='trend_continuation',
            bot_timeframes=['15m', '1h', '4h'],
            exit_template='first_failed_follow_through',
            max_hold_hours=48,
            risk_flags=['high_beta'],
            monitoring_status='ACTIVE',
        )
        db.add(row)
        db.flush()

        monitor_state = WatchlistMonitorState(
            watchlist_symbol_id=row.id,
            upload_id=upload.upload_id,
            scope='crypto_only',
            symbol='2Z',
            monitoring_status='ACTIVE',
            latest_decision_state='WAITING_FOR_SETUP',
            latest_decision_reason='Trend continuation thresholds are not met.',
            decision_context_json={
                'latestEvaluation': {
                    'state': 'WAITING_FOR_SETUP',
                    'reason': 'Trend continuation thresholds are not met.',
                    'evaluatedAtUtc': '2026-04-03T16:05:55+00:00',
                    'marketDataAtUtc': '2026-04-03T16:05:56+00:00',
                    'details': {
                        'currentPrice': 0.07592,
                        'recentHigh': 0.07592,
                        'recentLow': 0.07576,
                        'continuityOk': True,
                    },
                }
            },
            required_timeframes_json=['15m', '1h', '4h'],
            evaluation_interval_seconds=300,
            last_decision_at_utc=datetime(2026, 4, 3, 16, 5, tzinfo=UTC),
            last_evaluated_at_utc=datetime(2026, 4, 3, 16, 5, tzinfo=UTC),
            next_evaluation_at_utc=datetime(2026, 4, 3, 16, 10, tzinfo=UTC),
            last_market_data_at_utc=datetime(2026, 4, 3, 16, 5, 56, tzinfo=UTC),
        )
        db.add(monitor_state)
        db.commit()

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [
                {
                    'pair': '2Z/USD',
                    'ohlcvPair': '2ZUSD',
                    'amount': 1000.0,
                    'avgPrice': 0.0778,
                    'currentPrice': 0.07592,
                    'marketValue': 75.92,
                    'costBasis': 77.8,
                    'pnl': -1.88,
                    'pnlPercent': -2.41645,
                    'entryTimeUtc': '2026-04-03T14:05:00+00:00',
                    'realizedPnl': 0.0,
                    'stopLoss': 0.076633,
                    'profitTarget': 0.079745,
                    'trailingStop': 0.075466,
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
            response = client.get('/api/positions/inspect', params={'asset_class': 'crypto', 'symbol': '2Z/USD'})
        finally:
            app.dependency_overrides.clear()
            db.close()

        assert response.status_code == 200
        payload = response.json()
        assert payload['exitPlan']['stopLoss'] == 0.076633
        assert payload['positionSnapshot']['currentPrice'] == 0.07592
        assert payload['signalSnapshot']['latestDecisionState'] == 'EXIT_PENDING'
        assert 'STOP_LOSS_BREACH' in payload['signalSnapshot']['latestDecisionReason']
        assert payload['latestEvaluation']['state'] == 'EXIT_PENDING'
        assert 'STOP_LOSS_BREACH' in payload['latestEvaluation']['reason']
        assert payload['exitWorker']['logicState'] == 'EXIT_PENDING'


def test_crypto_position_inspect_uses_live_runner_protection_state(tmp_path, monkeypatch) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        upload = WatchlistUpload(
            upload_id='upl-crypto-protection-1',
            scan_id='scan-crypto-protection-1',
            schema_version='bot_watchlist_v3',
            provider='claude_tradier_mcp',
            scope='crypto_only',
            source='test',
            payload_hash='hash-protection-1',
            generated_at_utc=datetime(2026, 4, 5, 12, 0, tzinfo=UTC),
            received_at_utc=datetime(2026, 4, 5, 12, 1, tzinfo=UTC),
            watchlist_expires_at_utc=datetime(2026, 4, 6, 12, 0, tzinfo=UTC),
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
            symbol='BTC/USD',
            quote_currency='USD',
            asset_class='crypto',
            enabled=True,
            trade_direction='long',
            priority_rank=1,
            tier='tier_1',
            bias='bullish',
            setup_template='trend_continuation',
            bot_timeframes=['15m', '1h', '4h'],
            exit_template='first_failed_follow_through',
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
            symbol='BTC/USD',
            monitoring_status='ACTIVE',
            latest_decision_state='SKIPPED',
            latest_decision_reason='OPEN_POSITION_EXISTS',
            decision_context_json={
                'latestEvaluation': {
                    'state': 'SKIPPED',
                    'reason': 'OPEN_POSITION_EXISTS',
                    'evaluatedAtUtc': '2026-04-05T12:05:00+00:00',
                    'marketDataAtUtc': '2026-04-05T12:04:30+00:00',
                    'details': {
                        'currentPrice': 68210.34,
                    },
                }
            },
            required_timeframes_json=['15m', '1h', '4h'],
            evaluation_interval_seconds=300,
            last_decision_at_utc=datetime(2026, 4, 5, 12, 5, tzinfo=UTC),
            last_evaluated_at_utc=datetime(2026, 4, 5, 12, 5, tzinfo=UTC),
            next_evaluation_at_utc=datetime(2026, 4, 5, 12, 10, tzinfo=UTC),
            last_market_data_at_utc=datetime(2026, 4, 5, 12, 4, 30, tzinfo=UTC),
        )
        db.add(monitor_state)
        db.commit()

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [
                {
                    'pair': 'BTC/USD',
                    'ohlcvPair': 'XBTUSD',
                    'amount': 0.25,
                    'avgPrice': 65000.0,
                    'currentPrice': 68210.34,
                    'marketValue': 17052.585,
                    'costBasis': 16250.0,
                    'pnl': 802.585,
                    'pnlPercent': 4.939,
                    'entryTimeUtc': '2026-04-05T11:00:00+00:00',
                    'realizedPnl': 0.0,
                }
            ],
        )

        monkeypatch.setattr(
            watchlist_service,
            'get_monitoring_snapshot',
            lambda session, **kwargs: {
                'scope': 'crypto_only',
                'capturedAtUtc': '2026-04-05T12:05:00+00:00',
                'activeUploadId': upload.upload_id,
                'summary': {},
                'rows': [
                    {
                        'symbol': 'BTC/USD',
                        'monitoringStatus': 'ACTIVE',
                        'monitoring': {
                            'latestDecisionState': 'SKIPPED',
                            'latestDecisionReason': 'OPEN_POSITION_EXISTS',
                        },
                        'positionState': {
                            'hasOpenPosition': True,
                            'currentPrice': 68210.34,
                            'profitTarget': 67600.0,
                            'profitTargetReached': True,
                            'trailingStop': 66100.0,
                            'peakPrice': 68420.0,
                            'protectionMode': 'BREAK_EVEN_PROMOTED',
                            'feeAdjustedBreakEven': 65120.0,
                            'promotedProtectiveFloor': 65120.0,
                            'tpTouchedAtUtc': '2026-04-05T12:02:00+00:00',
                            'strongerMarginReached': False,
                            'lastConfirmedHigherLow': None,
                            'followThroughFailed': False,
                        },
                    }
                ],
            },
        )

        payload = position_inspect_service.get_inspect_payload(db, asset_class='crypto', symbol='BTC/USD')
        db.close()

        assert payload['exitWorker']['protectionMode'] == 'BREAK_EVEN_PROMOTED'
        assert payload['exitWorker']['logicSummary'] == 'TP hit awaiting weakness'
        assert payload['exitWorker']['currentPhase'] == 'Break-even promoted'
        assert payload['exitWorker']['nextExitTrigger'] == 'Follow-through failure or trail breach'
        assert payload['exitWorker']['feeAdjustedBreakEven'] == 65120.0
        assert payload['exitWorker']['promotedProtectiveFloor'] == 65120.0


def test_crypto_inspect_returns_cooldown_payload_after_exit(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        payload = build_crypto_payload()
        payload['bot_payload']['symbols'] = [payload['bot_payload']['symbols'][0]]
        payload['ui_payload']['summary']['selected_count'] = 1
        payload['ui_payload']['summary']['primary_focus'] = ['BTC']
        payload['ui_payload']['symbol_context'] = {'BTC': payload['ui_payload']['symbol_context']['BTC']}
        watchlist_service.ingest_watchlist(db, payload, source='api')

        monitor_row = db.query(WatchlistMonitorState).filter(WatchlistMonitorState.symbol == 'BTC').one()
        monitor_row.latest_decision_state = 'EXIT_FILLED'
        monitor_row.latest_decision_reason = 'CRYPTO_LEDGER_EXIT_FILLED'
        monitor_row.decision_context_json = {
            **dict(monitor_row.decision_context_json or {}),
            'lastExitAtUtc': datetime.now(UTC).isoformat(),
            'lastExitReason': 'CRYPTO_LEDGER_EXIT_FILLED',
            'reentryBlockedUntilUtc': (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            'cooldownActive': True,
            'exitExecution': {
                'action': 'EXIT_FILLED',
                'filledQuantity': 1.25,
                'filledPrice': 70000.0,
                'displayPair': 'BTC/USD',
            },
        }
        db.commit()

        payload = position_inspect_service.get_inspect_payload(db, asset_class='crypto', symbol='BTC')
        assert payload['positionSnapshot']['isOpen'] is False
        assert payload['signalSnapshot']['latestDecisionState'] == 'EXIT_FILLED'
        assert payload['signalSnapshot']['cooldownActive'] is True
        assert payload['signalSnapshot']['reentryBlockedUntilUtc']
        assert payload['latestEvaluation']['details']['cooldownActive'] is True



def test_crypto_position_inspect_cooldown_payload_surfaces_guard_state(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        upload = WatchlistUpload(
            upload_id='upl-cooldown-1',
            scan_id='scan-cooldown-1',
            schema_version='bot_watchlist_v3',
            provider='claude_tradier_mcp',
            scope='crypto_only',
            source='test',
            payload_hash='hash-cooldown-1',
            generated_at_utc=datetime(2026, 4, 1, 13, 0, tzinfo=UTC),
            received_at_utc=datetime(2026, 4, 1, 13, 1, tzinfo=UTC),
            watchlist_expires_at_utc=datetime(2026, 4, 2, 13, 0, tzinfo=UTC),
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
            symbol='SEI/USD',
            quote_currency='USD',
            asset_class='crypto',
            enabled=True,
            trade_direction='long',
            priority_rank=3,
            tier='tier_2',
            bias='bullish',
            setup_template='pullback_reclaim',
            bot_timeframes=['5m', '15m', '1h'],
            exit_template='trail_after_impulse',
            max_hold_hours=48,
            risk_flags=['high_beta'],
            monitoring_status='COOLDOWN',
        )
        db.add(row)
        db.flush()

        monitor_state = WatchlistMonitorState(
            watchlist_symbol_id=row.id,
            upload_id=upload.upload_id,
            scope='crypto_only',
            symbol='SEI/USD',
            monitoring_status='COOLDOWN',
            latest_decision_state='COOLDOWN_ACTIVE',
            latest_decision_reason='Re-entry cooldown still active after recent exit.',
            decision_context_json={
                'executionSource': 'WATCHLIST_MONITOR',
                'reentryBlockedUntilUtc': '2026-04-01T14:00:00+00:00',
                'lastExitAtUtc': '2026-04-01T13:32:00+00:00',
                'exitExecution': {
                    'displayPair': 'SEI/USD',
                    'lastExitPrice': 0.551,
                },
                'riskFlags': ['high_beta'],
                'botTimeframes': ['5m', '15m', '1h'],
            },
            required_timeframes_json=['5m', '15m', '1h'],
            evaluation_interval_seconds=300,
            last_decision_at_utc=datetime(2026, 4, 1, 13, 35, tzinfo=UTC),
            last_evaluated_at_utc=datetime(2026, 4, 1, 13, 35, tzinfo=UTC),
            next_evaluation_at_utc=datetime(2026, 4, 1, 13, 40, tzinfo=UTC),
            last_market_data_at_utc=datetime(2026, 4, 1, 13, 34, 30, tzinfo=UTC),
        )
        db.add(monitor_state)
        db.commit()

        payload = position_inspect_service.get_inspect_payload(db, asset_class='crypto', symbol='SEI/USD')
        db.close()

        assert payload['inspectSource'] == 'watchlist_monitor_state'
        assert payload['signalSnapshot']['cooldownActive'] is True
        assert payload['signalSnapshot']['reentryBlockedUntilUtc'] == '2026-04-01T14:00:00+00:00'
        assert payload['signalSnapshot']['monitoringStatus'] == 'COOLDOWN'
        assert payload['latestEvaluation']['details']['cooldownActive'] is True
        assert payload['latestEvaluation']['details']['lastExitAtUtc'] == '2026-04-01T13:32:00+00:00'
        assert payload['exitPlan']['template'] == 'trail_after_impulse'
        assert payload['timeframeAlignment']['configured'] == ['5m', '15m', '1h']
