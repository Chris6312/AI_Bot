from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.services.kraken_service import kraken_service
from app.services.market_sessions import calculate_next_scope_evaluation_at
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

TIMEFRAME_TO_KRAKEN_INTERVAL = {
    '5m': 5,
    '15m': 15,
    '1h': 60,
    '4h': 240,
    '1d': 1440,
}


@dataclass
class TemplateEvaluationResult:
    state: str
    reason: str
    market_data_at_utc: datetime | None
    details: dict[str, Any]


class TemplateEvaluationService:
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
        last_price = self._safe_float(quote.get('last') or quote.get('close'))
        prev_close = self._safe_float(quote.get('prevclose') or quote.get('close') or quote.get('open'))
        open_price = self._safe_float(quote.get('open') or prev_close)
        volume = self._safe_float(quote.get('volume'))
        change_pct = 0.0
        if prev_close > 0:
            change_pct = ((last_price - prev_close) / prev_close) * 100.0

        details = {
            'mode': mode,
            'currentPrice': last_price,
            'prevClose': prev_close,
            'openPrice': open_price,
            'changePct': round(change_pct, 4),
            'volume': volume,
            'quoteAgeSeconds': round(quote_age_seconds, 3),
            'template': row.setup_template,
            'bias': row.bias,
            'riskFlags': row.risk_flags or [],
        }
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

        return self._evaluate_template(
            template=row.setup_template,
            bias=row.bias,
            metrics={
                'last_price': last_price,
                'prev_close': prev_close,
                'open_price': open_price,
                'change_pct': change_pct,
                'volume': volume,
            },
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
            limit=25,
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
        continuity = self._check_candle_continuity(candles, self._kraken_interval_for_row(row))
        last_close = float(candles[-1]['close'])
        prev_close = float(candles[-2]['close'])
        open_price = float(candles[-1]['open'])
        recent_high = max(float(item['high']) for item in candles[-5:])
        recent_low = min(float(item['low']) for item in candles[-5:])
        sma5 = sum(float(item['close']) for item in candles[-5:]) / 5.0
        sma10_source = candles[-10:] if len(candles) >= 10 else candles
        sma10 = sum(float(item['close']) for item in sma10_source) / float(len(sma10_source))
        change_pct = 0.0
        if prev_close > 0:
            change_pct = ((last_close - prev_close) / prev_close) * 100.0

        details = {
            'pair': pair,
            'ohlcvPair': ohlcv_pair,
            'currentPrice': last_close,
            'prevClose': prev_close,
            'openPrice': open_price,
            'changePct': round(change_pct, 4),
            'recentHigh': recent_high,
            'recentLow': recent_low,
            'sma5': round(sma5, 6),
            'sma10': round(sma10, 6),
            'tickerAgeSeconds': round(ticker_age_seconds, 3),
            'continuityOk': continuity['ok'],
            'continuityGapSeconds': continuity['max_gap_seconds'],
            'template': row.setup_template,
            'bias': row.bias,
            'riskFlags': row.risk_flags or [],
        }
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

        return self._evaluate_template(
            template=row.setup_template,
            bias=row.bias,
            metrics={
                'last_price': last_close,
                'prev_close': prev_close,
                'open_price': open_price,
                'change_pct': change_pct,
                'recent_high': recent_high,
                'recent_low': recent_low,
                'sma5': sma5,
                'sma10': sma10,
            },
            market_data_at_utc=market_timestamp,
            details=details,
        )

    def _evaluate_template(
        self,
        *,
        template: str,
        bias: str,
        metrics: dict[str, float],
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
            is_ready = change_pct >= (0.5 + threshold_bias) and last_price >= max(sma5, prev_close)
            reason = 'Momentum is continuing above reference trend anchors.' if is_ready else 'Trend continuation thresholds are not met.'
        elif template == 'breakout_retest':
            trigger_level = recent_high * 0.995
            is_ready = change_pct >= (0.8 + threshold_bias) and last_price >= trigger_level and last_price >= open_price
            reason = 'Price is pressing prior range highs with follow-through.' if is_ready else 'Breakout retest conditions are not confirmed.'
            details['triggerLevel'] = round(trigger_level, 6)
        elif template == 'pullback_reclaim':
            is_ready = last_price >= max(open_price, sma5) and change_pct >= (-0.25 - threshold_bias)
            reason = 'Pullback reclaim is back above short-term reference levels.' if is_ready else 'Pullback reclaim is still below reclaim thresholds.'
        elif template == 'mean_reversion_bounce':
            bounce_floor = min(recent_low, sma10)
            is_ready = change_pct >= (-1.5 - threshold_bias) and last_price >= bounce_floor and last_price >= open_price
            reason = 'Mean reversion bounce is stabilizing off recent pressure.' if is_ready else 'Mean reversion bounce has not stabilized yet.'
            details['bounceFloor'] = round(bounce_floor, 6)
        elif template == 'range_breakout':
            breakout_level = recent_high * 0.998
            is_ready = last_price >= breakout_level and change_pct >= (0.6 + threshold_bias)
            reason = 'Price is escaping the recent range with enough velocity.' if is_ready else 'Range breakout conditions are not met.'
            details['breakoutLevel'] = round(breakout_level, 6)
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
