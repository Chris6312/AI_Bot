from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.services.kraken_service import kraken_service
from app.services.market_sessions import ET, STOCK_MARKET_CLOSE_ET, STOCK_MARKET_OPEN_ET, calculate_next_scope_evaluation_at
from app.services.runtime_state import runtime_state
from app.services.trade_validator import trade_validator
from app.services.tradier_client import tradier_client
from app.services.watchlist_service import (
    ACTIVE,
    INACTIVE,
    MANAGED_ONLY,
    MONITOR_ONLY,
    PENDING_EVALUATION,
    WATCHLIST_SCOPE,
    watchlist_service,
)

DATA_UNAVAILABLE = 'DATA_UNAVAILABLE'
DATA_STALE = 'DATA_STALE'
WAITING_FOR_SETUP = 'WAITING_FOR_SETUP'
ENTRY_CANDIDATE = 'ENTRY_CANDIDATE'
BIAS_CONFLICT = 'BIAS_CONFLICT'
EVALUATION_BLOCKED = 'EVALUATION_BLOCKED'
SKIPPED = 'SKIPPED'

TIMEFRAME_TO_KRAKEN_INTERVAL = {
    '5m': 5,
    '15m': 15,
    '1h': 60,
    '4h': 240,
    '1d': 1440,
}

MIN_COMPLETED_CANDLES = 50
PREFERRED_COMPLETED_CANDLES = 50
STOCK_CANDLE_LOOKBACK_DAYS = 5
TREND_VOLUME_CONFIRMATION_MULTIPLIER = 1.1
BREAKOUT_VOLUME_CONFIRMATION_MULTIPLIER = 1.2
PHASE2_VOLUME_CONFIRMATION_MULTIPLIER = 1.05
MEAN_REVERSION_VOLUME_CONFIRMATION_MULTIPLIER = 1.0
STOCK_TREND_EXTENSION_ATR_MULTIPLIER = 1.25
CRYPTO_TREND_EXTENSION_ATR_MULTIPLIER = 1.5
STOCK_BREAKOUT_RANGE_ATR_MULTIPLIER = 1.25
CRYPTO_BREAKOUT_RANGE_ATR_MULTIPLIER = 1.5
STOCK_PULLBACK_DEPTH_ATR_MULTIPLIER = 1.2
CRYPTO_PULLBACK_DEPTH_ATR_MULTIPLIER = 1.8
STOCK_BREAKOUT_RETEST_ATR_MULTIPLIER = 1.0
CRYPTO_BREAKOUT_RETEST_ATR_MULTIPLIER = 1.5
STOCK_PULLBACK_SMA10_TOLERANCE_ATR_MULTIPLIER = 0.25
CRYPTO_PULLBACK_SMA10_TOLERANCE_ATR_MULTIPLIER = 0.5
STOCK_MEAN_REVERSION_DEVIATION_RATIO = 1.2
CRYPTO_MEAN_REVERSION_DEVIATION_RATIO = 1.5


def _coerce_candle_float(value: Any) -> float | None:
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_candle_timestamp(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value), tz=UTC)
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    result = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _normalize_candle_series(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_by_timestamp: dict[int, dict[str, Any]] = {}
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        timestamp = _coerce_candle_timestamp(
            candle.get('timestamp') or candle.get('time') or candle.get('datetime') or candle.get('date')
        )
        open_price = _coerce_candle_float(candle.get('open'))
        high_price = _coerce_candle_float(candle.get('high'))
        low_price = _coerce_candle_float(candle.get('low'))
        close_price = _coerce_candle_float(candle.get('close'))
        if timestamp is None or None in {open_price, high_price, low_price, close_price}:
            continue
        if high_price < max(open_price, close_price) or low_price > min(open_price, close_price):
            continue
        volume = _coerce_candle_float(candle.get('volume'))
        normalized_by_timestamp[int(timestamp.timestamp())] = {
            'timestamp': int(timestamp.timestamp()),
            'datetime': timestamp,
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'volume': volume,
        }
    return [normalized_by_timestamp[key] for key in sorted(normalized_by_timestamp)]


def build_candle_metrics(candles: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = _normalize_candle_series(candles)
    signal_candle = ordered[-1] if ordered else None
    previous_candle = ordered[-2] if len(ordered) >= 2 else None
    details: dict[str, Any] = {
        'ready': False,
        'candleCount': len(ordered),
        'recent_high': None,
        'recent_low': None,
        'sma5': None,
        'sma10': None,
        'sma20': None,
        'avg_volume_10': None,
        'range_high': None,
        'range_low': None,
        'atr14': None,
        'last_price': signal_candle['close'] if signal_candle is not None else None,
        'prev_close': previous_candle['close'] if previous_candle is not None else None,
        'open_price': signal_candle['open'] if signal_candle is not None else None,
        'last_high': signal_candle['high'] if signal_candle is not None else None,
        'last_low': signal_candle['low'] if signal_candle is not None else None,
        'latest_volume': signal_candle['volume'] if signal_candle is not None else None,
        'signalAtUtc': signal_candle['datetime'] if signal_candle is not None else None,
        'price_deviation': None,
        'price_deviation_ratio': None,
        'recent_swing_low': None,
        'reversal_signal': None,
    }
    if len(ordered) < MIN_COMPLETED_CANDLES:
        return details

    structure_source = ordered[:-1] if len(ordered) >= 6 else ordered
    structure_window = structure_source[-5:]
    if len(structure_window) < 5:
        return details

    sma5_window = ordered[-5:]
    sma10_window = ordered[-10:]
    sma20_window = ordered[-20:]
    recent_high = max(item['high'] for item in structure_window)
    recent_low = min(item['low'] for item in structure_window)
    sma5 = sum(item['close'] for item in sma5_window) / 5.0
    sma10 = sum(item['close'] for item in sma10_window) / 10.0
    sma20 = sum(item['close'] for item in sma20_window) / 20.0
    recent_swing_low = min(item['low'] for item in sma5_window)

    volume_window = ordered[-10:]
    volume_values = [item['volume'] for item in volume_window if item['volume'] is not None]
    avg_volume_10 = (sum(volume_values) / 10.0) if len(volume_values) == 10 else None

    true_ranges: list[float] = []
    for index in range(1, len(ordered)):
        current = ordered[index]
        previous_close = ordered[index - 1]['close']
        true_ranges.append(
            max(
                current['high'] - current['low'],
                abs(current['high'] - previous_close),
                abs(current['low'] - previous_close),
            )
        )
    atr14 = (sum(true_ranges[-14:]) / 14.0) if len(true_ranges) >= 14 else None
    last_price = signal_candle['close'] if signal_candle is not None else None
    open_price = signal_candle['open'] if signal_candle is not None else None
    prev_close = previous_candle['close'] if previous_candle is not None else None
    price_deviation = (last_price - sma20) if last_price is not None else None
    price_deviation_ratio = (abs(price_deviation) / atr14) if price_deviation is not None and atr14 else None
    reversal_signal = bool(
        signal_candle is not None
        and previous_candle is not None
        and signal_candle['close'] > previous_candle['close']
        and signal_candle['close'] > float(open_price or 0.0)
    )

    details.update(
        {
            'ready': atr14 is not None and sma20 is not None,
            'recent_high': recent_high,
            'recent_low': recent_low,
            'sma5': sma5,
            'sma10': sma10,
            'sma20': sma20,
            'avg_volume_10': avg_volume_10,
            'range_high': recent_high,
            'range_low': recent_low,
            'atr14': atr14,
            'range_width': recent_high - recent_low,
            'price_deviation': price_deviation,
            'price_deviation_ratio': price_deviation_ratio,
            'recent_swing_low': recent_swing_low,
            'reversal_signal': reversal_signal,
        }
    )
    return details


@dataclass
class TemplateEvaluationResult:
    state: str
    reason: str
    market_data_at_utc: datetime | None
    details: dict[str, Any]


class TemplateEvaluationService:
    @staticmethod
    def _ensure_utc(value: datetime | None) -> datetime:
        result = value or datetime.now(UTC)
        if result.tzinfo is None:
            result = result.replace(tzinfo=UTC)
        return result.astimezone(UTC)

    def evaluate_scope(
        self,
        db: Session,
        *,
        scope: WATCHLIST_SCOPE,
        limit: int = 25,
        force: bool = False,
        eligible_statuses: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        watchlist_service._backfill_missing_monitor_states(db, scope=scope, observed_at=observed_at)

        query = (
            db.query(WatchlistMonitorState, WatchlistSymbol)
            .join(WatchlistSymbol, WatchlistSymbol.id == WatchlistMonitorState.watchlist_symbol_id)
            .filter(WatchlistMonitorState.scope == scope)
        )
        if eligible_statuses is not None:
            query = query.filter(WatchlistMonitorState.monitoring_status.in_(eligible_statuses))
        if not force:
            query = query.filter(
                (WatchlistMonitorState.next_evaluation_at_utc.is_(None))
                | (WatchlistMonitorState.next_evaluation_at_utc <= observed_at)
                | (WatchlistMonitorState.latest_decision_state == PENDING_EVALUATION)
            )
        pairs = query.order_by(
            WatchlistMonitorState.monitoring_status.asc(),
            WatchlistSymbol.priority_rank.asc(),
            WatchlistSymbol.id.asc(),
        ).limit(max(1, int(limit))).all()

        summary_counts = {
            ENTRY_CANDIDATE: 0,
            WAITING_FOR_SETUP: 0,
            DATA_STALE: 0,
            DATA_UNAVAILABLE: 0,
            MONITOR_ONLY: 0,
            INACTIVE: 0,
            BIAS_CONFLICT: 0,
            EVALUATION_BLOCKED: 0,
            SKIPPED: 0,
        }
        rows: list[dict[str, Any]] = []
        changed = False
        for monitor_state, symbol_row in pairs:
            result = self._evaluate_row(symbol_row)
            summary_counts[result.state] = summary_counts.get(result.state, 0) + 1
            changed = self._apply_result(monitor_state, symbol_row, result, observed_at=observed_at) or changed
            rows.append(
                {
                    'symbol': symbol_row.symbol,
                    'scope': symbol_row.scope,
                    'monitoringStatus': symbol_row.monitoring_status,
                    'latestDecisionState': monitor_state.latest_decision_state,
                    'latestDecisionReason': monitor_state.latest_decision_reason,
                    'lastEvaluatedAtUtc': monitor_state.last_evaluated_at_utc.isoformat() if monitor_state.last_evaluated_at_utc else None,
                    'nextEvaluationAtUtc': monitor_state.next_evaluation_at_utc.isoformat() if monitor_state.next_evaluation_at_utc else None,
                    'lastMarketDataAtUtc': monitor_state.last_market_data_at_utc.isoformat() if monitor_state.last_market_data_at_utc else None,
                }
            )

        if changed:
            db.commit()
        else:
            db.rollback()

        active_snapshot = watchlist_service.get_monitoring_snapshot(db, scope=scope, include_inactive=False)
        return {
            'scope': scope,
            'capturedAtUtc': observed_at.isoformat(),
            'evaluatedCount': len(rows),
            'summary': {
                'entryCandidateCount': summary_counts.get(ENTRY_CANDIDATE, 0),
                'waitingForSetupCount': summary_counts.get(WAITING_FOR_SETUP, 0),
                'dataStaleCount': summary_counts.get(DATA_STALE, 0),
                'dataUnavailableCount': summary_counts.get(DATA_UNAVAILABLE, 0),
                'monitorOnlyCount': summary_counts.get(MONITOR_ONLY, 0),
                'inactiveCount': summary_counts.get(INACTIVE, 0),
                'biasConflictCount': summary_counts.get(BIAS_CONFLICT, 0),
                'evaluationBlockedCount': summary_counts.get(EVALUATION_BLOCKED, 0),
                'skippedCount': summary_counts.get(SKIPPED, 0),
            },
            'rows': rows,
            'monitoringSnapshot': active_snapshot,
        }

    def _evaluate_row(self, row: WatchlistSymbol) -> TemplateEvaluationResult:
        if row.monitoring_status == INACTIVE:
            return TemplateEvaluationResult(
                state=INACTIVE,
                reason='Symbol is inactive and not scheduled for evaluation.',
                market_data_at_utc=None,
                details={'template': row.setup_template},
            )
        if row.monitoring_status == MANAGED_ONLY:
            return TemplateEvaluationResult(
                state=MONITOR_ONLY,
                reason='Symbol is managed-only and restricted to exit monitoring.',
                market_data_at_utc=None,
                details={'template': row.setup_template},
            )
        if not row.enabled:
            return TemplateEvaluationResult(
                state=EVALUATION_BLOCKED,
                reason='Watchlist row is disabled.',
                market_data_at_utc=None,
                details={'enabled': False},
            )
        if str(row.trade_direction or '').lower() != 'long':
            return TemplateEvaluationResult(
                state=EVALUATION_BLOCKED,
                reason='Initial template runner only supports long monitoring.',
                market_data_at_utc=None,
                details={'tradeDirection': row.trade_direction},
            )
        if str(row.bias or '').lower() == 'bearish':
            return TemplateEvaluationResult(
                state=BIAS_CONFLICT,
                reason='Bearish bias does not arm long entries in the initial runner.',
                market_data_at_utc=None,
                details={'bias': row.bias},
            )

        if row.scope == 'crypto_only' and watchlist_service._has_open_crypto_position(row.symbol, row.quote_currency):
            pair = f"{str(row.symbol).upper()}/{str(row.quote_currency).upper()}"
            return TemplateEvaluationResult(
                state=SKIPPED,
                reason='OPEN_POSITION_EXISTS',
                market_data_at_utc=None,
                details={'pair': pair, 'template': row.setup_template},
            )

        if row.scope == 'stocks_only':
            return self._evaluate_stock_row(row)
        return self._evaluate_crypto_row(row)

    def _evaluate_stock_row(self, row: WatchlistSymbol) -> TemplateEvaluationResult:
        mode = runtime_state.get().stock_mode
        quote = tradier_client.get_quote_sync(str(row.symbol).upper(), mode=mode)
        if not quote:
            return TemplateEvaluationResult(
                state=DATA_UNAVAILABLE,
                reason='Tradier quote is unavailable for monitoring.',
                market_data_at_utc=None,
                details={'mode': mode},
            )

        market_timestamp = trade_validator._extract_market_timestamp(quote)
        quote_age_seconds = trade_validator._market_age_seconds(market_timestamp)
        interval_minutes = self._stock_interval_for_row(row)
        raw_candles = tradier_client.get_timesales_sync(
            str(row.symbol).upper(),
            mode=mode,
            interval_minutes=interval_minutes,
            start=self._ensure_utc(market_timestamp) - timedelta(days=STOCK_CANDLE_LOOKBACK_DAYS),
            end=self._ensure_utc(market_timestamp),
            session_filter='open',
        )
        completed_candles = self._completed_candles(
            raw_candles,
            interval_minutes=interval_minutes,
            reference_time=market_timestamp,
        )
        metrics = build_candle_metrics(completed_candles)

        last_price = self._safe_float(metrics.get('last_price'))
        prev_close = self._safe_float(metrics.get('prev_close'))
        open_price = self._safe_float(metrics.get('open_price'))
        volume = self._safe_float(metrics.get('latest_volume'))
        change_pct = self._calculate_change_pct(last_price, prev_close)

        details = {
            'mode': mode,
            'currentPrice': last_price,
            'prevClose': prev_close,
            'openPrice': open_price,
            'changePct': round(change_pct, 4),
            'volume': volume,
            'quoteAgeSeconds': round(quote_age_seconds, 3),
            'quoteLastPrice': self._safe_float(quote.get('last') or quote.get('close')),
            'candleIntervalMinutes': interval_minutes,
            'completedCandleCount': len(completed_candles),
            'rawCandleCount': len(raw_candles),
            'template': row.setup_template,
            'bias': row.bias,
            'riskFlags': row.risk_flags or [],
        }
        self._merge_candle_details(details, metrics)
        if market_timestamp:
            details['marketDataAtUtc'] = market_timestamp.isoformat()
        if quote_age_seconds > float(settings.PRE_TRADE_STOCK_QUOTE_MAX_AGE_SECONDS):
            return TemplateEvaluationResult(
                state=DATA_STALE,
                reason=(
                    f'Stock quote is stale ({quote_age_seconds:.1f}s > '
                    f'{settings.PRE_TRADE_STOCK_QUOTE_MAX_AGE_SECONDS}s).'
                ),
                market_data_at_utc=market_timestamp,
                details=details,
            )

        if not raw_candles:
            return TemplateEvaluationResult(
                state=DATA_UNAVAILABLE,
                reason='Tradier candle history is unavailable for monitoring.',
                market_data_at_utc=market_timestamp,
                details=details,
            )
        if len(completed_candles) < MIN_COMPLETED_CANDLES:
            return TemplateEvaluationResult(
                state=WAITING_FOR_SETUP,
                reason=(
                    f'Not ready: completed stock candle history is below the required minimum '
                    f'({len(completed_candles)}/{MIN_COMPLETED_CANDLES}).'
                ),
                market_data_at_utc=market_timestamp,
                details=details,
            )
        if not metrics.get('ready'):
            return TemplateEvaluationResult(
                state=WAITING_FOR_SETUP,
                reason='Not ready: ATR14 could not be derived from completed stock candles.',
                market_data_at_utc=market_timestamp,
                details=details,
            )

        return self._evaluate_template(
            template=row.setup_template,
            bias=row.bias,
            metrics={
                'last_price': last_price,
                'prev_close': prev_close,
                'open_price': open_price,
                'change_pct': change_pct,
                **metrics,
            },
            scope='stocks_only',
            candles=completed_candles,
            interval_minutes=interval_minutes,
            market_data_at_utc=market_timestamp,
            details=details,
        )

    def _evaluate_crypto_row(self, row: WatchlistSymbol) -> TemplateEvaluationResult:
        pair = f"{str(row.symbol).upper()}/{str(row.quote_currency).upper()}"
        resolved_pair = kraken_service.resolve_pair(pair)
        ohlcv_pair = resolved_pair.rest_pair if resolved_pair is not None else None
        if not ohlcv_pair:
            return TemplateEvaluationResult(
                state=DATA_UNAVAILABLE,
                reason=f'Crypto pair {pair} is not in the current Kraken AssetPairs universe.',
                market_data_at_utc=None,
                details={'pair': pair},
            )

        ticker = kraken_service.get_ticker(ohlcv_pair)
        candles = kraken_service.get_ohlc(
            ohlcv_pair,
            interval=self._kraken_interval_for_row(row),
            limit=PREFERRED_COMPLETED_CANDLES + 10,
        )
        if not ticker or 'c' not in ticker or len(candles) < 5:
            return TemplateEvaluationResult(
                state=DATA_UNAVAILABLE,
                reason='Kraken ticker or candle history is unavailable for monitoring.',
                market_data_at_utc=None,
                details={'pair': pair, 'ohlcvPair': ohlcv_pair, 'candleCount': len(candles)},
            )

        market_timestamp = trade_validator._extract_market_timestamp(ticker)
        ticker_age_seconds = trade_validator._market_age_seconds(market_timestamp)
        interval_minutes = self._kraken_interval_for_row(row)
        continuity = self._check_candle_continuity(candles, interval_minutes)
        completed_candles = self._completed_candles(
            candles,
            interval_minutes=interval_minutes,
            reference_time=market_timestamp,
        )
        metrics = build_candle_metrics(completed_candles)
        last_close = self._safe_float(metrics.get('last_price'))
        prev_close = self._safe_float(metrics.get('prev_close'))
        open_price = self._safe_float(metrics.get('open_price'))
        change_pct = self._calculate_change_pct(last_close, prev_close)

        details = {
            'pair': pair,
            'ohlcvPair': ohlcv_pair,
            'currentPrice': last_close,
            'prevClose': prev_close,
            'openPrice': open_price,
            'changePct': round(change_pct, 4),
            'tickerAgeSeconds': round(ticker_age_seconds, 3),
            'continuityOk': continuity['ok'],
            'continuityGapSeconds': continuity['max_gap_seconds'],
            'candleIntervalMinutes': interval_minutes,
            'completedCandleCount': len(completed_candles),
            'rawCandleCount': len(candles),
            'template': row.setup_template,
            'bias': row.bias,
            'riskFlags': row.risk_flags or [],
        }
        self._merge_candle_details(details, metrics)
        if market_timestamp:
            details['marketDataAtUtc'] = market_timestamp.isoformat()
        if ticker_age_seconds > float(settings.PRE_TRADE_CRYPTO_TICKER_MAX_AGE_SECONDS):
            return TemplateEvaluationResult(
                state=DATA_STALE,
                reason=(
                    f'Crypto ticker is stale ({ticker_age_seconds:.1f}s > '
                    f'{settings.PRE_TRADE_CRYPTO_TICKER_MAX_AGE_SECONDS}s).'
                ),
                market_data_at_utc=market_timestamp,
                details=details,
            )
        if not continuity['ok']:
            return TemplateEvaluationResult(
                state=DATA_STALE,
                reason='Crypto candle continuity check failed for the monitoring timeframe.',
                market_data_at_utc=market_timestamp,
                details=details,
            )
        if len(completed_candles) < MIN_COMPLETED_CANDLES:
            return TemplateEvaluationResult(
                state=WAITING_FOR_SETUP,
                reason=(
                    f'Not ready: completed crypto candle history is below the required minimum '
                    f'({len(completed_candles)}/{MIN_COMPLETED_CANDLES}).'
                ),
                market_data_at_utc=market_timestamp,
                details=details,
            )
        if not metrics.get('ready'):
            return TemplateEvaluationResult(
                state=WAITING_FOR_SETUP,
                reason='Not ready: ATR14 could not be derived from completed crypto candles.',
                market_data_at_utc=market_timestamp,
                details=details,
            )

        return self._evaluate_template(
            template=row.setup_template,
            bias=row.bias,
            metrics={
                'last_price': last_close,
                'prev_close': prev_close,
                'open_price': open_price,
                'change_pct': change_pct,
                **metrics,
            },
            scope='crypto_only',
            candles=completed_candles,
            interval_minutes=interval_minutes,
            market_data_at_utc=market_timestamp,
            details=details,
        )

    def _evaluate_template(
        self,
        *,
        template: str,
        bias: str,
        metrics: dict[str, Any],
        scope: str,
        candles: list[dict[str, Any]],
        interval_minutes: int,
        market_data_at_utc: datetime | None,
        details: dict[str, Any],
    ) -> TemplateEvaluationResult:
        if bias == 'neutral':
            threshold_bias = 0.35
        else:
            threshold_bias = 0.0

        last_price = float(metrics.get('last_price') or 0.0)
        prev_close = float(metrics.get('prev_close') or 0.0)
        open_price = float(metrics.get('open_price') or 0.0)
        change_pct = float(metrics.get('change_pct') or 0.0)
        recent_high = float(metrics.get('recent_high') or max(last_price, prev_close, open_price))
        recent_low = float(metrics.get('recent_low') or min(last_price, prev_close, open_price))
        sma5 = float(metrics.get('sma5') or last_price)
        sma10 = float(metrics.get('sma10') or prev_close or last_price)

        is_ready = False
        reason = 'Template conditions are not ready yet.'

        if template == 'trend_continuation':
            is_ready, reason = self._evaluate_trend_continuation(metrics=metrics, scope=scope, candles=candles, details=details)
        elif template == 'breakout_retest':
            is_ready, reason = self._evaluate_breakout_retest(
                metrics=metrics,
                scope=scope,
                candles=candles,
                interval_minutes=interval_minutes,
                details=details,
            )
        elif template == 'pullback_reclaim':
            is_ready, reason = self._evaluate_pullback_reclaim(
                metrics=metrics,
                scope=scope,
                candles=candles,
                interval_minutes=interval_minutes,
                details=details,
            )
        elif template == 'mean_reversion_bounce':
            is_ready, reason = self._evaluate_mean_reversion_bounce(
                metrics=metrics,
                scope=scope,
                candles=candles,
                details=details,
            )
        elif template == 'range_breakout':
            is_ready, reason = self._evaluate_range_breakout(
                metrics=metrics,
                scope=scope,
                candles=candles,
                interval_minutes=interval_minutes,
                details=details,
            )
        else:
            return TemplateEvaluationResult(
                state=EVALUATION_BLOCKED,
                reason=f'Unsupported template for evaluation: {template}',
                market_data_at_utc=market_data_at_utc,
                details=details,
            )

        return TemplateEvaluationResult(
            state=ENTRY_CANDIDATE if is_ready else WAITING_FOR_SETUP,
            reason=reason,
            market_data_at_utc=market_data_at_utc,
            details=details,
        )

    def _evaluate_trend_continuation(
        self,
        *,
        metrics: dict[str, Any],
        scope: str,
        candles: list[dict[str, Any]],
        details: dict[str, Any],
    ) -> tuple[bool, str]:
        atr14 = self._safe_float(metrics.get('atr14'))
        sma5 = self._safe_float(metrics.get('sma5'))
        sma10 = self._safe_float(metrics.get('sma10'))
        last_price = self._safe_float(metrics.get('last_price'))
        prev_close = self._safe_float(metrics.get('prev_close'))
        avg_volume_10 = metrics.get('avg_volume_10')
        latest_volume = metrics.get('latest_volume')

        if atr14 <= 0:
            return False, 'Trend continuation requires ATR14 from completed candles.'

        recent_candles = _normalize_candle_series(candles)[-5:]
        higher_close_count = sum(1 for index in range(1, len(recent_candles)) if recent_candles[index]['close'] > recent_candles[index - 1]['close'])
        higher_low_count = sum(1 for index in range(1, len(recent_candles)) if recent_candles[index]['low'] >= recent_candles[index - 1]['low'])
        net_progress = last_price - recent_candles[0]['close'] if recent_candles else 0.0
        trend_structure_ok = higher_close_count >= 3 and higher_low_count >= 2 and net_progress > (atr14 * 0.25)
        volume_ok = True
        if avg_volume_10 is not None and latest_volume is not None:
            volume_ok = float(latest_volume) >= float(avg_volume_10) * TREND_VOLUME_CONFIRMATION_MULTIPLIER
        extension_limit = atr14 * (STOCK_TREND_EXTENSION_ATR_MULTIPLIER if scope == 'stocks_only' else CRYPTO_TREND_EXTENSION_ATR_MULTIPLIER)
        extension_from_sma5 = last_price - sma5
        not_overextended = extension_from_sma5 <= extension_limit

        details['trendStructureOk'] = trend_structure_ok
        details['trendHigherCloseCount'] = higher_close_count
        details['trendHigherLowCount'] = higher_low_count
        details['trendNetProgress'] = round(net_progress, 6)
        details['trendExtensionFromSma5'] = round(extension_from_sma5, 6)
        details['trendExtensionLimit'] = round(extension_limit, 6)

        if sma5 <= sma10:
            return False, 'Trend continuation rejected: SMA5 is not above SMA10.'
        if last_price <= sma5:
            return False, 'Trend continuation rejected: last completed close is not above SMA5.'
        if prev_close >= last_price:
            return False, 'Trend continuation rejected: last completed close is not above the previous close.'
        if not trend_structure_ok:
            return False, 'Trend continuation rejected: recent completed candle structure is flat or sideways.'
        if not volume_ok:
            return False, 'Trend continuation rejected: breakout volume is below 1.1x average volume.'
        if not not_overextended:
            return False, 'Trend continuation rejected: price is too extended above SMA5 relative to ATR14.'
        return True, 'Trend continuation confirmed from completed candle structure.'

    def _evaluate_mean_reversion_bounce(
        self,
        *,
        metrics: dict[str, Any],
        scope: str,
        candles: list[dict[str, Any]],
        details: dict[str, Any],
    ) -> tuple[bool, str]:
        atr14 = self._safe_float(metrics.get('atr14'))
        sma5 = self._safe_float(metrics.get('sma5'))
        sma10 = self._safe_float(metrics.get('sma10'))
        sma20 = self._safe_float(metrics.get('sma20'))
        last_price = self._safe_float(metrics.get('last_price'))
        prev_close = self._safe_float(metrics.get('prev_close'))
        open_price = self._safe_float(metrics.get('open_price'))
        price_deviation = self._safe_float(metrics.get('price_deviation'))
        price_deviation_ratio = self._safe_float(metrics.get('price_deviation_ratio'))
        recent_swing_low = self._safe_float(metrics.get('recent_swing_low'))
        reversal_signal = bool(metrics.get('reversal_signal'))
        avg_volume_10 = metrics.get('avg_volume_10')
        latest_volume = metrics.get('latest_volume')

        if atr14 <= 0:
            return False, 'Mean reversion bounce requires ATR14 from completed candles.'
        if sma20 <= 0:
            return False, 'Mean reversion bounce requires SMA20 from completed candles.'

        ordered = _normalize_candle_series(candles)
        if len(ordered) < MIN_COMPLETED_CANDLES:
            return False, 'Mean reversion bounce requires at least 50 completed candles.'

        threshold = (
            STOCK_MEAN_REVERSION_DEVIATION_RATIO
            if scope == 'stocks_only'
            else CRYPTO_MEAN_REVERSION_DEVIATION_RATIO
        )
        volume_ok = self._volume_confirmation_ok(
            avg_volume_10=avg_volume_10,
            latest_volume=latest_volume,
            multiplier=MEAN_REVERSION_VOLUME_CONFIRMATION_MULTIPLIER,
        )
        swing_window = ordered[-5:]
        swing_low_offset = min(range(len(swing_window)), key=lambda index: swing_window[index]['low'])
        swing_low_age_candles = (len(swing_window) - 1) - swing_low_offset
        swing_low_formed = swing_low_age_candles >= 1
        recent_closes = [float(candle['close']) for candle in swing_window]
        downward_close_count = sum(
            1 for index in range(1, len(recent_closes)) if recent_closes[index] < recent_closes[index - 1]
        )
        recent_net_momentum = recent_closes[-1] - recent_closes[0]
        trend_collapse = (
            sma5 < (sma10 - (atr14 * 0.2))
            and downward_close_count >= 3
            and recent_net_momentum < -(atr14 * 0.5)
        )
        price_recovery_started = last_price > recent_swing_low and last_price > prev_close and last_price > open_price

        details['meanReversionDeviationRatioThreshold'] = round(threshold, 6)
        details['meanReversionVolumeConfirmationPassed'] = volume_ok
        details['recentSwingLowAgeCandles'] = swing_low_age_candles
        details['meanReversionDownwardCloseCount'] = downward_close_count
        details['meanReversionNetMomentum'] = round(recent_net_momentum, 6)
        details['meanReversionTrendCollapseGuard'] = trend_collapse
        details['meanReversionBelowSma20'] = last_price < sma20

        if last_price >= sma20:
            return False, 'Mean reversion bounce rejected: price is not below SMA20.'
        if price_deviation >= 0:
            return False, 'Mean reversion bounce rejected: price deviation is not oversold.'
        if price_deviation_ratio < threshold:
            return False, 'Mean reversion bounce rejected: price deviation from SMA20 is too small relative to ATR14.'
        if not reversal_signal:
            return False, 'Mean reversion bounce rejected: no bullish reversal candle is present yet.'
        if not swing_low_formed:
            return False, 'Mean reversion bounce rejected: recent swing low has not formed before the signal candle.'
        if not price_recovery_started:
            return False, 'Mean reversion bounce rejected: price is still falling after the oversold deviation.'
        if not volume_ok:
            return False, 'Mean reversion bounce rejected: latest volume is below the 10-candle average.'
        if trend_collapse:
            return False, 'Mean reversion bounce rejected: short-term trend collapse remains too strong.'
        return True, 'Mean reversion bounce confirmed from oversold deviation, reversal candle, and recovery structure.'

    def _evaluate_pullback_reclaim(
        self,
        *,
        metrics: dict[str, Any],
        scope: str,
        candles: list[dict[str, Any]],
        interval_minutes: int,
        details: dict[str, Any],
    ) -> tuple[bool, str]:
        atr14 = self._safe_float(metrics.get('atr14'))
        if atr14 <= 0:
            return False, 'Pullback reclaim requires ATR14 from completed candles.'

        trend_context = self._build_phase2_trend_context(metrics=metrics, candles=candles, details=details)
        if not trend_context['trendAligned']:
            return False, 'Pullback reclaim rejected: trend structure is not aligned (SMA5, SMA10, or slope failed).'

        ordered = _normalize_candle_series(candles)
        breakout_context = self._select_pullback_breakout_context(
            candles=ordered,
            scope=scope,
            interval_minutes=interval_minutes,
            atr14=atr14,
            details=details,
        )
        if breakout_context is None:
            return False, 'Pullback reclaim rejected: prior breakout structure was not found from completed candles.'

        signal_index = len(ordered) - 1
        breakout_index = int(breakout_context['index'])
        breakout_level = float(breakout_context['level'])
        breakout_segment = ordered[breakout_index:signal_index]
        if len(breakout_segment) < 3:
            return False, 'Pullback reclaim rejected: breakout, pullback, and reclaim sequence is incomplete.'

        swing_high_offset = max(range(len(breakout_segment)), key=lambda index: breakout_segment[index]['high'])
        swing_high_index = breakout_index + swing_high_offset
        if swing_high_index >= signal_index - 1:
            return False, 'Pullback reclaim rejected: completed candles do not show a pullback after the breakout high.'

        pullback_segment = ordered[swing_high_index + 1:signal_index]
        if not pullback_segment:
            return False, 'Pullback reclaim rejected: completed pullback candles are missing.'

        pullback_low = min(candle['low'] for candle in pullback_segment)
        reclaim_level = max(self._safe_float(metrics.get('sma5')), self._safe_float(metrics.get('prev_close')))
        pullback_depth = max(0.0, breakout_level - pullback_low)
        pullback_depth_ratio = pullback_depth / atr14
        sma10 = self._safe_float(metrics.get('sma10'))
        last_price = self._safe_float(metrics.get('last_price'))
        prev_close = self._safe_float(metrics.get('prev_close'))
        open_price = self._safe_float(metrics.get('open_price'))
        latest_volume = metrics.get('latest_volume')
        avg_volume_10 = metrics.get('avg_volume_10')
        volume_ok = self._volume_confirmation_ok(
            avg_volume_10=avg_volume_10,
            latest_volume=latest_volume,
            multiplier=PHASE2_VOLUME_CONFIRMATION_MULTIPLIER,
        )
        depth_limit = (
            STOCK_PULLBACK_DEPTH_ATR_MULTIPLIER
            if scope == 'stocks_only'
            else CRYPTO_PULLBACK_DEPTH_ATR_MULTIPLIER
        )
        sma10_tolerance = atr14 * (
            STOCK_PULLBACK_SMA10_TOLERANCE_ATR_MULTIPLIER
            if scope == 'stocks_only'
            else CRYPTO_PULLBACK_SMA10_TOLERANCE_ATR_MULTIPLIER
        )
        sma10_break_distance = max(0.0, sma10 - pullback_low)

        details['priorBreakoutLevel'] = round(breakout_level, 6)
        details['breakoutLevel'] = round(breakout_level, 6)
        details['breakoutSource'] = breakout_context['source']
        details['breakoutCandleAtUtc'] = breakout_context['datetime'].isoformat()
        details['reclaimLevel'] = round(reclaim_level, 6)
        details['pullbackLow'] = round(pullback_low, 6)
        details['pullbackDepth'] = round(pullback_depth, 6)
        details['pullbackDepthRatio'] = round(pullback_depth_ratio, 6)
        details['pullbackDepthRatioLimit'] = round(depth_limit, 6)
        details['pullbackSma10BreakDistance'] = round(sma10_break_distance, 6)
        details['pullbackSma10Tolerance'] = round(sma10_tolerance, 6)
        details['pullbackSwingHigh'] = round(float(ordered[swing_high_index]['high']), 6)
        details['volumeConfirmationPassed'] = volume_ok

        if pullback_depth_ratio > depth_limit:
            return False, 'Pullback reclaim rejected: pullback depth is too large relative to ATR14.'
        if pullback_low < sma10 and sma10_break_distance > sma10_tolerance:
            return False, 'Pullback reclaim rejected: pullback broke too far below SMA10.'
        if last_price <= reclaim_level:
            return False, 'Pullback reclaim rejected: last completed close is not back above the reclaim level.'
        if last_price <= prev_close:
            return False, 'Pullback reclaim rejected: current candle did not close above the prior candle.'
        if last_price <= open_price:
            return False, 'Pullback reclaim rejected: current candle is not directional to the upside.'
        if not volume_ok:
            return False, 'Pullback reclaim rejected: latest volume is below 1.05x average volume.'
        return True, 'Pullback reclaim confirmed from completed-candle trend, pullback, and reclaim structure.'

    def _evaluate_breakout_retest(
        self,
        *,
        metrics: dict[str, Any],
        scope: str,
        candles: list[dict[str, Any]],
        interval_minutes: int,
        details: dict[str, Any],
    ) -> tuple[bool, str]:
        atr14 = self._safe_float(metrics.get('atr14'))
        if atr14 <= 0:
            return False, 'Breakout retest requires ATR14 from completed candles.'

        ordered = _normalize_candle_series(candles)
        breakout_context = self._select_retest_breakout_context(
            candles=ordered,
            scope=scope,
            interval_minutes=interval_minutes,
            atr14=atr14,
            details=details,
        )
        if breakout_context is None:
            return False, 'Breakout retest rejected: prior breakout was not confirmed from completed candles.'

        signal_index = len(ordered) - 1
        breakout_index = int(breakout_context['index'])
        breakout_level = float(breakout_context['level'])
        retest_segment = ordered[breakout_index + 1:signal_index + 1]
        if len(retest_segment) < 2:
            return False, 'Breakout retest rejected: breakout and retest sequence is incomplete.'

        pullback_low = min(candle['low'] for candle in retest_segment)
        lowest_close = min(candle['close'] for candle in retest_segment)
        retest_distance = abs(pullback_low - breakout_level)
        last_price = self._safe_float(metrics.get('last_price'))
        prev_close = self._safe_float(metrics.get('prev_close'))
        open_price = self._safe_float(metrics.get('open_price'))
        avg_volume_10 = metrics.get('avg_volume_10')
        latest_volume = metrics.get('latest_volume')
        volume_ok = self._volume_confirmation_ok(
            avg_volume_10=avg_volume_10,
            latest_volume=latest_volume,
            multiplier=PHASE2_VOLUME_CONFIRMATION_MULTIPLIER,
        )
        retest_limit = atr14 * (
            STOCK_BREAKOUT_RETEST_ATR_MULTIPLIER
            if scope == 'stocks_only'
            else CRYPTO_BREAKOUT_RETEST_ATR_MULTIPLIER
        )
        close_below_limit = breakout_level - retest_limit
        two_closes_above = (
            len(ordered) >= 2
            and ordered[-1]['close'] > breakout_level
            and ordered[-2]['close'] > breakout_level
        )
        trend_context = self._build_phase2_trend_context(metrics=metrics, candles=candles, details=details)

        details['priorBreakoutLevel'] = round(breakout_level, 6)
        details['breakoutLevel'] = round(breakout_level, 6)
        details['breakoutSource'] = breakout_context['source']
        details['breakoutCandleAtUtc'] = breakout_context['datetime'].isoformat()
        details['pullbackLow'] = round(pullback_low, 6)
        details['retestDistance'] = round(retest_distance, 6)
        details['retestDistanceLimit'] = round(retest_limit, 6)
        details['breakoutCloseFloor'] = round(close_below_limit, 6)
        details['twoConsecutiveClosesAboveBreakoutLevel'] = two_closes_above
        details['volumeConfirmationPassed'] = volume_ok

        if lowest_close < close_below_limit:
            return False, 'Breakout retest rejected: post-breakout candles collapsed below the breakout level.'
        if retest_distance > retest_limit:
            return False, 'Breakout retest rejected: retest distance is too large relative to ATR14.'
        if not trend_context['trendAligned']:
            return False, 'Breakout retest rejected: trend alignment is no longer valid.'
        if last_price <= breakout_level:
            return False, 'Breakout retest rejected: last completed close is not back above the breakout level.'
        if last_price <= prev_close:
            return False, 'Breakout retest rejected: current candle did not close above the prior candle.'
        if last_price <= open_price:
            return False, 'Breakout retest rejected: current candle is not directional to the upside.'
        if not volume_ok:
            return False, 'Breakout retest rejected: latest volume is below 1.05x average volume.'
        return True, 'Breakout retest confirmed from completed breakout, retest, and continuation candles.'

    def _build_phase2_trend_context(
        self,
        *,
        metrics: dict[str, Any],
        candles: list[dict[str, Any]],
        details: dict[str, Any],
    ) -> dict[str, Any]:
        ordered = _normalize_candle_series(candles)
        recent_candles = ordered[-5:]
        sma5 = self._safe_float(metrics.get('sma5'))
        sma10 = self._safe_float(metrics.get('sma10'))
        last_price = self._safe_float(metrics.get('last_price'))
        atr14 = self._safe_float(metrics.get('atr14'))
        higher_close_count = sum(
            1
            for index in range(1, len(recent_candles))
            if recent_candles[index]['close'] > recent_candles[index - 1]['close']
        )
        higher_low_count = sum(
            1
            for index in range(1, len(recent_candles))
            if recent_candles[index]['low'] >= recent_candles[index - 1]['low']
        )
        trend_spread = sma5 - sma10
        net_progress = last_price - recent_candles[0]['close'] if recent_candles else 0.0
        trend_not_flat = trend_spread > (atr14 * 0.05) and net_progress > (atr14 * 0.1)
        trend_aligned = sma5 > sma10 and last_price > sma10 and trend_not_flat

        details['phase2TrendHigherCloseCount'] = higher_close_count
        details['phase2TrendHigherLowCount'] = higher_low_count
        details['phase2TrendSpread'] = round(trend_spread, 6)
        details['phase2TrendNetProgress'] = round(net_progress, 6)
        details['phase2TrendNotFlat'] = trend_not_flat
        details['phase2TrendAligned'] = trend_aligned

        return {
            'trendAligned': trend_aligned,
            'trendNotFlat': trend_not_flat,
            'higherCloseCount': higher_close_count,
            'higherLowCount': higher_low_count,
            'trendSpread': trend_spread,
            'netProgress': net_progress,
        }

    def _select_pullback_breakout_context(
        self,
        *,
        candles: list[dict[str, Any]],
        scope: str,
        interval_minutes: int,
        atr14: float,
        details: dict[str, Any],
    ) -> dict[str, Any] | None:
        candidates = self._build_breakout_candidates(
            candles=candles,
            scope=scope,
            interval_minutes=interval_minutes,
            details=details,
        )
        signal_index = len(candles) - 1
        minimum_pullback_distance = atr14 * 0.1
        for candidate in reversed(candidates):
            candidate_index = int(candidate['index'])
            if candidate_index > signal_index - 3:
                continue
            post_breakout_segment = candles[candidate_index + 1:signal_index]
            if not post_breakout_segment:
                continue
            post_breakout_low = min(candle['low'] for candle in post_breakout_segment)
            post_breakout_high = max(candle['high'] for candle in candles[candidate_index:signal_index])
            if (post_breakout_high - post_breakout_low) >= minimum_pullback_distance:
                return candidate
        return None

    def _select_retest_breakout_context(
        self,
        *,
        candles: list[dict[str, Any]],
        scope: str,
        interval_minutes: int,
        atr14: float,
        details: dict[str, Any],
    ) -> dict[str, Any] | None:
        candidates = self._build_breakout_candidates(
            candles=candles,
            scope=scope,
            interval_minutes=interval_minutes,
            details=details,
        )
        signal_index = len(candles) - 1
        for candidate in reversed(candidates):
            candidate_index = int(candidate['index'])
            if candidate_index > signal_index - 2:
                continue
            post_breakout_segment = candles[candidate_index + 1:signal_index + 1]
            if not post_breakout_segment:
                continue
            return candidate
        return None

    def _build_breakout_candidates(
        self,
        *,
        candles: list[dict[str, Any]],
        scope: str,
        interval_minutes: int,
        details: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        signal_index = len(candles) - 1

        if scope == 'stocks_only':
            opening_range = self._get_stock_opening_range_context(candles=candles, interval_minutes=interval_minutes)
            details['openingRangeAvailable'] = opening_range['available']
            if opening_range['available']:
                details['openingRangeHigh'] = round(float(opening_range['high']), 6)
                details['openingRangeLow'] = round(float(opening_range['low']), 6)
                details['openingRangeWidth'] = round(float(opening_range['width']), 6)
                signal_date = candles[signal_index]['datetime'].astimezone(ET).date()
                for index, candle in enumerate(candles[:signal_index]):
                    candle_time_et = candle['datetime'].astimezone(ET)
                    if candle_time_et.date() != signal_date:
                        continue
                    if candle_time_et.time() < time(9, 45):
                        continue
                    if candle['close'] > opening_range['high']:
                        candidates.append(
                            {
                                'index': index,
                                'level': opening_range['high'],
                                'source': 'opening_range_high',
                                'datetime': candle['datetime'],
                            }
                        )
            else:
                details['openingRangeHigh'] = None
                details['openingRangeLow'] = None
                details['openingRangeWidth'] = None

        for index in range(5, signal_index):
            breakout_level = max(candle['high'] for candle in candles[index - 5:index])
            if candles[index]['close'] > breakout_level:
                candidates.append(
                    {
                        'index': index,
                        'level': breakout_level,
                        'source': 'range_high',
                        'datetime': candles[index]['datetime'],
                    }
                )
        return candidates

    def _get_stock_opening_range_context(
        self,
        *,
        candles: list[dict[str, Any]],
        interval_minutes: int,
    ) -> dict[str, Any]:
        if interval_minutes > 15:
            return {'available': False}
        ordered = _normalize_candle_series(candles)
        if len(ordered) < MIN_COMPLETED_CANDLES:
            return {'available': False}
        signal_candle = ordered[-1]
        signal_date_et = signal_candle['datetime'].astimezone(ET).date()
        session_candles = [
            candle
            for candle in ordered
            if candle['datetime'].astimezone(ET).date() == signal_date_et
            and STOCK_MARKET_OPEN_ET <= candle['datetime'].astimezone(ET).time() < STOCK_MARKET_CLOSE_ET
        ]
        opening_range_candles = [
            candle
            for candle in session_candles
            if STOCK_MARKET_OPEN_ET <= candle['datetime'].astimezone(ET).time() < time(9, 45)
        ]
        if not opening_range_candles:
            return {'available': False}
        opening_range_high = max(candle['high'] for candle in opening_range_candles)
        opening_range_low = min(candle['low'] for candle in opening_range_candles)
        return {
            'available': True,
            'high': opening_range_high,
            'low': opening_range_low,
            'width': opening_range_high - opening_range_low,
        }

    @staticmethod
    def _volume_confirmation_ok(*, avg_volume_10: Any, latest_volume: Any, multiplier: float) -> bool:
        if avg_volume_10 is None or latest_volume is None:
            return True
        try:
            return float(latest_volume) >= float(avg_volume_10) * float(multiplier)
        except (TypeError, ValueError):
            return True

    def _evaluate_range_breakout(
        self,
        *,
        metrics: dict[str, Any],
        scope: str,
        candles: list[dict[str, Any]],
        interval_minutes: int,
        details: dict[str, Any],
    ) -> tuple[bool, str]:
        if scope == 'stocks_only':
            orb_ready, orb_reason = self._evaluate_stock_orb_breakout(
                metrics=metrics,
                candles=candles,
                interval_minutes=interval_minutes,
                details=details,
            )
            if details.get('openingRangeAvailable'):
                return orb_ready, orb_reason

        return self._evaluate_consolidation_breakout(metrics=metrics, scope=scope, details=details)

    def _evaluate_stock_orb_breakout(
        self,
        *,
        metrics: dict[str, Any],
        candles: list[dict[str, Any]],
        interval_minutes: int,
        details: dict[str, Any],
    ) -> tuple[bool, str]:
        opening_range = self._get_stock_opening_range_context(candles=candles, interval_minutes=interval_minutes)
        details['openingRangeAvailable'] = opening_range['available']
        if interval_minutes > 15:
            return False, 'Opening range breakout unavailable for stock interval greater than 15 minutes.'
        ordered = _normalize_candle_series(candles)
        if len(ordered) < MIN_COMPLETED_CANDLES:
            return False, 'Opening range breakout requires more completed stock candles.'

        if not opening_range['available']:
            return False, 'Opening range breakout unavailable because the first 15 minutes are not complete.'

        opening_range_high = float(opening_range['high'])
        opening_range_low = float(opening_range['low'])
        opening_range_width = float(opening_range['width'])
        details['openingRangeHigh'] = round(opening_range_high, 6)
        details['openingRangeLow'] = round(opening_range_low, 6)
        details['openingRangeWidth'] = round(opening_range_width, 6)
        details['breakoutLevel'] = round(opening_range_high, 6)

        atr14 = self._safe_float(metrics.get('atr14'))
        last_price = self._safe_float(metrics.get('last_price'))
        avg_volume_10 = metrics.get('avg_volume_10')
        latest_volume = metrics.get('latest_volume')
        last_high = self._safe_float(metrics.get('last_high'))
        volume_ok = True
        if avg_volume_10 is not None and latest_volume is not None:
            volume_ok = float(latest_volume) >= float(avg_volume_10) * BREAKOUT_VOLUME_CONFIRMATION_MULTIPLIER

        if atr14 <= 0:
            return False, 'Stock opening range breakout requires ATR14 from completed candles.'
        if opening_range_width > (atr14 * 1.5):
            return False, 'Stock opening range breakout rejected: opening range is too wide relative to ATR14.'
        if last_high > opening_range_high and last_price <= opening_range_high:
            return False, 'Stock opening range breakout rejected: level was breached intrabar but not closed above.'
        if last_price <= opening_range_high:
            return False, 'Stock opening range breakout rejected: last completed close is not above opening range resistance.'
        if not volume_ok:
            return False, 'Stock opening range breakout rejected: breakout volume is below 1.2x average volume.'
        return True, 'Stock opening range breakout confirmed above first 15-minute resistance.'

    def _evaluate_consolidation_breakout(
        self,
        *,
        metrics: dict[str, Any],
        scope: str,
        details: dict[str, Any],
    ) -> tuple[bool, str]:
        atr14 = self._safe_float(metrics.get('atr14'))
        range_high = self._safe_float(metrics.get('range_high'))
        range_low = self._safe_float(metrics.get('range_low'))
        range_width = self._safe_float(metrics.get('range_width'))
        last_price = self._safe_float(metrics.get('last_price'))
        last_high = self._safe_float(metrics.get('last_high'))
        avg_volume_10 = metrics.get('avg_volume_10')
        latest_volume = metrics.get('latest_volume')
        volume_ok = True
        if avg_volume_10 is not None and latest_volume is not None:
            volume_ok = float(latest_volume) >= float(avg_volume_10) * BREAKOUT_VOLUME_CONFIRMATION_MULTIPLIER

        details['breakoutLevel'] = round(range_high, 6)
        details['rangeWidth'] = round(range_width, 6)

        if atr14 <= 0:
            return False, 'Range breakout requires ATR14 from completed candles.'
        range_limit = atr14 * (STOCK_BREAKOUT_RANGE_ATR_MULTIPLIER if scope == 'stocks_only' else CRYPTO_BREAKOUT_RANGE_ATR_MULTIPLIER)
        details['rangeWidthAtrLimit'] = round(range_limit, 6)
        if range_width > range_limit:
            return False, 'Range breakout rejected: consolidation width is too large relative to ATR14.'
        if last_high > range_high and last_price <= range_high:
            return False, 'Range breakout rejected: level was breached intrabar but not closed above.'
        if last_price <= range_high:
            return False, 'Range breakout rejected: last completed close is not above consolidation resistance.'
        if not volume_ok:
            return False, 'Range breakout rejected: breakout volume is below 1.2x average volume.'
        if scope == 'crypto_only':
            return True, 'Crypto consolidation breakout confirmed above completed-candle resistance.'
        return True, 'Stock consolidation breakout confirmed above completed-candle resistance.'

    def _apply_result(
        self,
        monitor_state: WatchlistMonitorState,
        symbol_row: WatchlistSymbol,
        result: TemplateEvaluationResult,
        *,
        observed_at: datetime,
    ) -> bool:
        changed = False
        next_evaluation_at = None
        if symbol_row.monitoring_status != INACTIVE:
            next_evaluation_at = calculate_next_scope_evaluation_at(
                symbol_row.scope,
                observed_at,
                monitor_state.evaluation_interval_seconds,
            )

        merged_context = dict(monitor_state.decision_context_json or {})
        merged_context['latestEvaluation'] = {
            'state': result.state,
            'reason': result.reason,
            'evaluatedAtUtc': observed_at.isoformat(),
            'marketDataAtUtc': result.market_data_at_utc.isoformat() if result.market_data_at_utc else None,
            'details': result.details,
        }
        merged_context['evaluationVersion'] = 'phase_4_4_initial_runner'

        if monitor_state.latest_decision_state != result.state:
            monitor_state.latest_decision_state = result.state
            changed = True
        if monitor_state.latest_decision_reason != result.reason:
            monitor_state.latest_decision_reason = result.reason
            changed = True
        if monitor_state.last_market_data_at_utc != result.market_data_at_utc:
            monitor_state.last_market_data_at_utc = result.market_data_at_utc
            changed = True
        if monitor_state.next_evaluation_at_utc != next_evaluation_at:
            monitor_state.next_evaluation_at_utc = next_evaluation_at
            changed = True
        if monitor_state.decision_context_json != merged_context:
            monitor_state.decision_context_json = merged_context
            changed = True
        if monitor_state.last_evaluated_at_utc != observed_at:
            monitor_state.last_evaluated_at_utc = observed_at
            changed = True
        if monitor_state.last_decision_at_utc != observed_at:
            monitor_state.last_decision_at_utc = observed_at
            changed = True
        return changed

    @staticmethod
    def _kraken_interval_for_row(row: WatchlistSymbol) -> int:
        intervals = [TIMEFRAME_TO_KRAKEN_INTERVAL[item] for item in (row.bot_timeframes or []) if item in TIMEFRAME_TO_KRAKEN_INTERVAL]
        if not intervals:
            return 15
        return min(intervals)

    @staticmethod
    def _stock_interval_for_row(row: WatchlistSymbol) -> int:
        interval_map = {
            '5m': 5,
            '15m': 15,
            '1h': 60,
            '4h': 60,
            '1d': 60,
        }
        intervals = [interval_map[item] for item in (row.bot_timeframes or []) if item in interval_map]
        if not intervals:
            return 5
        return min(intervals)

    def _completed_candles(
        self,
        candles: list[dict[str, Any]],
        *,
        interval_minutes: int,
        reference_time: datetime | None,
    ) -> list[dict[str, Any]]:
        ordered = _normalize_candle_series(candles)
        if not ordered:
            return []
        reference_utc = self._ensure_utc(reference_time)
        reference_timestamp = int(reference_utc.timestamp())
        candle_span_seconds = max(60, int(interval_minutes) * 60)
        completed = [candle for candle in ordered if candle['timestamp'] + candle_span_seconds <= reference_timestamp]
        return completed[-PREFERRED_COMPLETED_CANDLES:] if len(completed) > PREFERRED_COMPLETED_CANDLES else completed

    @staticmethod
    def _calculate_change_pct(last_price: float, prev_close: float) -> float:
        if prev_close <= 0:
            return 0.0
        return ((last_price - prev_close) / prev_close) * 100.0

    @staticmethod
    def _merge_candle_details(details: dict[str, Any], metrics: dict[str, Any]) -> None:
        for source_key, target_key, decimals in (
            ('recent_high', 'recentHigh', 6),
            ('recent_low', 'recentLow', 6),
            ('sma5', 'sma5', 6),
            ('sma10', 'sma10', 6),
            ('sma20', 'sma20', 6),
            ('avg_volume_10', 'avgVolume10', 6),
            ('range_high', 'rangeHigh', 6),
            ('range_low', 'rangeLow', 6),
            ('range_width', 'rangeWidth', 6),
            ('atr14', 'atr14', 6),
            ('price_deviation', 'priceDeviation', 6),
            ('price_deviation_ratio', 'priceDeviationRatio', 6),
            ('recent_swing_low', 'recentSwingLow', 6),
        ):
            value = metrics.get(source_key)
            details[target_key] = round(float(value), decimals) if value is not None else None
        details['reversalSignal'] = bool(metrics.get('reversal_signal')) if metrics.get('reversal_signal') is not None else None
        signal_at_utc = metrics.get('signalAtUtc')
        details['signalAtUtc'] = signal_at_utc.isoformat() if isinstance(signal_at_utc, datetime) else None

    @staticmethod
    def _check_candle_continuity(candles: list[dict[str, Any]], interval_minutes: int) -> dict[str, Any]:
        expected_gap = max(60, int(interval_minutes) * 60)
        max_allowed_gap = expected_gap * float(settings.PRE_TRADE_CRYPTO_MAX_CANDLE_GAP_FACTOR)
        max_gap = 0
        ordered = sorted(candles, key=lambda item: int(item.get('timestamp') or 0))
        previous_ts = None
        for candle in ordered:
            current_ts = int(candle.get('timestamp') or 0)
            if previous_ts is not None:
                gap = current_ts - previous_ts
                if gap > max_gap:
                    max_gap = gap
            previous_ts = current_ts
        return {
            'ok': max_gap <= max_allowed_gap if max_gap else True,
            'expected_gap_seconds': expected_gap,
            'max_gap_seconds': max_gap,
        }

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0


template_evaluation_service = TemplateEvaluationService()
