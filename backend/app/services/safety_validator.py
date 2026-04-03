from __future__ import annotations

import logging
from datetime import UTC, datetime, time as dt_time
from typing import Any, Dict
from zoneinfo import ZoneInfo

from sqlalchemy import func

from app.core.config import settings
from app.models.trade import Trade
from app.services.runtime_state import runtime_state

logger = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')


class SafetyValidator:
    """Validates trading decisions against safety rules."""

    async def validate(
        self,
        decision: Dict[str, Any],
        account: Dict[str, Any],
        db,
        *,
        account_id: str,
        asset_class: str = 'stock',
    ) -> Dict[str, Any]:
        return self.validate_sync(
            decision,
            account,
            db,
            account_id=account_id,
            asset_class=asset_class,
        )

    def validate_sync(
        self,
        decision: Dict[str, Any],
        account: Dict[str, Any],
        db,
        *,
        account_id: str,
        asset_class: str = 'stock',
    ) -> Dict[str, Any]:
        candidates = decision.get('candidates', [])

        if len(candidates) > settings.SAFETY_MAX_TRADES_PER_DAY:
            return self._fail(f"Too many trades in decision ({len(candidates)})")

        today_utc = datetime.now(UTC).date()

        trades_today = db.query(Trade).filter(
            Trade.account_id == account_id,
            func.date(Trade.entry_time) == today_utc,
        ).count()
        if trades_today >= settings.SAFETY_MAX_TRADES_PER_DAY:
            return self._fail(
                f"Daily trade limit reached ({trades_today}/{settings.SAFETY_MAX_TRADES_PER_DAY})"
            )

        daily_pnl = db.query(func.sum(Trade.net_pnl)).filter(
            Trade.account_id == account_id,
            func.date(Trade.entry_time) == today_utc,
        ).scalar() or 0.0
        if daily_pnl <= -settings.SAFETY_MAX_DAILY_LOSS:
            return self._fail(f"Daily loss limit hit (${daily_pnl:.2f})")

        enforce_vix = bool(decision.get('enforce_vix', False))
        raw_vix = decision.get('vix')
        if asset_class == 'stock' and enforce_vix and raw_vix in (None, ''):
            return self._fail('VIX unavailable during stock safety validation')
        try:
            vix = float(raw_vix) if raw_vix not in (None, '') else 0.0
        except (TypeError, ValueError):
            if asset_class == 'stock' and enforce_vix:
                return self._fail('VIX unavailable during stock safety validation')
            vix = 0.0
        if vix > settings.SAFETY_VIX_MAX:
            return self._fail(f"VIX too high ({vix:.1f} > {settings.SAFETY_VIX_MAX})")

        require_market_hours = bool(
            decision.get('require_market_hours', runtime_state.get().safety_require_market_hours)
        )
        session_open_hint = decision.get('marketSessionOpen', decision.get('sessionOpen'))
        if session_open_hint is not None:
            session_is_open = bool(session_open_hint)
        else:
            session_is_open = self._is_market_hours_et()

        if asset_class == 'stock' and require_market_hours and not session_is_open:
            return self._fail('Market is closed')

        account_cash = float(account.get('cash') or account.get('buyingPower') or account.get('portfolioValue') or 0)
        max_position = account_cash * settings.SAFETY_MAX_POSITION_SIZE_PCT

        for candidate in candidates:
            quantity = self._candidate_quantity(candidate)
            if quantity <= 0:
                continue

            est_price = float(candidate.get('price') or 0)
            position_value = float(candidate.get('estimated_value') or (est_price * quantity))
            if position_value > max_position:
                label = candidate.get('ticker') or candidate.get('pair') or 'UNKNOWN'
                return self._fail(
                    f"{label} position too large (${position_value:,.0f} > ${max_position:,.0f})"
                )

        return {'safe': True}

    @staticmethod
    def _candidate_quantity(candidate: Dict[str, Any]) -> float:
        for field in ('shares', 'amount', 'quantity'):
            try:
                value = float(candidate.get(field) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return 0.0

    def _fail(self, reason: str) -> Dict[str, Any]:
        return {'safe': False, 'reason': reason}

    def _is_market_hours_et(self) -> bool:
        now_et = datetime.now(ET)
        market_open = dt_time(9, 30)
        market_close = dt_time(16, 0)
        if now_et.weekday() >= 5:
            return False
        return market_open <= now_et.time() <= market_close

safety_validator = SafetyValidator()