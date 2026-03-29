from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.services.template_evaluator import template_evaluation_service
from app.services.watchlist_service import ACTIVE, MANAGED_ONLY, PENDING_EVALUATION, WATCHLIST_SCOPE, watchlist_service

logger = logging.getLogger(__name__)

ELIGIBLE_DUE_STATUSES = (ACTIVE, MANAGED_ONLY)
DEFAULT_SCOPES: tuple[WATCHLIST_SCOPE, ...] = ('stocks_only', 'crypto_only')


@dataclass
class MonitorLoopRuntime:
    enabled: bool = True
    poll_seconds: int = 20
    last_started_at_utc: str | None = None
    last_finished_at_utc: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_run_summary: dict[str, Any] = field(default_factory=dict)


class WatchlistMonitoringOrchestrator:
    def __init__(self) -> None:
        self._runtime = MonitorLoopRuntime(
            enabled=bool(settings.WATCHLIST_MONITOR_ENABLED),
            poll_seconds=max(5, int(settings.WATCHLIST_MONITOR_POLL_SECONDS)),
        )

    def get_runtime_status(self, db: Session | None = None, *, scope: WATCHLIST_SCOPE | None = None) -> dict[str, Any]:
        due_snapshot = None
        if db is not None:
            due_snapshot = self.get_due_snapshot(db, scope=scope)
        return {
            'enabled': self._runtime.enabled,
            'pollSeconds': self._runtime.poll_seconds,
            'lastStartedAtUtc': self._runtime.last_started_at_utc,
            'lastFinishedAtUtc': self._runtime.last_finished_at_utc,
            'lastError': self._runtime.last_error,
            'consecutiveFailures': self._runtime.consecutive_failures,
            'lastRunSummary': self._runtime.last_run_summary,
            'dueSnapshot': due_snapshot,
        }

    def get_due_snapshot(self, db: Session, *, scope: WATCHLIST_SCOPE | None = None) -> dict[str, Any]:
        scopes: tuple[WATCHLIST_SCOPE, ...] = (scope,) if scope is not None else DEFAULT_SCOPES
        observed_at = datetime.now(UTC)
        result: dict[str, Any] = {
            'capturedAtUtc': observed_at.isoformat(),
            'scopes': {},
            'summary': {
                'totalDueCount': 0,
                'activeDueCount': 0,
                'managedOnlyDueCount': 0,
            },
        }
        for scope_value in scopes:
            watchlist_service._backfill_missing_monitor_states(db, scope=scope_value, observed_at=observed_at)
            scope_due = self._query_due_rows(db, scope=scope_value, observed_at=observed_at)
            active_due = scope_due.filter(WatchlistMonitorState.monitoring_status == ACTIVE).count()
            managed_only_due = scope_due.filter(WatchlistMonitorState.monitoring_status == MANAGED_ONLY).count()
            total_due = active_due + managed_only_due
            monitoring_snapshot = watchlist_service.get_monitoring_snapshot(db, scope=scope_value, include_inactive=False)
            result['scopes'][scope_value] = {
                'scope': scope_value,
                'dueCount': total_due,
                'activeDueCount': active_due,
                'managedOnlyDueCount': managed_only_due,
                'nextEvaluationAtUtc': monitoring_snapshot['summary']['nextEvaluationAtUtc'],
                'activeUploadId': monitoring_snapshot['activeUploadId'],
            }
            result['summary']['totalDueCount'] += total_due
            result['summary']['activeDueCount'] += active_due
            result['summary']['managedOnlyDueCount'] += managed_only_due
        if scope is not None:
            return result['scopes'][scope]
        return result

    def run_due_once(
        self,
        db: Session,
        *,
        scope: WATCHLIST_SCOPE | None = None,
        limit_per_scope: int | None = None,
    ) -> dict[str, Any]:
        scopes: tuple[WATCHLIST_SCOPE, ...] = (scope,) if scope is not None else DEFAULT_SCOPES
        observed_at = datetime.now(UTC)
        per_scope_limit = max(1, int(limit_per_scope or settings.WATCHLIST_MONITOR_BATCH_LIMIT))

        result: dict[str, Any] = {
            'capturedAtUtc': observed_at.isoformat(),
            'limitPerScope': per_scope_limit,
            'scopes': {},
            'summary': {
                'totalScopes': len(scopes),
                'scopesWithDueRows': 0,
                'totalDueBefore': 0,
                'totalDueAfter': 0,
                'totalEvaluated': 0,
                'totalEntryCandidates': 0,
                'totalWaitingForSetup': 0,
                'totalDataStale': 0,
                'totalDataUnavailable': 0,
                'totalMonitorOnly': 0,
                'totalBiasConflict': 0,
                'totalEvaluationBlocked': 0,
            },
        }

        for scope_value in scopes:
            watchlist_service.reconcile_scope_statuses(db, scope=scope_value)
            due_before = self._count_due_rows(db, scope=scope_value, observed_at=observed_at)
            scope_result: dict[str, Any] = {
                'scope': scope_value,
                'dueCountBefore': due_before,
                'dueCountAfter': due_before,
                'evaluatedCount': 0,
                'summary': {
                    'entryCandidateCount': 0,
                    'waitingForSetupCount': 0,
                    'dataStaleCount': 0,
                    'dataUnavailableCount': 0,
                    'monitorOnlyCount': 0,
                    'inactiveCount': 0,
                    'biasConflictCount': 0,
                    'evaluationBlockedCount': 0,
                },
                'rows': [],
                'monitoringSnapshot': watchlist_service.get_monitoring_snapshot(db, scope=scope_value, include_inactive=False),
            }
            if due_before > 0:
                scope_result = template_evaluation_service.evaluate_scope(
                    db,
                    scope=scope_value,
                    limit=min(per_scope_limit, due_before),
                    force=False,
                    eligible_statuses=ELIGIBLE_DUE_STATUSES,
                )
                scope_result['dueCountBefore'] = due_before
                scope_result['dueCountAfter'] = self._count_due_rows(db, scope=scope_value, observed_at=datetime.now(UTC))
                result['summary']['scopesWithDueRows'] += 1

            result['scopes'][scope_value] = scope_result
            result['summary']['totalDueBefore'] += scope_result['dueCountBefore']
            result['summary']['totalDueAfter'] += scope_result['dueCountAfter']
            result['summary']['totalEvaluated'] += scope_result['evaluatedCount']
            result['summary']['totalEntryCandidates'] += scope_result['summary']['entryCandidateCount']
            result['summary']['totalWaitingForSetup'] += scope_result['summary']['waitingForSetupCount']
            result['summary']['totalDataStale'] += scope_result['summary']['dataStaleCount']
            result['summary']['totalDataUnavailable'] += scope_result['summary']['dataUnavailableCount']
            result['summary']['totalMonitorOnly'] += scope_result['summary']['monitorOnlyCount']
            result['summary']['totalBiasConflict'] += scope_result['summary']['biasConflictCount']
            result['summary']['totalEvaluationBlocked'] += scope_result['summary']['evaluationBlockedCount']

        if scope is not None:
            return result['scopes'][scope]
        return result

    async def run_loop(self) -> None:
        self._runtime.enabled = bool(settings.WATCHLIST_MONITOR_ENABLED)
        self._runtime.poll_seconds = max(5, int(settings.WATCHLIST_MONITOR_POLL_SECONDS))
        if not self._runtime.enabled:
            logger.info('Watchlist monitoring orchestrator is disabled.')
            return

        logger.info(
            'Starting watchlist monitoring orchestrator loop (poll=%ss, batch_limit=%s).',
            self._runtime.poll_seconds,
            settings.WATCHLIST_MONITOR_BATCH_LIMIT,
        )
        while True:
            self._runtime.last_started_at_utc = datetime.now(UTC).isoformat()
            db = SessionLocal()
            try:
                run_summary = self.run_due_once(db, limit_per_scope=settings.WATCHLIST_MONITOR_BATCH_LIMIT)
                self._runtime.last_run_summary = run_summary
                self._runtime.last_error = None
                self._runtime.consecutive_failures = 0
                self._runtime.last_finished_at_utc = datetime.now(UTC).isoformat()
                if run_summary['summary']['totalEvaluated'] > 0:
                    logger.info(
                        'Watchlist due-run sweep complete: evaluated=%s due_before=%s due_after=%s',
                        run_summary['summary']['totalEvaluated'],
                        run_summary['summary']['totalDueBefore'],
                        run_summary['summary']['totalDueAfter'],
                    )
            except asyncio.CancelledError:
                db.close()
                raise
            except Exception as exc:
                logger.exception('Watchlist monitoring orchestrator sweep failed: %s', exc)
                self._runtime.last_error = str(exc)
                self._runtime.consecutive_failures += 1
                self._runtime.last_finished_at_utc = datetime.now(UTC).isoformat()
            finally:
                db.close()
            await asyncio.sleep(self._runtime.poll_seconds)

    @staticmethod
    def _query_due_rows(db: Session, *, scope: WATCHLIST_SCOPE, observed_at: datetime):
        return (
            db.query(WatchlistMonitorState)
            .filter(
                WatchlistMonitorState.scope == scope,
                WatchlistMonitorState.monitoring_status.in_(ELIGIBLE_DUE_STATUSES),
            )
            .filter(
                (WatchlistMonitorState.latest_decision_state == PENDING_EVALUATION)
                | (WatchlistMonitorState.next_evaluation_at_utc.is_(None))
                | (WatchlistMonitorState.next_evaluation_at_utc <= observed_at)
            )
        )

    def _count_due_rows(self, db: Session, *, scope: WATCHLIST_SCOPE, observed_at: datetime) -> int:
        return self._query_due_rows(db, scope=scope, observed_at=observed_at).count()


watchlist_monitoring_orchestrator = WatchlistMonitoringOrchestrator()
