"""
Trade Validation Service
Validates trades before execution to catch AI/screening errors
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timezone
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

from app.services.kraken_service import kraken_service
from app.services.market_sessions import get_scope_session_status
from app.services.tradier_client import tradier_client

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


class TradeValidator:
    """Validates trades before execution for both stocks and crypto"""

    def __init__(self):
        self.kraken = kraken_service
        self.tradier = tradier_client

        self.crypto_min_24h_volume_usd = 100000
        self.crypto_max_price_spike_pct = 50.0
        self.crypto_max_spread_pct = 2.0
        self.crypto_min_candles_required = 20

        self.stock_min_price = 0.50
        self.stock_max_price = 10000.0
        self.stock_min_volume = 100000
        self.stock_max_spread_pct = 1.0

    def validate_crypto_trade(self, pair: str, amount: float) -> Tuple[bool, str]:
        result = self.validate_crypto_trade_with_market_data(pair, amount)
        return result['valid'], result['reason']

    def validate_crypto_trade_with_market_data(
        self,
        pair: str,
        amount: float,
        *,
        ticker: dict[str, Any] | None = None,
        candles: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        resolved_pair = self.kraken.resolve_pair(pair)
        if resolved_pair is None:
            return self._result(False, f"❌ {pair} not in Kraken AssetPairs")

        if amount is None or amount <= 0:
            return self._result(False, f"❌ Invalid amount for {pair}")

        ohlcv_pair = resolved_pair.rest_pair

        ticker_payload = ticker or self.kraken.get_ticker(ohlcv_pair)
        if not ticker_payload or "c" not in ticker_payload:
            return self._result(False, f"❌ Cannot fetch current price for {pair}")

        current_price = float(ticker_payload["c"][0])
        if current_price <= 0:
            return self._result(False, f"❌ Invalid price: ${current_price}")

        volume_usd = 0.0
        if "v" in ticker_payload:
            volume_24h = float(ticker_payload["v"][1])
            volume_usd = volume_24h * current_price
            if volume_usd < self.crypto_min_24h_volume_usd:
                return self._result(
                    False,
                    (
                        f"❌ Low liquidity: ${volume_usd:,.0f} 24h volume "
                        f"(min ${self.crypto_min_24h_volume_usd:,.0f})"
                    ),
                    price=current_price,
                    volume_usd=volume_usd,
                    ticker_fetched_at=self._extract_market_timestamp(ticker_payload),
                )

        if "o" in ticker_payload:
            open_price = float(ticker_payload["o"][0])
            if open_price > 0:
                change_pct = abs((current_price - open_price) / open_price * 100)
                if change_pct > self.crypto_max_price_spike_pct:
                    return self._result(
                        False,
                        (
                            f"❌ Extreme volatility: {change_pct:.1f}% change in 24h "
                            f"(max {self.crypto_max_price_spike_pct}%)"
                        ),
                        price=current_price,
                        volume_usd=volume_usd,
                        ticker_fetched_at=self._extract_market_timestamp(ticker_payload),
                    )

        spread_pct = 0.0
        if "a" in ticker_payload and "b" in ticker_payload:
            ask = float(ticker_payload["a"][0])
            bid = float(ticker_payload["b"][0])
            if ask > 0 and bid > 0:
                spread_pct = ((ask - bid) / ask) * 100
                if spread_pct > self.crypto_max_spread_pct:
                    return self._result(
                        False,
                        f"❌ Wide spread: {spread_pct:.2f}% (max {self.crypto_max_spread_pct}%)",
                        price=current_price,
                        volume_usd=volume_usd,
                        spread_pct=spread_pct,
                        ticker_fetched_at=self._extract_market_timestamp(ticker_payload),
                    )

        candle_payload = candles or self.kraken.get_ohlc(ohlcv_pair, interval=5, limit=self.crypto_min_candles_required)
        if len(candle_payload) < self.crypto_min_candles_required:
            return self._result(
                False,
                (
                    f"❌ Insufficient historical data: {len(candle_payload)} candles "
                    f"(need {self.crypto_min_candles_required})"
                ),
                price=current_price,
                volume_usd=volume_usd,
                spread_pct=spread_pct,
                ticker_fetched_at=self._extract_market_timestamp(ticker_payload),
            )

        trade_value_usd = amount * current_price
        if trade_value_usd < 100:
            return self._result(False, f"❌ Trade too small: ${trade_value_usd:.2f} (min $100)", price=current_price)
        if trade_value_usd > 50000:
            return self._result(False, f"⚠️ Trade too large: ${trade_value_usd:,.2f} (max $50k per position)", price=current_price)

        logger.info(
            "Crypto validation passed for %s at $%.2f with $%.0f 24h volume",
            pair,
            current_price,
            volume_usd,
        )
        ticker_fetched_at = self._extract_market_timestamp(ticker_payload)
        ticker_age_seconds = self._market_age_seconds(ticker_fetched_at)
        return self._result(
            True,
            "✅ All validation checks passed",
            price=current_price,
            volume_usd=volume_usd,
            spread_pct=spread_pct,
            trade_value=trade_value_usd,
            ticker=ticker_payload,
            candles=candle_payload,
            ticker_fetched_at=ticker_fetched_at.isoformat() if ticker_fetched_at else None,
            ticker_age_seconds=ticker_age_seconds,
        )

    def validate_stock_trade(self, ticker: str, shares: int, mode: str = "PAPER") -> Tuple[bool, str]:
        result = self.validate_stock_trade_with_quote(ticker, shares, mode=mode)
        return result['valid'], result['reason']

    def validate_stock_trade_with_quote(
        self,
        ticker: str,
        shares: int,
        mode: str = "PAPER",
        *,
        quote: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ticker = str(ticker or "").upper().strip()

        if not ticker or len(ticker) > 5:
            return self._result(False, f"❌ Invalid ticker format: '{ticker}'")
        if not ticker.isalpha():
            return self._result(False, f"❌ Ticker must contain only letters: '{ticker}'")
        if shares is None or shares < 1:
            return self._result(False, "❌ Must trade at least 1 share")

        selected_mode = (mode or "PAPER").upper()
        if selected_mode == "LIVE":
            session_status = get_scope_session_status("stocks_only", datetime.now(UTC))
            if not session_status.session_open:
                return self._result(False, f"❌ Market closed: {session_status.reason}")

        try:
            quote_payload = quote or self.tradier.get_quote_sync(ticker, selected_mode)
        except Exception as exc:
            return self._result(False, f"❌ Cannot fetch quote for {ticker}: {exc}")

        if not quote_payload:
            return self._result(False, f"❌ No quote data available for {ticker}")

        try:
            last_price = float(quote_payload.get("last") or quote_payload.get("close") or 0)
        except (TypeError, ValueError):
            last_price = 0.0

        if last_price <= 0:
            return self._result(False, f"❌ No tradable price available for {ticker}")
        if last_price < self.stock_min_price:
            return self._result(False, f"❌ Price too low: ${last_price:.2f} (min ${self.stock_min_price:.2f})", price=last_price)
        if last_price > self.stock_max_price:
            return self._result(False, f"❌ Price unrealistic: ${last_price:,.2f} (max ${self.stock_max_price:,.0f})", price=last_price)

        try:
            volume = int(float(quote_payload.get("volume") or 0))
        except (TypeError, ValueError):
            volume = 0

        if volume < self.stock_min_volume:
            return self._result(False, f"❌ Low volume: {volume:,} shares (min {self.stock_min_volume:,})", price=last_price, volume=volume)

        trade_status = str(quote_payload.get("type", "")).lower()
        if trade_status == "halt":
            return self._result(False, f"❌ Trading halted for {ticker}", price=last_price, volume=volume)

        try:
            bid = float(quote_payload.get("bid") or 0)
            ask = float(quote_payload.get("ask") or 0)
        except (TypeError, ValueError):
            bid = 0.0
            ask = 0.0

        spread_pct = 0.0
        if bid > 0 and ask > 0:
            spread_pct = ((ask - bid) / ask) * 100
            if spread_pct > self.stock_max_spread_pct:
                return self._result(False, f"❌ Wide spread: {spread_pct:.2f}% (max {self.stock_max_spread_pct}%)", price=last_price, volume=volume, spread_pct=spread_pct)

        trade_value = shares * last_price
        if trade_value < 100:
            return self._result(False, f"❌ Trade too small: ${trade_value:.2f} (min $100)", price=last_price, volume=volume, spread_pct=spread_pct)
        if trade_value > 100000:
            return self._result(False, f"⚠️ Trade too large: ${trade_value:,.2f} (max $100k per position)", price=last_price, volume=volume, spread_pct=spread_pct)

        quote_fetched_at = self._extract_market_timestamp(quote_payload)
        quote_age_seconds = self._market_age_seconds(quote_fetched_at)
        return self._result(
            True,
            "✅ All validation checks passed",
            price=last_price,
            volume=volume,
            spread_pct=spread_pct,
            trade_value=trade_value,
            quote=quote_payload,
            quote_fetched_at=quote_fetched_at.isoformat() if quote_fetched_at else None,
            quote_age_seconds=quote_age_seconds,
        )

    def validate_crypto_batch(self, candidates: List[Dict]) -> Dict[str, Tuple[bool, str]]:
        results: Dict[str, Tuple[bool, str]] = {}
        for candidate in candidates:
            pair = candidate.get("pair")
            amount = candidate.get("amount", 0)
            if not pair:
                results["UNKNOWN"] = (False, "❌ Missing pair name")
                continue
            results[pair] = self.validate_crypto_trade(pair, amount)
        return results

    def validate_stock_batch(self, candidates: List[Dict], mode: str = "PAPER") -> Dict[str, Tuple[bool, str]]:
        results: Dict[str, Tuple[bool, str]] = {}
        for candidate in candidates:
            ticker = candidate.get("ticker")
            shares = candidate.get("shares", 0)
            if not ticker:
                results["UNKNOWN"] = (False, "❌ Missing ticker symbol")
                continue
            results[str(ticker).upper()] = self.validate_stock_trade(str(ticker), int(shares or 0), mode)
        return results

    def _result(self, valid: bool, reason: str, **details: Any) -> dict[str, Any]:
        return {'valid': valid, 'reason': reason, **details}

    def _extract_market_timestamp(self, payload: dict[str, Any] | None) -> datetime | None:
        if not isinstance(payload, dict):
            return None
        for key in ('_fetched_at_utc', 'fetched_at_utc', 'quote_time', 'timestamp', 'time', 'datetime'):
            raw_value = payload.get(key)
            if not raw_value:
                continue
            if isinstance(raw_value, datetime):
                value = raw_value
            else:
                text = str(raw_value).strip()
                if text.endswith('Z'):
                    text = text[:-1] + '+00:00'
                try:
                    value = datetime.fromisoformat(text)
                except ValueError:
                    continue
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        return None

    def _market_age_seconds(self, timestamp: datetime | None) -> float:
        if timestamp is None:
            return 0.0
        return max((datetime.now(UTC) - timestamp).total_seconds(), 0.0)


trade_validator = TradeValidator()
