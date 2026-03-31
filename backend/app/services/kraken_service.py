"""
Kraken API Integration Service
Handles crypto market-data operations via Kraken REST API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from decimal import Decimal
from threading import Lock
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

PAIR_SYMBOL_SYNONYMS: dict[str, set[str]] = {
    'BTC': {'BTC', 'XBT'},
    'XBT': {'BTC', 'XBT'},
    'DOGE': {'DOGE', 'XDG'},
    'XDG': {'DOGE', 'XDG'},
}

ASSET_CODE_ALIASES: dict[str, str] = {
    'XXBT': 'BTC',
    'XBT': 'BTC',
    'XXDG': 'DOGE',
    'XDG': 'DOGE',
    'XETH': 'ETH',
    'XXRP': 'XRP',
    'XLTC': 'LTC',
    'XXLM': 'XLM',
    'XETC': 'ETC',
    'XXMR': 'XMR',
    'XZEC': 'ZEC',
    'ZAUD': 'AUD',
    'ZCAD': 'CAD',
    'ZEUR': 'EUR',
    'ZGBP': 'GBP',
    'ZJPY': 'JPY',
    'ZUSD': 'USD',
    'USDT': 'USDT',
    'USDC': 'USDC',
}

KNOWN_QUOTES = (
    'USDT',
    'USDC',
    'DAI',
    'PYUSD',
    'EUR',
    'GBP',
    'USD',
    'CAD',
    'AUD',
    'JPY',
    'CHF',
    'BTC',
    'XBT',
    'ETH',
)

QUOTE_ALIAS_CANDIDATES = tuple(
    sorted({*KNOWN_QUOTES, *ASSET_CODE_ALIASES.keys()}, key=len, reverse=True)
)


@dataclass(frozen=True)
class KrakenPairMetadata:
    display_pair: str
    rest_pair: str
    pair_key: str
    ws_pair: str | None = None
    altname: str | None = None


class KrakenAPIService:
    """Kraken REST API wrapper for crypto operations."""

    def __init__(self):
        self.base_url = 'https://api.kraken.com/0/public'
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'AI Trading Bot/1.0'})
        self.asset_pairs_cache_ttl_seconds = 60 * 60
        self._pair_alias_map: dict[str, KrakenPairMetadata] = {}
        self._display_pair_map: dict[str, KrakenPairMetadata] = {}
        self._asset_pairs_loaded_at: datetime | None = None
        self._pair_lock = Lock()

    @staticmethod
    def _normalize_pair_alias(value: str | None) -> str:
        raw = str(value or '').upper().strip()
        return ''.join(char for char in raw if char.isalnum())

    @staticmethod
    def _normalize_asset_code(value: str | None) -> str:
        raw = str(value or '').upper().strip()
        if not raw:
            return ''
        if raw in ASSET_CODE_ALIASES:
            return ASSET_CODE_ALIASES[raw]
        if raw.startswith('Z') and len(raw) in {4, 5}:
            trimmed = raw[1:]
            if trimmed:
                return trimmed
        if raw.startswith('X') and len(raw) in {4, 5}:
            trimmed = raw[1:]
            if trimmed:
                return trimmed
        return raw

    def _split_pair_components(self, value: str | None) -> tuple[str, str] | None:
        raw = str(value or '').upper().strip()
        if not raw:
            return None

        if '/' in raw:
            base_raw, quote_raw = raw.split('/', 1)
            base = self._normalize_asset_code(base_raw)
            quote = self._normalize_asset_code(quote_raw)
            return (base, quote) if base and quote else None

        compact = self._normalize_pair_alias(raw)
        if not compact:
            return None

        for quote in QUOTE_ALIAS_CANDIDATES:
            if compact.endswith(quote) and len(compact) > len(quote):
                base = self._normalize_asset_code(compact[:-len(quote)])
                normalized_quote = self._normalize_asset_code(quote)
                if base and normalized_quote:
                    return base, normalized_quote

        return None

    def _display_pair_from_market(self, pair_key: str, market: dict[str, Any]) -> str | None:
        wsname = str(market.get('wsname') or '').strip()
        if wsname:
            parts = self._split_pair_components(wsname)
            if parts:
                return f'{parts[0]}/{parts[1]}'

        altname = str(market.get('altname') or '').strip()
        if altname:
            parts = self._split_pair_components(altname)
            if parts:
                return f'{parts[0]}/{parts[1]}'

        base = self._normalize_asset_code(market.get('base'))
        quote = self._normalize_asset_code(market.get('quote'))
        if base and quote:
            return f'{base}/{quote}'

        parts = self._split_pair_components(pair_key)
        if parts:
            return f'{parts[0]}/{parts[1]}'

        return None

    def _pair_alias_variants(self, value: str | None) -> set[str]:
        raw = str(value or '').upper().strip()
        if not raw:
            return set()

        variants = {raw}
        compact = self._normalize_pair_alias(raw)
        if compact:
            variants.add(compact)

        parts = self._split_pair_components(raw)
        if not parts:
            return {variant for variant in variants if variant}

        base, quote = parts
        base_variants = PAIR_SYMBOL_SYNONYMS.get(base, {base})
        quote_variants = PAIR_SYMBOL_SYNONYMS.get(quote, {quote})

        for base_variant in base_variants:
            for quote_variant in quote_variants:
                variants.add(f'{base_variant}/{quote_variant}')
                variants.add(f'{base_variant}{quote_variant}')

        return {variant for variant in variants if variant}

    @staticmethod
    def _is_excluded_pair(pair_key: str, market: dict[str, Any]) -> bool:
        key = str(pair_key or '').strip().lower()
        altname = str(market.get('altname') or '').strip().lower()
        wsname = str(market.get('wsname') or '').strip().lower()
        return key.endswith('.d') or altname.endswith('.d') or wsname.endswith('.d')

    def _build_pair_metadata(self, pair_key: str, market: dict[str, Any]) -> KrakenPairMetadata | None:
        display_pair = self._display_pair_from_market(pair_key, market)
        rest_pair = str(market.get('altname') or pair_key or '').strip().upper()
        if not display_pair or not rest_pair:
            return None
        ws_pair = str(market.get('wsname') or '').strip().upper() or None
        altname = str(market.get('altname') or '').strip().upper() or None
        return KrakenPairMetadata(
            display_pair=display_pair.upper(),
            rest_pair=rest_pair,
            pair_key=str(pair_key or '').strip().upper(),
            ws_pair=ws_pair,
            altname=altname,
        )

    def refresh_asset_pairs(self, *, force: bool = False) -> dict[str, str]:
        now = datetime.now(UTC)
        with self._pair_lock:
            cache_is_fresh = (
                self._asset_pairs_loaded_at is not None
                and (now - self._asset_pairs_loaded_at).total_seconds() < self.asset_pairs_cache_ttl_seconds
            )
            if not force and self._display_pair_map and cache_is_fresh:
                return {display: metadata.rest_pair for display, metadata in self._display_pair_map.items()}

            result = self._api_call('AssetPairs')
            if not isinstance(result, dict):
                logger.warning('Kraken AssetPairs refresh failed; keeping existing cache.')
                return {display: metadata.rest_pair for display, metadata in self._display_pair_map.items()}

            alias_map: dict[str, KrakenPairMetadata] = {}
            display_map: dict[str, KrakenPairMetadata] = {}

            for pair_key, market in result.items():
                if not isinstance(market, dict) or self._is_excluded_pair(str(pair_key), market):
                    continue
                metadata = self._build_pair_metadata(str(pair_key), market)
                if metadata is None:
                    continue
                display_map.setdefault(metadata.display_pair, metadata)
                for alias in (
                    metadata.display_pair,
                    metadata.ws_pair,
                    metadata.altname,
                    metadata.pair_key,
                ):
                    for variant in self._pair_alias_variants(alias):
                        normalized = self._normalize_pair_alias(variant)
                        if normalized and normalized not in alias_map:
                            alias_map[normalized] = metadata
                for variant in self._pair_alias_variants(metadata.display_pair):
                    normalized = self._normalize_pair_alias(variant)
                    if normalized:
                        alias_map[normalized] = metadata

            self._pair_alias_map = alias_map
            self._display_pair_map = dict(sorted(display_map.items(), key=lambda item: item[0]))
            self._asset_pairs_loaded_at = now
            logger.info('Loaded %s Kraken asset pairs into resolver cache.', len(self._display_pair_map))
            return {display: metadata.rest_pair for display, metadata in self._display_pair_map.items()}

    def resolve_pair(self, pair: str, *, force_refresh: bool = False) -> KrakenPairMetadata | None:
        if not self._display_pair_map:
            self.refresh_asset_pairs(force=force_refresh)
        elif force_refresh:
            self.refresh_asset_pairs(force=True)

        normalized = self._normalize_pair_alias(pair)
        metadata = self._pair_alias_map.get(normalized)
        if metadata is not None:
            return metadata

        if not force_refresh:
            self.refresh_asset_pairs(force=True)
            return self._pair_alias_map.get(normalized)

        return None

    def get_supported_pairs(self, *, force_refresh: bool = False) -> dict[str, str]:
        return self.refresh_asset_pairs(force=force_refresh)

    def get_ohlcv_pair(self, pair: str, *, force_refresh: bool = False) -> str | None:
        metadata = self.resolve_pair(pair, force_refresh=force_refresh)
        return metadata.rest_pair if metadata is not None else None

    def get_display_pair(self, pair: str, *, force_refresh: bool = False) -> str | None:
        metadata = self.resolve_pair(pair, force_refresh=force_refresh)
        return metadata.display_pair if metadata is not None else None

    def _api_call(self, endpoint: str, params: Dict | None = None) -> Optional[Dict]:
        """Make API call to Kraken."""
        try:
            url = f"{self.base_url}/{endpoint}"
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()

            if data.get('error') and len(data['error']) > 0:
                logger.error(f"Kraken API error: {data['error']}")
                return None

            return data.get('result')

        except Exception as exc:
            logger.error(f'Kraken API call failed: {exc}')
            return None

    def get_ticker(self, pair: str) -> Optional[Dict]:
        """Get current ticker for a pair."""
        metadata = self.resolve_pair(pair)
        request_pair = metadata.rest_pair if metadata is not None else str(pair or '').strip().upper()
        result = self._api_call('Ticker', {'pair': request_pair})

        if result and request_pair in result:
            ticker = dict(result[request_pair])
            ticker.setdefault('_fetched_at_utc', datetime.now(timezone.utc).isoformat())
            return ticker

        for alt_pair in result.keys() if result else []:
            normalized_alt = self._normalize_pair_alias(alt_pair)
            if normalized_alt in {
                self._normalize_pair_alias(request_pair),
                self._normalize_pair_alias(metadata.display_pair) if metadata is not None else '',
                self._normalize_pair_alias(metadata.pair_key) if metadata is not None else '',
                self._normalize_pair_alias(metadata.altname) if metadata and metadata.altname else '',
            }:
                ticker = dict(result[alt_pair])
                ticker.setdefault('_fetched_at_utc', datetime.now(timezone.utc).isoformat())
                return ticker

        return None

    def get_ohlc(self, pair: str, interval: int = 5, limit: int = 100) -> List[Dict]:
        """
        Get OHLC candle data.

        Args:
            pair: Kraken pair format or display format (e.g., 'BTC/USD' or 'XBTUSD')
            interval: minutes (1, 5, 15, 30, 60, 240, 1440)
            limit: max number of candles (default 100, max 720)

        Returns:
            List of candle dicts
        """
        interval_map = {
            1: 1,
            5: 5,
            15: 15,
            30: 30,
            60: 60,
            240: 240,
            1440: 1440,
        }
        kraken_interval = interval_map.get(interval, 5)
        metadata = self.resolve_pair(pair)
        request_pair = metadata.rest_pair if metadata is not None else str(pair or '').strip().upper()

        result = self._api_call('OHLC', {
            'pair': request_pair,
            'interval': kraken_interval,
            'since': None,
        })

        if not result:
            return []

        pair_data = None
        for key, value in result.items():
            if key == 'last' or not isinstance(value, list):
                continue
            if self._normalize_pair_alias(key) in {
                self._normalize_pair_alias(request_pair),
                self._normalize_pair_alias(metadata.display_pair) if metadata is not None else '',
                self._normalize_pair_alias(metadata.pair_key) if metadata is not None else '',
                self._normalize_pair_alias(metadata.altname) if metadata and metadata.altname else '',
            }:
                pair_data = value
                break
            if pair_data is None:
                pair_data = value

        if not pair_data:
            return []

        pair_data = pair_data[-limit:] if len(pair_data) > limit else pair_data

        candles = []
        for entry in pair_data:
            candles.append({
                'timestamp': int(entry[0]),
                'open': float(entry[1]),
                'high': float(entry[2]),
                'low': float(entry[3]),
                'close': float(entry[4]),
                'vwap': float(entry[5]),
                'volume': float(entry[6]),
                'count': int(entry[7]),
            })

        return candles

    def get_prices(self, pairs: List[str]) -> Dict[str, float]:
        """Get current prices for multiple pairs."""
        prices: dict[str, float] = {}

        for pair in pairs:
            metadata = self.resolve_pair(pair)
            resolved_pair = metadata.rest_pair if metadata is not None else str(pair or '').strip().upper()
            ticker = self.get_ticker(resolved_pair)
            if ticker and 'c' in ticker:
                current_price = float(ticker['c'][0])
                for alias in {
                    resolved_pair,
                    str(pair),
                    metadata.pair_key if metadata is not None else None,
                    metadata.altname if metadata is not None else None,
                    metadata.ws_pair if metadata is not None else None,
                    metadata.display_pair if metadata is not None else None,
                }:
                    if str(alias or '').strip():
                        prices[str(alias)] = current_price

        return prices


class CryptoPaperLedger:
    """Paper trading ledger for crypto with real Kraken prices."""

    def __init__(self, starting_balance: float = 100000.0):
        self.balance = Decimal(str(starting_balance))
        self.starting_balance = Decimal(str(starting_balance))
        self.trades: List[Dict] = []
        self.positions: Dict[str, Dict] = {}
        self.kraken = KrakenAPIService()

    def execute_trade(
        self,
        pair: str,
        ohlcv_pair: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
    ) -> Dict:
        """Execute a paper trade."""
        if price is None:
            ticker = self.kraken.get_ticker(ohlcv_pair)
            if not ticker or 'c' not in ticker:
                return {'status': 'REJECTED', 'reason': 'Failed to fetch current price'}
            price = float(ticker['c'][0])

        amount_dec = Decimal(str(amount))
        price_dec = Decimal(str(price))
        total = amount_dec * price_dec

        if side == 'BUY':
            if total > self.balance:
                return {
                    'status': 'REJECTED',
                    'reason': f'Insufficient balance: ${self.balance:.2f} < ${total:.2f}',
                }
            self.balance -= total
            if pair not in self.positions:
                self.positions[pair] = {'amount': Decimal('0'), 'total_cost': Decimal('0')}
            self.positions[pair]['amount'] += amount_dec
            self.positions[pair]['total_cost'] += total
        elif side == 'SELL':
            if pair not in self.positions or self.positions[pair]['amount'] < amount_dec:
                return {'status': 'REJECTED', 'reason': f'Insufficient {pair} position'}

            self.balance += total
            self.positions[pair]['amount'] -= amount_dec
            if self.positions[pair]['amount'] > 0:
                ratio = amount_dec / (self.positions[pair]['amount'] + amount_dec)
                self.positions[pair]['total_cost'] -= self.positions[pair]['total_cost'] * ratio
            else:
                self.positions[pair]['total_cost'] = Decimal('0')
            if self.positions[pair]['amount'] == 0:
                del self.positions[pair]

        trade = {
            'id': f'paper_{len(self.trades) + 1}',
            'timestamp': datetime.now(UTC).isoformat(),
            'market': 'CRYPTO',
            'pair': pair,
            'ohlcvPair': ohlcv_pair,
            'side': side,
            'amount': float(amount_dec),
            'price': float(price_dec),
            'total': float(total),
            'status': 'FILLED',
            'balance': float(self.balance),
        }
        self.trades.append(trade)

        logger.info('Paper trade executed: %s %s %s @ $%.2f', side, amount, pair, price)
        return trade

    def _build_position_analytics(self) -> dict[str, Any]:
        analytics: dict[str, Any] = {
            'entry_times': {},
            'realized_pnl_by_pair': {},
            'realized_pnl_total': 0.0,
        }
        running_positions: dict[str, dict[str, Decimal | str | None]] = {}

        for trade in self.trades:
            pair = str(trade.get('pair') or '').strip()
            if not pair:
                continue
            side = str(trade.get('side') or '').upper().strip()
            amount_dec = Decimal(str(trade.get('amount') or 0.0))
            price_dec = Decimal(str(trade.get('price') or 0.0))
            total_dec = Decimal(str(trade.get('total') or 0.0))
            timestamp = str(trade.get('timestamp') or '').strip() or None
            state = running_positions.setdefault(
                pair,
                {'amount': Decimal('0'), 'total_cost': Decimal('0'), 'opened_at': None},
            )
            current_amount = Decimal(str(state['amount']))
            current_cost = Decimal(str(state['total_cost']))
            opened_at = state['opened_at']

            if side == 'BUY':
                if current_amount <= 0 and amount_dec > 0:
                    opened_at = timestamp
                current_amount += amount_dec
                current_cost += total_dec
            elif side == 'SELL' and amount_dec > 0 and current_amount > 0:
                sell_amount = min(amount_dec, current_amount)
                avg_cost = (current_cost / current_amount) if current_amount > 0 else Decimal('0')
                closed_cost = avg_cost * sell_amount
                realized = (price_dec * sell_amount) - closed_cost
                analytics['realized_pnl_by_pair'][pair] = float(
                    Decimal(str(analytics['realized_pnl_by_pair'].get(pair, 0.0))) + realized
                )
                current_amount -= sell_amount
                current_cost -= closed_cost
                if current_amount <= Decimal('0.0000000001'):
                    current_amount = Decimal('0')
                    current_cost = Decimal('0')
                    opened_at = None

            state['amount'] = current_amount
            state['total_cost'] = current_cost
            state['opened_at'] = opened_at

        analytics['entry_times'] = {
            pair: state.get('opened_at')
            for pair, state in running_positions.items()
            if Decimal(str(state.get('amount') or 0)) > 0
        }
        analytics['realized_pnl_total'] = round(sum(analytics['realized_pnl_by_pair'].values()), 8)
        return analytics

    def _get_price_for_pair(self, pair: str, prices: Dict[str, float], ohlcv_pair: str | None = None) -> float:
        metadata = self.kraken.resolve_pair(pair)
        aliases = [
            pair,
            ohlcv_pair,
            metadata.rest_pair if metadata is not None else None,
            metadata.pair_key if metadata is not None else None,
            metadata.altname if metadata is not None else None,
            metadata.ws_pair if metadata is not None else None,
            metadata.display_pair if metadata is not None else None,
        ]

        for alias in aliases:
            if alias in prices and prices.get(alias) is not None:
                try:
                    return float(prices[alias])
                except (TypeError, ValueError):
                    continue

        normalized_aliases = {
            self.kraken._normalize_pair_alias(variant)
            for alias in aliases
            for variant in self.kraken._pair_alias_variants(alias)
            if str(alias or '').strip()
        }
        normalized_aliases.discard('')

        for price_key, price_value in prices.items():
            price_variants = {
                self.kraken._normalize_pair_alias(variant)
                for variant in self.kraken._pair_alias_variants(price_key)
            }
            components = self.kraken._split_pair_components(price_key)
            if components is not None:
                base, quote = components
                for variant in self.kraken._pair_alias_variants(f'{base}/{quote}'):
                    price_variants.add(self.kraken._normalize_pair_alias(variant))
            price_variants.discard('')
            if normalized_aliases.isdisjoint(price_variants):
                continue
            try:
                return float(price_value)
            except (TypeError, ValueError):
                continue

        return 0.0

    def get_positions(self) -> List[Dict]:
        """Get current positions with P&L and paper-equity context."""
        positions = []
        if not self.positions:
            return positions

        pairs_to_check = list(self.positions.keys())
        ohlcv_pairs = [self._resolve_position_ohlcv_pair(pair) for pair in pairs_to_check]
        ohlcv_pairs = [pair for pair in ohlcv_pairs if pair]
        prices = self.kraken.get_prices(ohlcv_pairs)
        analytics = self._build_position_analytics()

        for pair, pos in self.positions.items():
            ohlcv_pair = self._resolve_position_ohlcv_pair(pair)
            current_price = self._get_price_for_pair(pair, prices, ohlcv_pair)
            if current_price <= 0:
                continue

            amount_dec = Decimal(str(pos.get('amount') or 0))
            total_cost_dec = Decimal(str(pos.get('total_cost') or 0))
            if amount_dec <= 0:
                continue

            avg_price = float(total_cost_dec / amount_dec) if amount_dec > 0 else 0.0
            market_value = float(amount_dec) * current_price
            cost_basis = float(total_cost_dec)
            pnl = market_value - cost_basis
            pnl_percent = (pnl / cost_basis) * 100 if cost_basis > 0 else 0.0

            positions.append({
                'pair': pair,
                'ohlcvPair': ohlcv_pair,
                'amount': float(amount_dec),
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'marketValue': market_value,
                'costBasis': cost_basis,
                'pnl': pnl,
                'pnlPercent': pnl_percent,
                'entryTimeUtc': analytics['entry_times'].get(pair),
                'realizedPnl': float(analytics['realized_pnl_by_pair'].get(pair, 0.0)),
            })

        return positions

    def _resolve_position_ohlcv_pair(self, pair: str) -> str | None:
        resolved_pair = self.kraken.get_ohlcv_pair(pair)
        if resolved_pair:
            return resolved_pair

        raw_pair = str(pair or '').strip().upper()
        if not raw_pair:
            return None
        if '/' in raw_pair:
            return raw_pair.replace('/', '')
        return raw_pair

    def get_ledger(self) -> Dict:
        """Get full ledger including balance, market value, equity, and P&L."""
        positions = self.get_positions()
        market_value = round(sum(float(position.get('marketValue') or 0.0) for position in positions), 8)
        total_pnl = round(sum(float(position.get('pnl') or 0.0) for position in positions), 8)
        analytics = self._build_position_analytics()
        realized_pnl = round(float(analytics.get('realized_pnl_total') or 0.0), 8)
        equity = round(float(self.balance) + market_value, 8)
        net_pnl = round(equity - float(self.starting_balance), 8)
        return_pct = round((net_pnl / float(self.starting_balance)) * 100, 8) if float(self.starting_balance) > 0 else 0.0

        return {
            'balance': float(self.balance),
            'startingBalance': float(self.starting_balance),
            'marketValue': market_value,
            'equity': equity,
            'totalPnL': total_pnl,
            'realizedPnL': realized_pnl,
            'netPnL': net_pnl,
            'returnPct': return_pct,
            'trades': self.trades,
            'positions': positions,
        }


kraken_service = KrakenAPIService()
crypto_ledger = CryptoPaperLedger()
