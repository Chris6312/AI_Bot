"""
Kraken API Integration Service
Handles crypto trading operations via Kraken REST API
"""
import requests
import json
import logging
from typing import Any, Dict, List, Optional
from datetime import UTC, datetime, timezone
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
        self.positions: Dict[str, Dict] = {}  # pair -> {amount, total_cost}
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
            'timestamp': datetime.now(UTC).isoformat(),
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
    
    def _build_position_analytics(self) -> dict[str, Any]:
        analytics: dict[str, Any] = {
            'entry_times': {},
            'realized_pnl_by_pair': {},
            'realized_pnl_total': 0.0,
        }
        running_positions: dict[str, dict[str, Decimal | str | None]] = {}

        for trade in self.trades:
            pair = str(trade.get('pair') or '').strip()
            if not pair:
                continue
            side = str(trade.get('side') or '').upper().strip()
            amount_dec = Decimal(str(trade.get('amount') or 0.0))
            price_dec = Decimal(str(trade.get('price') or 0.0))
            total_dec = Decimal(str(trade.get('total') or 0.0))
            timestamp = str(trade.get('timestamp') or '').strip() or None
            state = running_positions.setdefault(
                pair,
                {'amount': Decimal('0'), 'total_cost': Decimal('0'), 'opened_at': None},
            )
            current_amount = Decimal(str(state['amount']))
            current_cost = Decimal(str(state['total_cost']))
            opened_at = state['opened_at']

            if side == 'BUY':
                if current_amount <= 0 and amount_dec > 0:
                    opened_at = timestamp
                current_amount += amount_dec
                current_cost += total_dec
            elif side == 'SELL' and amount_dec > 0 and current_amount > 0:
                sell_amount = min(amount_dec, current_amount)
                avg_cost = (current_cost / current_amount) if current_amount > 0 else Decimal('0')
                closed_cost = avg_cost * sell_amount
                realized = (price_dec * sell_amount) - closed_cost
                analytics['realized_pnl_by_pair'][pair] = float(
                    Decimal(str(analytics['realized_pnl_by_pair'].get(pair, 0.0))) + realized
                )
                current_amount -= sell_amount
                current_cost -= closed_cost
                if current_amount <= Decimal('0.0000000001'):
                    current_amount = Decimal('0')
                    current_cost = Decimal('0')
                    opened_at = None

            state['amount'] = current_amount
            state['total_cost'] = current_cost
            state['opened_at'] = opened_at

        analytics['entry_times'] = {
            pair: state.get('opened_at')
            for pair, state in running_positions.items()
            if Decimal(str(state.get('amount') or 0)) > 0
        }
        analytics['realized_pnl_total'] = round(sum(analytics['realized_pnl_by_pair'].values()), 8)
        return analytics

    def get_positions(self) -> List[Dict]:
        """Get current positions with P&L and paper-equity context."""
        positions = []

        if not self.positions:
            return positions

        pairs_to_check = list(self.positions.keys())
        ohlcv_pairs = [TOP_30_PAIRS.get(p) for p in pairs_to_check if TOP_30_PAIRS.get(p)]
        prices = self.kraken.get_prices(ohlcv_pairs)
        analytics = self._build_position_analytics()

        for pair, pos in self.positions.items():
            ohlcv_pair = TOP_30_PAIRS.get(pair)
            if not ohlcv_pair:
                continue
            current_price = float(prices.get(ohlcv_pair, 0) or 0)
            if current_price <= 0:
                continue

            amount_dec = Decimal(str(pos.get('amount') or 0))
            total_cost_dec = Decimal(str(pos.get('total_cost') or 0))
            if amount_dec <= 0:
                continue

            avg_price = float(total_cost_dec / amount_dec) if amount_dec > 0 else 0.0
            market_value = float(amount_dec) * current_price
            cost_basis = float(total_cost_dec)
            pnl = market_value - cost_basis
            pnl_percent = (pnl / cost_basis) * 100 if cost_basis > 0 else 0.0

            positions.append({
                'pair': pair,
                'ohlcvPair': ohlcv_pair,
                'amount': float(amount_dec),
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'marketValue': market_value,
                'costBasis': cost_basis,
                'pnl': pnl,
                'pnlPercent': pnl_percent,
                'entryTimeUtc': analytics['entry_times'].get(pair),
                'realizedPnl': float(analytics['realized_pnl_by_pair'].get(pair, 0.0)),
            })

        return positions

    def get_ledger(self) -> Dict:
        """Get full ledger including balance, market value, equity, and P&L."""
        positions = self.get_positions()
        market_value = round(sum(float(position.get('marketValue') or 0.0) for position in positions), 8)
        total_pnl = round(sum(float(position.get('pnl') or 0.0) for position in positions), 8)
        analytics = self._build_position_analytics()
        realized_pnl = round(float(analytics.get('realized_pnl_total') or 0.0), 8)
        equity = round(float(self.balance) + market_value, 8)
        net_pnl = round(equity - float(self.starting_balance), 8)
        return_pct = round((net_pnl / float(self.starting_balance)) * 100, 8) if float(self.starting_balance) > 0 else 0.0

        return {
            'balance': float(self.balance),
            'startingBalance': float(self.starting_balance),
            'marketValue': market_value,
            'equity': equity,
            'totalPnL': total_pnl,
            'realizedPnL': realized_pnl,
            'netPnL': net_pnl,
            'returnPct': return_pct,
            'trades': self.trades,
            'positions': positions,
        }


# Global instances
kraken_service = KrakenAPIService()
crypto_ledger = CryptoPaperLedger()