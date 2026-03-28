"""
Kraken CLI Integration Service - WSL Compatible
Handles crypto trading operations via Kraken CLI in WSL
Paper trading only - tracks trades in-memory ledger

Requirements:
- Kraken CLI installed in WSL: curl --proto '=https' --tlsv1.2 -LsSf https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh
- Cargo PATH loaded: source $HOME/.cargo/env (add to .bashrc for persistence)
"""
import subprocess
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime
from decimal import Decimal

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
    """Kraken CLI wrapper for crypto operations - WSL compatible"""
    
    def __init__(self):
        # Call through WSL bash with login shell to load PATH from .bashrc
        # This ensures kraken CLI installed in $HOME/.cargo/bin is accessible
        self.wsl_prefix = ["wsl", "bash", "-lc"]
        
    def _run_cli(self, args: List[str]) -> Dict:
        """Execute Kraken CLI command via WSL"""
        try:
            # Build command: wsl bash -lc "kraken <args>"
            # The -l flag loads .bashrc which includes cargo PATH
            kraken_cmd = "kraken " + " ".join(f'"{arg}"' if " " in arg else arg for arg in args)
            cmd = self.wsl_prefix + [kraken_cmd]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                logger.error(f"Kraken CLI error: {result.stderr}")
                return {"error": result.stderr}
            
            return json.loads(result.stdout) if result.stdout else {}
            
        except subprocess.TimeoutExpired:
            logger.error("Kraken CLI timeout")
            return {"error": "CLI timeout"}
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse CLI output: {e}")
            return {"error": "Invalid JSON response"}
        except Exception as e:
            logger.error(f"CLI execution failed: {e}")
            return {"error": str(e)}
    
    def get_ticker(self, pair: str) -> Optional[Dict]:
        """Get current ticker for a pair (OHLCV format like XBTUSD)"""
        result = self._run_cli(['ticker', pair])
        if 'error' in result:
            return None
        return result.get(pair)
    
    def get_ohlc(self, pair: str, interval: int = 5, limit: int = 100) -> List[Dict]:
        """
        Get OHLC candle data
        pair: OHLCV format (e.g., XBTUSD)
        interval: minutes (1, 5, 15, 30, 60, 240, 1440)
        """
        result = self._run_cli(['ohlc', pair, '--interval', str(interval), '--limit', str(limit)])
        if 'error' in result:
            return []
        
        # Parse OHLC data
        candles = []
        for entry in result.get(pair, []):
            candles.append({
                'timestamp': datetime.fromtimestamp(entry[0]).isoformat(),
                'open': float(entry[1]),
                'high': float(entry[2]),
                'low': float(entry[3]),
                'close': float(entry[4]),
                'volume': float(entry[6])
            })
        return candles
    
    def get_prices(self, pairs: List[str]) -> Dict[str, float]:
        """Get current prices for multiple pairs"""
        prices = {}
        for pair in pairs:
            ticker = self.get_ticker(pair)
            if ticker and 'c' in ticker:
                prices[pair] = float(ticker['c'][0])
        return prices


class CryptoPaperLedger:
    """
    Paper trading ledger for crypto
    Tracks simulated trades with real Kraken prices
    """
    
    def __init__(self, starting_balance: float = 100000.0):
        self.balance = Decimal(str(starting_balance))
        self.starting_balance = Decimal(str(starting_balance))
        self.trades: List[Dict] = []
        self.positions: Dict[str, Dict] = {}  # pair -> {amount, avg_price}
        self.kraken = KrakenCLIService()
    
    def execute_trade(
        self,
        pair: str,
        ohlcv_pair: str,
        side: str,
        amount: float,
        price: Optional[float] = None
    ) -> Dict:
        """
        Execute a paper trade
        pair: Display format (BTC/USD)
        ohlcv_pair: Kraken format (XBTUSD)
        side: BUY or SELL
        amount: Crypto amount to trade
        price: Optional override price (uses current market if None)
        """
        # Get current price if not provided
        if price is None:
            ticker = self.kraken.get_ticker(ohlcv_pair)
            if not ticker or 'c' not in ticker:
                return {
                    'status': 'REJECTED',
                    'reason': 'Failed to fetch current price'
                }
            price = float(ticker['c'][0])
        
        amount_dec = Decimal(str(amount))
        price_dec = Decimal(str(price))
        total = amount_dec * price_dec
        
        # Validate trade
        if side == 'BUY':
            if total > self.balance:
                return {
                    'status': 'REJECTED',
                    'reason': f'Insufficient balance: ${self.balance:.2f} < ${total:.2f}'
                }
            self.balance -= total
            
            # Update position
            if pair not in self.positions:
                self.positions[pair] = {'amount': Decimal('0'), 'total_cost': Decimal('0')}
            
            self.positions[pair]['amount'] += amount_dec
            self.positions[pair]['total_cost'] += total
            
        elif side == 'SELL':
            if pair not in self.positions or self.positions[pair]['amount'] < amount_dec:
                return {
                    'status': 'REJECTED',
                    'reason': f'Insufficient {pair} position'
                }
            
            self.balance += total
            self.positions[pair]['amount'] -= amount_dec
            self.positions[pair]['total_cost'] -= (
                self.positions[pair]['total_cost'] / self.positions[pair]['amount'] * amount_dec
            )
            
            # Remove empty positions
            if self.positions[pair]['amount'] == 0:
                del self.positions[pair]
        
        # Record trade
        trade = {
            'id': f"paper_{len(self.trades) + 1}",
            'timestamp': datetime.utcnow().isoformat(),
            'market': 'CRYPTO',
            'pair': pair,
            'ohlcvPair': ohlcv_pair,
            'side': side,
            'amount': float(amount_dec),
            'price': float(price_dec),
            'total': float(total),
            'status': 'FILLED',
            'balance': float(self.balance)
        }
        self.trades.append(trade)
        
        logger.info(f"Paper trade executed: {side} {amount} {pair} @ ${price:.2f}")
        return trade
    
    def get_positions(self) -> List[Dict]:
        """Get current positions with P&L"""
        positions = []
        prices = self.kraken.get_prices([TOP_15_PAIRS[p] for p in self.positions.keys()])
        
        for pair, pos in self.positions.items():
            ohlcv_pair = TOP_15_PAIRS[pair]
            current_price = prices.get(ohlcv_pair, 0)
            
            if current_price == 0:
                continue
            
            avg_price = float(pos['total_cost'] / pos['amount'])
            current_value = float(pos['amount']) * current_price
            cost_basis = float(pos['total_cost'])
            pnl = current_value - cost_basis
            pnl_percent = (pnl / cost_basis) * 100 if cost_basis > 0 else 0
            
            positions.append({
                'pair': pair,
                'ohlcvPair': ohlcv_pair,
                'amount': float(pos['amount']),
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'pnl': pnl,
                'pnlPercent': pnl_percent
            })
        
        return positions
    
    def get_ledger(self) -> Dict:
        """Get full ledger including balance and all trades"""
        total_pnl = sum(p['pnl'] for p in self.get_positions())
        
        return {
            'balance': float(self.balance),
            'startingBalance': float(self.starting_balance),
            'totalPnL': total_pnl,
            'trades': self.trades,
            'positions': self.get_positions()
        }


# Global instance
crypto_ledger = CryptoPaperLedger()
kraken_service = KrakenCLIService()
