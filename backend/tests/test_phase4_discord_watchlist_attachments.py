from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


if 'discord' not in sys.modules:
    discord_module = types.ModuleType('discord')

    class _FakeIntents:
        def __init__(self) -> None:
            self.message_content = False
            self.reactions = False

        @staticmethod
        def default() -> "_FakeIntents":
            return _FakeIntents()

    discord_module.Intents = _FakeIntents

    ext_module = types.ModuleType('discord.ext')
    commands_module = types.ModuleType('discord.ext.commands')
    tasks_module = types.ModuleType('discord.ext.tasks')

    class _FakeBot:
        def __init__(self, *args, **kwargs) -> None:
            self.user = None

        async def close(self) -> None:
            return None

    commands_module.Bot = _FakeBot

    class _FakeLoop:
        def __init__(self, *args, **kwargs) -> None:
            self._running = False

        def __call__(self, fn):
            fn.start = lambda *a, **k: None
            fn.is_running = lambda *a, **k: False
            return fn

    tasks_module.loop = lambda *a, **k: _FakeLoop(*a, **k)
    ext_module.commands = commands_module
    ext_module.tasks = tasks_module

    sys.modules['discord'] = discord_module
    sys.modules['discord.ext'] = ext_module
    sys.modules['discord.ext.commands'] = commands_module
    sys.modules['discord.ext.tasks'] = tasks_module

from app.services import discord_bot as discord_bot_module
from app.services.discord_bot import TradingBot


def build_stock_watchlist_payload() -> dict:
    return {
        'schema_version': 'bot_stock_watchlist_v1',
        'generated_at_utc': '2026-03-29T16:00:00Z',
        'provider': 'claude_tradier_mcp',
        'scope': 'stocks_only',
        'bot_payload': {
            'market_regime': 'mixed',
            'symbols': [
                {
                    'symbol': 'AAPL',
                    'quote_currency': 'USD',
                    'asset_class': 'stock',
                    'enabled': True,
                    'trade_direction': 'long',
                    'priority_rank': 1,
                    'tier': 'tier_1',
                    'bias': 'bullish',
                    'setup_template': 'pullback_reclaim',
                    'bot_timeframes': ['15m', '1h', '4h', '1d'],
                    'exit_template': 'scale_out_then_trail',
                    'max_hold_hours': 72,
                    'risk_flags': ['crowded_trade'],
                }
            ],
        },
        'ui_payload': {
            'summary': {
                'selected_count': 1,
                'primary_focus': ['AAPL'],
                'regime_note': 'Mixed tape.',
            },
            'provider_limitations': ['Provider does not expose exact triggers.'],
            'symbol_context': {
                'AAPL': {
                    'scan_reason': 'relative_strength',
                    'sector': 'Technology',
                    'thesis': 'Large-cap leader.',
                    'why_now': 'Holding up in a mixed tape.',
                    'notes': 'Let the bot confirm the reclaim.',
                }
            },
        },
    }


class FakeAttachment:
    def __init__(self, filename: str, payload: bytes, content_type: str = 'application/json') -> None:
        self.filename = filename
        self.content_type = content_type
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


class FakeMessage:
    def __init__(self, *, content: str = '', attachments: list[FakeAttachment] | None = None, channel_id: int = 777) -> None:
        self.id = 12345
        self.content = content
        self.attachments = attachments or []
        self.author = SimpleNamespace(id=999, bot=False)
        self.channel = SimpleNamespace(id=channel_id)
        self.reactions: list[str] = []
        self.replies: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def reply(self, content: str):
        self.replies.append(content)
        return SimpleNamespace(add_reaction=AsyncMock())


class DummySession:
    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_on_message_processes_watchlist_json_attachment(monkeypatch) -> None:
    payload = build_stock_watchlist_payload()
    bot = TradingBot()
    bot.trading_channel_id = 777
    bot.process_commands = AsyncMock()
    bot._process_watchlist_upload = AsyncMock()

    monkeypatch.setattr(
        discord_bot_module.discord_decision_guard,
        'authorize_message',
        lambda message: SimpleNamespace(authorized=True, reason='accepted'),
    )
    monkeypatch.setattr(
        discord_bot_module.discord_decision_guard,
        'validate_and_register',
        lambda message, decision: (True, 'accepted'),
    )

    message = FakeMessage(
        attachments=[FakeAttachment('stocks.json', json.dumps(payload).encode('utf-8'))],
    )

    await bot.on_message(message)

    bot.process_commands.assert_awaited_once_with(message)
    bot._process_watchlist_upload.assert_awaited_once()
    args = bot._process_watchlist_upload.await_args.args
    kwargs = bot._process_watchlist_upload.await_args.kwargs
    assert args[0] is message
    assert args[1] == payload
    assert kwargs['source_label'] == 'attachment:stocks.json'
    assert '🧾' in message.reactions

    await bot.close()


@pytest.mark.asyncio
async def test_on_message_processes_watchlist_txt_attachment(monkeypatch) -> None:
    payload = build_stock_watchlist_payload()
    bot = TradingBot()
    bot.trading_channel_id = 777
    bot.process_commands = AsyncMock()
    bot._process_watchlist_upload = AsyncMock()

    monkeypatch.setattr(
        discord_bot_module.discord_decision_guard,
        'authorize_message',
        lambda message: SimpleNamespace(authorized=True, reason='accepted'),
    )
    monkeypatch.setattr(
        discord_bot_module.discord_decision_guard,
        'validate_and_register',
        lambda message, decision: (True, 'accepted'),
    )

    message = FakeMessage(
        attachments=[FakeAttachment('stocks.txt', json.dumps(payload).encode('utf-8'), content_type='text/plain')],
    )

    await bot.on_message(message)

    bot.process_commands.assert_awaited_once_with(message)
    bot._process_watchlist_upload.assert_awaited_once()
    args = bot._process_watchlist_upload.await_args.args
    kwargs = bot._process_watchlist_upload.await_args.kwargs
    assert args[0] is message
    assert args[1] == payload
    assert kwargs['source_label'] == 'attachment:stocks.txt'
    assert '🧾' in message.reactions

    await bot.close()


@pytest.mark.asyncio
async def test_on_message_replies_for_invalid_json_attachment(monkeypatch) -> None:
    bot = TradingBot()
    bot.trading_channel_id = 777
    bot.process_commands = AsyncMock()

    message = FakeMessage(
        attachments=[FakeAttachment('stocks.json', b'{"schema_version":')],
    )

    await bot.on_message(message)

    assert '❌' in message.reactions
    assert any('Invalid JSON attachment `stocks.json`' in reply for reply in message.replies)

    await bot.close()


@pytest.mark.asyncio
async def test_watchlist_upload_reply_acknowledges_attachment_source(monkeypatch) -> None:
    payload = build_stock_watchlist_payload()
    persisted = {
        'schemaVersion': 'bot_stock_watchlist_v1',
        'scope': 'stocks_only',
        'selectedCount': 1,
        'marketRegime': 'mixed',
        'uploadId': 'wlu_test',
        'scanId': 'scan_test',
    }

    bot = TradingBot()
    message = FakeMessage()

    monkeypatch.setattr(discord_bot_module, 'SessionLocal', lambda: DummySession())
    monkeypatch.setattr(
        discord_bot_module.watchlist_service,
        'ingest_watchlist',
        lambda db, payload, **kwargs: persisted,
    )

    await bot._process_watchlist_upload(message, payload, source_label='attachment:stocks.json')

    assert '✅' in message.reactions
    assert any('attachment `stocks.json`' in reply for reply in message.replies)
    assert any('Accepted **bot_stock_watchlist_v1** watchlist' in reply for reply in message.replies)

    await bot.close()
