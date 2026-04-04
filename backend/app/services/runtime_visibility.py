from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.order_event import OrderEvent
from app.models.order_intent import OrderIntent
from app.services.control_plane import (
    discord_decision_guard,
    get_control_plane_status,
    get_execution_gate_status,
)
from app.services.kraken_service import kraken_service
from app.services.tradier_client import tradier_client
from app.services.watchlist_exit_worker import watchlist_exit_worker

UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _serialize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
        payload = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision or {})
        record = {
            "recordedAtUtc": _utcnow().isoformat(),
            "allowed": bool(payload.get("allowed")),
            "assetClass": payload.get("assetClass") or payload.get("asset_class"),
            "symbol": payload.get("symbol"),
            "state": payload.get("state"),
            "rejectionReason": payload.get("rejectionReason") or payload.get("rejection_reason") or "",
            "executionSource": execution_source,
            "checks": deepcopy(payload.get("checks") or []),
            "marketData": deepcopy(payload.get("marketData") or payload.get("market_data") or {}),
            "riskData": deepcopy(payload.get("riskData") or payload.get("risk_data") or {}),
            "context": deepcopy(context or {}),
        }
        with self._lock:
            self._gate_records.appendleft(record)
        return deepcopy(record)

    def get_gate_snapshot(self, *, limit: int = 10) -> dict[str, Any]:
        with self._lock:
            records = [deepcopy(item) for item in list(self._gate_records)]

        recent = records[:limit]
        rejections = [item for item in records if not item.get("allowed")]
        approvals = [item for item in records if item.get("allowed")]
        return {
            "capturedAtUtc": _utcnow().isoformat(),
            "summary": {
                "total": len(records),
                "allowedCount": len(approvals),
                "rejectedCount": len(rejections),
                "lastDecision": recent[0] if recent else None,
                "lastAllowed": approvals[0] if approvals else None,
                "lastRejected": rejections[0] if rejections else None,
            },
            "recent": recent,
            "recentRejections": rejections[:limit],
        }

    def get_dependency_status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = _utcnow()
        with self._lock:
            if (
                not force_refresh
                and self._dependency_cache is not None
                and self._dependency_cache_expires_at is not None
                and self._dependency_cache_expires_at > now
            ):
                return deepcopy(self._dependency_cache)

        payload = self._probe_dependencies(now)
        expires_at_raw = payload.get("expiresAtUtc")
        expires_at = (
            datetime.fromisoformat(str(expires_at_raw))
            if expires_at_raw
            else now + timedelta(seconds=max(int(settings.RUNTIME_VISIBILITY_PROBE_TTL_SECONDS), 5))
        )
        with self._lock:
            self._dependency_cache = deepcopy(payload)
            self._dependency_cache_expires_at = expires_at
        return payload

    def get_runtime_snapshot(self, *, limit: int = 10, force_refresh: bool = False) -> dict[str, Any]:
        control_plane = get_control_plane_status()
        execution_gate = get_execution_gate_status()
        dependencies = self.get_dependency_status(force_refresh=force_refresh)
        gate = self.get_gate_snapshot(limit=limit)
        truth_board = self._build_truth_board(
            control_plane=control_plane,
            execution_gate={
                "allowed": execution_gate.allowed,
                "state": execution_gate.state,
                "reason": execution_gate.reason,
                "statusCode": execution_gate.status_code,
            },
            dependencies=dependencies,
        )
        return {
            "capturedAtUtc": _utcnow().isoformat(),
            "controlPlane": control_plane,
            "executionGate": {
                "allowed": execution_gate.allowed,
                "state": execution_gate.state,
                "reason": execution_gate.reason,
                "statusCode": execution_gate.status_code,
            },
            "dependencies": dependencies,
            "truthBoard": truth_board,
            "gate": gate,
            "audit": {
                "replayRejections": self.get_replay_rejections(limit=limit),
                "systemErrors": self.get_system_error_timeline(limit=limit),
                "exitTimeline": self.get_exit_timeline(limit=limit),
            },
        }

    def get_replay_rejections(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return discord_decision_guard.get_replay_rejections(limit=limit)

    def get_system_error_timeline(self, limit: int = 25) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        dependencies = self.get_dependency_status(force_refresh=False) or {}
        dependency_checks = dependencies.get("checks") if isinstance(dependencies, dict) else None
        if isinstance(dependency_checks, dict):
            for key, dep in dependency_checks.items():
                if not isinstance(dep, dict):
                    continue
                if bool(dep.get("ready", True)):
                    continue

                rows.append(
                    {
                        "id": f"dependency:{key}:{dep.get('checkedAtUtc') or dep.get('checked_at') or ''}",
                        "timestamp": dep.get("checkedAtUtc") or dep.get("checked_at") or dep.get("last_checked_at"),
                        "source": "dependency_probe",
                        "component": dep.get("name") or dep.get("label") or str(key),
                        "severity": "error",
                        "state": dep.get("state") or "DEGRADED",
                        "event": "DEPENDENCY_UNHEALTHY",
                        "message": dep.get("reason") or dep.get("message") or "Dependency reported unhealthy state",
                        "detail": dep.get("reason") or dep.get("message") or "Dependency reported unhealthy state",
                        "symbol": None,
                        "details": deepcopy(dep.get("details") or {}),
                    }
                )

        try:
            db = SessionLocal()
            try:
                query_rows = (
                    db.query(OrderEvent, OrderIntent)
                    .join(OrderIntent, OrderIntent.intent_id == OrderEvent.intent_id)
                    .filter(
                        (OrderEvent.status.in_(["REJECTED", "FAILED", "ERROR"]))
                        | (OrderEvent.event_type.in_(["ORDER_SUBMISSION_FAILED", "ORDER_ERROR", "EXIT_ERROR"]))
                    )
                    .order_by(OrderEvent.event_time.desc())
                    .limit(limit)
                    .all()
                )
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception:
            query_rows = []

        for event, intent in query_rows:
            event_time = getattr(event, "event_time", None)
            rows.append(
                {
                    "id": f"order-event:{getattr(event, 'id', '')}",
                    "timestamp": _serialize_timestamp(event_time),
                    "source": "order_events",
                    "component": getattr(intent, "execution_source", None) or "execution",
                    "severity": "error",
                    "state": getattr(event, "status", None) or "ERROR",
                    "event": getattr(event, "event_type", None) or "SYSTEM_ERROR",
                    "message": getattr(event, "message", None) or "Execution failure",
                    "detail": getattr(event, "message", None) or "Execution failure",
                    "symbol": getattr(intent, "symbol", None),
                    "assetClass": getattr(intent, "asset_class", None),
                    "intentId": getattr(intent, "intent_id", None),
                    "details": deepcopy(getattr(event, "payload_json", None) or {}),
                }
            )

        rows.sort(key=lambda row: row.get("timestamp") or "", reverse=True)
        return rows[:limit]

    def get_exit_timeline(self, limit: int = 25) -> list[dict[str, Any]]:
        try:
            db = SessionLocal()
            try:
                query_rows = (
                    db.query(OrderEvent, OrderIntent)
                    .join(OrderIntent, OrderIntent.intent_id == OrderEvent.intent_id)
                    .filter(
                        (OrderEvent.event_type.like("%EXIT%"))
                        | (OrderEvent.event_type.like("%CLOSE%"))
                    )
                    .order_by(OrderEvent.event_time.desc())
                    .limit(limit)
                    .all()
                )
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception:
            return []

        rows: list[dict[str, Any]] = []
        for event, intent in query_rows:
            payload = deepcopy(getattr(event, "payload_json", None) or {})
            context = deepcopy(getattr(intent, "context_json", None) or {})
            rows.append(
                {
                    "id": f"exit:{getattr(event, 'id', '')}",
                    "timestamp": _serialize_timestamp(getattr(event, "event_time", None)),
                    "symbol": getattr(intent, "symbol", None),
                    "assetClass": getattr(intent, "asset_class", None),
                    "status": getattr(event, "status", None) or getattr(intent, "status", None) or "",
                    "eventType": getattr(event, "event_type", None) or "",
                    "executionSource": getattr(intent, "execution_source", None) or "",
                    "trigger": (
                        payload.get("trigger")
                        or payload.get("reason")
                        or context.get("exitTrigger")
                        or context.get("exit_trigger")
                        or ""
                    ),
                    "message": getattr(event, "message", None) or "",
                    "details": payload,
                }
            )
        return rows


    def _build_truth_board(
        self,
        *,
        control_plane: dict[str, Any],
        execution_gate: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> dict[str, Any]:
        dependency_summary = dependencies.get("summary") if isinstance(dependencies, dict) else {}
        checks = dependencies.get("checks") if isinstance(dependencies, dict) else {}
        monitor_check = checks.get("watchlistMonitor") if isinstance(checks, dict) else {}
        scope_truth_raw = monitor_check.get("details", {}).get("scopeTruth") if isinstance(monitor_check, dict) else {}
        scopes: dict[str, dict[str, Any]] = {}
        tracked_scope_payloads: list[dict[str, Any]] = []

        authorization_ready = bool(control_plane.get("authorizationReady"))
        runtime_running = bool(control_plane.get("runtimeRunning"))
        dependencies_operational = bool(dependency_summary.get("operationalReady"))
        supervision_ready = bool(authorization_ready and dependencies_operational)
        fresh_entry_base_ready = bool(supervision_ready and runtime_running and execution_gate.get("allowed"))

        for scope_name in ("stocks_only", "crypto_only"):
            truth = scope_truth_raw.get(scope_name) if isinstance(scope_truth_raw, dict) else {}
            normalized = {
                "scope": scope_name,
                "state": str((truth or {}).get("state") or "MISSING"),
                "reason": str((truth or {}).get("reason") or ""),
                "ready": bool((truth or {}).get("ready", False)),
                "activeUploadId": (truth or {}).get("activeUploadId"),
                "activeUploadReceivedAtUtc": (truth or {}).get("activeUploadReceivedAtUtc"),
                "watchlistExpiresAtUtc": (truth or {}).get("watchlistExpiresAtUtc"),
                "watchlistExpired": bool((truth or {}).get("watchlistExpired", False)),
                "activeSymbolCount": _safe_int((truth or {}).get("activeSymbolCount")),
                "managedOnlyCount": _safe_int((truth or {}).get("managedOnlyCount")),
                "openPositionCount": _safe_int((truth or {}).get("openPositionCount")),
                "dataWarningCount": _safe_int((truth or {}).get("dataWarningCount")),
            }
            tracked = bool(
                normalized["activeUploadId"]
                or normalized["activeSymbolCount"] > 0
                or normalized["managedOnlyCount"] > 0
                or normalized["openPositionCount"] > 0
            )
            normalized["tracked"] = tracked
            normalized["freshEntryReady"] = bool(fresh_entry_base_ready and tracked and normalized["ready"])
            normalized["supervisionReady"] = bool(supervision_ready and (tracked or authorization_ready))
            scopes[scope_name] = normalized
            if tracked:
                tracked_scope_payloads.append(normalized)

        active_issues: list[str] = []
        if not authorization_ready:
            active_issues.append(str(control_plane.get("reason") or "Authorization surfaces are not fully configured."))
        elif not runtime_running:
            active_issues.append(str(control_plane.get("reason") or "Runtime running flag is false."))

        if not dependencies_operational:
            active_issues.append("One or more critical dependencies or worker probes are degraded.")

        if not execution_gate.get("allowed"):
            gate_reason = str(execution_gate.get("reason") or execution_gate.get("state") or "Execution gate is blocking entries.")
            if gate_reason:
                active_issues.append(gate_reason)

        for scope_name, scope_payload in scopes.items():
            if not scope_payload["tracked"]:
                continue
            if scope_payload["state"] != "READY":
                reason = scope_payload.get("reason") or scope_payload["state"]
                active_issues.append(f"{scope_name}: {reason}")

        unique_issues: list[str] = []
        seen: set[str] = set()
        for issue in active_issues:
            normalized_issue = str(issue).strip()
            if not normalized_issue or normalized_issue in seen:
                continue
            seen.add(normalized_issue)
            unique_issues.append(normalized_issue)

        fresh_entry_ready = bool(any(scope["freshEntryReady"] for scope in tracked_scope_payloads))
        if not tracked_scope_payloads:
            fresh_entry_ready = False
            if runtime_running and authorization_ready:
                unique_issues.append("No active watchlist scopes are currently loaded for fresh entries.")

        if not supervision_ready:
            truth_state = "BLOCKED"
            truth_reason = unique_issues[0] if unique_issues else "Supervision rails are not operational."
        elif fresh_entry_ready:
            truth_state = "READY"
            truth_reason = "At least one tracked scope is eligible for fresh entries and supervision is healthy."
        else:
            truth_state = "REVIEW"
            truth_reason = unique_issues[0] if unique_issues else "No tracked scope is currently eligible for fresh entries."

        return {
            "state": truth_state,
            "reason": truth_reason,
            "freshEntryReady": fresh_entry_ready,
            "supervisionReady": supervision_ready,
            "trackedScopeCount": len(tracked_scope_payloads),
            "activeIssues": unique_issues,
            "scopes": scopes,
        }

    def _probe_dependencies(self, observed_at: datetime) -> dict[str, Any]:
        ttl = max(int(settings.RUNTIME_VISIBILITY_PROBE_TTL_SECONDS), 5)
        payload = {
            "observedAtUtc": observed_at.isoformat(),
            "expiresAtUtc": (observed_at + timedelta(seconds=ttl)).isoformat(),
            "summary": {},
            "checks": {
                "tradierPaper": self._probe_tradier("PAPER", observed_at),
                "tradierLive": self._probe_tradier("LIVE", observed_at),
                "krakenMarketData": self._probe_kraken(observed_at),
                "watchlistMonitor": self._probe_watchlist_monitor(observed_at),
                "watchlistExitWorker": self._probe_watchlist_exit_worker(observed_at),
            },
        }
        checks = payload["checks"]
        ready_count = sum(1 for item in checks.values() if item["ready"])
        degraded_count = sum(1 for item in checks.values() if item["state"] == "DEGRADED")
        missing_count = sum(1 for item in checks.values() if item["state"] == "MISSING")
        stale_count = sum(1 for item in checks.values() if item["state"] == "STALE")
        disabled_count = sum(1 for item in checks.values() if item["state"] == "DISABLED")
        critical_ready = bool(checks["tradierPaper"]["ready"] and checks["krakenMarketData"]["ready"])
        worker_ready = bool(checks["watchlistMonitor"]["ready"] and checks["watchlistExitWorker"]["ready"])
        payload["summary"] = {
            "readyCount": ready_count,
            "degradedCount": degraded_count,
            "missingCount": missing_count,
            "staleCount": stale_count,
            "disabledCount": disabled_count,
            "criticalReady": critical_ready,
            "workerReady": worker_ready,
            "operationalReady": bool(critical_ready and worker_ready),
        }
        return payload

    def _probe_tradier(self, mode: str, observed_at: datetime) -> dict[str, Any]:
        selected_mode = str(mode or "PAPER").upper()
        if not tradier_client.is_ready(selected_mode):
            return {
                "name": f"Tradier {selected_mode.title()}",
                "state": "MISSING",
                "ready": False,
                "reason": f"Tradier {selected_mode} credentials are not configured.",
                "checkedAtUtc": observed_at.isoformat(),
                "details": {"mode": selected_mode},
            }

        try:
            snapshot = tradier_client.get_account_snapshot(selected_mode)
        except Exception as exc:  # pragma: no cover
            return {
                "name": f"Tradier {selected_mode.title()}",
                "state": "DEGRADED",
                "ready": False,
                "reason": str(exc),
                "checkedAtUtc": observed_at.isoformat(),
                "details": {"mode": selected_mode},
            }

        connected = bool(snapshot.get("connected"))
        return {
            "name": f"Tradier {selected_mode.title()}",
            "state": "READY" if connected else "DEGRADED",
            "ready": connected,
            "reason": "" if connected else "Tradier account snapshot did not report a live connection.",
            "checkedAtUtc": observed_at.isoformat(),
            "details": {
                "mode": selected_mode,
                "accountId": snapshot.get("accountId") or "",
                "portfolioValue": snapshot.get("portfolioValue") or 0.0,
            },
        }

    def _probe_kraken(self, observed_at: datetime) -> dict[str, Any]:
        supported_pairs = kraken_service.get_supported_pairs()
        probe_pair = kraken_service.get_ohlcv_pair("BTC/USD") or next(iter(supported_pairs.values()), "XBTUSD")
        try:
            ticker = kraken_service.get_ticker(probe_pair)
        except Exception as exc:  # pragma: no cover
            return {
                "name": "Kraken Market Data",
                "state": "DEGRADED",
                "ready": False,
                "reason": str(exc),
                "checkedAtUtc": observed_at.isoformat(),
                "details": {"pair": probe_pair},
            }

        if ticker and ticker.get("c"):
            return {
                "name": "Kraken Market Data",
                "state": "READY",
                "ready": True,
                "reason": "",
                "checkedAtUtc": observed_at.isoformat(),
                "details": {"pair": probe_pair, "lastPrice": float(ticker["c"][0])},
            }

        return {
            "name": "Kraken Market Data",
            "state": "DEGRADED",
            "ready": False,
            "reason": "Kraken ticker probe did not return a current price.",
            "checkedAtUtc": observed_at.isoformat(),
            "details": {"pair": probe_pair},
        }

    def _probe_watchlist_monitor(self, observed_at: datetime) -> dict[str, Any]:
        from app.services.watchlist_monitoring import watchlist_monitoring_orchestrator
        from app.services.watchlist_service import watchlist_service

        db = SessionLocal()
        try:
            status = watchlist_monitoring_orchestrator.get_runtime_status(db)
            monitoring_snapshot = watchlist_service.get_monitoring_snapshot(db)
        except Exception as exc:  # pragma: no cover
            return {
                "name": "Watchlist Monitor",
                "state": "DEGRADED",
                "ready": False,
                "reason": str(exc),
                "checkedAtUtc": observed_at.isoformat(),
                "details": {},
            }
        finally:
            db.close()

        scope_truth: dict[str, dict[str, Any]] = {}
        for scope, snapshot in (monitoring_snapshot or {}).items():
            if not isinstance(snapshot, dict):
                continue
            truth_payload = snapshot.get("scopeTruth") if isinstance(snapshot.get("scopeTruth"), dict) else snapshot
            normalized_truth = {
                "state": str((truth_payload or {}).get("state") or "READY"),
                "reason": str((truth_payload or {}).get("reason") or ""),
            }
            scope_truth[str(scope)] = normalized_truth

        scope_issues = [
            f"{scope}: {truth.get('reason')}"
            for scope, truth in scope_truth.items()
            if str(truth.get("state") or "READY") != "READY"
        ]

        probe = self._build_worker_probe(
            name="Watchlist Monitor",
            observed_at=observed_at,
            enabled=bool(status.get("enabled")),
            poll_seconds=int(status.get("pollSeconds") or 0),
            last_started_at=status.get("lastStartedAtUtc"),
            last_finished_at=status.get("lastFinishedAtUtc"),
            last_error=status.get("lastError"),
            consecutive_failures=int(status.get("consecutiveFailures") or 0),
            details={
                "dueSnapshot": status.get("dueSnapshot"),
                "lastRunSummary": status.get("lastRunSummary") or {},
                "scopeTruth": scope_truth,
                "scopeIssues": scope_issues,
            },
        )
        if probe["state"] == "READY" and scope_issues:
            probe["state"] = "DEGRADED"
            probe["ready"] = True
            probe["reason"] = "Worker loop is active, but one or more scopes need review."
        return probe

    def _probe_watchlist_exit_worker(self, observed_at: datetime) -> dict[str, Any]:
        db = SessionLocal()
        try:
            status = watchlist_exit_worker.get_status(db)
        except Exception as exc:  # pragma: no cover
            return {
                "name": "Watchlist Exit Worker",
                "state": "DEGRADED",
                "ready": False,
                "reason": str(exc),
                "checkedAtUtc": observed_at.isoformat(),
                "details": {},
            }
        finally:
            db.close()

        return self._build_worker_probe(
            name="Watchlist Exit Worker",
            observed_at=observed_at,
            enabled=bool(status.get("enabled")),
            poll_seconds=int(status.get("pollSeconds") or 0),
            last_started_at=status.get("lastStartedAtUtc"),
            last_finished_at=status.get("lastFinishedAtUtc"),
            last_error=status.get("lastError"),
            consecutive_failures=int(status.get("consecutiveFailures") or 0),
            details={
                "summary": status.get("summary") or {},
                "session": status.get("session") or {},
                "lastRunSummary": status.get("lastRunSummary") or {},
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
            "pollSeconds": poll_seconds,
            "lastStartedAtUtc": last_started_at,
            "lastFinishedAtUtc": last_finished_at,
            "consecutiveFailures": consecutive_failures,
        }
        if details:
            payload_details.update(details)

        if not enabled:
            return {
                "name": name,
                "state": "DISABLED",
                "ready": True,
                "reason": "Worker loop is disabled by configuration.",
                "checkedAtUtc": observed_at.isoformat(),
                "details": payload_details,
            }

        if last_error and consecutive_failures > 0:
            return {
                "name": name,
                "state": "DEGRADED",
                "ready": False,
                "reason": last_error,
                "checkedAtUtc": observed_at.isoformat(),
                "details": payload_details,
            }

        freshness_window = max(poll_seconds * 3, 30)
        fresh_cutoff = observed_at - timedelta(seconds=freshness_window)
        finished_at = self._parse_timestamp(last_finished_at)
        started_at = self._parse_timestamp(last_started_at)

        if finished_at and finished_at >= fresh_cutoff:
            return {
                "name": name,
                "state": "READY",
                "ready": True,
                "reason": "",
                "checkedAtUtc": observed_at.isoformat(),
                "details": payload_details,
            }

        if started_at and started_at >= fresh_cutoff:
            return {
                "name": name,
                "state": "READY",
                "ready": True,
                "reason": "Worker loop is running its current sweep.",
                "checkedAtUtc": observed_at.isoformat(),
                "details": payload_details,
            }

        last_seen = finished_at or started_at
        if last_seen is None:
            return {
                "name": name,
                "state": "DEGRADED",
                "ready": False,
                "reason": "Worker loop has not reported a run yet.",
                "checkedAtUtc": observed_at.isoformat(),
                "details": payload_details,
            }

        age_seconds = int((observed_at - last_seen).total_seconds())
        return {
            "name": name,
            "state": "STALE",
            "ready": False,
            "reason": f"Last worker activity was {age_seconds}s ago, outside the expected poll window.",
            "checkedAtUtc": observed_at.isoformat(),
            "details": payload_details,
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