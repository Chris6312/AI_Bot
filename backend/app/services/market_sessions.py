from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

WATCHLIST_SCOPE = Literal['stocks_only', 'crypto_only']

ET = ZoneInfo('America/New_York')
STOCK_MARKET_OPEN_ET = time(9, 30)
STOCK_MARKET_CLOSE_ET = time(16, 0)
MONITORING_OFFSET_SECONDS = 20


@dataclass(frozen=True)
class ScopeSessionStatus:
    scope: WATCHLIST_SCOPE
    observed_at_utc: datetime
    session_open: bool
    reason: str
    next_session_start_utc: datetime | None = None
    next_session_start_et: datetime | None = None
    session_close_utc: datetime | None = None
    session_close_et: datetime | None = None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            'scope': self.scope,
            'observedAtUtc': self.observed_at_utc.isoformat(),
            'sessionOpen': self.session_open,
            'reason': self.reason,
            'nextSessionStartUtc': self.next_session_start_utc.isoformat() if self.next_session_start_utc else None,
            'nextSessionStartEt': self.next_session_start_et.isoformat() if self.next_session_start_et else None,
            'sessionCloseUtc': self.session_close_utc.isoformat() if self.session_close_utc else None,
            'sessionCloseEt': self.session_close_et.isoformat() if self.session_close_et else None,
        }


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def get_scope_session_status(scope: WATCHLIST_SCOPE, observed_at: datetime) -> ScopeSessionStatus:
    observed_at_utc = normalize_utc(observed_at)
    if scope == 'crypto_only':
        return ScopeSessionStatus(
            scope=scope,
            observed_at_utc=observed_at_utc,
            session_open=True,
            reason='Crypto monitoring is always on.',
        )

    observed_at_et = observed_at_utc.astimezone(ET)
    open_et = datetime.combine(observed_at_et.date(), STOCK_MARKET_OPEN_ET, tzinfo=ET)
    close_et = datetime.combine(observed_at_et.date(), STOCK_MARKET_CLOSE_ET, tzinfo=ET)
    weekday = observed_at_et.weekday() < 5
    session_open = weekday and open_et <= observed_at_et <= close_et
    if session_open:
        return ScopeSessionStatus(
            scope=scope,
            observed_at_utc=observed_at_utc,
            session_open=True,
            reason='Stock monitoring is inside the regular ET market session.',
            session_close_utc=close_et.astimezone(UTC),
            session_close_et=close_et,
        )

    next_open_et = _next_stock_session_open_et(observed_at_et)
    reason = 'Stock monitoring is outside the regular ET market session.'
    if observed_at_et.weekday() >= 5:
        reason = 'Stock monitoring is paused for the weekend.'
    elif observed_at_et < open_et:
        reason = 'Stock monitoring is waiting for the regular ET market open.'
    elif observed_at_et > close_et:
        reason = 'Stock monitoring is paused after the regular ET market close.'

    return ScopeSessionStatus(
        scope=scope,
        observed_at_utc=observed_at_utc,
        session_open=False,
        reason=reason,
        next_session_start_utc=next_open_et.astimezone(UTC),
        next_session_start_et=next_open_et,
        session_close_utc=close_et.astimezone(UTC),
        session_close_et=close_et,
    )


def is_scope_session_open(scope: WATCHLIST_SCOPE, observed_at: datetime) -> bool:
    return get_scope_session_status(scope, observed_at).session_open


def calculate_next_scope_evaluation_at(
    scope: WATCHLIST_SCOPE,
    reference_time: datetime,
    interval_seconds: int | None,
) -> datetime | None:
    if interval_seconds is None:
        return None

    base_time_utc = normalize_utc(reference_time)
    if scope == 'crypto_only':
        return _align_to_next_interval(base_time_utc, interval_seconds)

    session = get_scope_session_status(scope, base_time_utc)
    if not session.session_open:
        if session.next_session_start_utc is None:
            return None
        return session.next_session_start_utc + timedelta(seconds=MONITORING_OFFSET_SECONDS)

    candidate = _align_to_next_interval(base_time_utc, interval_seconds)
    if session.session_close_utc is None:
        return candidate
    if candidate <= session.session_close_utc:
        return candidate

    next_open_et = _next_stock_session_open_et(base_time_utc.astimezone(ET) + timedelta(days=1))
    return next_open_et.astimezone(UTC) + timedelta(seconds=MONITORING_OFFSET_SECONDS)



def _align_to_next_interval(reference_time: datetime, interval_seconds: int) -> datetime:
    epoch = int(normalize_utc(reference_time).timestamp())
    next_boundary = ((epoch // interval_seconds) + 1) * interval_seconds
    return datetime.fromtimestamp(next_boundary + MONITORING_OFFSET_SECONDS, tz=UTC)



def _next_stock_session_open_et(reference_time_et: datetime) -> datetime:
    cursor_date = reference_time_et.date()
    open_today = datetime.combine(cursor_date, STOCK_MARKET_OPEN_ET, tzinfo=ET)
    if reference_time_et.weekday() < 5 and reference_time_et < open_today:
        return open_today

    cursor_date = cursor_date + timedelta(days=1)
    while cursor_date.weekday() >= 5:
        cursor_date = cursor_date + timedelta(days=1)
    return datetime.combine(cursor_date, STOCK_MARKET_OPEN_ET, tzinfo=ET)
