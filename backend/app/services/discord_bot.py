"""Discord bot for trading notifications and control."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, time
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks

from app.core.config import settings
from app.core.database import SessionLocal
from app.services.control_plane import discord_decision_guard, get_control_plane_status, get_execution_gate_status
from app.services.execution_lifecycle import execution_lifecycle
from app.services.crypto_analyzer import crypto_analyzer
from app.services.position_sizer import position_sizer
from app.services.runtime_state import runtime_state
from app.services.safety_validator import SafetyValidator
from app.services.trade_validator import trade_validator
from app.services.tradier_client import TradierClient, tradier_client

logger = logging.getLogger(__name__)


class TradingBot(commands.Bot):
    """Discord bot for trading operations with global position sizing."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(command_prefix="!", intents=intents)

        self.trading_channel_id = settings.DISCORD_TRADING_CHANNEL_ID
        self.tradier = TradierClient()
        self.safety = SafetyValidator()
        self.executions: dict[str, list[dict]] = {}

    async def on_ready(self):
        logger.info('Discord bot connected as %s', self.user)
        channel = self.get_channel(self.trading_channel_id)
        if channel:
            control_plane = get_control_plane_status()
            if control_plane['state'] == 'ARMED':
                await channel.send('🤖 **Trading bot online and armed for decisions**')
            else:
                await channel.send(
                    f"🤖 **Trading bot online in {control_plane['state']} mode**\n"
                    f"Execution blocked: {control_plane['reason']}"
                )

        if settings.APP_ENV == 'production' and not self.daily_summary.is_running():
            self.daily_summary.start()

    async def on_message(self, message):
        if message.author == self.user:
            return
        if message.channel.id != self.trading_channel_id:
            return

        await self.process_commands(message)

        content = message.content.strip()
        if '{' not in content or '}' not in content:
            return

        authorization = discord_decision_guard.authorize_message(message)
        if not authorization.authorized:
            logger.warning('Rejected Discord message before parsing: %s', authorization.reason)
            await message.add_reaction('⛔')
            await message.reply(f'Unauthorized decision message: {authorization.reason}')
            return

        gate = get_execution_gate_status()
        if not gate.allowed:
            await message.add_reaction('🛑')
            await message.reply(f'Execution blocked ({gate.state}): {gate.reason}')
            return

        try:
            json_start = content.index('{')
            json_end = content.rindex('}') + 1
            decision = json.loads(content[json_start:json_end])

            if not isinstance(decision, dict):
                await message.add_reaction('❌')
                await message.reply('Invalid format - must be a JSON object')
                return

            accepted, reason = discord_decision_guard.validate_and_register(message, decision)
            if not accepted:
                await message.add_reaction('⛔')
                await message.reply(reason)
                return

            decision_type = str(decision.get('type', '')).upper()

            if decision_type == 'SCREENING':
                logger.info('Processing SCREENING from %s', message.author)
                await message.add_reaction('👀')
                await self._process_stock_decision(message, decision)
            elif decision_type == 'CRYPTO_SCREENING':
                logger.info('Processing CRYPTO_SCREENING from %s', message.author)
                await message.add_reaction('👀')
                await self._process_crypto_decision(message, decision)
            else:
                await message.add_reaction('❓')
                await message.reply(
                    f"Unknown decision type: `{decision_type}`\n"
                    'Supported types: `SCREENING` (stocks), `CRYPTO_SCREENING` (crypto)'
                )
        except json.JSONDecodeError as exc:
            await message.add_reaction('❌')
            await message.reply(f'Invalid JSON format: {exc}')
        except Exception as exc:
            logger.error('Error processing message: %s', exc, exc_info=True)
            await message.add_reaction('❌')
            await message.reply(f'Error: {exc}')

    async def process_user_command(self, message):
        try:
            authorization = discord_decision_guard.authorize_message(message)
            if not authorization.authorized:
                await message.add_reaction('⛔')
                await message.reply(f'Unauthorized command message: {authorization.reason}')
                return

            gate = get_execution_gate_status()
            if not gate.allowed:
                await message.add_reaction('🛑')
                await message.reply(f'Execution blocked ({gate.state}): {gate.reason}')
                return

            decision_data = self._extract_decision(message)
            if not decision_data:
                return

            await message.add_reaction('👀')
            decision_type = decision_data.get('type', 'SCREENING').upper()

            if 'CRYPTO' in decision_type:
                await self._process_crypto_decision(message, decision_data)
            else:
                await self._process_stock_decision(message, decision_data)
        except Exception as exc:
            logger.error('Error processing user command: %s', exc, exc_info=True)
            await message.add_reaction('❌')
            await message.reply(f'**Error:** {str(exc)}')

    async def process_ai_decision(self, message):
        try:
            authorization = discord_decision_guard.authorize_message(message)
            if not authorization.authorized:
                await message.add_reaction('⛔')
                await message.reply(f'Unauthorized AI decision message: {authorization.reason}')
                return

            gate = get_execution_gate_status()
            if not gate.allowed:
                await message.add_reaction('🛑')
                await message.reply(f'Execution blocked ({gate.state}): {gate.reason}')
                return

            decision_data = self._extract_decision(message)
            if not decision_data:
                logger.warning('Could not extract decision from: %s', message.content[:100])
                return

            await message.add_reaction('👀')
            decision_type = decision_data.get('type', 'SCREENING').upper()

            if 'CRYPTO' in decision_type:
                await self._process_crypto_decision(message, decision_data)
            else:
                await self._process_stock_decision(message, decision_data)
        except Exception as exc:
            logger.error('Error processing AI decision: %s', exc, exc_info=True)
            await message.add_reaction('❌')
            await message.reply(f'**Error:** {str(exc)}')

    async def _process_stock_decision(self, message, decision_data: Dict):
        try:
            gate = get_execution_gate_status()
            if not gate.allowed:
                await message.add_reaction('🛑')
                await message.reply(f'Execution blocked ({gate.state}): {gate.reason}')
                return

            candidates = decision_data.get('candidates', [])
            valid, reason = position_sizer.validate_candidate_count(candidates)
            if not valid:
                await message.add_reaction('⛔')
                await message.reply(f'❌ {reason}')
                return

            mode = runtime_state.get().stock_mode
            account = tradier_client.get_account_snapshot(mode)
            cash_available = float(
                account.get('cash') or account.get('buyingPower') or account.get('portfolioValue') or 0
            )

            if cash_available <= 0:
                await message.add_reaction('⛔')
                await message.reply('❌ No stock cash available for position sizing')
                return

            prices = await self._get_stock_prices(candidates, mode)
            positions = position_sizer.calculate_stock_positions(candidates, cash_available, prices=prices)
            positions = [position for position in positions if int(position.get('shares') or 0) > 0]

            if not positions:
                await message.add_reaction('⛔')
                await message.reply('❌ No valid positions after safety checks')
                return

            validation_results = []
            valid_positions = []

            for pos in positions:
                ticker = pos['ticker']
                shares = int(pos.get('shares') or 0)
                is_valid, validation_msg = trade_validator.validate_stock_trade(ticker, shares, mode)

                validation_results.append(
                    {
                        'ticker': ticker,
                        'valid': is_valid,
                        'reason': validation_msg,
                    }
                )

                if is_valid:
                    valid_positions.append(pos)
                else:
                    logger.warning('Stock validation failed for %s: %s', ticker, validation_msg)

            if not valid_positions:
                rejection_msg = '❌ **All stock trades failed validation:**\n\n'
                for result in validation_results:
                    rejection_msg += f"• **{result['ticker']}**: {result['reason']}\n"

                await message.add_reaction('⛔')
                await message.reply(rejection_msg)
                return

            if len(valid_positions) < len(positions):
                warning_msg = '⚠️ **Some stock trades rejected by validation:**\n\n'
                for result in validation_results:
                    if not result['valid']:
                        warning_msg += f"• **{result['ticker']}**: {result['reason']}\n"

                warning_msg += f"\n**Proceeding with {len(valid_positions)} valid trade(s)**"
                await message.reply(warning_msg)

            db = SessionLocal()
            try:
                safety_candidates = []
                for pos in valid_positions:
                    ticker = str(pos.get('ticker', '')).upper()
                    shares = int(pos.get('shares') or 0)
                    estimated_value = float(pos.get('estimated_value') or (prices.get(ticker, 0.0) * shares))
                    safety_candidates.append({
                        'ticker': ticker,
                        'shares': shares,
                        'estimated_value': estimated_value,
                    })

                safety_result = await self.safety.validate(
                    {
                        'candidates': safety_candidates,
                        'vix': decision_data.get('vix', 0),
                    },
                    account,
                    db,
                    account_id=str(account.get('accountId') or settings.TRADIER_ACCOUNT_ID or 'TRADIER'),
                    asset_class='stock',
                )
                if not safety_result.get('safe'):
                    await message.add_reaction('⛔')
                    await message.reply(f"❌ Safety gate rejected stock decision: {safety_result.get('reason', 'Unknown reason')}")
                    return

                await message.add_reaction('⚡')
                result = await self._execute_stock_positions(valid_positions, mode, db)

                if not result:
                    await message.add_reaction('⛔')
                    await message.reply('❌ No stock orders were submitted')
                    return

                confirmed_fills = [trade for trade in result if int(trade.get('filled_shares') or 0) > 0]
                if not confirmed_fills:
                    await message.add_reaction('⏳')

                execution_id = f"exec_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
                self.executions[execution_id] = result

                formatted = self._format_stock_result(result)
                reply = await message.reply(
                    f"✅ **STOCK ORDER LIFECYCLE ({mode})** (ID: `{execution_id}`)\n"
                    f"{formatted}\n\n"
                    f"Cash used for sizing: ${cash_available:,.2f}\n"
                    f"React with ❌ within {settings.SAFETY_GRACE_PERIOD_SECONDS}s to unwind confirmed fills only"
                )

                await reply.add_reaction('❌')
                await self._handle_cancellation_window(reply, execution_id, result, trade_type='stock', mode=mode)
            finally:
                db.close()

        except Exception as exc:
            logger.error('Error processing stock decision: %s', exc, exc_info=True)
            await message.add_reaction('❌')
            await message.reply(f'**Stock Error:** {str(exc)}')

    async def _process_crypto_decision(self, message, decision_data: Dict):
        try:
            gate = get_execution_gate_status()
            if not gate.allowed:
                await message.add_reaction('🛑')
                await message.reply(f'Execution blocked ({gate.state}): {gate.reason}')
                return

            from app.services.kraken_service import TOP_30_PAIRS, crypto_ledger, kraken_service

            candidates = decision_data.get('candidates', [])
            valid, reason = position_sizer.validate_candidate_count(candidates)
            if not valid:
                await message.add_reaction('⛔')
                await message.reply(f'❌ {reason}')
                return

            ledger = crypto_ledger.get_ledger()
            balance = ledger['balance']

            prices = {}
            for candidate in candidates:
                pair = candidate.get('pair')
                ohlcv_pair = TOP_30_PAIRS.get(pair)
                if not pair or not ohlcv_pair:
                    continue

                ticker = kraken_service.get_ticker(ohlcv_pair)
                if ticker and 'c' in ticker:
                    prices[pair] = float(ticker['c'][0])

            positions = position_sizer.calculate_crypto_positions(candidates, balance, prices=prices)
            positions = [position for position in positions if float(position.get('amount') or 0) > 0]

            if not positions:
                await message.add_reaction('⛔')
                await message.reply('❌ No valid positions after safety checks')
                return

            validation_results = []
            valid_positions = []

            for pos in positions:
                pair = pos['pair']
                amount = float(pos.get('amount') or 0)
                is_valid, validation_msg = trade_validator.validate_crypto_trade(pair, amount)

                validation_results.append(
                    {
                        'pair': pair,
                        'valid': is_valid,
                        'reason': validation_msg,
                    }
                )

                if is_valid:
                    valid_positions.append(pos)
                else:
                    logger.warning('Validation failed for %s: %s', pair, validation_msg)

            if not valid_positions:
                rejection_msg = '❌ **All trades failed validation:**\n\n'
                for result in validation_results:
                    rejection_msg += f"• **{result['pair']}**: {result['reason']}\n"

                await message.add_reaction('⛔')
                await message.reply(rejection_msg)
                return

            if len(valid_positions) < len(positions):
                warning_msg = '⚠️ **Some trades rejected by validation:**\n\n'
                for result in validation_results:
                    if not result['valid']:
                        warning_msg += f"• **{result['pair']}**: {result['reason']}\n"

                warning_msg += f"\n**Proceeding with {len(valid_positions)} valid trade(s)**"
                await message.reply(warning_msg)

            db = SessionLocal()
            try:
                safety_candidates = []
                for pos in valid_positions:
                    pair = str(pos.get('pair', ''))
                    amount = float(pos.get('amount') or 0)
                    estimated_value = float(pos.get('estimated_value') or (prices.get(pair, 0.0) * amount))
                    safety_candidates.append({
                        'pair': pair,
                        'amount': amount,
                        'estimated_value': estimated_value,
                    })

                safety_result = await self.safety.validate(
                    {
                        'candidates': safety_candidates,
                        'vix': decision_data.get('vix', 0),
                    },
                    {
                        'cash': balance,
                        'buyingPower': balance,
                        'portfolioValue': ledger.get('equity', balance),
                    },
                    db,
                    account_id='CRYPTO_PAPER',
                    asset_class='crypto',
                )
                if not safety_result.get('safe'):
                    await message.add_reaction('⛔')
                    await message.reply(f"❌ Safety gate rejected crypto decision: {safety_result.get('reason', 'Unknown reason')}")
                    return
            finally:
                db.close()

            await message.add_reaction('⚡')
            result = await self._execute_crypto_positions(valid_positions)

            if not result:
                await message.add_reaction('⛔')
                await message.reply('❌ No crypto orders were executed')
                return

            execution_id = f"exec_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            self.executions[execution_id] = result

            formatted = self._format_crypto_result(result)
            remaining_balance = crypto_ledger.get_ledger()['balance']

            reply = await message.reply(
                f"✅ **CRYPTO TRADES EXECUTED (Paper)** (ID: `{execution_id}`)\n"
                f"{formatted}\n\n"
                f"Remaining balance: ${remaining_balance:,.2f}\n"
                f"React with ❌ within {settings.SAFETY_GRACE_PERIOD_SECONDS}s to CANCEL"
            )

            await reply.add_reaction('❌')
            await self._handle_cancellation_window(reply, execution_id, result, trade_type='crypto')

        except Exception as exc:
            logger.error('Error processing crypto decision: %s', exc, exc_info=True)
            await message.add_reaction('❌')
            await message.reply(f'**Crypto Error:** {str(exc)}')

    async def _get_stock_prices(self, candidates: List[Dict], mode: str) -> Dict[str, float]:
        tickers = [str(candidate.get('ticker', '')).upper() for candidate in candidates if candidate.get('ticker')]
        quotes = await self.tradier.get_quotes_async(tickers, mode=mode)

        prices: Dict[str, float] = {}
        for ticker, quote in quotes.items():
            try:
                last_price = float(quote.get('last') or quote.get('close') or 0)
            except (TypeError, ValueError):
                last_price = 0.0

            if last_price > 0:
                prices[ticker] = last_price

        return prices

    async def _execute_stock_positions(self, positions: List[Dict], mode: str, db) -> List[Dict]:
        results = []
        account_id = self.tradier.get_account_snapshot(mode).get('accountId') or self.tradier._credentials_for_mode(mode)['account_id']

        for pos in positions:
            gate = get_execution_gate_status()
            if not gate.allowed:
                logger.warning('Stock execution loop stopped because control plane is %s: %s', gate.state, gate.reason)
                break

            ticker = pos['ticker']
            quote = await self.tradier.get_quote_async(ticker, mode=mode)
            current_price = float(quote.get('last') or quote.get('close') or 0)
            shares = int(pos.get('shares') or 0)

            if current_price <= 0:
                logger.error('%s: Could not get price, skipping', ticker)
                continue

            if shares <= 0:
                logger.warning('%s: Calculated 0 shares, skipping', ticker)
                continue

            intent = execution_lifecycle.create_order_intent(
                db,
                account_id=str(account_id),
                asset_class='stock',
                symbol=ticker,
                side='BUY',
                requested_quantity=shares,
                requested_price=current_price,
                execution_source='DISCORD_SCREENING',
                context={
                    'mode': mode,
                    'positionPct': pos.get('position_pct'),
                    'estimatedValue': pos.get('estimated_value'),
                },
            )

            try:
                order = await self.tradier.place_order_async(
                    ticker=ticker,
                    qty=shares,
                    side='buy',
                    mode=mode,
                    order_type='market',
                )
                execution_lifecycle.record_submission(db, intent, order)
            except Exception as exc:
                execution_lifecycle.record_event(
                    db,
                    intent,
                    event_type='ORDER_SUBMISSION_FAILED',
                    status='REJECTED',
                    message=f'Order submission failed for {ticker}: {exc}',
                    payload={'error': str(exc)},
                )
                intent.status = 'REJECTED'
                intent.rejection_reason = str(exc)
                db.commit()
                db.refresh(intent)
                results.append(
                    {
                        'ticker': ticker,
                        'requested_shares': shares,
                        'filled_shares': 0,
                        'entry_price': None,
                        'value': 0.0,
                        'position_pct': pos.get('position_pct'),
                        'order_id': None,
                        'intent_id': intent.intent_id,
                        'status': intent.status,
                        'reason': str(exc),
                    }
                )
                continue

            confirmed_order = await self._confirm_stock_order(order, mode=mode)
            intent = execution_lifecycle.refresh_from_order_snapshot(db, intent, confirmed_order)
            fill_record = execution_lifecycle.materialize_stock_fill(
                db,
                intent,
                strategy='AI_SCREENING',
                stop_loss=current_price * (1 - settings.STOP_LOSS_PCT),
                profit_target=current_price * (1 + settings.PROFIT_TARGET_PCT),
                trailing_stop=current_price * (1 - settings.TRAILING_STOP_PCT),
                current_price=current_price,
            )

            filled_shares = int(fill_record['filled_shares']) if fill_record else int(round(float(intent.filled_quantity or 0.0)))
            entry_price = float(fill_record['avg_fill_price']) if fill_record else (float(intent.avg_fill_price) if intent.avg_fill_price is not None else None)
            results.append(
                {
                    'ticker': ticker,
                    'requested_shares': shares,
                    'filled_shares': filled_shares,
                    'shares': filled_shares,
                    'entry_price': entry_price,
                    'value': (filled_shares * entry_price) if entry_price is not None and filled_shares > 0 else 0.0,
                    'position_pct': pos.get('position_pct'),
                    'order_id': intent.submitted_order_id,
                    'intent_id': intent.intent_id,
                    'status': intent.status,
                    'reason': intent.rejection_reason,
                }
            )

        return results

    async def _confirm_stock_order(self, order_snapshot: Dict, mode: str) -> Dict:
        snapshot = order_snapshot
        normalized = self.tradier.normalize_order_response(snapshot)
        order_id = normalized.get('id')

        if normalized.get('is_terminal') or normalized.get('filled_quantity', 0) > 0 or not order_id:
            return snapshot

        attempts = max(int(settings.ORDER_FILL_CONFIRM_RETRIES), 0)
        for _ in range(attempts):
            await asyncio.sleep(float(settings.ORDER_FILL_CONFIRM_DELAY_SECONDS))
            snapshot = await self.tradier.get_order_async(str(order_id), mode=mode)
            normalized = self.tradier.normalize_order_response(snapshot)
            if normalized.get('is_terminal') or normalized.get('filled_quantity', 0) > 0:
                break

        return snapshot

    async def _execute_crypto_positions(self, positions: List[Dict]) -> List[Dict]:
        from app.services.kraken_service import TOP_30_PAIRS, crypto_ledger, kraken_service

        results = []

        for pos in positions:
            gate = get_execution_gate_status()
            if not gate.allowed:
                logger.warning('Crypto execution loop stopped because control plane is %s: %s', gate.state, gate.reason)
                break

            pair = pos['pair']
            ohlcv_pair = TOP_30_PAIRS.get(pair)
            if not ohlcv_pair:
                logger.error('Unknown pair: %s', pair)
                continue

            if pos.get('amount') is None:
                ticker = kraken_service.get_ticker(ohlcv_pair)
                if not ticker or 'c' not in ticker:
                    logger.error('%s: Could not get price, skipping', pair)
                    continue

                current_price = float(ticker['c'][0])
                crypto_amount = pos['estimated_value'] / current_price
            else:
                crypto_amount = float(pos['amount'])
                ticker = kraken_service.get_ticker(ohlcv_pair)
                current_price = float(ticker['c'][0]) if ticker and 'c' in ticker else 0.0

            trade = crypto_ledger.execute_trade(
                pair=pair,
                ohlcv_pair=ohlcv_pair,
                side='BUY',
                amount=crypto_amount,
                price=current_price if current_price > 0 else None,
            )

            if trade.get('status') == 'FILLED':
                results.append(
                    {
                        'pair': pair,
                        'amount': crypto_amount,
                        'price': float(trade.get('price', current_price) or 0),
                        'value': float(trade.get('total', pos.get('estimated_value') or 0) or 0),
                        'position_pct': pos.get('position_pct'),
                        'trade_id': trade.get('id'),
                    }
                )

        return results

    def _extract_decision(self, message) -> Optional[Dict]:
        content = message.content

        try:
            json_match = re.search(r'```json\n(.*?)\n```', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            return json.loads(content)
        except Exception:
            pass

        if 'SCREENING' in content.upper() or 'BUY' in content.upper():
            return self._parse_screening_text(content)

        return None

    def _parse_screening_text(self, content: str) -> Optional[Dict]:
        candidates = []
        for line in content.split('\n'):
            match = re.search(r'BUY\s+(\d+)\s+(?:shares\s+)?([A-Z]{1,5})', line, re.IGNORECASE)
            if match:
                candidates.append({'ticker': match.group(2).upper(), 'shares': int(match.group(1))})

        if candidates:
            return {'type': 'SCREENING', 'candidates': candidates}
        return None

    def _format_stock_result(self, result: List[Dict]) -> str:
        lines = []
        total_value = 0.0

        for trade in result:
            pct_str = f", {trade['position_pct'] * 100:.0f}%" if trade.get('position_pct') else ''
            requested = int(trade.get('requested_shares') or trade.get('shares') or 0)
            filled = int(trade.get('filled_shares') or 0)
            status = str(trade.get('status') or 'UNKNOWN').upper()
            order_id = trade.get('order_id') or 'n/a'
            intent_id = trade.get('intent_id') or 'n/a'
            reason = trade.get('reason')

            if filled > 0 and trade.get('entry_price') is not None:
                lines.append(
                    f"• **{trade['ticker']}**: {filled}/{requested} filled @ ${float(trade['entry_price']):.2f} "
                    f"(${float(trade.get('value') or 0):,.2f}{pct_str}) [{status}] "
                    f"[Intent `{intent_id}` / Order #{order_id}]"
                )
                total_value += float(trade.get('value') or 0)
            else:
                pending_reason = f" - {reason}" if reason else ''
                lines.append(
                    f"• **{trade['ticker']}**: 0/{requested} filled [{status}] "
                    f"[Intent `{intent_id}` / Order #{order_id}]{pending_reason}"
                )

        summary = '\n'.join(lines)
        if total_value > 0:
            summary += f'\n\n**Confirmed fill value:** ${total_value:,.2f}'
        return summary

    def _format_crypto_result(self, result: List[Dict]) -> str:
        lines = []
        total_value = 0.0

        for trade in result:
            pct_str = f", {trade['position_pct'] * 100:.0f}%" if trade.get('position_pct') else ''
            lines.append(
                f"• **{trade['pair']}**: {trade['amount']:.4f} @ ${trade['price']:.2f} "
                f"(${trade['value']:,.2f}{pct_str})"
            )
            total_value += trade['value']

        summary = '\n'.join(lines)
        if total_value > 0:
            summary += f'\n\n**Total:** ${total_value:,.2f}'
        return summary

    async def _handle_cancellation_window(
        self,
        message,
        execution_id: str,
        result: List[Dict],
        trade_type: str = 'stock',
        mode: str = 'PAPER',
    ):
        def check_cancel(reaction, user):
            return (
                str(reaction.emoji) == '❌'
                and reaction.message.id == message.id
                and user.id == settings.DISCORD_USER_ID
            )

        try:
            await self.wait_for(
                'reaction_add',
                timeout=float(settings.SAFETY_GRACE_PERIOD_SECONDS),
                check=check_cancel,
            )

            await message.edit(content=f'{message.content}\n\n🚨 **CANCELING - Exiting positions...**')

            if trade_type == 'stock':
                cancel_result = await self._cancel_stock_execution(result, mode)
            else:
                cancel_result = await self._cancel_crypto_execution(result)

            await message.edit(content=f'{message.content}\n\n❌ **CANCELED**\n{cancel_result}')
            self.executions.pop(execution_id, None)

        except asyncio.TimeoutError:
            await message.edit(
                content=(
                    f'{message.content}\n\n'
                    f'🔒 **Trade finalized** ({settings.SAFETY_GRACE_PERIOD_SECONDS}s expired)'
                )
            )

    async def _cancel_stock_execution(self, result: List[Dict], mode: str = 'PAPER') -> str:
        from app.models.order_intent import OrderIntent

        cancel_results = []
        db = SessionLocal()

        try:
            for trade in result:
                ticker = str(trade.get('ticker') or '').upper()
                intent_id = trade.get('intent_id')
                entry_intent = None
                if intent_id:
                    entry_intent = db.query(OrderIntent).filter(OrderIntent.intent_id == intent_id).first()

                try:
                    broker_shares = self.tradier.get_position_quantity_sync(ticker, mode=mode)
                except Exception as exc:
                    logger.warning('Could not fetch broker position quantity for %s: %s', ticker, exc)
                    broker_shares = 0

                fallback_shares = int(
                    round(
                        float(
                            (entry_intent.filled_quantity if entry_intent else 0)
                            or trade.get('filled_shares')
                            or trade.get('shares')
                            or 0
                        )
                    )
                )
                shares = broker_shares or fallback_shares

                if shares <= 0:
                    if entry_intent is not None:
                        execution_lifecycle.record_event(
                            db,
                            entry_intent,
                            event_type='EXIT_SKIPPED_NO_OPEN_SHARES',
                            status=entry_intent.status,
                            message=f'No broker-confirmed open shares remained for {ticker}',
                            payload={'brokerOpenShares': broker_shares, 'fallbackShares': fallback_shares},
                        )
                        db.commit()
                        db.refresh(entry_intent)
                    cancel_results.append(f"• **{ticker}**: No broker-confirmed open shares to unwind")
                    continue

                linked_position_id = entry_intent.position_id if entry_intent else None
                linked_trade_id = entry_intent.trade_id if entry_intent else None
                linked_account_id = (entry_intent.account_id if entry_intent else None) or str(
                    self.tradier.get_account_snapshot(mode).get('accountId')
                    or self.tradier._credentials_for_mode(mode)['account_id']
                )

                exit_intent = execution_lifecycle.create_exit_intent(
                    db,
                    account_id=linked_account_id,
                    asset_class='stock',
                    symbol=ticker,
                    requested_quantity=shares,
                    requested_price=float(trade.get('entry_price') or 0) or None,
                    execution_source='DISCORD_CANCEL_WINDOW',
                    position_id=linked_position_id,
                    trade_id=linked_trade_id,
                    linked_intent_id=intent_id,
                    context={
                        'mode': mode,
                        'exitTrigger': 'SAFETY_CANCEL_WINDOW',
                        'brokerOpenShares': broker_shares,
                        'fallbackShares': fallback_shares,
                    },
                )

                try:
                    exit_order = await self.tradier.place_order_async(
                        ticker=ticker,
                        qty=shares,
                        side='sell',
                        mode=mode,
                        order_type='market',
                    )
                    execution_lifecycle.record_submission(db, exit_intent, exit_order)
                except Exception as exc:
                    execution_lifecycle.record_event(
                        db,
                        exit_intent,
                        event_type='ORDER_SUBMISSION_FAILED',
                        status='REJECTED',
                        message=f'Exit order submission failed for {ticker}: {exc}',
                        payload={'error': str(exc)},
                    )
                    exit_intent.status = 'REJECTED'
                    exit_intent.rejection_reason = str(exc)
                    db.commit()
                    db.refresh(exit_intent)
                    cancel_results.append(f"• **{ticker}**: Exit submit failed ({exc})")
                    continue

                confirmed_exit = await self._confirm_stock_order(exit_order, mode=mode)
                exit_intent = execution_lifecycle.refresh_from_order_snapshot(db, exit_intent, confirmed_exit)
                exit_record = execution_lifecycle.materialize_stock_exit(
                    db,
                    exit_intent,
                    current_price=None,
                    exit_trigger='SAFETY_CANCEL_WINDOW',
                )

                order_payload = exit_order.get('order', {}) if isinstance(exit_order.get('order'), dict) else exit_order
                closed_shares = int(exit_record['closed_shares']) if exit_record else int(round(float(exit_intent.filled_quantity or 0.0)))
                remaining_shares = int(exit_record['remaining_shares']) if exit_record else max(shares - closed_shares, 0)

                if closed_shares <= 0:
                    cancel_results.append(
                        f"• **{ticker}**: Exit submitted against {shares} broker-confirmed shares, but no confirmed exit fill yet [Order #{order_payload.get('id', 'n/a')}]"
                    )
                    continue

                if remaining_shares > 0:
                    cancel_results.append(
                        f"• **{ticker}**: Sold {closed_shares} shares from broker-confirmed {shares}; {remaining_shares} still open [Order #{order_payload.get('id', 'n/a')}]"
                    )
                else:
                    cancel_results.append(
                        f"• **{ticker}**: Sold {closed_shares} shares using broker-confirmed quantity [Order #{order_payload.get('id', 'n/a')}]"
                    )
        finally:
            db.close()

        return '\n'.join(cancel_results)

    async def _cancel_crypto_execution(self, result: List[Dict]) -> str:
        from app.services.kraken_service import TOP_30_PAIRS, crypto_ledger

        cancel_results = []

        for trade in result:
            pair = trade['pair']
            amount = trade['amount']
            ohlcv_pair = TOP_30_PAIRS.get(pair)

            exit_trade = crypto_ledger.execute_trade(
                pair=pair,
                ohlcv_pair=ohlcv_pair,
                side='SELL',
                amount=amount,
            )

            cancel_results.append(
                f"• **{pair}**: Sold {amount:.4f} @ ${float(exit_trade.get('price', 0) or 0):.2f}"
            )

        return '\n'.join(cancel_results)

    async def _handle_override(
        self,
        reply_message,
        original_message,
        decision_data,
        positions: List[Dict],
        trade_type: str = 'stock',
    ):
        await original_message.add_reaction('🚫')
        await reply_message.edit(
            content=(
                f'{reply_message.content}\n\n'
                '🚫 **Safety overrides are disabled during Phase 0 lockdown.**\n'
                'Execution must pass the normal authorization, freshness, and safety gates.'
            )
        )

    @commands.command(name='screen')
    async def crypto_screen(self, ctx):
        if ctx.author.id != settings.DISCORD_USER_ID:
            await ctx.send('❌ Unauthorized')
            return
        if ctx.channel.id != self.trading_channel_id:
            return

        await ctx.send('🔍 **Screening Kraken top 15 pairs for momentum...**')

        try:
            results = crypto_analyzer.screen_for_momentum(
                min_change_24h=5.0,
                min_volume_ratio=1.5,
                rsi_min=50,
                rsi_max=70,
            )
            summary = crypto_analyzer.get_screening_summary(results)
            await ctx.send(summary)

            if results:
                candidates = [{'pair': result['pair']} for result in results[:3]]
                decision_json = {
                    'type': 'CRYPTO_SCREENING',
                    'generated_at': datetime.utcnow().isoformat() + 'Z',
                    'candidates': candidates,
                    'reasoning': 'Found momentum signals with RSI 50-70, volume spike, and >5% gain',
                }

                await ctx.send(
                    f"**📋 Recommended Trades:**\n```json\n{json.dumps(decision_json, indent=2)}\n```\n\n"
                    '💡 Copy the JSON above and paste it as a new message to execute.'
                )
            else:
                await ctx.send(
                    'ℹ️ No pairs currently meet momentum criteria.\n\n'
                    '**Criteria:**\n'
                    '• 24h gain: >5%\n'
                    '• Volume: >1.5x average\n'
                    '• RSI: 50-70'
                )

        except Exception as exc:
            logger.error('Screen command error: %s', exc, exc_info=True)
            await ctx.send(f'❌ **Error:** {exc}')

    @commands.command(name='analyze')
    async def crypto_analyze(self, ctx, pair: str = None):
        if ctx.author.id != settings.DISCORD_USER_ID:
            await ctx.send('❌ Unauthorized')
            return
        if ctx.channel.id != self.trading_channel_id:
            return
        if not pair:
            await ctx.send('❌ **Usage:** `!analyze <PAIR>`\n**Example:** `!analyze SOL/USD`')
            return

        pair = pair.upper().replace('-', '/')
        await ctx.send(f'🔍 **Analyzing {pair}...**')

        try:
            analysis = crypto_analyzer.analyze_pair(pair)
            if analysis['price'] == 0:
                await ctx.send(f'❌ Could not fetch data for {pair}')
                return

            emoji = '🟢' if analysis.get('change_24h', 0) > 0 else '🔴'
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

            if all([
                analysis.get('volume_spike'),
                analysis.get('rsi_momentum'),
                analysis.get('bullish_24h'),
            ]):
                decision_json = {
                    'type': 'CRYPTO_SCREENING',
                    'generated_at': datetime.utcnow().isoformat() + 'Z',
                    'candidates': [{'pair': pair}],
                    'reasoning': (
                        f"Strong momentum: RSI {analysis['rsi']:.1f}, "
                        f"Volume {analysis['volume_ratio']:.1f}x, +{analysis['change_24h']:.1f}%"
                    ),
                }
                await ctx.send(f"✅ **Strong momentum detected!**\n\n```json\n{json.dumps(decision_json, indent=2)}\n```")

        except Exception as exc:
            logger.error('Analyze command error: %s', exc, exc_info=True)
            await ctx.send(f'❌ **Error:** {exc}')

    @commands.command(name='help')
    async def show_help(self, ctx):
        if ctx.author.id != settings.DISCORD_USER_ID:
            return

        help_text = f"""
**🤖 Trading Bot Commands**

**Crypto Screening:**
- `!screen` - Screen all Kraken pairs for momentum
- `!analyze <PAIR>` - Analyze a specific pair (e.g., !analyze SOL/USD)

**Manual Trading:**
Just paste JSON in this channel:
```json
{{
  "type": "SCREENING",
  "generated_at": "{datetime.utcnow().isoformat()}Z",
  "candidates": [{{"ticker": "AAPL"}}]
}}
```

Or for crypto:
```json
{{
  "type": "CRYPTO_SCREENING",
  "generated_at": "{datetime.utcnow().isoformat()}Z",
  "candidates": [{{"pair": "SOL/USD"}}]
}}
```

**Position Sizing:**
Automatic - configured in .env
Current: {settings.POSITION_SIZE_PCT * 100:.0f}% per position
        """.strip()
        await ctx.send(help_text)

    @tasks.loop(time=time(hour=16, minute=30))
    async def daily_summary(self):
        channel = self.get_channel(self.trading_channel_id)
        if channel:
            await channel.send('📊 **DAILY SUMMARY** - End of day report')


async def start_discord_bot():
    if not settings.DISCORD_BOT_TOKEN:
        logger.warning('Discord bot start skipped because DISCORD_BOT_TOKEN is not configured.')
        return
    bot = TradingBot()
    await bot.start(settings.DISCORD_BOT_TOKEN)
