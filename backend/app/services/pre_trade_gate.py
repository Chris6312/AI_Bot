from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.control_plane import get_execution_gate_status
from app.services.kraken_service import kraken_service
from app.services.runtime_visibility import runtime_visibility_service
from app.services.safety_validator import SafetyValidator
from app.services.trade_validator import trade_validator
from app.services.tradier_client import tradier_client


@dataclass
class PreTradeGateCheck:
    name: str
    passed: bool
    reason: str = ''
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreTradeGateDecision:
    allowed: bool
    asset_class: str
    symbol: str
    state: str
    rejection_reason: str = ''
    checks: list[PreTradeGateCheck] = field(default_factory=list)
    market_data: dict[str, Any] = field(default_factory=dict)
    risk_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            'allowed': self.allowed,
            'assetClass': self.asset_class,
            'symbol': self.symbol,
            'state': self.state,
            'rejectionReason': self.rejection_reason,
            'checks': [asdict(check) for check in self.checks],
            'marketData': self.market_data,
            'riskData': self.risk_data,
        }


class PreTradeGateService:
    def __init__(self) -> None:
        self.safety = SafetyValidator()

    def _record_decision(
        self,
        decision: PreTradeGateDecision,
        *,
        execution_source: str,
        context: dict[str, Any] | None = None,
    ) -> PreTradeGateDecision:
        runtime_visibility_service.record_gate_decision(
            decision,
            execution_source=execution_source,
            context=context,
        )
        return decision

    async def evaluate_stock_order(
        self,
        *,
        ticker: str,
        shares: int,
        mode: str,
        account: dict[str, Any],
        db: Session,
        execution_source: str,
        decision_context: dict[str, Any] | None = None,
    ) -> PreTradeGateDecision:
        return self.evaluate_stock_order_sync(
            ticker=ticker,
            shares=shares,
            mode=mode,
            account=account,
            db=db,
            execution_source=execution_source,
            decision_context=decision_context,
        )

    def evaluate_stock_order_sync(
        self,
        *,
        ticker: str,
        shares: int,
        mode: str,
        account: dict[str, Any],
        db: Session,
        execution_source: str,
        decision_context: dict[str, Any] | None = None,
    ) -> PreTradeGateDecision:
        symbol = str(ticker or '').upper().strip()
        selected_mode = str(mode or 'PAPER').upper()
        context = {
            'mode': selected_mode,
            'requestedQuantity': int(shares or 0),
            'decisionContext': decision_context or {},
        }
        checks: list[PreTradeGateCheck] = []

        control_gate = get_execution_gate_status()
        checks.append(
            PreTradeGateCheck(
                name='control_plane',
                passed=control_gate.allowed,
                reason='' if control_gate.allowed else control_gate.reason,
                details={'state': control_gate.state},
            )
        )
        if not control_gate.allowed:
            return self._reject(
                'stock',
                symbol,
                control_gate.state,
                checks,
                control_gate.reason,
                execution_source=execution_source,
                context=context,
            )

        broker_ready = tradier_client.is_ready(selected_mode)
        checks.append(
            PreTradeGateCheck(
                name='broker_ready',
                passed=broker_ready,
                reason='' if broker_ready else f'Tradier {selected_mode} credentials are not configured.',
                details={'mode': selected_mode},
            )
        )
        if not broker_ready:
            return self._reject(
                'stock',
                symbol,
                'REJECTED',
                checks,
                checks[-1].reason,
                execution_source=execution_source,
                context=context,
            )

        quote = tradier_client.get_quote_sync(symbol, mode=selected_mode)
        validation = trade_validator.validate_stock_trade_with_quote(symbol, shares, selected_mode, quote=quote)
        checks.append(
            PreTradeGateCheck(
                name='symbol_and_quote_validation',
                passed=validation['valid'],
                reason='' if validation['valid'] else validation['reason'],
                details={
                    'currentPrice': validation.get('price'),
                    'volume': validation.get('volume'),
                    'spreadPct': validation.get('spread_pct'),
                },
            )
        )
        if not validation['valid']:
            return self._reject(
                'stock',
                symbol,
                'REJECTED',
                checks,
                validation['reason'],
                execution_source=execution_source,
                context=context,
            )

        quote_age_seconds = float(validation.get('quote_age_seconds') or 0.0)
        quote_fresh = quote_age_seconds <= float(settings.PRE_TRADE_STOCK_QUOTE_MAX_AGE_SECONDS)
        checks.append(
            PreTradeGateCheck(
                name='quote_freshness',
                passed=quote_fresh,
                reason=(
                    ''
                    if quote_fresh
                    else (
                        f'Stock quote is stale ({quote_age_seconds:.1f}s > '
                        f'{settings.PRE_TRADE_STOCK_QUOTE_MAX_AGE_SECONDS}s)'
                    )
                ),
                details={
                    'quoteAgeSeconds': quote_age_seconds,
                    'fetchedAtUtc': validation.get('quote_fetched_at'),
                },
            )
        )
        if not quote_fresh:
            return self._reject(
                'stock',
                symbol,
                'REJECTED',
                checks,
                checks[-1].reason,
                execution_source=execution_source,
                context=context,
            )

        estimated_value = float(validation.get('trade_value') or 0.0)
        account_id = str(
            account.get('accountId')
            or account.get('account_id')
            or tradier_client._credentials_for_mode(selected_mode)['account_id']
            or 'TRADIER'
        )
        session_open_hint = None
        session_payload = (decision_context or {}).get('session') if isinstance(decision_context, dict) else None
        if isinstance(decision_context, dict):
            if 'marketSessionOpen' in decision_context:
                session_open_hint = bool(decision_context.get('marketSessionOpen'))
            elif 'sessionOpen' in decision_context:
                session_open_hint = bool(decision_context.get('sessionOpen'))
            elif isinstance(session_payload, dict) and 'sessionOpen' in session_payload:
                session_open_hint = bool(session_payload.get('sessionOpen'))

        safety_payload = {
            'candidates': [
                {
                    'ticker': symbol,
                    'shares': int(shares or 0),
                    'estimated_value': estimated_value,
                    'price': validation.get('price'),
                }
            ],
            'vix': (decision_context or {}).get('vix'),
            'enforce_vix': bool(selected_mode == 'LIVE'),
            'require_market_hours': bool(selected_mode == 'LIVE'),
            'marketSessionOpen': session_open_hint,
        }
        safety_result = self.safety.validate_sync(
            safety_payload,
            account,
            db,
            account_id=account_id,
            asset_class='stock',
        )
        safety_ok = bool(safety_result.get('safe'))
        checks.append(
            PreTradeGateCheck(
                name='safety_budget',
                passed=safety_ok,
                reason='' if safety_ok else str(safety_result.get('reason') or 'Safety validation failed'),
                details={'accountId': account_id, 'executionSource': execution_source},
            )
        )
        if not safety_ok:
            return self._reject(
                'stock',
                symbol,
                'REJECTED',
                checks,
                checks[-1].reason,
                execution_source=execution_source,
                context=context,
            )

        return self._record_decision(
            PreTradeGateDecision(
                allowed=True,
                asset_class='stock',
                symbol=symbol,
                state='READY',
                checks=checks,
                market_data={
                    'currentPrice': validation.get('price'),
                    'quoteFetchedAtUtc': validation.get('quote_fetched_at'),
                    'quoteAgeSeconds': quote_age_seconds,
                    'volume': validation.get('volume'),
                    'spreadPct': validation.get('spread_pct'),
                },
                risk_data={
                    'estimatedValue': estimated_value,
                    'mode': selected_mode,
                    'accountId': account_id,
                },
            ),
            execution_source=execution_source,
            context=context,
        )

    async def evaluate_crypto_order(
        self,
        *,
        pair: str,
        amount: float,
        account: dict[str, Any],
        db: Session,
        execution_source: str,
        decision_context: dict[str, Any] | None = None,
    ) -> PreTradeGateDecision:
        symbol = str(pair or '').upper().strip()
        context = {
            'requestedAmount': float(amount or 0.0),
            'decisionContext': decision_context or {},
        }
        checks: list[PreTradeGateCheck] = []

        control_gate = get_execution_gate_status()
        checks.append(
            PreTradeGateCheck(
                name='control_plane',
                passed=control_gate.allowed,
                reason='' if control_gate.allowed else control_gate.reason,
                details={'state': control_gate.state},
            )
        )
        if not control_gate.allowed:
            return self._reject(
                'crypto',
                symbol,
                control_gate.state,
                checks,
                control_gate.reason,
                execution_source=execution_source,
                context=context,
            )

        resolved_pair = kraken_service.resolve_pair(symbol)
        supported_pair = resolved_pair is not None
        checks.append(
            PreTradeGateCheck(
                name='symbol_resolution',
                passed=supported_pair,
                reason='' if supported_pair else f'{symbol} is not in the current Kraken AssetPairs universe.',
                details={
                    'pair': symbol,
                    'resolvedDisplayPair': resolved_pair.display_pair if resolved_pair else None,
                    'ohlcvPair': resolved_pair.rest_pair if resolved_pair else None,
                },
            )
        )
        if not supported_pair or resolved_pair is None:
            return self._reject(
                'crypto',
                symbol,
                'REJECTED',
                checks,
                checks[-1].reason,
                execution_source=execution_source,
                context=context,
            )

        ohlcv_pair = resolved_pair.rest_pair
        ticker = kraken_service.get_ticker(ohlcv_pair)
        candles = kraken_service.get_ohlc(ohlcv_pair, interval=5, limit=trade_validator.crypto_min_candles_required)
        validation = trade_validator.validate_crypto_trade_with_market_data(symbol, amount, ticker=ticker, candles=candles)
        checks.append(
            PreTradeGateCheck(
                name='market_validation',
                passed=validation['valid'],
                reason='' if validation['valid'] else validation['reason'],
                details={
                    'currentPrice': validation.get('price'),
                    'volumeUsd24h': validation.get('volume_usd'),
                    'spreadPct': validation.get('spread_pct'),
                },
            )
        )
        if not validation['valid']:
            return self._reject(
                'crypto',
                symbol,
                'REJECTED',
                checks,
                validation['reason'],
                execution_source=execution_source,
                context=context,
            )

        ticker_age_seconds = float(validation.get('ticker_age_seconds') or 0.0)
        ticker_fresh = ticker_age_seconds <= float(settings.PRE_TRADE_CRYPTO_TICKER_MAX_AGE_SECONDS)
        checks.append(
            PreTradeGateCheck(
                name='ticker_freshness',
                passed=ticker_fresh,
                reason=(
                    ''
                    if ticker_fresh
                    else (
                        f'Crypto ticker is stale ({ticker_age_seconds:.1f}s > '
                        f'{settings.PRE_TRADE_CRYPTO_TICKER_MAX_AGE_SECONDS}s)'
                    )
                ),
                details={
                    'tickerAgeSeconds': ticker_age_seconds,
                    'fetchedAtUtc': validation.get('ticker_fetched_at'),
                },
            )
        )
        if not ticker_fresh:
            return self._reject(
                'crypto',
                symbol,
                'REJECTED',
                checks,
                checks[-1].reason,
                execution_source=execution_source,
                context=context,
            )

        continuity = self._validate_candle_continuity(candles, interval_minutes=5)
        checks.append(
            PreTradeGateCheck(
                name='candle_continuity',
                passed=continuity['valid'],
                reason='' if continuity['valid'] else continuity['reason'],
                details={
                    'candleCount': len(candles),
                    'largestGapSeconds': continuity['largest_gap_seconds'],
                },
            )
        )
        if not continuity['valid']:
            return self._reject(
                'crypto',
                symbol,
                'REJECTED',
                checks,
                continuity['reason'],
                execution_source=execution_source,
                context=context,
            )

        estimated_value = float(validation.get('trade_value') or 0.0)
        safety_payload = {
            'candidates': [
                {
                    'pair': symbol,
                    'amount': float(amount or 0.0),
                    'estimated_value': estimated_value,
                    'price': validation.get('price'),
                }
            ],
            'vix': (decision_context or {}).get('vix'),
        }
        safety_result = await self.safety.validate(
            safety_payload,
            account,
            db,
            account_id='CRYPTO_PAPER',
            asset_class='crypto',
        )
        safety_ok = bool(safety_result.get('safe'))
        checks.append(
            PreTradeGateCheck(
                name='safety_budget',
                passed=safety_ok,
                reason='' if safety_ok else str(safety_result.get('reason') or 'Safety validation failed'),
                details={'executionSource': execution_source},
            )
        )
        if not safety_ok:
            return self._reject(
                'crypto',
                symbol,
                'REJECTED',
                checks,
                checks[-1].reason,
                execution_source=execution_source,
                context=context,
            )

        return self._record_decision(
            PreTradeGateDecision(
                allowed=True,
                asset_class='crypto',
                symbol=symbol,
                state='READY',
                checks=checks,
                market_data={
                    'currentPrice': validation.get('price'),
                    'tickerFetchedAtUtc': validation.get('ticker_fetched_at'),
                    'tickerAgeSeconds': ticker_age_seconds,
                    'volumeUsd24h': validation.get('volume_usd'),
                    'spreadPct': validation.get('spread_pct'),
                    'ohlcvPair': ohlcv_pair,
                },
                risk_data={
                    'estimatedValue': estimated_value,
                    'accountId': 'CRYPTO_PAPER',
                },
            ),
            execution_source=execution_source,
            context=context,
        )

    def _validate_candle_continuity(self, candles: list[dict[str, Any]], *, interval_minutes: int) -> dict[str, Any]:
        if len(candles) < trade_validator.crypto_min_candles_required:
            return {
                'valid': False,
                'reason': (
                    f'Insufficient historical data: {len(candles)} candles '
                    f'(need {trade_validator.crypto_min_candles_required})'
                ),
                'largest_gap_seconds': None,
            }

        timestamps = [int(candle.get('timestamp') or 0) for candle in candles if candle.get('timestamp')]
        if len(timestamps) != len(candles):
            return {'valid': False, 'reason': 'One or more candles are missing timestamps.', 'largest_gap_seconds': None}
        if timestamps != sorted(timestamps):
            return {'valid': False, 'reason': 'Candles are not sorted by timestamp.', 'largest_gap_seconds': None}
        if len(set(timestamps)) != len(timestamps):
            return {'valid': False, 'reason': 'Duplicate candle timestamps detected.', 'largest_gap_seconds': None}

        expected_gap = max(int(interval_minutes * 60), 1)
        max_allowed_gap = int(expected_gap * float(settings.PRE_TRADE_CRYPTO_MAX_CANDLE_GAP_FACTOR))
        largest_gap = 0
        for previous, current in zip(timestamps, timestamps[1:]):
            gap = current - previous
            largest_gap = max(largest_gap, gap)
            if gap > max_allowed_gap:
                return {
                    'valid': False,
                    'reason': f'Candle continuity broken ({gap}s gap > {max_allowed_gap}s threshold)',
                    'largest_gap_seconds': gap,
                }

        return {'valid': True, 'reason': '', 'largest_gap_seconds': largest_gap}

    def _reject(
        self,
        asset_class: str,
        symbol: str,
        state: str,
        checks: list[PreTradeGateCheck],
        reason: str,
        *,
        execution_source: str,
        context: dict[str, Any] | None = None,
    ) -> PreTradeGateDecision:
        return self._record_decision(
            PreTradeGateDecision(
                allowed=False,
                asset_class=asset_class,
                symbol=symbol,
                state=state,
                rejection_reason=reason,
                checks=checks,
            ),
            execution_source=execution_source,
            context=context,
        )


pre_trade_gate = PreTradeGateService()
