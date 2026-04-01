from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


class DiscordNotificationService:
    def __init__(self) -> None:
        self._bot = None

    def register_bot(self, bot: Any) -> None:
        self._bot = bot

    def unregister_bot(self, bot: Any) -> None:
        if self._bot is bot:
            self._bot = None

    def send_trade_alert(
        self,
        *,
        asset_class: str,
        side: str,
        symbol: str,
        quantity: float,
        price: float,
        execution_source: str,
        account_id: str | None = None,
        status: str = 'FILLED',
        extra: dict[str, Any] | None = None,
    ) -> bool:
        bot = self._bot
        if bot is None or not settings.DISCORD_TRADING_CHANNEL_ID:
            return False
        message = self._format_trade_alert(
            asset_class=asset_class,
            side=side,
            symbol=symbol,
            quantity=quantity,
            price=price,
            execution_source=execution_source,
            account_id=account_id,
            status=status,
            extra=extra or {},
        )
        return self._schedule_send(bot, message)

    def _schedule_send(self, bot: Any, message: str) -> bool:
        if not message:
            return False
        if getattr(bot, 'is_closed', lambda: False)():
            return False
        loop = getattr(bot, 'loop', None)
        if loop is None or not loop.is_running():
            return False
        coro = self._send_message(bot, message)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)
        return True

    async def _send_message(self, bot: Any, message: str) -> None:
        try:
            channel = bot.get_channel(settings.DISCORD_TRADING_CHANNEL_ID)
            if channel is None and hasattr(bot, 'fetch_channel'):
                channel = await bot.fetch_channel(settings.DISCORD_TRADING_CHANNEL_ID)
            if channel is None:
                logger.warning('Discord trade alert skipped because trading channel %s was not found.', settings.DISCORD_TRADING_CHANNEL_ID)
                return
            await channel.send(message)
        except Exception:
            logger.exception('Failed to send Discord trade alert.')

    def _format_trade_alert(
        self,
        *,
        asset_class: str,
        side: str,
        symbol: str,
        quantity: float,
        price: float,
        execution_source: str,
        account_id: str | None,
        status: str,
        extra: dict[str, Any],
    ) -> str:
        normalized_asset = 'crypto' if str(asset_class).lower().startswith('crypto') else 'stock'
        normalized_side = str(side or '').upper()
        action_label = 'BUY' if normalized_side == 'BUY' else 'SELL'
        title = f"{normalized_asset.title()} {action_label} {status.title()}"
        emoji = '🟢' if normalized_side == 'BUY' else '🔻'
        quantity_label = self._format_quantity(normalized_asset, quantity)
        notional = self._money(quantity * price)
        lines = [
            f"{emoji} **{title}**",
            f"• **Symbol:** {symbol}",
            f"• **Size:** {quantity_label}",
            f"• **Fill:** {self._money(price)}",
            f"• **Notional:** {notional}",
        ]

        mode = extra.get('mode') or self._infer_mode(account_id)
        if mode:
            lines.append(f"• **Mode:** {mode}")
        if execution_source:
            lines.append(f"• **Source:** `{execution_source}`")
        trigger = str(extra.get('trigger') or '').strip()
        if trigger:
            lines.append(f"• **Trigger:** {trigger}")
        remaining = extra.get('remainingShares')
        if remaining is not None:
            try:
                remaining_value = int(round(float(remaining)))
            except (TypeError, ValueError):
                remaining_value = None
            if remaining_value is not None:
                lines.append(f"• **Remaining open:** {remaining_value} shares")
        pnl = extra.get('pnl')
        if pnl is not None:
            lines.append(f"• **P&L:** {self._signed_money(pnl)}")
        reason = str(extra.get('reason') or '').strip()
        if reason:
            lines.append(f"• **Reason:** {reason}")
        return '\n'.join(lines)

    @staticmethod
    def _infer_mode(account_id: str | None) -> str | None:
        raw = str(account_id or '').strip().lower()
        if not raw:
            return None
        if 'paper' in raw:
            return 'PAPER'
        if 'live' in raw:
            return 'LIVE'
        return None

    @staticmethod
    def _money(value: float | int | Decimal | None) -> str:
        try:
            amount = float(value or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        return f"${amount:,.2f}"

    @staticmethod
    def _signed_money(value: float | int | Decimal | None) -> str:
        try:
            amount = float(value or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        prefix = '+' if amount > 0 else ''
        return f"{prefix}${amount:,.2f}"

    @staticmethod
    def _format_quantity(asset_class: str, quantity: float) -> str:
        if asset_class == 'stock':
            try:
                shares = int(round(float(quantity)))
            except (TypeError, ValueError):
                shares = 0
            unit = 'share' if abs(shares) == 1 else 'shares'
            return f"**{shares} {unit}**"
        try:
            quantized = Decimal(str(quantity)).normalize()
        except (InvalidOperation, ValueError):
            quantized = Decimal('0')
        amount = format(quantized, 'f').rstrip('0').rstrip('.') or '0'
        return f"**{amount} units**"


discord_notifications = DiscordNotificationService()
