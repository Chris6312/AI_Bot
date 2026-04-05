from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from requests.exceptions import RequestException

from app.core.config import settings

logger = logging.getLogger(__name__)


def _coalesce_numeric(payload: dict[str, Any], keys: list[str]) -> float:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _normalize_to_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _extract_collection(payload: Any, *path: str) -> list[dict[str, Any]]:
    current = payload
    for key in path:
        if current in (None, ''):
            return []
        if isinstance(current, dict):
            current = current.get(key)
            continue
        if isinstance(current, list):
            if len(current) == 1 and isinstance(current[0], dict):
                current = current[0].get(key)
                continue
            return _normalize_to_list(current)
        return []
    return _normalize_to_list(current)


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value), tz=timezone.utc)
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
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)

class TradierClient:
    def __init__(self) -> None:
        self.timeout = max(1.0, float(settings.TRADIER_REQUEST_TIMEOUT_SECONDS))
        self._cache_ttl_seconds = 15.0
        self._orders_cache_ttl_seconds = 10.0
        self._max_positions_cache_entries = 8
        self._max_positions_snapshot_cache_entries = 8
        self._max_orders_cache_entries = 32
        self._positions_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._positions_snapshot_cache: dict[tuple[str, bool], tuple[float, list[dict[str, Any]]]] = {}
        self._orders_cache: dict[tuple[str, str, str, tuple[str, ...]], tuple[float, list[dict[str, Any]]]] = {}


    @staticmethod
    def _cache_is_fresh(captured_at: float, ttl_seconds: float) -> bool:
        return (time.monotonic() - captured_at) <= ttl_seconds

    @staticmethod
    def _prune_cache(cache: dict[Any, tuple[float, Any]], *, ttl_seconds: float, max_entries: int) -> None:
        now = time.monotonic()
        stale_keys = [key for key, (captured_at, _value) in cache.items() if (now - captured_at) > ttl_seconds]
        for key in stale_keys:
            cache.pop(key, None)
        overflow = len(cache) - max_entries
        if overflow > 0:
            oldest_keys = sorted(cache.items(), key=lambda item: item[1][0])[:overflow]
            for key, _value in oldest_keys:
                cache.pop(key, None)

    def _credentials_for_mode(self, mode: str | None = None) -> dict[str, str]:
        selected_mode = (mode or "PAPER").upper()
        if selected_mode == "LIVE":
            return settings.live_tradier_credentials()
        return settings.paper_tradier_credentials()

    def is_ready(self, mode: str | None = None) -> bool:
        creds = self._credentials_for_mode(mode)
        return bool(creds["api_key"] and creds["account_id"])

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        mode: str | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        creds = self._credentials_for_mode(mode)
        selected_mode = (mode or "PAPER").upper()

        if not creds["api_key"] or not creds["account_id"]:
            raise RuntimeError(f"Tradier {selected_mode} credentials are not configured.")

        url = f"{creds['base_url'].rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {creds['api_key']}",
            "Accept": "application/json",
        }

        response = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            params=params,
            data=data,
            timeout=timeout if timeout is not None else self.timeout,
        )
        response.raise_for_status()
        return response.json() if response.text else {}

    def get_account_sync(self, mode: str | None = None) -> dict[str, Any]:
        creds = self._credentials_for_mode(mode)
        return self._request_json("GET", f"accounts/{creds['account_id']}/balances", mode=mode)

    async def get_account_async(self, mode: str | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_account_sync, mode)

    def get_quotes_sync(
        self,
        symbols: list[str],
        mode: str | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        unique_symbols = [str(symbol).upper() for symbol in dict.fromkeys(symbols) if symbol]
        if not unique_symbols:
            return {}

        payload = self._request_json(
            "GET",
            "markets/quotes",
            mode=mode,
            params={"symbols": ",".join(unique_symbols)},
            timeout=timeout,
        )
        raw_quotes = _extract_collection(payload, 'quotes', 'quote')

        fetched_at = datetime.now(timezone.utc).isoformat()
        normalized_quotes: dict[str, dict[str, Any]] = {}
        for quote in raw_quotes:
            if not isinstance(quote, dict) or not quote.get('symbol'):
                continue
            enriched = dict(quote)
            enriched.setdefault('_fetched_at_utc', fetched_at)
            normalized_quotes[str(quote.get('symbol', '')).upper()] = enriched
        return normalized_quotes

    async def get_quotes_async(self, symbols: list[str], mode: str | None = None) -> dict[str, dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_quotes_sync, symbols, mode)

    def get_quote_sync(self, ticker: str, mode: str | None = None) -> dict[str, Any]:
        return self.get_quotes_sync([ticker], mode=mode).get(str(ticker).upper(), {})

    async def get_quote_async(self, ticker: str, mode: str | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_quote_sync, ticker, mode)

    def get_timesales_sync(
        self,
        symbol: str,
        *,
        interval_minutes: int = 5,
        mode: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        session_filter: str = 'open',
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        interval_labels = {
            1: '1min',
            5: '5min',
            10: '10min',
            15: '15min',
            30: '30min',
            60: '1hour',
        }
        target_symbol = str(symbol or '').upper().strip()
        if not target_symbol:
            return []

        end_at = _parse_datetime(end) or datetime.now(timezone.utc)
        start_at = _parse_datetime(start) or (end_at - timedelta(days=5))
        payload = self._request_json(
            'GET',
            'markets/timesales',
            mode=mode,
            params={
                'symbol': target_symbol,
                'interval': interval_labels.get(max(1, int(interval_minutes)), '5min'),
                'start': start_at.strftime('%Y-%m-%d %H:%M'),
                'end': end_at.strftime('%Y-%m-%d %H:%M'),
                'session_filter': session_filter,
            },
            timeout=timeout,
        )
        raw_bars = _extract_collection(payload, 'series', 'data')
        candles: list[dict[str, Any]] = []
        for bar in raw_bars:
            timestamp = _parse_datetime(bar.get('time') or bar.get('timestamp') or bar.get('datetime') or bar.get('date'))
            if timestamp is None:
                continue
            candles.append(
                {
                    'timestamp': int(timestamp.timestamp()),
                    'open': _coalesce_numeric(bar, ['open']),
                    'high': _coalesce_numeric(bar, ['high']),
                    'low': _coalesce_numeric(bar, ['low']),
                    'close': _coalesce_numeric(bar, ['close']),
                    'volume': bar.get('volume') if bar.get('volume') not in (None, '') else None,
                }
            )
        return candles

    async def get_timesales_async(
        self,
        symbol: str,
        *,
        interval_minutes: int = 5,
        mode: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        session_filter: str = 'open',
    ) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.get_timesales_sync(
                symbol,
                interval_minutes=interval_minutes,
                mode=mode,
                start=start,
                end=end,
                session_filter=session_filter,
            ),
        )

    def place_order_sync(
        self,
        ticker: str,
        qty: int,
        side: str,
        mode: str | None = None,
        order_type: str = "market",
        duration: str = "day",
    ) -> dict[str, Any]:
        creds = self._credentials_for_mode(mode)
        return self._request_json(
            "POST",
            f"accounts/{creds['account_id']}/orders",
            mode=mode,
            data={
                "class": "equity",
                "symbol": ticker,
                "side": side,
                "quantity": qty,
                "type": order_type,
                "duration": duration,
            },
        )

    async def place_order_async(
        self,
        ticker: str,
        qty: int,
        side: str,
        mode: str | None = None,
        order_type: str = "market",
        duration: str = "day",
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.place_order_sync, ticker, qty, side, mode, order_type, duration)

    def get_order_sync(self, order_id: str, mode: str | None = None) -> dict[str, Any]:
        creds = self._credentials_for_mode(mode)
        return self._request_json(
            "GET",
            f"accounts/{creds['account_id']}/orders/{order_id}",
            mode=mode,
        )

    async def get_order_async(self, order_id: str, mode: str | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_order_sync, order_id, mode)

    @staticmethod
    def extract_order_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        order = payload.get("order")
        return order if isinstance(order, dict) else payload

    def normalize_order_response(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        order = self.extract_order_payload(payload)
        status = str(order.get("status") or "UNKNOWN").upper()
        requested_quantity = _coalesce_numeric(order, ["quantity", "qty"])
        filled_quantity = _coalesce_numeric(order, ["exec_quantity", "filled_quantity", "filled_qty"])
        avg_fill_price = _coalesce_numeric(
            order,
            ["avg_fill_price", "avg_execution_price", "avg_price", "last_fill_price"],
        )
        return {
            "id": str(order.get("id") or "") or None,
            "status": status,
            "requested_quantity": requested_quantity,
            "filled_quantity": filled_quantity,
            "avg_fill_price": avg_fill_price,
            "is_terminal": status in {"FILLED", "REJECTED", "CANCELED", "CANCELLED", "ERROR", "FAILED"},
            "raw": payload or {},
        }

    def get_positions_sync(self, mode: str | None = None, *, timeout: float | None = None, use_cache: bool = True) -> list[dict[str, Any]]:
        selected_mode = (mode or "PAPER").upper()
        cache_key = selected_mode
        cached = self._positions_cache.get(cache_key)
        if use_cache and cached and self._cache_is_fresh(cached[0], self._cache_ttl_seconds):
            return [dict(item) for item in cached[1]]

        creds = self._credentials_for_mode(mode)
        payload = self._request_json("GET", f"accounts/{creds['account_id']}/positions", mode=mode, timeout=timeout)
        normalized = _extract_collection(payload, 'positions', 'position')
        self._positions_cache[cache_key] = (time.monotonic(), [dict(item) for item in normalized if isinstance(item, dict)])
        self._prune_cache(self._positions_cache, ttl_seconds=self._cache_ttl_seconds, max_entries=self._max_positions_cache_entries)
        return [dict(item) for item in normalized if isinstance(item, dict)]

    async def get_positions_async(self, mode: str | None = None) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_positions_sync, mode)

    def get_position_quantity_sync(self, symbol: str, mode: str | None = None, *, timeout: float | None = None, use_cache: bool = True) -> int:
        target = str(symbol or "").upper()
        if not target:
            return 0

        try:
            positions = self.get_positions_sync(mode, timeout=timeout, use_cache=use_cache)
        except TypeError:
            # Some tests monkeypatch get_positions_sync with a lambda that only
            # accepts the legacy mode argument. Fall back gracefully so those
            # call sites continue to work.
            positions = self.get_positions_sync(mode)

        for position in positions:
            if str(position.get("symbol", "")).upper() != target:
                continue
            quantity = abs(_coalesce_numeric(position, ["quantity", "qty", "shares", "share_quantity"]))
            return int(round(quantity))
        return 0

    async def get_position_quantity_async(self, symbol: str, mode: str | None = None) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_position_quantity_sync, symbol, mode)

    def get_orders_sync(
        self,
        mode: str | None = None,
        *,
        symbol: str | None = None,
        side: str | None = None,
        statuses: list[str] | None = None,
        timeout: float | None = None,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        selected_mode = (mode or "PAPER").upper()
        target_symbol = str(symbol or '').upper().strip()
        target_side = str(side or '').upper().strip()
        normalized_statuses = tuple(sorted({str(item).upper().strip() for item in (statuses or []) if str(item).strip()}))
        cache_key = (selected_mode, target_symbol, target_side, normalized_statuses)
        cached = self._orders_cache.get(cache_key)
        if use_cache and cached and self._cache_is_fresh(cached[0], self._orders_cache_ttl_seconds):
            return [dict(item) for item in cached[1]]

        creds = self._credentials_for_mode(mode)
        payload = self._request_json(
            "GET",
            f"accounts/{creds['account_id']}/orders",
            mode=mode,
            timeout=timeout,
        )
        raw_orders = _extract_collection(payload, 'orders', 'order')
        orders = self.normalize_orders_response(raw_orders)

        filtered: list[dict[str, Any]] = []
        for order in orders:
            if target_symbol and order.get('symbol') != target_symbol:
                continue
            if target_side and order.get('side') != target_side:
                continue
            if normalized_statuses and str(order.get('status') or '').upper() not in normalized_statuses:
                continue
            filtered.append(order)
        self._orders_cache[cache_key] = (time.monotonic(), [dict(item) for item in filtered])
        self._prune_cache(self._orders_cache, ttl_seconds=self._orders_cache_ttl_seconds, max_entries=self._max_orders_cache_entries)
        return [dict(item) for item in filtered]

    async def get_orders_async(
        self,
        mode: str | None = None,
        *,
        symbol: str | None = None,
        side: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.get_orders_sync(mode, symbol=symbol, side=side, statuses=statuses))

    def normalize_orders_response(self, payload: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for order in _normalize_to_list(payload):
            status = str(order.get('status') or 'UNKNOWN').upper()
            requested_quantity = _coalesce_numeric(order, ['quantity', 'qty'])
            filled_quantity = _coalesce_numeric(order, ['exec_quantity', 'filled_quantity', 'filled_qty'])
            remaining_quantity = max(requested_quantity - filled_quantity, 0.0)
            normalized.append({
                'id': str(order.get('id') or '') or None,
                'symbol': str(order.get('symbol') or '').upper().strip(),
                'side': str(order.get('side') or '').upper().strip(),
                'status': status,
                'requested_quantity': requested_quantity,
                'filled_quantity': filled_quantity,
                'remaining_quantity': remaining_quantity,
                'avg_fill_price': _coalesce_numeric(order, ['avg_fill_price', 'avg_execution_price', 'avg_price', 'last_fill_price']),
                'is_open': status not in {'FILLED', 'REJECTED', 'CANCELED', 'CANCELLED', 'ERROR', 'FAILED', 'EXPIRED'},
                'raw': dict(order),
            })
        return normalized

    def get_account_snapshot(self, mode: str | None = None) -> dict[str, Any]:
        selected_mode = (mode or "PAPER").upper()
        if not self.is_ready(selected_mode):
            return {
                "mode": selected_mode,
                "connected": False,
                "accountId": "",
                "buyingPower": 0.0,
                "brokerBuyingPower": 0.0,
                "availableToTrade": 0.0,
                "portfolioValue": 0.0,
                "cash": 0.0,
                "unrealizedPnL": 0.0,
                "dailyPnL": 0.0,
            }

        payload = self.get_account_sync(selected_mode)
        balances = payload.get("balances", payload)
        margin = balances.get("margin", {}) if isinstance(balances.get("margin"), dict) else {}
        creds = self._credentials_for_mode(selected_mode)

        account_id = balances.get("account_number") or balances.get("account_id") or creds["account_id"]
        portfolio_value = _coalesce_numeric(
            balances,
            ["total_equity", "equity", "net_liquidating_value", "portfolio_value"],
        )
        cash = _coalesce_numeric(balances, ["total_cash", "cash_available", "cash"])

        broker_buying_power = _coalesce_numeric(
            balances,
            ["buying_power", "margin_buying_power", "stock_buying_power", "option_buying_power"],
        )
        if broker_buying_power <= 0:
            broker_buying_power = _coalesce_numeric(
                margin,
                ["stock_buying_power", "option_buying_power", "buying_power"],
            )
        if broker_buying_power <= 0:
            broker_buying_power = cash or portfolio_value

        available_to_trade = cash if cash > 0 else broker_buying_power or portfolio_value

        unrealized_pnl = _coalesce_numeric(
            balances,
            ["unrealized_pnl", "unrealized_gain_loss", "open_pl"],
        )
        daily_pnl = _coalesce_numeric(balances, ["close_pl", "daily_pnl", "today_change"])

        return {
            "mode": selected_mode,
            "connected": True,
            "accountId": str(account_id),
            "buyingPower": broker_buying_power,
            "brokerBuyingPower": broker_buying_power,
            "availableToTrade": available_to_trade,
            "portfolioValue": portfolio_value,
            "cash": cash,
            "unrealizedPnL": unrealized_pnl,
            "dailyPnL": daily_pnl,
            "raw": balances,
        }

    def get_positions_snapshot(self, mode: str | None = None, *, include_quotes: bool = True, use_cache: bool = True) -> list[dict[str, Any]]:
        selected_mode = (mode or "PAPER").upper()
        if not self.is_ready(selected_mode):
            return []

        cache_key = (selected_mode, bool(include_quotes))
        cached = self._positions_snapshot_cache.get(cache_key)
        if use_cache and cached and self._cache_is_fresh(cached[0], self._cache_ttl_seconds):
            return [dict(item) for item in cached[1]]

        try:
            raw_positions = self.get_positions_sync(
                selected_mode,
                timeout=max(1.0, float(settings.TRADIER_POSITIONS_TIMEOUT_SECONDS)),
                use_cache=use_cache,
            )
        except RequestException as exc:
            logger.warning("Tradier positions snapshot failed for %s: %s", selected_mode, exc)
            if cached:
                return [dict(item) for item in cached[1]]
            return []
        except Exception as exc:
            logger.warning("Tradier positions snapshot failed for %s: %s", selected_mode, exc)
            if cached:
                return [dict(item) for item in cached[1]]
            return []

        symbols = [str(position.get("symbol", "")).upper() for position in raw_positions if position.get("symbol")]
        quotes: dict[str, dict[str, Any]] = {}
        if include_quotes and symbols:
            try:
                quotes = self.get_quotes_sync(symbols, mode=selected_mode, timeout=max(1.0, float(settings.TRADIER_POSITIONS_TIMEOUT_SECONDS)))
            except RequestException as exc:
                logger.warning("Tradier quote refresh failed during positions snapshot for %s: %s", selected_mode, exc)
                quotes = {}
            except Exception as exc:
                logger.warning("Tradier quote refresh failed during positions snapshot for %s: %s", selected_mode, exc)
                quotes = {}

        cached_by_symbol = {
            str(item.get('symbol') or '').upper(): dict(item)
            for item in (cached[1] if cached else [])
            if isinstance(item, dict) and item.get('symbol')
        }

        positions: list[dict[str, Any]] = []
        for raw_position in raw_positions:
            symbol = str(raw_position.get("symbol", "")).upper()
            if not symbol:
                continue

            quantity = abs(_coalesce_numeric(raw_position, ["quantity", "qty", "shares", "share_quantity"]))
            if quantity <= 0:
                continue

            total_cost = _coalesce_numeric(raw_position, ["cost_basis", "total_cost"])
            avg_price = _coalesce_numeric(
                raw_position,
                ["cost_basis_per_share", "average_price", "avg_price", "purchase_price"],
            )

            if avg_price <= 0 and total_cost > 0:
                avg_price = total_cost / quantity
            if total_cost <= 0:
                total_cost = avg_price * quantity

            quote = quotes.get(symbol, {})
            current_price = _coalesce_numeric(
                quote,
                ["last", "last_extended_hours_trade", "close", "bid", "ask"],
            )
            if current_price <= 0:
                cached_snapshot = cached_by_symbol.get(symbol, {})
                current_price = _coalesce_numeric(cached_snapshot, ['currentPrice', 'avgPrice'])
            if current_price <= 0:
                current_price = avg_price

            market_value = current_price * quantity if current_price > 0 else total_cost
            pnl = market_value - total_cost
            pnl_percent = (pnl / total_cost * 100.0) if total_cost else 0.0

            positions.append(
                {
                    "symbol": symbol,
                    "shares": quantity,
                    "avgPrice": avg_price,
                    "currentPrice": current_price,
                    "marketValue": market_value,
                    "pnl": pnl,
                    "pnlPercent": pnl_percent,
                }
            )

        self._positions_snapshot_cache[cache_key] = (time.monotonic(), [dict(item) for item in positions])
        self._prune_cache(self._positions_snapshot_cache, ttl_seconds=self._cache_ttl_seconds, max_entries=self._max_positions_snapshot_cache_entries)
        return [dict(item) for item in positions]


tradier_client = TradierClient()
