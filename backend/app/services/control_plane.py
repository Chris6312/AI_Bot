from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Optional

from fastapi import Header, HTTPException, status

from app.core.config import settings
from app.services.runtime_state import runtime_state

logger = logging.getLogger(__name__)

_SUPPORTED_DECISION_TYPES = {
    'SCREENING',
    'CRYPTO_SCREENING',
    'BOT_STOCK_WATCHLIST_V1',
    'BOT_WATCHLIST_V3',
}
_TIMESTAMP_FIELDS = ('generated_at_utc', 'generated_at', 'timestamp', 'created_at', 'decision_time')


@dataclass(frozen=True)
class DiscordAuthorizationResult:
    authorized: bool
    reason: str = ''


@dataclass(frozen=True)
class ExecutionGateResult:
    allowed: bool
    state: str
    reason: str = ''
    status_code: int = status.HTTP_200_OK


class DiscordReplayGuard:
    def __init__(self, ttl_seconds: int = 60 * 60 * 24):
        self.ttl_seconds = ttl_seconds
        self._lock = Lock()
        self._seen: dict[str, datetime] = {}

    def _prune(self, now: datetime) -> None:
        expired = [key for key, expires_at in self._seen.items() if expires_at <= now]
        for key in expired:
            self._seen.pop(key, None)

    def register(self, *keys: str) -> bool:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        with self._lock:
            self._prune(now)
            for key in keys:
                if key in self._seen:
                    return False
            for key in keys:
                self._seen[key] = expires_at
        return True


class DiscordDecisionGuard:
    def __init__(self) -> None:
        self._replay_guard = DiscordReplayGuard()

    def authorize_message(self, message: Any) -> DiscordAuthorizationResult:
        if not settings.DISCORD_TRADING_CHANNEL_ID:
            return DiscordAuthorizationResult(False, 'Trading channel is not configured.')
        if getattr(getattr(message, 'channel', None), 'id', None) != settings.DISCORD_TRADING_CHANNEL_ID:
            return DiscordAuthorizationResult(False, 'Message came from a non-trading channel.')

        author = getattr(message, 'author', None)
        author_id = getattr(author, 'id', None)
        if settings.DISCORD_USER_ID and author_id != settings.DISCORD_USER_ID:
            return DiscordAuthorizationResult(False, 'Message author is not the authorized Discord user.')

        allowed_roles = settings.discord_allowed_role_ids
        if allowed_roles:
            roles = getattr(author, 'roles', []) or []
            role_ids = {getattr(role, 'id', 0) for role in roles}
            if not role_ids.intersection(allowed_roles):
                return DiscordAuthorizationResult(False, 'Message author is missing a required Discord role.')

        return DiscordAuthorizationResult(True)

    def validate_and_register(self, message: Any, payload: dict[str, Any]) -> tuple[bool, str]:
        decision_type = str(payload.get('type') or payload.get('schema_version') or '').upper().strip()
        if decision_type not in _SUPPORTED_DECISION_TYPES:
            return False, f'Unsupported decision type: {decision_type or "(missing)"}'

        generated_at = self._extract_timestamp(payload)
        is_watchlist_payload = decision_type in {'BOT_STOCK_WATCHLIST_V1', 'BOT_WATCHLIST_V3'}
        if settings.DISCORD_REQUIRE_DECISION_TIMESTAMP and generated_at is None and not is_watchlist_payload:
            return False, 'Decision JSON is missing a freshness timestamp.'
        if generated_at is not None:
            max_age = max(1, int(settings.DISCORD_DECISION_MAX_AGE_SECONDS))
            age_seconds = (datetime.now(timezone.utc) - generated_at).total_seconds()
            if age_seconds < -30:
                return False, 'Decision timestamp is in the future.'
            if age_seconds > max_age:
                return False, f'Decision payload is stale ({int(age_seconds)}s old).'

        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        message_id = getattr(message, 'id', None)
        keys = [f'payload:{payload_hash}']
        if message_id is not None:
            keys.append(f'message:{message_id}')

        if not self._replay_guard.register(*keys):
            return False, 'Duplicate Discord payload suppressed.'
        return True, 'accepted'

    @staticmethod
    def _extract_timestamp(payload: dict[str, Any]) -> datetime | None:
        for field in _TIMESTAMP_FIELDS:
            raw_value = payload.get(field)
            if not raw_value:
                continue
            if isinstance(raw_value, datetime):
                value = raw_value
            else:
                text = str(raw_value).strip()
                if text.endswith('Z'):
                    text = text[:-1] + '+00:00'
                try:
                    value = datetime.fromisoformat(text)
                except ValueError:
                    logger.warning('Invalid decision timestamp in %s: %s', field, raw_value)
                    return None
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return None


def ensure_runtime_running() -> None:
    if not runtime_state.get().running:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Runtime is paused. No execution is allowed.')


def _discord_auth_ready() -> bool:
    return bool(
        settings.DISCORD_BOT_TOKEN and settings.DISCORD_TRADING_CHANNEL_ID and settings.DISCORD_USER_ID
    )


def get_control_plane_status() -> dict[str, Any]:
    state = runtime_state.get()
    discord_ready = _discord_auth_ready()
    admin_ready = settings.admin_api_ready
    authorization_ready = discord_ready and admin_ready

    if not admin_ready:
        control_state = 'LOCKED'
        reason = 'ADMIN_API_TOKEN is not configured.'
    elif not discord_ready:
        control_state = 'READ_ONLY'
        reason = 'Discord authorization settings are incomplete.'
    elif not state.running:
        control_state = 'PAUSED'
        reason = 'Runtime running flag is false.'
    else:
        control_state = 'ARMED'
        reason = 'Execution surfaces are authenticated and runtime is enabled.'

    return {
        'state': control_state,
        'reason': reason,
        'runtimeRunning': state.running,
        'adminApiReady': admin_ready,
        'discordAuthReady': discord_ready,
        'authorizationReady': authorization_ready,
        'lastHeartbeat': state.last_heartbeat,
    }


def get_execution_gate_status() -> ExecutionGateResult:
    status_payload = get_control_plane_status()
    state = status_payload['state']
    reason = status_payload['reason']

    if state == 'ARMED':
        return ExecutionGateResult(True, state='ARMED')
    if state == 'PAUSED':
        return ExecutionGateResult(False, state=state, reason=reason, status_code=status.HTTP_409_CONFLICT)
    return ExecutionGateResult(False, state=state, reason=reason, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


def ensure_execution_armed() -> None:
    gate = get_execution_gate_status()
    if gate.allowed:
        return
    raise HTTPException(status_code=gate.status_code, detail=f'Execution blocked: {gate.reason}')


def _normalize_admin_token(value: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='ignore')
    elif not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _extract_admin_token(*, x_admin_token: str | None, authorization: str | None) -> str | None:
    direct_token = _normalize_admin_token(x_admin_token)
    if direct_token is not None:
        return direct_token

    normalized_authorization = _normalize_admin_token(authorization)
    if normalized_authorization is None:
        return None

    scheme, _, credentials = normalized_authorization.partition(' ')
    if scheme.lower() == 'bearer':
        return _normalize_admin_token(credentials)
    return normalized_authorization


def require_admin_token(
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> bool:
    configured_token = settings.ADMIN_API_TOKEN.strip()
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Control plane is locked. Configure ADMIN_API_TOKEN before using state-changing routes.',
        )

    provided_token = _extract_admin_token(
        x_admin_token=x_admin_token,
        authorization=authorization,
    )
    if provided_token != configured_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Unauthorized control-plane request.',
        )
    return True


discord_decision_guard = DiscordDecisionGuard()
