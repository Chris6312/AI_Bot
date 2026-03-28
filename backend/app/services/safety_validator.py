import logging
from datetime import datetime, time as dt_time
from typing import Dict
from app.core.config import settings
from app.models.trade import Trade
from app.models.position import Position
from sqlalchemy import func

logger = logging.getLogger(__name__)

class SafetyValidator:
    """Validates trading decisions against safety rules"""
    
    async def validate(self, decision: Dict, account: Dict, db) -> Dict:
        """Run all safety checks"""
        
        candidates = decision.get('candidates', [])
        
        # Check 1: Trade count per decision
        if len(candidates) > settings.SAFETY_MAX_TRADES_PER_DAY:
            return self._fail(f"Too many trades in decision ({len(candidates)})")
        
        # Check 2: Daily trade limit
        trades_today = db.query(Trade).filter(
            Trade.account_id == settings.TRADIER_ACCOUNT_ID,
            func.date(Trade.entry_time) == datetime.utcnow().date()
        ).count()
        
        if trades_today >= settings.SAFETY_MAX_TRADES_PER_DAY:
            return self._fail(f"Daily trade limit reached ({trades_today}/{settings.SAFETY_MAX_TRADES_PER_DAY})")
        
        # Check 3: Daily loss limit
        daily_pnl = db.query(func.sum(Trade.net_pnl)).filter(
            Trade.account_id == settings.TRADIER_ACCOUNT_ID,
            func.date(Trade.entry_time) == datetime.utcnow().date()
        ).scalar() or 0.0
        
        if daily_pnl <= -settings.SAFETY_MAX_DAILY_LOSS:
            return self._fail(f"Daily loss limit hit (${daily_pnl:.2f})")
        
        # Check 4: VIX threshold
        vix = decision.get('vix', 0)
        if vix > settings.SAFETY_VIX_MAX:
            return self._fail(f"VIX too high ({vix:.1f} > {settings.SAFETY_VIX_MAX})")
        
        # Check 5: Market hours
        if settings.SAFETY_REQUIRE_MARKET_HOURS:
            if not self._is_market_hours():
                return self._fail("Market is closed")
        
        # Check 6: Position sizing
        balances = account.get('balances', {})
        equity = balances.get('total_equity', 0)
        
        for candidate in candidates:
            # Estimate position value (rough - will use actual quote in execution)
            est_price = candidate.get('price', 100)
            position_value = est_price * candidate['shares']
            max_position = equity * settings.SAFETY_MAX_POSITION_SIZE_PCT
            
            if position_value > max_position:
                return self._fail(
                    f"{candidate['ticker']} position too large "
                    f"(${position_value:,.0f} > ${max_position:,.0f})"
                )
        
        return {'safe': True}
    
    def _fail(self, reason: str):
        return {'safe': False, 'reason': reason}
    
    def _is_market_hours(self) -> bool:
        """Check if market is currently open"""
        now = datetime.now().time()
        market_open = dt_time(9, 30)
        market_close = dt_time(16, 0)
        
        # Simple check - doesn't account for holidays
        weekday = datetime.now().weekday()
        if weekday >= 5:  # Weekend
            return False
        
        return market_open <= now <= market_close
