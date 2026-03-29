"""
Global position sizing service for stocks and crypto.
Handles all position size calculations independent of AI prompts.
"""

import logging
from typing import Dict, List, Tuple, Optional
from decimal import Decimal
from app.core.config import settings

logger = logging.getLogger(__name__)


class PositionSizer:
    """Calculate position sizes for stocks and crypto based on global settings"""
    
    def __init__(self):
        self.global_pct = settings.POSITION_SIZE_PCT
        self.fixed_amount = settings.POSITION_SIZE_FIXED
        self.max_positions = settings.MAX_POSITIONS_PER_DECISION
        self.min_position = settings.MIN_POSITION_USD
        self.max_position_pct = settings.MAX_POSITION_PCT
    
    def calculate_stock_positions(
        self,
        candidates: List[Dict],
        account_equity: float,
        prices: Optional[Dict[str, float]] = None
    ) -> List[Dict]:
        """
        Calculate position sizes for stock candidates.
        
        Args:
            candidates: List of {"ticker": "AAPL"} or {"ticker": "AAPL", "shares": 10}
            account_equity: Current account equity from Tradier
            prices: Optional dict of {"AAPL": 178.50} to calculate shares immediately
        
        Returns:
            List of {"ticker": "AAPL", "shares": 56, "estimated_value": 10000.00, "position_pct": 0.10}
        """
        # Use stock-specific override if set, else global
        position_pct = settings.STOCK_POSITION_SIZE_PCT or self.global_pct
        min_position = settings.STOCK_MIN_POSITION_USD or self.min_position
        
        # Validate candidate count
        if len(candidates) > self.max_positions:
            logger.warning(
                f"Too many stock candidates ({len(candidates)} > {self.max_positions}). "
                f"Using first {self.max_positions}."
            )
            candidates = candidates[:self.max_positions]
        
        positions = []
        
        for candidate in candidates:
            ticker = candidate['ticker']
            
            # Check if shares already specified (legacy format)
            if 'shares' in candidate and candidate['shares']:
                shares = candidate['shares']
                # Estimate value if price provided
                if prices and ticker in prices:
                    estimated_value = shares * prices[ticker]
                else:
                    estimated_value = None
                
                positions.append({
                    'ticker': ticker,
                    'shares': shares,
                    'estimated_value': estimated_value,
                    'position_pct': None,  # Not calculated
                    'source': 'specified'
                })
                continue
            
            # Calculate position value
            if self.fixed_amount:
                position_value = self.fixed_amount
            else:
                position_value = account_equity * position_pct
            
            # Safety checks
            if position_value < min_position:
                logger.warning(
                    f"{ticker}: Position too small (${position_value:.2f} < ${min_position:.2f}). Skipping."
                )
                continue
            
            max_position_value = account_equity * self.max_position_pct
            if position_value > max_position_value:
                logger.warning(
                    f"{ticker}: Position too large (${position_value:.2f} > ${max_position_value:.2f}). "
                    f"Capping at {self.max_position_pct*100:.0f}%"
                )
                position_value = max_position_value
            
            # Calculate shares if price provided
            shares = None
            if prices and ticker in prices:
                price = prices[ticker]
                if price > 0:
                    shares = int(position_value / price)
            
            positions.append({
                'ticker': ticker,
                'shares': shares,  # None if price not provided
                'estimated_value': position_value,
                'position_pct': position_pct,
                'source': 'calculated'
            })
        
        return positions
    
    def calculate_crypto_positions(
        self,
        candidates: List[Dict],
        available_balance: float,
        prices: Optional[Dict[str, float]] = None
    ) -> List[Dict]:
        """
        Calculate position sizes for crypto candidates.
        
        Args:
            candidates: List of {"pair": "BTC/USD"} or {"pair": "BTC/USD", "amount": 0.5}
            available_balance: Current balance in paper ledger
            prices: Optional dict of {"BTC/USD": 65000.00} to calculate amounts immediately
        
        Returns:
            List of {"pair": "BTC/USD", "amount": 0.1538, "estimated_value": 10000.00, "position_pct": 0.10}
        """
        # Use crypto-specific override if set, else global
        position_pct = settings.CRYPTO_POSITION_SIZE_PCT or self.global_pct
        min_position = settings.CRYPTO_MIN_POSITION_USD or self.min_position
        
        # Validate candidate count
        if len(candidates) > self.max_positions:
            logger.warning(
                f"Too many crypto candidates ({len(candidates)} > {self.max_positions}). "
                f"Using first {self.max_positions}."
            )
            candidates = candidates[:self.max_positions]
        
        positions = []
        
        for candidate in candidates:
            pair = candidate['pair']
            
            # Check if amount already specified (legacy format)
            if 'amount' in candidate and candidate['amount']:
                amount = candidate['amount']
                # Estimate value if price provided
                if prices and pair in prices:
                    estimated_value = amount * prices[pair]
                else:
                    estimated_value = None
                
                positions.append({
                    'pair': pair,
                    'amount': amount,
                    'estimated_value': estimated_value,
                    'position_pct': None,  # Not calculated
                    'source': 'specified'
                })
                continue
            
            # Calculate position value
            if self.fixed_amount:
                position_value = self.fixed_amount
            else:
                position_value = available_balance * position_pct
            
            # Safety checks
            if position_value < min_position:
                logger.warning(
                    f"{pair}: Position too small (${position_value:.2f} < ${min_position:.2f}). Skipping."
                )
                continue
            
            max_position_value = available_balance * self.max_position_pct
            if position_value > max_position_value:
                logger.warning(
                    f"{pair}: Position too large (${position_value:.2f} > ${max_position_value:.2f}). "
                    f"Capping at {self.max_position_pct*100:.0f}%"
                )
                position_value = max_position_value
            
            # Calculate crypto amount if price provided
            crypto_amount = None
            if prices and pair in prices:
                price = prices[pair]
                if price > 0:
                    crypto_amount = position_value / price
            
            positions.append({
                'pair': pair,
                'amount': crypto_amount,  # None if price not provided
                'estimated_value': position_value,
                'position_pct': position_pct,
                'source': 'calculated'
            })
        
        return positions
    
    def validate_candidate_count(self, candidates: List) -> Tuple[bool, str]:
        """Check if candidate count is within limits"""
        if len(candidates) == 0:
            return True, "No candidates to validate"
        
        if len(candidates) > self.max_positions:
            return False, f"Too many positions ({len(candidates)} > {self.max_positions})"
        
        return True, "OK"
    
    def get_position_summary(self, positions: List[Dict], asset_type: str = "stock") -> str:
        """Generate summary string for positions"""
        if not positions:
            return "No valid positions after safety checks"
        
        lines = []
        total_value = 0
        
        for pos in positions:
            if asset_type == "stock":
                ticker = pos['ticker']
                shares = pos.get('shares', '?')
                value = pos.get('estimated_value', 0)
                pct = pos.get('position_pct', 0)
                
                if pct:
                    lines.append(f"• {ticker}: {shares} shares (${value:,.2f}, {pct*100:.0f}%)")
                else:
                    lines.append(f"• {ticker}: {shares} shares")
                
                if value:
                    total_value += value
            else:  # crypto
                pair = pos['pair']
                amount = pos.get('amount', '?')
                value = pos.get('estimated_value', 0)
                pct = pos.get('position_pct', 0)
                
                if pct:
                    lines.append(f"• {pair}: {amount:.4f} (${value:,.2f}, {pct*100:.0f}%)")
                else:
                    lines.append(f"• {pair}: {amount}")
                
                if value:
                    total_value += value
        
        summary = "\n".join(lines)
        if total_value > 0:
            summary += f"\n\nTotal allocation: ${total_value:,.2f}"
        
        return summary


# Global instance
position_sizer = PositionSizer()
