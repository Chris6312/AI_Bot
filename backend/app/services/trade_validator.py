"""
Trade Validation Service
Validates trades before execution to catch AI/screening errors
"""
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.services.kraken_service import kraken_service, TOP_30_PAIRS
from app.services.tradier_client import tradier_client

logger = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')


class TradeValidator:
    """Validates trades before execution for both stocks and crypto"""
    
    def __init__(self):
        self.kraken = kraken_service
        self.tradier = tradier_client
        
        # Crypto validation thresholds
        self.crypto_min_24h_volume_usd = 100000  # $100k minimum daily volume
        self.crypto_max_price_spike_pct = 50  # Max 50% price change in 24h
        self.crypto_max_spread_pct = 2.0  # Max 2% bid-ask spread
        self.crypto_min_candles_required = 20  # Need historical data
        
        # Stock validation thresholds
        self.stock_min_price = 0.50  # Minimum $0.50 (avoid penny stocks)
        self.stock_max_price = 10000  # Maximum $10k (sanity check)
        self.stock_min_volume = 100000  # Min 100k shares daily volume
        self.stock_min_market_cap = 50000000  # Min $50M market cap
        self.stock_max_spread_pct = 1.0  # Max 1% bid-ask spread
    
    # ========================================
    # CRYPTO VALIDATION
    # ========================================
    
    def validate_crypto_trade(self, pair: str, amount: float) -> Tuple[bool, str]:
        """
        Validate a crypto trade before execution
        
        Args:
            pair: Display format (e.g., 'HYPE/USD')
            amount: Crypto amount to trade
        
        Returns:
            (is_valid, reason)
        """
        # Check 1: Pair exists in our supported list
        if pair not in TOP_30_PAIRS:
            return False, f"❌ {pair} not in supported pairs list"
        
        ohlcv_pair = TOP_30_PAIRS[pair]
        
        # Check 2: Can fetch current ticker
        ticker = self.kraken.get_ticker(ohlcv_pair)
        if not ticker or 'c' not in ticker:
            return False, f"❌ Cannot fetch current price for {pair}"
        
        current_price = float(ticker['c'][0])
        
        # Check 3: Price is reasonable (not zero or negative)
        if current_price <= 0:
            return False, f"❌ Invalid price: ${current_price}"
        
        # Check 4: 24h volume check
        if 'v' in ticker:
            volume_24h = float(ticker['v'][1])
            volume_usd = volume_24h * current_price
            
            if volume_usd < self.crypto_min_24h_volume_usd:
                return False, f"❌ Low liquidity: ${volume_usd:,.0f} 24h volume (min ${self.crypto_min_24h_volume_usd:,.0f})"
        
        # Check 5: Price stability (detect pump & dumps)
        if 'o' in ticker and 'h' in ticker and 'l' in ticker:
            open_price = float(ticker['o'][0])
            
            if open_price > 0:
                change_pct = abs((current_price - open_price) / open_price * 100)
                
                if change_pct > self.crypto_max_price_spike_pct:
                    return False, f"❌ Extreme volatility: {change_pct:.1f}% change in 24h (max {self.crypto_max_price_spike_pct}%)"
        
        # Check 6: Bid-Ask spread (if available)
        if 'a' in ticker and 'b' in ticker:
            ask = float(ticker['a'][0])
            bid = float(ticker['b'][0])
            
            if ask > 0 and bid > 0:
                spread_pct = ((ask - bid) / ask) * 100
                
                if spread_pct > self.crypto_max_spread_pct:
                    return False, f"❌ Wide spread: {spread_pct:.2f}% (max {self.crypto_max_spread_pct}%)"
        
        # Check 7: Historical data available (for exits)
        candles = self.kraken.get_ohlc(ohlcv_pair, interval=5, limit=self.crypto_min_candles_required)
        
        if len(candles) < self.crypto_min_candles_required:
            return False, f"❌ Insufficient historical data: {len(candles)} candles (need {self.crypto_min_candles_required})"
        
        # Check 8: Position size sanity check
        trade_value_usd = amount * current_price
        
        if trade_value_usd < 100:
            return False, f"❌ Trade too small: ${trade_value_usd:.2f} (min $100)"
        
        if trade_value_usd > 50000:
            return False, f"⚠️ Trade too large: ${trade_value_usd:,.2f} (max $50k per position)"
        
        # All checks passed!
        logger.info(f"✅ Crypto validation passed for {pair}: ${current_price:.2f}, ${volume_usd:,.0f} volume")
        return True, "✅ All validation checks passed"
    
    # ========================================
    # STOCK VALIDATION
    # ========================================
    
    def validate_stock_trade(self, ticker: str, shares: int, mode: str = 'PAPER') -> Tuple[bool, str]:
        """
        Validate a stock trade before execution
        
        Args:
            ticker: Stock symbol (e.g., 'AAPL')
            shares: Number of shares to trade
            mode: 'PAPER' or 'LIVE'
        
        Returns:
            (is_valid, reason)
        """
        # Check 1: Ticker format (basic sanity)
        ticker = ticker.upper().strip()
        
        if not ticker or len(ticker) > 5:
            return False, f"❌ Invalid ticker format: '{ticker}'"
        
        if not ticker.isalpha():
            return False, f"❌ Ticker must contain only letters: '{ticker}'"
        
        # Check 2: Market hours (for live trading)
        if mode == 'LIVE':
            now_et = datetime.now(ET)
            market_open = time(9, 30)
            market_close = time(16, 0)
            is_weekday = now_et.weekday() < 5
            is_market_hours = market_open <= now_et.time() <= market_close
            
            if not (is_weekday and is_market_hours):
                return False, f"❌ Market closed (only paper trading allowed)"
        
        # Check 3: Fetch quote from Tradier
        try:
            quote = self.tradier.get_quote(ticker, mode)
        except Exception as e:
            return False, f"❌ Cannot fetch quote for {ticker}: {str(e)}"
        
        if not quote or 'last' not in quote:
            return False, f"❌ No quote data available for {ticker}"
        
        last_price = quote.get('last', 0)
        
        # Check 4: Price range validation
        if last_price < self.stock_min_price:
            return False, f"❌ Price too low: ${last_price:.2f} (min ${self.stock_min_price})"
        
        if last_price > self.stock_max_price:
            return False, f"❌ Price unrealistic: ${last_price:,.2f} (max ${self.stock_max_price:,.0f})"
        
        # Check 5: Volume check
        volume = quote.get('volume', 0)
        
        if volume < self.stock_min_volume:
            return False, f"❌ Low volume: {volume:,} shares (min {self.stock_min_volume:,})"
        
        # Check 6: Trading status
        trade_status = quote.get('type', '').lower()
        
        if trade_status == 'halt':
            return False, f"❌ Trading halted for {ticker}"
        
        # Check 7: Bid-Ask spread
        bid = quote.get('bid', 0)
        ask = quote.get('ask', 0)
        
        if bid > 0 and ask > 0:
            spread_pct = ((ask - bid) / ask) * 100
            
            if spread_pct > self.stock_max_spread_pct:
                return False, f"❌ Wide spread: {spread_pct:.2f}% (max {self.stock_max_spread_pct}%)"
        
        # Check 8: Position size sanity
        trade_value = shares * last_price
        
        if trade_value < 100:
            return False, f"❌ Trade too small: ${trade_value:.2f} (min $100)"
        
        if trade_value > 100000:
            return False, f"⚠️ Trade too large: ${trade_value:,.2f} (max $100k per position)"
        
        if shares < 1:
            return False, f"❌ Must trade at least 1 share"
        
        # Check 9: Symbol description (detect invalid/delisted)
        description = quote.get('description', '')
        
        if not description:
            return False, f"⚠️ No description available for {ticker} (may be delisted)"
        
        # All checks passed!
        logger.info(f"✅ Stock validation passed for {ticker}: ${last_price:.2f}, {volume:,} volume")
        return True, "✅ All validation checks passed"
    
    # ========================================
    # BATCH VALIDATION
    # ========================================
    
    def validate_crypto_batch(self, candidates: List[Dict]) -> Dict[str, Tuple[bool, str]]:
        """
        Validate multiple crypto trades
        
        Args:
            candidates: List of {pair, amount} dicts
        
        Returns:
            Dict mapping pair -> (is_valid, reason)
        """
        results = {}
        
        for candidate in candidates:
            pair = candidate.get('pair')
            amount = candidate.get('amount', 0)
            
            if not pair:
                results['UNKNOWN'] = (False, "❌ Missing pair name")
                continue
            
            is_valid, reason = self.validate_crypto_trade(pair, amount)
            results[pair] = (is_valid, reason)
        
        return results
    
    def validate_stock_batch(self, candidates: List[Dict], mode: str = 'PAPER') -> Dict[str, Tuple[bool, str]]:
        """
        Validate multiple stock trades
        
        Args:
            candidates: List of {ticker, shares} dicts
            mode: 'PAPER' or 'LIVE'
        
        Returns:
            Dict mapping ticker -> (is_valid, reason)
        """
        results = {}
        
        for candidate in candidates:
            ticker = candidate.get('ticker')
            shares = candidate.get('shares', 0)
            
            if not ticker:
                results['UNKNOWN'] = (False, "❌ Missing ticker symbol")
                continue
            
            is_valid, reason = self.validate_stock_trade(ticker, shares, mode)
            results[ticker] = (is_valid, reason)
        
        return results


# Global instance
trade_validator = TradeValidator()