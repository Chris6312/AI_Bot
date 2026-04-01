from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
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
