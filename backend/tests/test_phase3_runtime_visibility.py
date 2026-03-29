from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.pre_trade_gate import PreTradeGateDecision
from app.services.runtime_visibility import runtime_visibility_service


def _mock_dependencies() -> dict:
    return {
        'observedAtUtc': '2026-03-29T14:30:00+00:00',
        'expiresAtUtc': '2026-03-29T14:30:20+00:00',
        'summary': {
            'readyCount': 2,
            'degradedCount': 1,
            'missingCount': 0,
            'criticalReady': True,
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
    assert payload['runtimeVisibility']['lastRejected']['symbol'] == 'DOGE/USD'
    assert payload['runtimeVisibility']['gateSummary']['rejectedCount'] == 1
