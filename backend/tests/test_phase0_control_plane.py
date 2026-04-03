from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import control_plane
from app.services.runtime_state import runtime_state


@contextmanager
def patched_settings(**updates) -> Iterator[None]:
    original = {key: getattr(settings, key) for key in updates}
    try:
        for key, value in updates.items():
            setattr(settings, key, value)
        yield
    finally:
        for key, value in original.items():
            setattr(settings, key, value)




def test_require_admin_token_accepts_bearer_authorization_header() -> None:
    with patched_settings(ADMIN_API_TOKEN='admin-token'):
        assert control_plane.require_admin_token(authorization='Bearer admin-token') is True


def test_require_admin_token_accepts_trimmed_x_admin_token_header() -> None:
    with patched_settings(ADMIN_API_TOKEN='admin-token'):
        assert control_plane.require_admin_token(x_admin_token='  admin-token  ') is True


def test_require_admin_token_rejects_wrong_authorization_header() -> None:
    with patched_settings(ADMIN_API_TOKEN='admin-token'):
        try:
            control_plane.require_admin_token(authorization='Bearer wrong-token')
        except Exception as exc:  # pragma: no cover - explicit assertion below
            from fastapi import HTTPException

            assert isinstance(exc, HTTPException)
            assert exc.status_code == 403
            assert exc.detail == 'Unauthorized control-plane request.'
        else:  # pragma: no cover
            raise AssertionError('Expected wrong bearer token to be rejected.')

def test_execution_gate_reports_locked_when_admin_token_missing() -> None:
    with patched_settings(
        ADMIN_API_TOKEN='',
        DISCORD_BOT_TOKEN='discord-token',
        DISCORD_TRADING_CHANNEL_ID=123,
        DISCORD_USER_ID=456,
    ):
        runtime_state.set_running(True)
        gate = control_plane.get_execution_gate_status()

    assert gate.allowed is False
    assert gate.state == 'LOCKED'
    assert gate.status_code == 503
    assert 'ADMIN_API_TOKEN' in gate.reason


def test_execution_gate_reports_read_only_when_discord_auth_incomplete() -> None:
    with patched_settings(
        ADMIN_API_TOKEN='admin-token',
        DISCORD_BOT_TOKEN='',
        DISCORD_TRADING_CHANNEL_ID=0,
        DISCORD_USER_ID=0,
    ):
        runtime_state.set_running(True)
        gate = control_plane.get_execution_gate_status()

    assert gate.allowed is False
    assert gate.state == 'READ_ONLY'
    assert gate.status_code == 503
    assert 'Discord authorization settings are incomplete' in gate.reason


def test_execution_gate_reports_paused_when_runtime_not_running() -> None:
    with patched_settings(
        ADMIN_API_TOKEN='admin-token',
        DISCORD_BOT_TOKEN='discord-token',
        DISCORD_TRADING_CHANNEL_ID=123,
        DISCORD_USER_ID=456,
    ):
        runtime_state.set_running(False)
        gate = control_plane.get_execution_gate_status()

    assert gate.allowed is False
    assert gate.state == 'PAUSED'
    assert gate.status_code == 409
    assert 'Runtime running flag is false' in gate.reason

    runtime_state.set_running(True)


def test_execution_gate_reports_armed_when_all_controls_are_ready() -> None:
    with patched_settings(
        ADMIN_API_TOKEN='admin-token',
        DISCORD_BOT_TOKEN='discord-token',
        DISCORD_TRADING_CHANNEL_ID=123,
        DISCORD_USER_ID=456,
    ):
        runtime_state.set_running(True)
        gate = control_plane.get_execution_gate_status()

    assert gate.allowed is True
    assert gate.state == 'ARMED'
    assert gate.status_code == 200


def test_crypto_trade_route_refuses_execution_when_control_plane_is_read_only() -> None:
    with patched_settings(
        ADMIN_API_TOKEN='admin-token',
        DISCORD_BOT_TOKEN='',
        DISCORD_TRADING_CHANNEL_ID=0,
        DISCORD_USER_ID=0,
    ):
        runtime_state.set_running(True)
        with TestClient(app) as client:
            response = client.post(
                '/api/crypto/trade',
                headers={'X-Admin-Token': 'admin-token'},
                json={'pair': 'BTC/USD', 'side': 'BUY', 'amount': 0.1},
            )

    assert response.status_code == 503
    assert 'Execution blocked' in response.json()['detail']
    assert 'Discord authorization settings are incomplete' in response.json()['detail']



