"""
Trade Validation Service
Validates trades before execution to catch AI/screening errors
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

from app.services.kraken_service import TOP_30_PAIRS, kraken_service
from app.services.tradier_client import tradier_client

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


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
        if pair not in TOP_30_PAIRS:
            return False, f"❌ {pair} not in supported pairs list"

        if amount is None or amount <= 0:
            return False, f"❌ Invalid amount for {pair}"

        ohlcv_pair = TOP_30_PAIRS[pair]

        ticker = self.kraken.get_ticker(ohlcv_pair)
        if not ticker or "c" not in ticker:
            return False, f"❌ Cannot fetch current price for {pair}"

        current_price = float(ticker["c"][0])
        if current_price <= 0:
            return False, f"❌ Invalid price: ${current_price}"

        volume_usd = 0.0
        if "v" in ticker:
            volume_24h = float(ticker["v"][1])
            volume_usd = volume_24h * current_price
            if volume_usd < self.crypto_min_24h_volume_usd:
                return False, (
                    f"❌ Low liquidity: ${volume_usd:,.0f} 24h volume "
                    f"(min ${self.crypto_min_24h_volume_usd:,.0f})"
                )

        if "o" in ticker:
            open_price = float(ticker["o"][0])
            if open_price > 0:
                change_pct = abs((current_price - open_price) / open_price * 100)
                if change_pct > self.crypto_max_price_spike_pct:
                    return False, (
                        f"❌ Extreme volatility: {change_pct:.1f}% change in 24h "
                        f"(max {self.crypto_max_price_spike_pct}%)"
                    )

        if "a" in ticker and "b" in ticker:
            ask = float(ticker["a"][0])
            bid = float(ticker["b"][0])
            if ask > 0 and bid > 0:
                spread_pct = ((ask - bid) / ask) * 100
                if spread_pct > self.crypto_max_spread_pct:
                    return False, f"❌ Wide spread: {spread_pct:.2f}% (max {self.crypto_max_spread_pct}%)"

        candles = self.kraken.get_ohlc(ohlcv_pair, interval=5, limit=self.crypto_min_candles_required)
        if len(candles) < self.crypto_min_candles_required:
            return False, (
                f"❌ Insufficient historical data: {len(candles)} candles "
                f"(need {self.crypto_min_candles_required})"
            )

        trade_value_usd = amount * current_price
        if trade_value_usd < 100:
            return False, f"❌ Trade too small: ${trade_value_usd:.2f} (min $100)"
        if trade_value_usd > 50000:
            return False, f"⚠️ Trade too large: ${trade_value_usd:,.2f} (max $50k per position)"

        logger.info(
            "Crypto validation passed for %s at $%.2f with $%.0f 24h volume",
            pair,
            current_price,
            volume_usd,
        )
        return True, "✅ All validation checks passed"

    def validate_stock_trade(self, ticker: str, shares: int, mode: str = "PAPER") -> Tuple[bool, str]:
        ticker = str(ticker or "").upper().strip()

        if not ticker or len(ticker) > 5:
            return False, f"❌ Invalid ticker format: '{ticker}'"
        if not ticker.isalpha():
            return False, f"❌ Ticker must contain only letters: '{ticker}'"
        if shares is None or shares < 1:
            return False, "❌ Must trade at least 1 share"

        selected_mode = (mode or "PAPER").upper()
        if selected_mode == "LIVE":
            now_et = datetime.now(ET)
            if now_et.weekday() >= 5:
                return False, "❌ Market closed (weekend)"
            if not (time(9, 30) <= now_et.time() <= time(16, 0)):
                return False, "❌ Market closed (only paper trading allowed)"

        try:
            quote = self.tradier.get_quote_sync(ticker, selected_mode)
        except Exception as exc:
            return False, f"❌ Cannot fetch quote for {ticker}: {exc}"

        if not quote:
            return False, f"❌ No quote data available for {ticker}"

        try:
            last_price = float(quote.get("last") or quote.get("close") or 0)
        except (TypeError, ValueError):
            last_price = 0.0

        if last_price <= 0:
            return False, f"❌ No tradable price available for {ticker}"
        if last_price < self.stock_min_price:
            return False, f"❌ Price too low: ${last_price:.2f} (min ${self.stock_min_price:.2f})"
        if last_price > self.stock_max_price:
            return False, f"❌ Price unrealistic: ${last_price:,.2f} (max ${self.stock_max_price:,.0f})"

        try:
            volume = int(float(quote.get("volume") or 0))
        except (TypeError, ValueError):
            volume = 0

        if volume < self.stock_min_volume:
            return False, f"❌ Low volume: {volume:,} shares (min {self.stock_min_volume:,})"

        trade_status = str(quote.get("type", "")).lower()
        if trade_status == "halt":
            return False, f"❌ Trading halted for {ticker}"

        try:
            bid = float(quote.get("bid") or 0)
            ask = float(quote.get("ask") or 0)
        except (TypeError, ValueError):
            bid = 0.0
            ask = 0.0

        if bid > 0 and ask > 0:
            spread_pct = ((ask - bid) / ask) * 100
            if spread_pct > self.stock_max_spread_pct:
                return False, f"❌ Wide spread: {spread_pct:.2f}% (max {self.stock_max_spread_pct}%)"

        trade_value = shares * last_price
        if trade_value < 100:
            return False, f"❌ Trade too small: ${trade_value:.2f} (min $100)"
        if trade_value > 100000:
            return False, f"⚠️ Trade too large: ${trade_value:,.2f} (max $100k per position)"

        return True, "✅ All validation checks passed"

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


trade_validator = TradeValidator()
