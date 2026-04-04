from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.services.control_plane import discord_decision_guard
from app.services.pre_trade_gate import PreTradeGateDecision
from app.services.runtime_visibility import runtime_visibility_service


def _mock_dependencies() -> dict:
    return {
        'observedAtUtc': '2026-03-29T14:30:00+00:00',
        'expiresAtUtc': '2026-03-29T14:30:20+00:00',
        'summary': {
            'readyCount': 4,
            'degradedCount': 1,
            'missingCount': 0,
            'staleCount': 0,
            'disabledCount': 0,
            'criticalReady': True,
            'workerReady': True,
            'operationalReady': True,
        },
        'checks': {
            'tradierPaper': {
                'name': 'Tradier Paper',
                'state': 'READY',
                'ready': True,
                'reason': '',
                'checkedAtUtc': '2026-03-29T14:30:00+00:00',
                'details': {'mode': 'PAPER'},
            },
            'tradierLive': {
                'name': 'Tradier Live',
                'state': 'DEGRADED',
                'ready': False,
                'reason': 'Live credentials missing for probe',
                'checkedAtUtc': '2026-03-29T14:30:00+00:00',
                'details': {'mode': 'LIVE'},
            },
            'krakenMarketData': {
                'name': 'Kraken Market Data',
                'state': 'READY',
                'ready': True,
                'reason': '',
                'checkedAtUtc': '2026-03-29T14:30:00+00:00',
                'details': {'pair': 'XXBTZUSD'},
            },
            'watchlistMonitor': {
                'name': 'Watchlist Monitor',
                'state': 'READY',
                'ready': True,
                'reason': '',
                'checkedAtUtc': '2026-03-29T14:30:00+00:00',
                'details': {'pollSeconds': 20},
            },
            'watchlistExitWorker': {
                'name': 'Watchlist Exit Worker',
                'state': 'READY',
                'ready': True,
                'reason': '',
                'checkedAtUtc': '2026-03-29T14:30:00+00:00',
                'details': {'pollSeconds': 20},
            },
        },
    }


def test_runtime_visibility_tracks_recent_gate_decisions() -> None:
    runtime_visibility_service.reset_for_tests()

    runtime_visibility_service.record_gate_decision(
        PreTradeGateDecision(
            allowed=False,
            asset_class='stock',
            symbol='AAPL',
            state='REJECTED',
            rejection_reason='Synthetic stale quote',
        ),
        execution_source='TEST_REJECT',
        context={'mode': 'PAPER'},
    )
    runtime_visibility_service.record_gate_decision(
        PreTradeGateDecision(
            allowed=True,
            asset_class='crypto',
            symbol='BTC/USD',
            state='READY',
        ),
        execution_source='TEST_ALLOW',
        context={'requestedAmount': 0.25},
    )

    snapshot = runtime_visibility_service.get_gate_snapshot(limit=5)

    assert snapshot['summary']['total'] == 2
    assert snapshot['summary']['allowedCount'] == 1
    assert snapshot['summary']['rejectedCount'] == 1
    assert snapshot['summary']['lastDecision']['symbol'] == 'BTC/USD'
    assert snapshot['summary']['lastRejected']['rejectionReason'] == 'Synthetic stale quote'


def test_runtime_visibility_endpoint_includes_gate_and_dependency_state(monkeypatch) -> None:
    runtime_visibility_service.reset_for_tests()
    runtime_visibility_service.record_gate_decision(
        PreTradeGateDecision(
            allowed=False,
            asset_class='stock',
            symbol='MSFT',
            state='REJECTED',
            rejection_reason='Synthetic rejection reason',
        ),
        execution_source='TEST_HTTP',
    )
    monkeypatch.setattr(runtime_visibility_service, 'get_dependency_status', lambda force_refresh=False: _mock_dependencies())

    with TestClient(app) as client:
        response = client.get('/api/runtime-visibility?limit=5')

    assert response.status_code == 200
    payload = response.json()
    assert payload['dependencies']['summary']['criticalReady'] is True
    assert payload['dependencies']['summary']['operationalReady'] is True
    assert payload['gate']['summary']['rejectedCount'] == 1
    assert payload['gate']['recentRejections'][0]['symbol'] == 'MSFT'



def test_status_endpoint_surfaces_runtime_visibility_summary(monkeypatch) -> None:
    runtime_visibility_service.reset_for_tests()
    runtime_visibility_service.record_gate_decision(
        PreTradeGateDecision(
            allowed=False,
            asset_class='crypto',
            symbol='DOGE/USD',
            state='REJECTED',
            rejection_reason='Synthetic candle continuity failure',
        ),
        execution_source='TEST_STATUS',
    )
    monkeypatch.setattr(runtime_visibility_service, 'get_dependency_status', lambda force_refresh=False: _mock_dependencies())

    with TestClient(app) as client:
        response = client.get('/api/status')

    assert response.status_code == 200
    payload = response.json()
    assert payload['runtimeVisibility']['dependencySummary']['criticalReady'] is True
    assert payload['runtimeVisibility']['dependencySummary']['workerReady'] is True
    assert payload['runtimeVisibility']['lastRejected']['symbol'] == 'DOGE/USD'
    assert payload['runtimeVisibility']['gateSummary']['rejectedCount'] == 1



def test_runtime_visibility_dependency_probe_tracks_worker_health(monkeypatch) -> None:
    runtime_visibility_service.reset_for_tests()

    monkeypatch.setattr(
        runtime_visibility_service,
        '_probe_tradier',
        lambda mode, observed_at: {
            'name': f'Tradier {mode}',
            'state': 'READY',
            'ready': True,
            'reason': '',
            'checkedAtUtc': observed_at.isoformat(),
            'details': {'mode': mode},
        },
    )
    monkeypatch.setattr(
        runtime_visibility_service,
        '_probe_kraken',
        lambda observed_at: {
            'name': 'Kraken Market Data',
            'state': 'READY',
            'ready': True,
            'reason': '',
            'checkedAtUtc': observed_at.isoformat(),
            'details': {'pair': 'XXBTZUSD'},
        },
    )
    monkeypatch.setattr(
        runtime_visibility_service,
        '_probe_watchlist_monitor',
        lambda observed_at: {
            'name': 'Watchlist Monitor',
            'state': 'READY',
            'ready': True,
            'reason': '',
            'checkedAtUtc': observed_at.isoformat(),
            'details': {'pollSeconds': 20},
        },
    )
    monkeypatch.setattr(
        runtime_visibility_service,
        '_probe_watchlist_exit_worker',
        lambda observed_at: {
            'name': 'Watchlist Exit Worker',
            'state': 'STALE',
            'ready': False,
            'reason': 'Synthetic stale worker check',
            'checkedAtUtc': observed_at.isoformat(),
            'details': {'pollSeconds': 20},
        },
    )

    snapshot = runtime_visibility_service.get_dependency_status(force_refresh=True)

    assert snapshot['summary']['criticalReady'] is True
    assert snapshot['summary']['workerReady'] is False
    assert snapshot['summary']['operationalReady'] is False
    assert snapshot['summary']['staleCount'] == 1
    assert snapshot['checks']['watchlistExitWorker']['state'] == 'STALE'



def test_runtime_visibility_build_worker_probe_marks_stale_loop() -> None:
    observed_at = datetime(2026, 3, 30, 15, 0, tzinfo=UTC)
    stale_started = (observed_at - timedelta(seconds=120)).isoformat()
    stale_finished = (observed_at - timedelta(seconds=95)).isoformat()

    payload = runtime_visibility_service._build_worker_probe(
        name='Watchlist Monitor',
        observed_at=observed_at,
        enabled=True,
        poll_seconds=20,
        last_started_at=stale_started,
        last_finished_at=stale_finished,
        last_error=None,
        consecutive_failures=0,
        details={'dueSnapshot': None},
    )

    assert payload['state'] == 'STALE'
    assert payload['ready'] is False
    assert 'outside the expected poll window' in payload['reason']


def test_runtime_visibility_watchlist_probe_surfaces_scope_truth_issues(monkeypatch) -> None:
    class DummyDb:
        def close(self) -> None:
            return None

    observed_at = datetime(2026, 3, 31, 14, 30, tzinfo=UTC)
    monkeypatch.setattr('app.services.runtime_visibility.SessionLocal', lambda: DummyDb())
    monkeypatch.setattr(
        'app.services.watchlist_monitoring.watchlist_monitoring_orchestrator.get_runtime_status',
        lambda db: {
            'enabled': True,
            'pollSeconds': 20,
            'lastStartedAtUtc': observed_at.isoformat(),
            'lastFinishedAtUtc': observed_at.isoformat(),
            'lastError': None,
            'consecutiveFailures': 0,
            'lastRunSummary': {},
            'dueSnapshot': {'summary': {'eligibleDueCount': 0, 'blockedDueCount': 0}},
        },
    )
    monkeypatch.setattr(
        'app.services.watchlist_service.watchlist_service.get_monitoring_snapshot',
        lambda db: {
            'stocks_only': {'scopeTruth': {'state': 'STALE', 'reason': 'Latest stock watchlist expired.'}},
            'crypto_only': {'scopeTruth': {'state': 'READY', 'reason': ''}},
        },
    )
    monkeypatch.setattr(
        runtime_visibility_service,
        '_build_worker_probe',
        lambda **kwargs: {
            'name': kwargs['name'],
            'state': 'READY',
            'ready': True,
            'reason': '',
            'checkedAtUtc': kwargs['observed_at'].isoformat(),
            'details': kwargs['details'],
        },
    )

    payload = runtime_visibility_service._probe_watchlist_monitor(observed_at)

    assert payload['state'] == 'DEGRADED'
    assert payload['ready'] is True
    assert payload['details']['scopeTruth']['stocks_only']['state'] == 'STALE'
    assert payload['details']['scopeIssues'] == ['stocks_only: Latest stock watchlist expired.']


def test_runtime_visibility_endpoint_includes_audit_holes_payload(monkeypatch) -> None:
    runtime_visibility_service.reset_for_tests()
    discord_decision_guard.reset_for_tests()

    discord_decision_guard._record_replay_rejection(
        type('Message', (), {'id': 99, 'author': type('Author', (), {'id': 7})(), 'channel': type('Channel', (), {'id': 11})()})(),
        {'schema_version': 'bot_watchlist_v3', 'scope': 'crypto_only', 'provider': 'pytest'},
        reason='Duplicate Discord payload suppressed.',
        payload_hash='abc123',
    )

    monkeypatch.setattr(runtime_visibility_service, 'get_dependency_status', lambda force_refresh=False: _mock_dependencies())
    monkeypatch.setattr(
        runtime_visibility_service,
        'get_system_error_timeline',
        lambda limit=10: [
            {
                'id': 'system-1',
                'timestamp': '2026-03-29T14:31:00+00:00',
                'source': 'dependency_probe',
                'component': 'Tradier Live',
                'severity': 'error',
                'state': 'DEGRADED',
                'message': 'Live credentials missing for probe',
                'symbol': None,
                'details': {},
            }
        ],
    )
    monkeypatch.setattr(
        runtime_visibility_service,
        'get_exit_timeline',
        lambda limit=10: [
            {
                'id': 'exit-1',
                'timestamp': '2026-03-29T14:32:00+00:00',
                'symbol': 'AAPL',
                'assetClass': 'stock',
                'status': 'CLOSED',
                'eventType': 'EXIT_FILLED',
                'executionSource': 'WATCHLIST_EXIT_WORKER',
                'trigger': 'TIME_STOP_EXPIRED',
                'message': 'Exit completed',
                'details': {},
            }
        ],
    )

    with TestClient(app) as client:
        response = client.get('/api/runtime-visibility?limit=5')

    assert response.status_code == 200
    payload = response.json()
    assert payload['audit']['replayRejections'][0]['reason'] == 'Duplicate Discord payload suppressed.'
    assert payload['audit']['systemErrors'][0]['component'] == 'Tradier Live'
    assert payload['audit']['exitTimeline'][0]['eventType'] == 'EXIT_FILLED'


def test_runtime_visibility_system_error_timeline_includes_dependency_and_order_failures(monkeypatch) -> None:
    runtime_visibility_service.reset_for_tests()

    monkeypatch.setattr(runtime_visibility_service, 'get_dependency_status', lambda force_refresh=False: _mock_dependencies())

    class DummyEvent:
        id = 1
        event_time = datetime(2026, 3, 29, 14, 40, tzinfo=UTC)
        status = 'REJECTED'
        event_type = 'ORDER_SUBMISSION_FAILED'
        message = 'Synthetic broker timeout'
        payload_json = {'stage': 'submit'}

    class DummyIntent:
        execution_source = 'WATCHLIST_MONITOR'
        symbol = 'MSFT'
        asset_class = 'stock'
        intent_id = 'intent_1'

    class DummyQuery:
        def join(self, *args, **kwargs):
            return self
        def filter(self, *args, **kwargs):
            return self
        def order_by(self, *args, **kwargs):
            return self
        def limit(self, *args, **kwargs):
            return self
        def all(self):
            return [(DummyEvent(), DummyIntent())]

    class DummyDb:
        def query(self, *args, **kwargs):
            return DummyQuery()
        def close(self):
            return None

    monkeypatch.setattr('app.services.runtime_visibility.SessionLocal', lambda: DummyDb())

    rows = runtime_visibility_service.get_system_error_timeline(limit=5)

    assert any(row['component'] == 'Tradier Live' for row in rows)
    assert any(row['component'] == 'WATCHLIST_MONITOR' and row['symbol'] == 'MSFT' for row in rows)


def test_runtime_visibility_truth_board_marks_managed_only_scope_as_review(monkeypatch) -> None:
    runtime_visibility_service.reset_for_tests()

    payload = _mock_dependencies()
    payload['checks']['watchlistMonitor']['details'] = {
        'scopeTruth': {
            'stocks_only': {
                'scope': 'stocks_only',
                'state': 'READY',
                'ready': True,
                'reason': '',
                'activeUploadId': 'stocks-1',
                'activeSymbolCount': 2,
                'managedOnlyCount': 0,
                'openPositionCount': 1,
                'dataWarningCount': 0,
            },
            'crypto_only': {
                'scope': 'crypto_only',
                'state': 'DEGRADED',
                'ready': False,
                'reason': 'Scope is supervision-only. Managed-only rows remain, but no fresh-entry symbols are eligible.',
                'activeUploadId': 'crypto-1',
                'activeSymbolCount': 0,
                'managedOnlyCount': 2,
                'openPositionCount': 2,
                'dataWarningCount': 0,
            },
        }
    }
    monkeypatch.setattr(runtime_visibility_service, 'get_dependency_status', lambda force_refresh=False: payload)

    snapshot = runtime_visibility_service.get_runtime_snapshot(limit=5, force_refresh=True)
    truth_board = snapshot['truthBoard']

    assert truth_board['state'] == 'READY'
    assert truth_board['supervisionReady'] is True
    assert truth_board['freshEntryReady'] is True
    assert truth_board['scopes']['stocks_only']['freshEntryReady'] is True
    assert truth_board['scopes']['crypto_only']['freshEntryReady'] is False
    assert any('crypto_only:' in issue for issue in truth_board['activeIssues'])



def test_ready_endpoint_uses_truth_board_fresh_entry_semantics(monkeypatch) -> None:
    runtime_visibility_service.reset_for_tests()
    monkeypatch.setattr(
        runtime_visibility_service,
        'get_runtime_snapshot',
        lambda limit=5, force_refresh=False: {
            'capturedAtUtc': '2026-03-31T14:30:00+00:00',
            'controlPlane': {
                'state': 'ARMED',
                'reason': 'Execution surfaces are authenticated and runtime is enabled.',
                'runtimeRunning': True,
                'adminApiReady': True,
                'discordAuthReady': True,
                'authorizationReady': True,
                'lastHeartbeat': '2026-03-31T14:29:55+00:00',
            },
            'executionGate': {
                'allowed': True,
                'state': 'ARMED',
                'reason': '',
                'statusCode': 200,
            },
            'dependencies': _mock_dependencies(),
            'truthBoard': {
                'state': 'REVIEW',
                'reason': 'No tracked scope is currently eligible for fresh entries.',
                'freshEntryReady': False,
                'supervisionReady': True,
                'trackedScopeCount': 1,
                'activeIssues': ['crypto_only: Scope is supervision-only.'],
                'scopes': {},
            },
            'gate': {'summary': {'total': 0, 'allowedCount': 0, 'rejectedCount': 0, 'lastDecision': None, 'lastAllowed': None, 'lastRejected': None}, 'recent': [], 'recentRejections': []},
            'audit': {'replayRejections': [], 'systemErrors': [], 'exitTimeline': []},
        },
    )

    with TestClient(app) as client:
        response = client.get('/ready')

    assert response.status_code == 200
    payload = response.json()
    assert payload['status'] == 'degraded'
    assert payload['truthBoard']['supervisionReady'] is True
    assert payload['truthBoard']['freshEntryReady'] is False
