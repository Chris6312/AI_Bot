"""Discord bot for trading notifications and control"""
import discord
from discord.ext import commands, tasks
import json
import re
import asyncio
from datetime import datetime, time
from typing import Optional, Dict, List
import logging

from app.core.config import settings
from app.services.tradier_client import TradierClient
from app.services.safety_validator import SafetyValidator
from app.core.database import SessionLocal
from app.models.position import Position
from app.models.trade import Trade

logger = logging.getLogger(__name__)


class TradingBot(commands.Bot):
    """Discord bot for trading operations"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(command_prefix='!', intents=intents)
        
        self.trading_channel_id = settings.DISCORD_TRADING_CHANNEL_ID
        self.tradier = TradierClient()
        self.safety = SafetyValidator()
        self.executions = {}
    
    async def on_ready(self):
        """Bot connected"""
        logger.info(f'Discord bot connected as {self.user}')
        channel = self.get_channel(self.trading_channel_id)
        if channel:
            await channel.send('🤖 **Trading bot online and listening for decisions**')
        
        if settings.APP_ENV == "production":
            if not self.daily_summary.is_running():
                self.daily_summary.start()
    
    async def on_message(self, message):
        """Process incoming messages"""
        
        if message.author == self.user:
            return
        
        if message.channel.id != self.trading_channel_id:
            return
        
        # Messages from webhook (Claude/ChatGPT)
        if message.webhook_id:
            await self.process_ai_decision(message)
        
        await self.process_commands(message)
    
    async def process_ai_decision(self, message):
        """Process AI decision - EXECUTE IMMEDIATELY with safety checks"""
        
        try:
            decision_data = self._extract_decision(message)
            
            if not decision_data:
                logger.warning(f"Could not extract decision from: {message.content[:100]}")
                return
            
            await message.add_reaction('👀')
            
            # SAFETY CHECKS
            db = SessionLocal()
            try:
                account = self.tradier.get_account_sync()
                safety_result = await self.safety.validate(decision_data, account, db)
                
                if not safety_result['safe']:
                    await message.add_reaction('⛔')
                    reply = await message.reply(
                        f"❌ **REJECTED - Safety Check Failed**\n"
                        f"Reason: {safety_result['reason']}\n\n"
                        f"React with 🔓 to override (if you're sure)"
                    )
                    
                    if settings.SAFETY_ALLOW_OVERRIDE:
                        await reply.add_reaction('🔓')
                        asyncio.create_task(
                            self._handle_override(reply, message, decision_data)
                        )
                    return
                
                # EXECUTE IMMEDIATELY
                await message.add_reaction('⚡')
                result = await self._execute_screening(decision_data, db)
                
                execution_id = f"exec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                self.executions[execution_id] = {
                    'result': result,
                    'decision': decision_data,
                    'timestamp': datetime.now()
                }
                
                # POST CONFIRMATION with cancel option
                confirmation = await message.reply(
                    f"✅ **EXECUTED** (ID: `{execution_id}`)\n"
                    f"{self._format_result(result)}\n\n"
                    f"🚨 React with ❌ within {settings.SAFETY_GRACE_PERIOD_SECONDS}s to CANCEL and exit positions"
                )
                
                await confirmation.add_reaction('❌')
                
                asyncio.create_task(
                    self._handle_cancellation_window(confirmation, execution_id, result)
                )
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"Error processing AI decision: {e}", exc_info=True)
            await message.add_reaction('❌')
            await message.reply(f"**Error:** {str(e)}")
    
    def _extract_decision(self, message) -> Optional[Dict]:
        """Extract decision from message"""
        
        content = message.content
        
        # Try JSON
        try:
            json_match = re.search(r'```json\n(.*?)\n```', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            return json.loads(content)
        except:
            pass
        
        # Try text parsing
        if 'SCREENING' in content.upper() or 'BUY' in content.upper():
            return self._parse_screening_text(content)
        
        return None
    
    def _parse_screening_text(self, content: str) -> Optional[Dict]:
        """Parse 'BUY X shares TICKER' format"""
        
        candidates = []
        for line in content.split('\n'):
            match = re.search(r'BUY\s+(\d+)\s+(?:shares\s+)?([A-Z]{1,5})', line, re.IGNORECASE)
            if match:
                candidates.append({
                    'ticker': match.group(2).upper(),
                    'shares': int(match.group(1))
                })
        
        if candidates:
            return {'type': 'SCREENING', 'candidates': candidates}
        
        return None
    
    async def _execute_screening(self, decision: Dict, db) -> List[Dict]:
        """Execute trades"""
        
        results = []
        
        for candidate in decision.get('candidates', []):
            ticker = candidate['ticker']
            shares = candidate['shares']
            
            # Place order
            order = await self.tradier.place_order_async(ticker=ticker, qty=shares, side='buy')
            
            # Get entry price
            quote = await self.tradier.get_quote_async(ticker)
            entry_price = quote.get('last', 0)
            
            # Create position
            position = Position(
                account_id=settings.TRADIER_ACCOUNT_ID,
                ticker=ticker,
                shares=shares,
                avg_entry_price=entry_price,
                current_price=entry_price,
                strategy=decision.get('strategy', 'AI_SCREENING'),
                entry_time=datetime.utcnow(),
                stop_loss=entry_price * (1 - settings.STOP_LOSS_PCT),
                profit_target=entry_price * (1 + settings.PROFIT_TARGET_PCT),
                peak_price=entry_price,
                trailing_stop=entry_price * (1 - settings.TRAILING_STOP_PCT),
                is_open=True
            )
            
            db.add(position)
            db.commit()
            
            results.append({
                'ticker': ticker,
                'shares': shares,
                'entry_price': entry_price,
                'order_id': order.get('order', {}).get('id'),
                'status': order.get('order', {}).get('status')
            })
        
        return results
    
    def _format_result(self, result: List[Dict]) -> str:
        """Format for Discord"""
        
        lines = []
        for r in result:
            lines.append(
                f"• **{r['ticker']}**: {r['shares']} shares @ ${r['entry_price']:.2f} "
                f"(Order #{r['order_id']}, {r['status']})"
            )
        return '\n'.join(lines)
    
    async def _handle_cancellation_window(self, message, execution_id: str, result: List[Dict]):
        """30-second cancel window"""
        
        def check_cancel(reaction, user):
            return (
                str(reaction.emoji) == '❌' and
                reaction.message.id == message.id and
                user.id == settings.DISCORD_USER_ID
            )
        
        try:
            reaction, user = await self.wait_for(
                'reaction_add',
                timeout=float(settings.SAFETY_GRACE_PERIOD_SECONDS),
                check=check_cancel
            )
            
            # CANCEL
            await message.edit(
                content=f"{message.content}\n\n🚨 **CANCELING - Exiting positions...**"
            )
            
            cancel_result = await self._cancel_execution(result)
            
            await message.edit(
                content=f"{message.content}\n\n❌ **CANCELED**\n{cancel_result}"
            )
            
            if execution_id in self.executions:
                del self.executions[execution_id]
                
        except asyncio.TimeoutError:
            await message.edit(
                content=f"{message.content}\n\n🔒 **Trade finalized** ({settings.SAFETY_GRACE_PERIOD_SECONDS}s expired)"
            )
    
    async def _cancel_execution(self, result: List[Dict]) -> str:
        """Exit positions"""
        
        cancel_results = []
        
        for trade in result:
            ticker = trade['ticker']
            shares = trade['shares']
            
            exit_order = await self.tradier.place_order_async(ticker=ticker, qty=shares, side='sell')
            
            cancel_results.append(
                f"• **{ticker}**: Sold {shares} shares (Order #{exit_order.get('order', {}).get('id')})"
            )
        
        return '\n'.join(cancel_results)
    
    async def _handle_override(self, reply_message, original_message, decision_data):
        """Handle override"""
        
        def check_override(reaction, user):
            return (
                str(reaction.emoji) == '🔓' and
                reaction.message.id == reply_message.id and
                user.id == settings.DISCORD_USER_ID
            )
        
        try:
            await self.wait_for('reaction_add', timeout=60.0, check=check_override)
            
            await original_message.add_reaction('🔓')
            await reply_message.edit(
                content=f"{reply_message.content}\n\n🔓 **Override activated - Executing...**"
            )
            
            db = SessionLocal()
            try:
                result = await self._execute_screening(decision_data, db)
                await reply_message.edit(
                    content=f"{reply_message.content}\n\n✅ **EXECUTED (overridden)**\n{self._format_result(result)}"
                )
            finally:
                db.close()
                
        except asyncio.TimeoutError:
            pass
    
    @tasks.loop(time=time(hour=16, minute=30))
    async def daily_summary(self):
        """Daily summary at 4:30 PM"""
        
        channel = self.get_channel(self.trading_channel_id)
        if not channel:
            return
        
        await channel.send("📊 **DAILY SUMMARY** - End of day report")


async def start_discord_bot():
    """Start bot"""
    bot = TradingBot()
    await bot.start(settings.DISCORD_BOT_TOKEN)
