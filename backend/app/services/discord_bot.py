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
from app.services.position_sizer import position_sizer
from app.services.crypto_analyzer import crypto_analyzer
from app.core.database import SessionLocal
from app.models.position import Position
from app.models.trade import Trade

logger = logging.getLogger(__name__)


class TradingBot(commands.Bot):
    """Discord bot for trading operations with global position sizing"""
    
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
        """Process incoming messages from webhooks or authorized users"""
        
        # Ignore own messages
        if message.author == self.user:
            return
        
        # Only process in trading channel
        if message.channel.id != self.trading_channel_id:
            return
        
        # Route based on source
        if message.webhook_id:
            # Message from Claude/ChatGPT via webhook
            await self.process_ai_decision(message)
        elif message.author.id == settings.DISCORD_USER_ID:
            # Message directly from authorized user
            await self.process_user_command(message)
        else:
            # Message from someone else - ignore
            logger.info(f"Ignored message from unauthorized user: {message.author.id}")
        
        # Process bot commands (optional)
        await self.process_commands(message)

    async def process_user_command(self, message):
        """Process manual commands from authorized user"""
        
        try:
            decision_data = self._extract_decision(message)
            
            if not decision_data:
                return  # Not a trade command
            
            # Acknowledge
            await message.add_reaction('👀')
            
            # Route to appropriate handler based on decision type
            decision_type = decision_data.get('type', 'SCREENING').upper()
            
            if 'CRYPTO' in decision_type:
                await self._process_crypto_decision(message, decision_data)
            else:
                await self._process_stock_decision(message, decision_data)
                
        except Exception as e:
            logger.error(f"Error processing user command: {e}", exc_info=True)
            await message.add_reaction('❌')
            await message.reply(f"**Error:** {str(e)}")
    
    async def process_ai_decision(self, message):
        """Process AI decision from webhook"""
        
        try:
            decision_data = self._extract_decision(message)
            
            if not decision_data:
                logger.warning(f"Could not extract decision from: {message.content[:100]}")
                return
            
            # Acknowledge
            await message.add_reaction('👀')
            
            # Route to appropriate handler based on decision type
            decision_type = decision_data.get('type', 'SCREENING').upper()
            
            if 'CRYPTO' in decision_type:
                await self._process_crypto_decision(message, decision_data)
            else:
                await self._process_stock_decision(message, decision_data)
                
        except Exception as e:
            logger.error(f"Error processing AI decision: {e}", exc_info=True)
            await message.add_reaction('❌')
            await message.reply(f"**Error:** {str(e)}")
    
    async def _process_stock_decision(self, message, decision_data: Dict):
        """Process stock screening with automatic position sizing"""
        
        db = SessionLocal()
        try:
            # Validate candidate count
            candidates = decision_data.get('candidates', [])
            valid, reason = position_sizer.validate_candidate_count(candidates)
            if not valid:
                await message.add_reaction('⛔')
                await message.reply(f"❌ {reason}")
                return
            
            # Get account equity
            account = self.tradier.get_account_sync()
            equity = account.get('equity', 0)
            
            # Calculate positions with global sizing
            positions = position_sizer.calculate_stock_positions(candidates, equity)
            
            if not positions:
                await message.add_reaction('⛔')
                await message.reply("❌ No valid positions after safety checks")
                return
            
            # Safety validation
            # Note: We'll pass the original decision for safety checks
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
                        self._handle_override(reply, message, decision_data, positions, 'stock')
                    )
                return
            
            # Execute trades
            await message.add_reaction('⚡')
            result = await self._execute_stock_positions(positions, db)
            
            if not result:
                await message.reply("❌ No trades executed")
                return
            
            # Store execution
            execution_id = f"exec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            self.executions[execution_id] = {
                'result': result,
                'decision': decision_data,
                'timestamp': datetime.now(),
                'type': 'stock'
            }
            
            # Post confirmation
            confirmation = await message.reply(
                f"✅ **STOCK TRADES EXECUTED** (ID: `{execution_id}`)\n"
                f"{self._format_stock_result(result)}\n\n"
                f"🚨 React with ❌ within {settings.SAFETY_GRACE_PERIOD_SECONDS}s to CANCEL"
            )
            
            await confirmation.add_reaction('❌')
            
            asyncio.create_task(
                self._handle_cancellation_window(confirmation, execution_id, result, 'stock')
            )
            
        finally:
            db.close()
    
    async def _process_crypto_decision(self, message, decision_data: Dict):
        """Process crypto screening with automatic position sizing"""
        
        try:
            # Import crypto services
            from app.services.kraken_service import crypto_ledger, kraken_service, TOP_15_PAIRS
            
            # Validate candidate count
            candidates = decision_data.get('candidates', [])
            valid, reason = position_sizer.validate_candidate_count(candidates)
            if not valid:
                await message.add_reaction('⛔')
                await message.reply(f"❌ {reason}")
                return
            
            # Get current balance
            ledger = crypto_ledger.get_ledger()
            balance = ledger['balance']
            
            # Calculate positions with global sizing
            positions = position_sizer.calculate_crypto_positions(candidates, balance)
            
            if not positions:
                await message.add_reaction('⛔')
                await message.reply("❌ No valid positions after safety checks")
                return
            
            # Execute trades
            await message.add_reaction('⚡')
            result = await self._execute_crypto_positions(positions)
            
            if not result:
                await message.reply("❌ No trades executed")
                return
            
            # Store execution
            execution_id = f"exec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            self.executions[execution_id] = {
                'result': result,
                'decision': decision_data,
                'timestamp': datetime.now(),
                'type': 'crypto'
            }
            
            # Get updated balance
            new_ledger = crypto_ledger.get_ledger()
            new_balance = new_ledger['balance']
            
            # Post confirmation
            confirmation = await message.reply(
                f"✅ **CRYPTO TRADES EXECUTED** (Paper) (ID: `{execution_id}`)\n"
                f"{self._format_crypto_result(result)}\n\n"
                f"Remaining balance: ${new_balance:,.2f}\n"
                f"🚨 React with ❌ within {settings.SAFETY_GRACE_PERIOD_SECONDS}s to CANCEL"
            )
            
            await confirmation.add_reaction('❌')
            
            asyncio.create_task(
                self._handle_cancellation_window(confirmation, execution_id, result, 'crypto')
            )
            
        except Exception as e:
            logger.error(f"Error processing crypto decision: {e}", exc_info=True)
            await message.add_reaction('❌')
            await message.reply(f"**Crypto Error:** {str(e)}")
    
    async def _execute_stock_positions(self, positions: List[Dict], db) -> List[Dict]:
        """Execute stock trades with calculated position sizes"""
        
        results = []
        
        for pos in positions:
            ticker = pos['ticker']
            
            # Get current price if not already fetched
            if pos['shares'] is None:
                quote = await self.tradier.get_quote_async(ticker)
                current_price = quote.get('last', 0)
                
                if current_price == 0:
                    logger.error(f"{ticker}: Could not get price, skipping")
                    continue
                
                # Calculate shares
                shares = int(pos['estimated_value'] / current_price)
            else:
                shares = pos['shares']
                quote = await self.tradier.get_quote_async(ticker)
                current_price = quote.get('last', 0)
            
            if shares == 0:
                logger.warning(f"{ticker}: Calculated 0 shares, skipping")
                continue
            
            # Place order
            order = await self.tradier.place_order_async(
                ticker=ticker,
                qty=shares,
                side='buy',
                order_type='market'
            )
            
            # Create position record
            position = Position(
                account_id=settings.TRADIER_ACCOUNT_ID or settings.TRADIER_PAPER_ACCOUNT_ID,
                ticker=ticker,
                shares=shares,
                avg_entry_price=current_price,
                current_price=current_price,
                strategy='AI_SCREENING',
                entry_time=datetime.utcnow(),
                stop_loss=current_price * (1 - settings.STOP_LOSS_PCT),
                profit_target=current_price * (1 + settings.PROFIT_TARGET_PCT),
                peak_price=current_price,
                trailing_stop=current_price * (1 - settings.TRAILING_STOP_PCT),
                is_open=True
            )
            
            db.add(position)
            db.commit()
            
            results.append({
                'ticker': ticker,
                'shares': shares,
                'entry_price': current_price,
                'value': shares * current_price,
                'position_pct': pos.get('position_pct'),
                'order_id': order.get('order', {}).get('id'),
                'status': order.get('order', {}).get('status', 'filled')
            })
        
        return results
    
    async def _execute_crypto_positions(self, positions: List[Dict]) -> List[Dict]:
        """Execute crypto paper trades with calculated position sizes"""
        
        from app.services.kraken_service import crypto_ledger, kraken_service, TOP_15_PAIRS
        
        results = []
        
        for pos in positions:
            pair = pos['pair']
            
            # Get OHLCV pair name
            ohlcv_pair = TOP_15_PAIRS.get(pair)
            if not ohlcv_pair:
                logger.error(f"Unknown pair: {pair}")
                continue
            
            # Get current price if not already calculated
            if pos['amount'] is None:
                ticker = kraken_service.get_ticker(ohlcv_pair)
                if not ticker or 'c' not in ticker:
                    logger.error(f"{pair}: Could not get price, skipping")
                    continue
                
                current_price = float(ticker['c'][0])
                crypto_amount = pos['estimated_value'] / current_price
            else:
                crypto_amount = pos['amount']
                ticker = kraken_service.get_ticker(ohlcv_pair)
                current_price = float(ticker['c'][0]) if ticker and 'c' in ticker else 0
            
            # Execute paper trade
            trade = crypto_ledger.execute_trade(
                pair=pair,
                ohlcv_pair=ohlcv_pair,
                side='BUY',
                amount=crypto_amount,
                price=current_price if current_price > 0 else None
            )
            
            if trade.get('status') == 'FILLED':
                results.append({
                    'pair': pair,
                    'amount': crypto_amount,
                    'price': trade.get('price', current_price),
                    'value': trade.get('total', pos['estimated_value']),
                    'position_pct': pos.get('position_pct'),
                    'trade_id': trade.get('id')
                })
        
        return results
    
    def _extract_decision(self, message) -> Optional[Dict]:
        """Extract decision from message"""
        
        content = message.content
        
        # Try JSON
        try:
            # Try JSON code block
            json_match = re.search(r'```json\n(.*?)\n```', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            
            # Try plain JSON
            return json.loads(content)
        except:
            pass
        
        # Try text parsing for stocks
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
    
    def _format_stock_result(self, result: List[Dict]) -> str:
        """Format stock results for Discord"""
        
        lines = []
        total_value = 0
        
        for r in result:
            pct_str = f", {r['position_pct']*100:.0f}%" if r.get('position_pct') else ""
            lines.append(
                f"• **{r['ticker']}**: {r['shares']} shares @ ${r['entry_price']:.2f} "
                f"(${r['value']:,.2f}{pct_str}) "
                f"[Order #{r['order_id']}]"
            )
            total_value += r['value']
        
        summary = '\n'.join(lines)
        if total_value > 0:
            summary += f"\n\n**Total:** ${total_value:,.2f}"
        
        return summary
    
    def _format_crypto_result(self, result: List[Dict]) -> str:
        """Format crypto results for Discord"""
        
        lines = []
        total_value = 0
        
        for r in result:
            pct_str = f", {r['position_pct']*100:.0f}%" if r.get('position_pct') else ""
            lines.append(
                f"• **{r['pair']}**: {r['amount']:.4f} @ ${r['price']:.2f} "
                f"(${r['value']:,.2f}{pct_str})"
            )
            total_value += r['value']
        
        summary = '\n'.join(lines)
        if total_value > 0:
            summary += f"\n\n**Total:** ${total_value:,.2f}"
        
        return summary
    
    async def _handle_cancellation_window(
        self,
        message,
        execution_id: str,
        result: List[Dict],
        trade_type: str = 'stock'
    ):
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
            
            if trade_type == 'stock':
                cancel_result = await self._cancel_stock_execution(result)
            else:  # crypto
                cancel_result = await self._cancel_crypto_execution(result)
            
            await message.edit(
                content=f"{message.content}\n\n❌ **CANCELED**\n{cancel_result}"
            )
            
            if execution_id in self.executions:
                del self.executions[execution_id]
                
        except asyncio.TimeoutError:
            await message.edit(
                content=f"{message.content}\n\n🔒 **Trade finalized** ({settings.SAFETY_GRACE_PERIOD_SECONDS}s expired)"
            )
    
    async def _cancel_stock_execution(self, result: List[Dict]) -> str:
        """Exit stock positions"""
        
        cancel_results = []
        
        for trade in result:
            ticker = trade['ticker']
            shares = trade['shares']
            
            exit_order = await self.tradier.place_order_async(
                ticker=ticker,
                qty=shares,
                side='sell',
                order_type='market'
            )
            
            cancel_results.append(
                f"• **{ticker}**: Sold {shares} shares [Order #{exit_order.get('order', {}).get('id')}]"
            )
        
        return '\n'.join(cancel_results)
    
    async def _cancel_crypto_execution(self, result: List[Dict]) -> str:
        """Exit crypto positions"""
        
        from app.services.kraken_service import crypto_ledger, TOP_15_PAIRS
        
        cancel_results = []
        
        for trade in result:
            pair = trade['pair']
            amount = trade['amount']
            ohlcv_pair = TOP_15_PAIRS.get(pair)
            
            # Execute sell
            exit_trade = crypto_ledger.execute_trade(
                pair=pair,
                ohlcv_pair=ohlcv_pair,
                side='SELL',
                amount=amount
            )
            
            cancel_results.append(
                f"• **{pair}**: Sold {amount:.4f} @ ${exit_trade.get('price', 0):.2f}"
            )
        
        return '\n'.join(cancel_results)
    
    async def _handle_override(
        self,
        reply_message,
        original_message,
        decision_data,
        positions: List[Dict],
        trade_type: str = 'stock'
    ):
        """Handle safety override"""
        
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
                if trade_type == 'stock':
                    result = await self._execute_stock_positions(positions, db)
                    formatted = self._format_stock_result(result)
                else:
                    result = await self._execute_crypto_positions(positions)
                    formatted = self._format_crypto_result(result)
                
                await reply_message.edit(
                    content=f"{reply_message.content}\n\n✅ **EXECUTED (overridden)**\n{formatted}"
                )
            finally:
                db.close()
                
        except asyncio.TimeoutError:
            pass
            
    @commands.command(name='screen')
    async def crypto_screen(self, ctx):
        """
        Manual crypto momentum screening command
        Usage: !screen
        """
        # Only authorized user
        if ctx.author.id != settings.DISCORD_USER_ID:
            await ctx.send("❌ Unauthorized")
            return
        
        # Only in trading channel
        if ctx.channel.id != self.trading_channel_id:
            return
        
        await ctx.send("🔍 **Screening Kraken top 15 pairs for momentum...**")
        
        try:
            # Screen with default criteria
            results = crypto_analyzer.screen_for_momentum(
                min_change_24h=5.0,      # >5% gain
                min_volume_ratio=1.5,    # 1.5x average volume
                rsi_min=50,              # RSI between 50-70
                rsi_max=70
            )
            
            # Format summary
            summary = crypto_analyzer.get_screening_summary(results)
            await ctx.send(summary)
            
            # If signals found, generate JSON
            if results:
                candidates = [{"pair": r['pair']} for r in results[:3]]  # Top 3
                
                decision_json = {
                    "type": "CRYPTO_SCREENING",
                    "candidates": candidates,
                    "reasoning": f"Found {len(results)} momentum signals with RSI 50-70, volume spike, >5% gain"
                }
                
                await ctx.send(
                    f"**📋 Recommended Trades:**\n```json\n{json.dumps(decision_json, indent=2)}\n```\n\n"
                    f"💡 Copy the JSON above and paste it as a new message to execute."
                )
            else:
                await ctx.send(
                    "ℹ️ No pairs currently meet momentum criteria.\n\n"
                    "**Criteria:**\n"
                    "• 24h gain: >5%\n"
                    "• Volume: >1.5x average\n"
                    "• RSI: 50-70"
                )
        
        except Exception as e:
            logger.error(f"Screen command error: {e}", exc_info=True)
            await ctx.send(f"❌ **Error:** {str(e)}")
    
    @commands.command(name='analyze')
    async def crypto_analyze(self, ctx, pair: str = None):
        """
        Analyze a specific crypto pair
        Usage: !analyze SOL/USD
        """
        # Only authorized user
        if ctx.author.id != settings.DISCORD_USER_ID:
            await ctx.send("❌ Unauthorized")
            return
        
        # Only in trading channel
        if ctx.channel.id != self.trading_channel_id:
            return
        
        if not pair:
            await ctx.send("❌ **Usage:** `!analyze <PAIR>`\n**Example:** `!analyze SOL/USD`")
            return
        
        # Normalize pair format
        pair = pair.upper().replace('-', '/')
        
        await ctx.send(f"🔍 **Analyzing {pair}...**")
        
        try:
            # Get full analysis
            analysis = crypto_analyzer.analyze_pair(pair)
            
            if analysis['price'] == 0:
                await ctx.send(f"❌ Could not fetch data for {pair}")
                return
            
            # Format response
            emoji = "🟢" if analysis.get('change_24h', 0) > 0 else "🔴"
            
            response = f"""
{emoji} **{pair} Analysis**

**Price:** ${analysis['price']:,.2f}
**24h Change:** {analysis.get('change_24h', 0):+.2f}%
**RSI (14):** {analysis.get('rsi', 0):.1f}
**Volume Ratio:** {analysis.get('volume_ratio', 0):.1f}x average

**Signals:**
- Volume Spike: {'✅ Yes' if analysis.get('volume_spike') else '❌ No'}
- RSI Momentum: {'✅ Yes (50-70)' if analysis.get('rsi_momentum') else '❌ No'}
- Bullish 24h: {'✅ Yes (>5%)' if analysis.get('bullish_24h') else '❌ No'}
            """.strip()
            
            await ctx.send(response)
            
            # If all signals are positive, suggest trade
            if all([
                analysis.get('volume_spike'),
                analysis.get('rsi_momentum'),
                analysis.get('bullish_24h')
            ]):
                decision_json = {
                    "type": "CRYPTO_SCREENING",
                    "candidates": [{"pair": pair}],
                    "reasoning": f"Strong momentum: RSI {analysis['rsi']:.1f}, Volume {analysis['volume_ratio']:.1f}x, +{analysis['change_24h']:.1f}%"
                }
                
                await ctx.send(
                    f"✅ **Strong momentum detected!**\n\n"
                    f"```json\n{json.dumps(decision_json, indent=2)}\n```"
                )
        
        except Exception as e:
            logger.error(f"Analyze command error: {e}", exc_info=True)
            await ctx.send(f"❌ **Error:** {str(e)}")
    
    @commands.command(name='help')
    async def show_help(self, ctx):
        """Show available commands"""
        if ctx.author.id != settings.DISCORD_USER_ID:
            return
        
        help_text = """
**🤖 Trading Bot Commands**

**Crypto Screening:**
- `!screen` - Screen all Kraken pairs for momentum
- `!analyze <PAIR>` - Analyze a specific pair (e.g., !analyze SOL/USD)

**Manual Trading:**
Just paste JSON in this channel:
```json
{
  "type": "SCREENING",
  "candidates": [{"ticker": "AAPL"}]
}
```

Or for crypto:
```json
{
  "type": "CRYPTO_SCREENING",
  "candidates": [{"pair": "SOL/USD"}]
}
```

**Position Sizing:**
Automatic - configured in .env
Current: {settings.POSITION_SIZE_PCT*100:.0f}% per position

**Need help?** Check the bot logs or documentation.
        """.strip()
        
        await ctx.send(help_text)
    
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
