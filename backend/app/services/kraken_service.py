"""
Kraken API Integration Service
Handles crypto trading operations via Kraken REST API
"""
import requests
import json
import logging
from typing import Dict, List, Optional
from datetime import datetime
from decimal import Decimal

logger = logging.getLogger(__name__)

# Top 30 liquid crypto pairs (display name -> API pair mapping)
TOP_30_PAIRS = {
    'ADA/USD': 'ADAUSD',
    'BTC/EUR': 'XXBTZEUR',
    'BTC/GBP': 'XXBTZGBP',
    'BTC/USD': 'XXBTZUSD',
    'BTC/USDC': 'XBTUSDC',
    'BTC/USDT': 'XBTUSDT',
    'DOGE/USD': 'XDGUSD',
    'ETH/EUR': 'XETHZEUR',
    'ETH/USD': 'XETHZUSD',
    'ETH/USDC': 'ETHUSDC',
    'ETH/USDT': 'ETHUSDT',
    'HYPE/USD': 'HYPEUSD',
    'PAXG/USD': 'PAXGUSD',
    'SOL/EUR': 'SOLEUR',
    'SOL/USD': 'SOLUSD',
    'SOL/USDC': 'SOLUSDC',
    'SUI/USD': 'SUIUSD',
    'TAO/EUR': 'TAOEUR',
    'TAO/USD': 'TAOUSD',
    'USDC/EUR': 'USDCEUR',
    'USDC/GBP': 'USDCGBP',
    'USDC/USD': 'USDCUSD',
    'USDC/USDT': 'USDCUSDT',
    'USDT/EUR': 'USDTEUR',
    'USDT/USD': 'USDTZUSD',
    'TRX/USD': 'TRXUSD',
    'XMR/USDT': 'XMRUSDT',
    'XRP/EUR': 'XXRPZEUR',
    'XRP/USD': 'XXRPZUSD',
    'ZEC/USD': 'XZECZUSD'
}


class KrakenAPIService:
    """Kraken REST API wrapper for crypto operations"""
    
    def __init__(self):
        self.base_url = 'https://api.kraken.com/0/public'
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'AI Trading Bot/1.0'
        })
    
    def _api_call(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """Make API call to Kraken"""
        try:
            url = f"{self.base_url}/{endpoint}"
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get('error') and len(data['error']) > 0:
                logger.error(f"Kraken API error: {data['error']}")
                return None
            
            return data.get('result')
            
        except Exception as e:
            logger.error(f"Kraken API call failed: {e}")
            return None
    
    def get_ticker(self, pair: str) -> Optional[Dict]:
        """Get current ticker for a pair"""
        result = self._api_call('Ticker', {'pair': pair})
        
        if result and pair in result:
            ticker = dict(result[pair])
            ticker.setdefault('_fetched_at_utc', datetime.now(timezone.utc).isoformat())
            return ticker
        
        # Try alternative pair name
        for alt_pair in result.keys() if result else []:
            if pair in alt_pair or alt_pair in pair:
                ticker = dict(result[alt_pair])
                ticker.setdefault('_fetched_at_utc', datetime.now(timezone.utc).isoformat())
                return ticker
        
        return None
    
    def get_ohlc(self, pair: str, interval: int = 5, limit: int = 100) -> List[Dict]:
        """
        Get OHLC candle data
        
        Args:
            pair: Kraken pair format (e.g., 'XXBTZUSD')
            interval: minutes (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)
            limit: max number of candles (default 100, max 720)
        
        Returns:
            List of candle dicts
        """
        # Map interval to Kraken's format
        interval_map = {
            1: 1,
            5: 5,
            15: 15,
            30: 30,
            60: 60,
            240: 240,
            1440: 1440
        }
        
        kraken_interval = interval_map.get(interval, 5)
        
        result = self._api_call('OHLC', {
            'pair': pair,
            'interval': kraken_interval,
            'since': None  # Get most recent candles
        })
        
        if not result:
            return []
        
        # Find the pair data (key might vary)
        pair_data = None
        for key, value in result.items():
            if key != 'last' and isinstance(value, list):
                pair_data = value
                break
        
        if not pair_data:
            return []
        
        # Limit to requested number
        pair_data = pair_data[-limit:] if len(pair_data) > limit else pair_data
        
        # Parse OHLC data
        candles = []
        for entry in pair_data:
            candles.append({
                'timestamp': int(entry[0]),
                'open': float(entry[1]),
                'high': float(entry[2]),
                'low': float(entry[3]),
                'close': float(entry[4]),
                'vwap': float(entry[5]),
                'volume': float(entry[6]),
                'count': int(entry[7])
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
        self.kraken = KrakenAPIService()
    
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
        
        Args:
            pair: Display format (BTC/USD)
            ohlcv_pair: Kraken format (XXBTZUSD)
            side: BUY or SELL
            amount: Crypto amount to trade
            price: Optional override price (uses current market if None)
        
        Returns:
            Trade result dict
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
            
            if self.positions[pair]['amount'] > 0:
                ratio = amount_dec / (self.positions[pair]['amount'] + amount_dec)
                self.positions[pair]['total_cost'] -= self.positions[pair]['total_cost'] * ratio
            else:
                self.positions[pair]['total_cost'] = Decimal('0')
            
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
        
        if not self.positions:
            return positions
        
        pairs_to_check = list(self.positions.keys())
        ohlcv_pairs = [TOP_30_PAIRS.get(p) for p in pairs_to_check]
        
        prices = self.kraken.get_prices(ohlcv_pairs)
        
        for pair, pos in self.positions.items():
            ohlcv_pair = TOP_30_PAIRS[pair]
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


# Global instances
kraken_service = KrakenAPIService()
crypto_ledger = CryptoPaperLedger()