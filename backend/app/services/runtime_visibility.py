from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from app.core.config import settings
from app.services.control_plane import get_control_plane_status, get_execution_gate_status
from app.services.kraken_service import kraken_service
from app.services.tradier_client import tradier_client

UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RuntimeVisibilityService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._gate_records: deque[dict[str, Any]] = deque(
            maxlen=max(int(settings.RUNTIME_VISIBILITY_GATE_HISTORY_LIMIT), 10)
        )
        self._dependency_cache: dict[str, Any] | None = None
        self._dependency_cache_expires_at: datetime | None = None

    def reset_for_tests(self) -> None:
        with self._lock:
            self._gate_records.clear()
            self._dependency_cache = None
            self._dependency_cache_expires_at = None

    def record_gate_decision(
        self,
        decision: Any,
        *,
        execution_source: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = decision.to_dict() if hasattr(decision, 'to_dict') else dict(decision or {})
        record = {
            'recordedAtUtc': _utcnow().isoformat(),
            'allowed': bool(payload.get('allowed')),
            'assetClass': payload.get('assetClass'),
            'symbol': payload.get('symbol'),
            'state': payload.get('state'),
            'rejectionReason': payload.get('rejectionReason') or '',
            'executionSource': execution_source,
            'checks': deepcopy(payload.get('checks') or []),
            'marketData': deepcopy(payload.get('marketData') or {}),
            'riskData': deepcopy(payload.get('riskData') or {}),
            'context': deepcopy(context or {}),
        }
        with self._lock:
            self._gate_records.appendleft(record)
        return deepcopy(record)

    def get_gate_snapshot(self, *, limit: int = 10) -> dict[str, Any]:
        with self._lock:
            records = [deepcopy(item) for item in list(self._gate_records)]

        recent = records[:limit]
        rejections = [item for item in records if not item.get('allowed')]
        approvals = [item for item in records if item.get('allowed')]
        return {
            'capturedAtUtc': _utcnow().isoformat(),
            'summary': {
                'total': len(records),
                'allowedCount': len(approvals),
                'rejectedCount': len(rejections),
                'lastDecision': recent[0] if recent else None,
                'lastAllowed': approvals[0] if approvals else None,
                'lastRejected': rejections[0] if rejections else None,
            },
            'recent': recent,
            'recentRejections': rejections[:limit],
        }

    def get_dependency_status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = _utcnow()
        with self._lock:
            if not force_refresh and self._dependency_cache and self._dependency_cache_expires_at and self._dependency_cache_expires_at > now:
                return deepcopy(self._dependency_cache)

        payload = self._probe_dependencies(now)
        with self._lock:
            self._dependency_cache = deepcopy(payload)
            self._dependency_cache_expires_at = datetime.fromisoformat(payload['expiresAtUtc'])
        return payload

    def get_runtime_snapshot(self, *, limit: int = 10, force_refresh: bool = False) -> dict[str, Any]:
        control_plane = get_control_plane_status()
        execution_gate = get_execution_gate_status()
        dependencies = self.get_dependency_status(force_refresh=force_refresh)
        gate = self.get_gate_snapshot(limit=limit)
        return {
            'capturedAtUtc': _utcnow().isoformat(),
            'controlPlane': control_plane,
            'executionGate': {
                'allowed': execution_gate.allowed,
                'state': execution_gate.state,
                'reason': execution_gate.reason,
                'statusCode': execution_gate.status_code,
            },
            'dependencies': dependencies,
            'gate': gate,
        }

    def _probe_dependencies(self, observed_at: datetime) -> dict[str, Any]:
        ttl = max(int(settings.RUNTIME_VISIBILITY_PROBE_TTL_SECONDS), 5)
        payload = {
            'observedAtUtc': observed_at.isoformat(),
            'expiresAtUtc': (observed_at + timedelta(seconds=ttl)).isoformat(),
            'summary': {},
            'checks': {
                'tradierPaper': self._probe_tradier('PAPER', observed_at),
                'tradierLive': self._probe_tradier('LIVE', observed_at),
                'krakenMarketData': self._probe_kraken(observed_at),
            },
        }
        checks = payload['checks']
        ready_count = sum(1 for item in checks.values() if item['ready'])
        missing_count = sum(1 for item in checks.values() if item['state'] == 'MISSING')
        degraded_count = sum(1 for item in checks.values() if item['state'] == 'DEGRADED')
        payload['summary'] = {
            'readyCount': ready_count,
            'degradedCount': degraded_count,
            'missingCount': missing_count,
            'criticalReady': bool(checks['tradierPaper']['ready'] and checks['krakenMarketData']['ready']),
        }
        return payload

    def _probe_tradier(self, mode: str, observed_at: datetime) -> dict[str, Any]:
        selected_mode = str(mode or 'PAPER').upper()
        if not tradier_client.is_ready(selected_mode):
            return {
                'name': f'Tradier {selected_mode.title()}',
                'state': 'MISSING',
                'ready': False,
                'reason': f'Tradier {selected_mode} credentials are not configured.',
                'checkedAtUtc': observed_at.isoformat(),
                'details': {'mode': selected_mode},
            }

        try:
            snapshot = tradier_client.get_account_snapshot(selected_mode)
        except Exception as exc:  # pragma: no cover - exercised by tests with monkeypatch
            return {
                'name': f'Tradier {selected_mode.title()}',
                'state': 'DEGRADED',
                'ready': False,
                'reason': str(exc),
                'checkedAtUtc': observed_at.isoformat(),
                'details': {'mode': selected_mode},
            }

        connected = bool(snapshot.get('connected'))
        return {
            'name': f'Tradier {selected_mode.title()}',
            'state': 'READY' if connected else 'DEGRADED',
            'ready': connected,
            'reason': '' if connected else 'Tradier account snapshot did not report a live connection.',
            'checkedAtUtc': observed_at.isoformat(),
            'details': {
                'mode': selected_mode,
                'accountId': snapshot.get('accountId') or '',
                'portfolioValue': snapshot.get('portfolioValue') or 0.0,
            },
        }

    def _probe_kraken(self, observed_at: datetime) -> dict[str, Any]:
        supported_pairs = kraken_service.get_supported_pairs()
        probe_pair = kraken_service.get_ohlcv_pair('BTC/USD') or next(iter(supported_pairs.values()), 'XBTUSD')
        try:
            ticker = kraken_service.get_ticker(probe_pair)
        except Exception as exc:  # pragma: no cover - exercised by tests with monkeypatch
            return {
                'name': 'Kraken Market Data',
                'state': 'DEGRADED',
                'ready': False,
                'reason': str(exc),
                'checkedAtUtc': observed_at.isoformat(),
                'details': {'pair': probe_pair},
            }

        if ticker and ticker.get('c'):
            return {
                'name': 'Kraken Market Data',
                'state': 'READY',
                'ready': True,
                'reason': '',
                'checkedAtUtc': observed_at.isoformat(),
                'details': {'pair': probe_pair, 'lastPrice': float(ticker['c'][0])},
            }

        return {
            'name': 'Kraken Market Data',
            'state': 'DEGRADED',
            'ready': False,
            'reason': 'Kraken ticker probe did not return a current price.',
            'checkedAtUtc': observed_at.isoformat(),
            'details': {'pair': probe_pair},
        }


runtime_visibility_service = RuntimeVisibilityService()
