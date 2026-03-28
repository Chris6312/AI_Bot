"""
Kraken CLI Integration Service - WSL Compatible
Handles crypto trading operations via Kraken CLI in WSL.

Paper trading is fully supported. Live credentials are surfaced via configuration so
ChatGPT/MCP wiring can be added later without changing the UI contract.
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# Top 15 liquid crypto pairs (display name -> OHLCV name mapping)
TOP_15_PAIRS = {
    'BTC/USD': 'XBTUSD',
    'ETH/USD': 'ETHUSD',
    'SOL/USD': 'SOLUSD',
    'XRP/USD': 'XRPUSD',
    'ADA/USD': 'ADAUSD',
    'AVAX/USD': 'AVAXUSD',
    'DOT/USD': 'DOTUSD',
    'MATIC/USD': 'MATICUSD',
    'LINK/USD': 'LINKUSD',
    'UNI/USD': 'UNIUSD',
    'ATOM/USD': 'ATOMUSD',
    'LTC/USD': 'LTCUSD',
    'BCH/USD': 'BCHUSD',
    'ALGO/USD': 'ALGOUSD',
    'XLM/USD': 'XLMUSD',
}


class KrakenCLIService:
    """Kraken CLI wrapper for crypto operations, executed via WSL."""

    def __init__(self):
        self.cli_name = settings.KRAKEN_CLI_PATH
        self.wsl_prefix = ['wsl', 'bash', '-lc']

    def _run_cli(self, args: List[str]) -> Dict:
        try:
            quoted_args = ' '.join(shlex.quote(arg) for arg in args)
            kraken_cmd = f'{shlex.quote(self.cli_name)} {quoted_args}'.strip()
            cmd = self.wsl_prefix + [kraken_cmd]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error('Kraken CLI error: %s', result.stderr.strip())
                return {'error': result.stderr.strip() or 'Kraken CLI failed'}

            stdout = result.stdout.strip()
            return json.loads(stdout) if stdout else {}
        except subprocess.TimeoutExpired:
            logger.error('Kraken CLI timeout')
            return {'error': 'CLI timeout'}
        except json.JSONDecodeError as exc:
            logger.error('Failed to parse Kraken CLI output: %s', exc)
            return {'error': 'Invalid JSON response'}
        except Exception as exc:
            logger.error('Kraken CLI execution failed: %s', exc)
            return {'error': str(exc)}

    def get_ticker(self, pair: str) -> Optional[Dict]:
        result = self._run_cli(['ticker', pair])
        if 'error' in result:
            return None
        return result.get(pair)

    def get_ohlc(self, pair: str, interval: int = 5, limit: int = 100) -> List[Dict]:
        result = self._run_cli(['ohlc', pair, '--interval', str(interval), '--limit', str(limit)])
        if 'error' in result:
            return []

        candles = []
        for entry in result.get(pair, []):
            candles.append(
                {
                    'timestamp': datetime.fromtimestamp(entry[0], tz=timezone.utc).isoformat(),
                    'open': float(entry[1]),
                    'high': float(entry[2]),
                    'low': float(entry[3]),
                    'close': float(entry[4]),
                    'volume': float(entry[6]),
                }
            )
        return candles

    def get_prices(self, pairs: List[str]) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        for pair in pairs:
            ticker = self.get_ticker(pair)
            if ticker and 'c' in ticker:
                try:
                    prices[pair] = float(ticker['c'][0])
                except (TypeError, ValueError, IndexError):
                    continue
        return prices


class CryptoPaperLedger:
    """Paper trading ledger for crypto using live Kraken prices when available."""

    def __init__(self, starting_balance: float = 100000.0):
        self.balance = Decimal(str(starting_balance))
        self.starting_balance = Decimal(str(starting_balance))
        self.trades: List[Dict] = []
        self.positions: Dict[str, Dict] = {}
        self.kraken = KrakenCLIService()

    def execute_trade(
        self,
        pair: str,
        ohlcv_pair: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
    ) -> Dict:
        if price is None:
            ticker = self.kraken.get_ticker(ohlcv_pair)
            if not ticker or 'c' not in ticker:
                return {'status': 'REJECTED', 'reason': 'Failed to fetch current price'}
            price = float(ticker['c'][0])

        side = side.upper()
        amount_dec = Decimal(str(amount))
        price_dec = Decimal(str(price))
        total = amount_dec * price_dec

        if side == 'BUY':
            if total > self.balance:
                return {
                    'status': 'REJECTED',
                    'reason': f'Insufficient balance: ${self.balance:.2f} < ${total:.2f}',
                }
            self.balance -= total
            if pair not in self.positions:
                self.positions[pair] = {'amount': Decimal('0'), 'total_cost': Decimal('0')}
            self.positions[pair]['amount'] += amount_dec
            self.positions[pair]['total_cost'] += total
        elif side == 'SELL':
            if pair not in self.positions or self.positions[pair]['amount'] < amount_dec:
                return {'status': 'REJECTED', 'reason': f'Insufficient {pair} position'}

            existing_amount = self.positions[pair]['amount']
            avg_cost = self.positions[pair]['total_cost'] / existing_amount if existing_amount else Decimal('0')
            self.balance += total
            self.positions[pair]['amount'] -= amount_dec
            self.positions[pair]['total_cost'] -= avg_cost * amount_dec
            if self.positions[pair]['amount'] <= Decimal('0'):
                del self.positions[pair]
        else:
            return {'status': 'REJECTED', 'reason': f'Unsupported side: {side}'}

        trade = {
            'id': f'paper_{len(self.trades) + 1}',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'market': 'CRYPTO',
            'pair': pair,
            'ohlcvPair': ohlcv_pair,
            'side': side,
            'amount': float(amount_dec),
            'price': float(price_dec),
            'total': float(total),
            'status': 'FILLED',
            'balance': float(self.balance),
        }
        self.trades.append(trade)

        logger.info('Paper trade executed: %s %s %s @ $%.2f', side, amount, pair, price)
        return trade

    def get_positions(self) -> List[Dict]:
        positions = []
        lookup_pairs = [TOP_15_PAIRS[pair] for pair in self.positions.keys() if pair in TOP_15_PAIRS]
        prices = self.kraken.get_prices(lookup_pairs) if lookup_pairs else {}

        for pair, pos in self.positions.items():
            ohlcv_pair = TOP_15_PAIRS[pair]
            current_price = prices.get(ohlcv_pair, 0.0)
            avg_price = float(pos['total_cost'] / pos['amount']) if pos['amount'] else 0.0
            cost_basis = float(pos['total_cost'])
            current_value = float(pos['amount']) * current_price if current_price else 0.0
            pnl = current_value - cost_basis if current_value else 0.0
            pnl_percent = (pnl / cost_basis) * 100 if cost_basis > 0 else 0.0
            positions.append(
                {
                    'pair': pair,
                    'ohlcvPair': ohlcv_pair,
                    'amount': float(pos['amount']),
                    'avgPrice': avg_price,
                    'currentPrice': current_price,
                    'marketValue': current_value,
                    'costBasis': cost_basis,
                    'pnl': pnl,
                    'pnlPercent': pnl_percent,
                }
            )
        return positions

    def get_ledger(self) -> Dict:
        positions = self.get_positions()
        total_market_value = sum(position['marketValue'] for position in positions)
        total_pnl = sum(position['pnl'] for position in positions)
        equity = float(self.balance) + total_market_value
        return {
            'balance': float(self.balance),
            'startingBalance': float(self.starting_balance),
            'equity': equity,
            'marketValue': total_market_value,
            'totalPnL': total_pnl,
            'trades': self.trades,
            'positions': positions,
        }


crypto_ledger = CryptoPaperLedger()
kraken_service = KrakenCLIService()
