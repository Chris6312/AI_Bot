from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

WATCHLIST_SCOPE = Literal['stocks_only', 'crypto_only']

ET = ZoneInfo('America/New_York')
STOCK_MARKET_OPEN_ET = time(9, 30)
STOCK_MARKET_CLOSE_ET = time(16, 0)
STOCK_MARKET_EARLY_CLOSE_ET = time(13, 0)
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
    session_window = get_stock_session_window(observed_at_et)
    if session_window is not None:
        open_et, close_et = session_window
        session_open = open_et <= observed_at_et <= close_et
        if session_open:
            close_label = 'early close' if close_et.timetz().replace(tzinfo=None) == STOCK_MARKET_EARLY_CLOSE_ET else 'regular close'
            return ScopeSessionStatus(
                scope=scope,
                observed_at_utc=observed_at_utc,
                session_open=True,
                reason=f'Stock monitoring is inside the ET market session ({close_label}).',
                session_close_utc=close_et.astimezone(UTC),
                session_close_et=close_et,
            )

    next_open_et = _next_stock_session_open_et(observed_at_et)
    next_window = get_stock_session_window(next_open_et)
    next_close_et = next_window[1] if next_window is not None else None

    if session_window is None:
        holiday_name = get_stock_market_holiday_name(observed_at_et.date())
        if holiday_name is not None:
            reason = f'Stock monitoring is paused for {holiday_name}.'
        elif observed_at_et.weekday() >= 5:
            reason = 'Stock monitoring is paused for the weekend.'
        else:
            reason = 'Stock monitoring is outside the ET market session.'
    else:
        open_et, close_et = session_window
        if observed_at_et < open_et:
            if close_et.timetz().replace(tzinfo=None) == STOCK_MARKET_EARLY_CLOSE_ET:
                reason = 'Stock monitoring is waiting for the ET market open before an early-close session.'
            else:
                reason = 'Stock monitoring is waiting for the regular ET market open.'
        else:
            if close_et.timetz().replace(tzinfo=None) == STOCK_MARKET_EARLY_CLOSE_ET:
                reason = 'Stock monitoring is paused after the ET early close.'
            else:
                reason = 'Stock monitoring is paused after the regular ET market close.'

    current_close_et = close_et if session_window else None

    return ScopeSessionStatus(
        scope=scope,
        observed_at_utc=observed_at_utc,
        session_open=False,
        reason=reason,
        next_session_start_utc=next_open_et.astimezone(UTC),
        next_session_start_et=next_open_et,
        session_close_utc=current_close_et.astimezone(UTC) if current_close_et else None,
        session_close_et=current_close_et,
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

    next_open_et = _next_stock_session_open_et(base_time_utc.astimezone(ET) + timedelta(seconds=1))
    return next_open_et.astimezone(UTC) + timedelta(seconds=MONITORING_OFFSET_SECONDS)


def get_stock_session_window(observed_at: datetime | date) -> tuple[datetime, datetime] | None:
    if isinstance(observed_at, datetime):
        observed_at_et = observed_at.astimezone(ET) if observed_at.tzinfo else observed_at.replace(tzinfo=ET)
        target_date = observed_at_et.date()
    else:
        target_date = observed_at

    if target_date.weekday() >= 5:
        return None
    if get_stock_market_holiday_name(target_date) is not None:
        return None

    close_time = get_stock_early_close_time(target_date) or STOCK_MARKET_CLOSE_ET
    return (
        datetime.combine(target_date, STOCK_MARKET_OPEN_ET, tzinfo=ET),
        datetime.combine(target_date, close_time, tzinfo=ET),
    )


def get_stock_market_holiday_name(target_date: date) -> str | None:
    year = target_date.year
    holidays = {
        _observed_date(date(year, 1, 1)): "New Year's Day",
        _nth_weekday_of_month(year, 1, 0, 3): 'Martin Luther King Jr. Day',
        _nth_weekday_of_month(year, 2, 0, 3): "Presidents' Day",
        _good_friday(year): 'Good Friday',
        _last_weekday_of_month(year, 5, 0): 'Memorial Day',
        _observed_date(date(year, 6, 19)): 'Juneteenth',
        _observed_date(date(year, 7, 4)): 'Independence Day',
        _nth_weekday_of_month(year, 9, 0, 1): 'Labor Day',
        _nth_weekday_of_month(year, 11, 3, 4): 'Thanksgiving Day',
        _observed_date(date(year, 12, 25)): 'Christmas Day',
    }
    return holidays.get(target_date)


def get_stock_early_close_time(target_date: date) -> time | None:
    if target_date.weekday() >= 5 or get_stock_market_holiday_name(target_date) is not None:
        return None

    thanksgiving = _nth_weekday_of_month(target_date.year, 11, 3, 4)
    if target_date == thanksgiving + timedelta(days=1):
        return STOCK_MARKET_EARLY_CLOSE_ET

    christmas_eve = date(target_date.year, 12, 24)
    if christmas_eve.weekday() < 5 and get_stock_market_holiday_name(christmas_eve) is None and target_date == christmas_eve:
        return STOCK_MARKET_EARLY_CLOSE_ET

    independence_day = date(target_date.year, 7, 4)
    july_3 = date(target_date.year, 7, 3)
    if target_date == july_3 and july_3.weekday() < 5:
        if independence_day.weekday() not in {5, 6}:
            return STOCK_MARKET_EARLY_CLOSE_ET

    return None


def _align_to_next_interval(reference_time: datetime, interval_seconds: int) -> datetime:
    epoch = int(normalize_utc(reference_time).timestamp())
    next_boundary = ((epoch // interval_seconds) + 1) * interval_seconds
    return datetime.fromtimestamp(next_boundary + MONITORING_OFFSET_SECONDS, tz=UTC)


def _next_stock_session_open_et(reference_time_et: datetime) -> datetime:
    cursor = reference_time_et.astimezone(ET)
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=ET)

    while True:
        session_window = get_stock_session_window(cursor)
        if session_window is not None:
            open_et, close_et = session_window
            if cursor <= open_et:
                return open_et
            if cursor <= close_et:
                return open_et
        cursor = datetime.combine(cursor.date() + timedelta(days=1), time(0, 0), tzinfo=ET)


def _observed_date(actual_date: date) -> date:
    if actual_date.weekday() == 5:
        return actual_date - timedelta(days=1)
    if actual_date.weekday() == 6:
        return actual_date + timedelta(days=1)
    return actual_date


def _nth_weekday_of_month(year: int, month: int, weekday: int, occurrence: int) -> date:
    cursor = date(year, month, 1)
    while cursor.weekday() != weekday:
        cursor += timedelta(days=1)
    cursor += timedelta(weeks=occurrence - 1)
    return cursor


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _good_friday(year: int) -> date:
    return _easter_sunday(year) - timedelta(days=2)
