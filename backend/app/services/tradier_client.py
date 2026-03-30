from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import requests

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


class TradierClient:
    def __init__(self) -> None:
        self.timeout = 20

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
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json() if response.text else {}

    def get_account_sync(self, mode: str | None = None) -> dict[str, Any]:
        creds = self._credentials_for_mode(mode)
        return self._request_json("GET", f"accounts/{creds['account_id']}/balances", mode=mode)

    async def get_account_async(self, mode: str | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_account_sync, mode)

    def get_quotes_sync(self, symbols: list[str], mode: str | None = None) -> dict[str, dict[str, Any]]:
        unique_symbols = [str(symbol).upper() for symbol in dict.fromkeys(symbols) if symbol]
        if not unique_symbols:
            return {}

        payload = self._request_json(
            "GET",
            "markets/quotes",
            mode=mode,
            params={"symbols": ",".join(unique_symbols)},
        )
        raw_quotes = payload.get("quotes", {}).get("quote", [])
        if isinstance(raw_quotes, dict):
            raw_quotes = [raw_quotes]

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

    def get_positions_sync(self, mode: str | None = None) -> list[dict[str, Any]]:
        creds = self._credentials_for_mode(mode)
        payload = self._request_json("GET", f"accounts/{creds['account_id']}/positions", mode=mode)
        positions = payload.get("positions", {}).get("position", [])
        if isinstance(positions, dict):
            return [positions]
        return positions if isinstance(positions, list) else []

    async def get_positions_async(self, mode: str | None = None) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_positions_sync, mode)

    def get_position_quantity_sync(self, symbol: str, mode: str | None = None) -> int:
        target = str(symbol or "").upper()
        if not target:
            return 0

        for position in self.get_positions_sync(mode):
            if str(position.get("symbol", "")).upper() != target:
                continue
            quantity = abs(_coalesce_numeric(position, ["quantity", "qty", "shares", "share_quantity"]))
            return int(round(quantity))
        return 0

    async def get_position_quantity_async(self, symbol: str, mode: str | None = None) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_position_quantity_sync, symbol, mode)

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

    def get_positions_snapshot(self, mode: str | None = None) -> list[dict[str, Any]]:
        selected_mode = (mode or "PAPER").upper()
        if not self.is_ready(selected_mode):
            return []

        raw_positions = self.get_positions_sync(selected_mode)
        symbols = [str(position.get("symbol", "")).upper() for position in raw_positions if position.get("symbol")]
        quotes = self.get_quotes_sync(symbols, mode=selected_mode) if symbols else {}

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
            market_value = current_price * quantity if current_price else 0.0
            pnl = market_value - total_cost if market_value else 0.0
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

        return positions


tradier_client = TradierClient()
