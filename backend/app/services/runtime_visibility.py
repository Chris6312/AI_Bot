from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from app.core.config import settings
from app.core.database import SessionLocal
from app.services.control_plane import get_control_plane_status, get_execution_gate_status
from app.services.kraken_service import kraken_service
from app.services.tradier_client import tradier_client
from app.services.watchlist_exit_worker import watchlist_exit_worker

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
                'watchlistMonitor': self._probe_watchlist_monitor(observed_at),
                'watchlistExitWorker': self._probe_watchlist_exit_worker(observed_at),
            },
        }
        checks = payload['checks']
        ready_count = sum(1 for item in checks.values() if item['ready'])
        degraded_count = sum(1 for item in checks.values() if item['state'] == 'DEGRADED')
        missing_count = sum(1 for item in checks.values() if item['state'] == 'MISSING')
        stale_count = sum(1 for item in checks.values() if item['state'] == 'STALE')
        disabled_count = sum(1 for item in checks.values() if item['state'] == 'DISABLED')
        critical_ready = bool(checks['tradierPaper']['ready'] and checks['krakenMarketData']['ready'])
        worker_ready = bool(checks['watchlistMonitor']['ready'] and checks['watchlistExitWorker']['ready'])
        payload['summary'] = {
            'readyCount': ready_count,
            'degradedCount': degraded_count,
            'missingCount': missing_count,
            'staleCount': stale_count,
            'disabledCount': disabled_count,
            'criticalReady': critical_ready,
            'workerReady': worker_ready,
            'operationalReady': bool(critical_ready and worker_ready),
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

    def _probe_watchlist_monitor(self, observed_at: datetime) -> dict[str, Any]:
        from app.services.watchlist_monitoring import watchlist_monitoring_orchestrator

        db = SessionLocal()
        try:
            status = watchlist_monitoring_orchestrator.get_runtime_status(db)
        except Exception as exc:  # pragma: no cover - exercised by tests with monkeypatch
            return {
                'name': 'Watchlist Monitor',
                'state': 'DEGRADED',
                'ready': False,
                'reason': str(exc),
                'checkedAtUtc': observed_at.isoformat(),
                'details': {},
            }
        finally:
            db.close()
        return self._build_worker_probe(
            name='Watchlist Monitor',
            observed_at=observed_at,
            enabled=bool(status.get('enabled')),
            poll_seconds=int(status.get('pollSeconds') or 0),
            last_started_at=status.get('lastStartedAtUtc'),
            last_finished_at=status.get('lastFinishedAtUtc'),
            last_error=status.get('lastError'),
            consecutive_failures=int(status.get('consecutiveFailures') or 0),
            details={
                'dueSnapshot': status.get('dueSnapshot'),
                'lastRunSummary': status.get('lastRunSummary') or {},
            },
        )

    def _probe_watchlist_exit_worker(self, observed_at: datetime) -> dict[str, Any]:
        db = SessionLocal()
        try:
            status = watchlist_exit_worker.get_status(db)
        except Exception as exc:  # pragma: no cover - exercised by tests with monkeypatch
            return {
                'name': 'Watchlist Exit Worker',
                'state': 'DEGRADED',
                'ready': False,
                'reason': str(exc),
                'checkedAtUtc': observed_at.isoformat(),
                'details': {},
            }
        finally:
            db.close()
        return self._build_worker_probe(
            name='Watchlist Exit Worker',
            observed_at=observed_at,
            enabled=bool(status.get('enabled')),
            poll_seconds=int(status.get('pollSeconds') or 0),
            last_started_at=status.get('lastStartedAtUtc'),
            last_finished_at=status.get('lastFinishedAtUtc'),
            last_error=status.get('lastError'),
            consecutive_failures=int(status.get('consecutiveFailures') or 0),
            details={
                'summary': status.get('summary') or {},
                'session': status.get('session') or {},
                'lastRunSummary': status.get('lastRunSummary') or {},
            },
        )

    def _build_worker_probe(
        self,
        *,
        name: str,
        observed_at: datetime,
        enabled: bool,
        poll_seconds: int,
        last_started_at: str | None,
        last_finished_at: str | None,
        last_error: str | None,
        consecutive_failures: int,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_details: dict[str, Any] = {
            'pollSeconds': poll_seconds,
            'lastStartedAtUtc': last_started_at,
            'lastFinishedAtUtc': last_finished_at,
            'consecutiveFailures': consecutive_failures,
        }
        if details:
            payload_details.update(details)

        if not enabled:
            return {
                'name': name,
                'state': 'DISABLED',
                'ready': True,
                'reason': 'Worker loop is disabled by configuration.',
                'checkedAtUtc': observed_at.isoformat(),
                'details': payload_details,
            }

        if last_error and consecutive_failures > 0:
            return {
                'name': name,
                'state': 'DEGRADED',
                'ready': False,
                'reason': last_error,
                'checkedAtUtc': observed_at.isoformat(),
                'details': payload_details,
            }

        freshness_window = max(poll_seconds * 3, 30)
        fresh_cutoff = observed_at - timedelta(seconds=freshness_window)
        finished_at = self._parse_timestamp(last_finished_at)
        started_at = self._parse_timestamp(last_started_at)

        if finished_at and finished_at >= fresh_cutoff:
            return {
                'name': name,
                'state': 'READY',
                'ready': True,
                'reason': '',
                'checkedAtUtc': observed_at.isoformat(),
                'details': payload_details,
            }

        if started_at and started_at >= fresh_cutoff:
            return {
                'name': name,
                'state': 'READY',
                'ready': True,
                'reason': 'Worker loop is running its current sweep.',
                'checkedAtUtc': observed_at.isoformat(),
                'details': payload_details,
            }

        last_seen = finished_at or started_at
        if last_seen is None:
            return {
                'name': name,
                'state': 'DEGRADED',
                'ready': False,
                'reason': 'Worker loop has not reported a run yet.',
                'checkedAtUtc': observed_at.isoformat(),
                'details': payload_details,
            }

        age_seconds = int((observed_at - last_seen).total_seconds())
        return {
            'name': name,
            'state': 'STALE',
            'ready': False,
            'reason': f'Last worker activity was {age_seconds}s ago, outside the expected poll window.',
            'checkedAtUtc': observed_at.isoformat(),
            'details': payload_details,
        }

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None


runtime_visibility_service = RuntimeVisibilityService()
