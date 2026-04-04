"""
Kraken API Integration Service
Handles crypto market-data operations via Kraken REST API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from threading import Lock
from typing import Any, Dict, List, Optional

import requests

from app.services.discord_notifications import discord_notifications
from app.services.crypto_paper_broker import crypto_paper_broker

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
        self._unresolved_pair_cache: dict[str, datetime] = {}
        self.unresolved_pair_ttl_seconds = 5 * 60

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

    def _is_unresolved_pair_cached(self, normalized: str) -> bool:
        cached_at = self._unresolved_pair_cache.get(normalized)
        if cached_at is None:
            return False
        if datetime.now(UTC) - cached_at <= timedelta(seconds=self.unresolved_pair_ttl_seconds):
            return True
        self._unresolved_pair_cache.pop(normalized, None)
        return False

    def resolve_pair(self, pair: str, *, force_refresh: bool = False) -> KrakenPairMetadata | None:
        if not self._display_pair_map:
            self.refresh_asset_pairs(force=force_refresh)
        elif force_refresh:
            self.refresh_asset_pairs(force=True)

        normalized = self._normalize_pair_alias(pair)
        metadata = self._pair_alias_map.get(normalized)
        if metadata is not None:
            self._unresolved_pair_cache.pop(normalized, None)
            return metadata

        if not force_refresh and self._is_unresolved_pair_cached(normalized):
            return None

        if not force_refresh:
            self.refresh_asset_pairs(force=True)
            metadata = self._pair_alias_map.get(normalized)
            if metadata is not None:
                self._unresolved_pair_cache.pop(normalized, None)
                return metadata

        self._unresolved_pair_cache[normalized] = datetime.now(UTC)
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
    """Compatibility wrapper around the persisted crypto paper broker service."""

    def __init__(self, starting_balance: float = 100000.0):
        self.starting_balance = Decimal(str(starting_balance))
        self.balance = Decimal(str(starting_balance))
        self.trades: List[Dict] = []
        self.positions: Dict[str, Dict] = {}
        self.kraken = KrakenAPIService()
        self._ledger_lock = RLock()

    def _price_lookup(self, pair: str, ohlcv_pair: str | None, fallback_price: float) -> float:
        resolved_pair = ohlcv_pair or self._resolve_position_ohlcv_pair(pair)
        prices = self.kraken.get_prices([resolved_pair] if resolved_pair else [pair])
        price = self._get_price_for_pair(pair, prices, resolved_pair)
        return price if price > 0 else fallback_price

    def _refresh_cache(self, db=None, *, include_trades: bool = True) -> None:
        try:
            ledger = crypto_paper_broker.get_ledger(db=db, price_lookup=self._price_lookup)
        except Exception:
            return
        self.balance = Decimal(str(ledger.get('balance') or self.starting_balance))
        if include_trades:
            self.trades = list(ledger.get('trades') or [])
        self.positions = {
            str(row.get('pair') or ''): {
                'amount': Decimal(str(row.get('amount') or 0)),
                'total_cost': Decimal(str(row.get('costBasis') or 0)),
                'entry_time_utc': row.get('entryTimeUtc'),
                'ohlcv_pair': row.get('ohlcvPair'),
            }
            for row in (ledger.get('positions') or [])
            if str(row.get('pair') or '').strip()
        }

    def _build_positions_from_cache(self) -> List[Dict]:
        rows: List[Dict] = []
        snapshot = [(str(pair), dict(pos or {})) for pair, pos in dict(self.positions or {}).items()]
        raw_pair_mappings: dict[str, str] = getattr(self, 'pair_mappings', {}) or {}
        reverse_pair_map: dict[str, str] = {str(v): str(k) for k, v in raw_pair_mappings.items()}
        for pair, pos in snapshot:
            amount_dec = Decimal(str(pos.get('amount') or 0))
            if amount_dec <= 0:
                continue
            total_cost_dec = Decimal(str(pos.get('total_cost') or 0))
            avg_price = float(total_cost_dec / amount_dec) if amount_dec > 0 else 0.0
            display_pair = reverse_pair_map.get(pair, pair)
            ohlcv_pair = pos.get('ohlcv_pair') or self._resolve_position_ohlcv_pair(display_pair)
            current_price = self._price_lookup(display_pair, ohlcv_pair, avg_price)
            market_value = float(amount_dec) * current_price
            cost_basis = float(total_cost_dec)
            pnl = market_value - cost_basis
            pnl_percent = (pnl / cost_basis) * 100 if cost_basis > 0 else 0.0
            rows.append({
                'pair': display_pair,
                'ohlcvPair': ohlcv_pair,
                'amount': float(amount_dec),
                'avgPrice': avg_price,
                'currentPrice': current_price,
                'marketValue': market_value,
                'costBasis': cost_basis,
                'pnl': pnl,
                'pnlPercent': pnl_percent,
                'entryTimeUtc': pos.get('entry_time_utc'),
                'realizedPnl': 0.0,
            })
        return rows

    def _execute_trade_in_memory(self, pair: str, ohlcv_pair: str, side: str, amount: float, price: float) -> Dict:
        amount_dec = Decimal(str(amount or 0))
        price_dec = Decimal(str(price or 0))
        if amount_dec <= 0 or price_dec <= 0:
            return {'status': 'REJECTED', 'reason': 'Invalid crypto paper trade request'}
        pair = str(pair or '').upper().strip()
        total = amount_dec * price_dec
        event_time = datetime.now(UTC).isoformat()
        if str(side or '').upper().strip() == 'BUY':
            if self.balance < total:
                return {'status': 'REJECTED', 'reason': f'Insufficient balance: {self.balance} < {total}'}
            self.balance -= total
            pos = dict(self.positions.get(pair) or {})
            old_amount = Decimal(str(pos.get('amount') or 0))
            old_cost = Decimal(str(pos.get('total_cost') or 0))
            pos['amount'] = old_amount + amount_dec
            pos['total_cost'] = old_cost + total
            pos['entry_time_utc'] = pos.get('entry_time_utc') or event_time
            pos['ohlcv_pair'] = ohlcv_pair
            self.positions[pair] = pos
        else:
            pos = dict(self.positions.get(pair) or {})
            available = Decimal(str(pos.get('amount') or 0))
            if available < amount_dec:
                return {'status': 'REJECTED', 'reason': f'Insufficient {pair} position'}
            cost = Decimal(str(pos.get('total_cost') or 0))
            avg_cost = (cost / available) if available > 0 else Decimal('0')
            closed_cost = avg_cost * amount_dec
            remaining = available - amount_dec
            self.balance += total
            if remaining <= 0:
                self.positions.pop(pair, None)
            else:
                pos['amount'] = remaining
                pos['total_cost'] = cost - closed_cost
                self.positions[pair] = pos
        trade_id = f'paper_{datetime.now(UTC).timestamp():.6f}'.replace('.', '')
        trade = {
            'id': trade_id,
            'timestamp': event_time,
            'market': 'CRYPTO',
            'pair': pair,
            'ohlcvPair': ohlcv_pair,
            'side': str(side).upper(),
            'amount': float(amount_dec),
            'price': float(price_dec),
            'total': float(total),
            'status': 'FILLED',
            'balance': float(self.balance),
        }
        self.trades.append(trade)
        return trade

    def execute_trade(self, pair: str, ohlcv_pair: str, side: str, amount: float, price: Optional[float] = None, db=None, intent_id: str | None = None, source: str | None = None) -> Dict:
        if price is None:
            ticker = self.kraken.get_ticker(ohlcv_pair)
            if not ticker or 'c' not in ticker:
                return {'status': 'REJECTED', 'reason': 'Failed to fetch current price'}
            price = float(ticker['c'][0])
        if db is None:
            trade = self._execute_trade_in_memory(pair, ohlcv_pair, side, amount, price)
        else:
            trade = crypto_paper_broker.execute_trade(
                db=db,
                pair=pair,
                ohlcv_pair=ohlcv_pair,
                side=side,
                amount=amount,
                price=price,
                source=source,
                intent_id=intent_id,
            )
        if str(trade.get('status') or '').upper() == 'FILLED':
            if db is not None:
                self._refresh_cache(db=db)
            discord_notifications.send_trade_alert(
                asset_class='crypto',
                side=side,
                symbol=pair,
                quantity=float(Decimal(str(amount or 0))),
                price=float(Decimal(str(price or 0))),
                execution_source='CRYPTO_PAPER_LEDGER',
                account_id='paper-crypto-ledger',
                status='FILLED',
                extra={'mode': 'PAPER'},
            )
            logger.info('Paper trade executed: %s %s %s @ %s', side, amount, pair, price)
        return trade

    def _get_price_for_pair(self, pair: str, prices: Dict[str, float], ohlcv_pair: str | None = None) -> float:
        metadata = self.kraken.resolve_pair(pair)
        aliases = [pair, ohlcv_pair, metadata.rest_pair if metadata is not None else None, metadata.pair_key if metadata is not None else None, metadata.altname if metadata is not None else None, metadata.ws_pair if metadata is not None else None, metadata.display_pair if metadata is not None else None]
        for alias in aliases:
            if alias in prices and prices.get(alias) is not None:
                try:
                    return float(prices[alias])
                except (TypeError, ValueError):
                    continue
        normalized_aliases = {self.kraken._normalize_pair_alias(variant) for alias in aliases for variant in self.kraken._pair_alias_variants(alias) if str(alias or '').strip()}
        normalized_aliases.discard('')
        for price_key, price_value in prices.items():
            price_variants = {self.kraken._normalize_pair_alias(variant) for variant in self.kraken._pair_alias_variants(price_key)}
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

    def get_positions(self, db=None) -> List[Dict]:
        if db is not None:
            positions = crypto_paper_broker.get_positions(db=db, price_lookup=self._price_lookup)
            self._refresh_cache(db=db)
            return positions
        return self._build_positions_from_cache()

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

    def get_ledger(self, db=None) -> Dict:
        if db is not None:
            ledger = crypto_paper_broker.get_ledger(db=db, price_lookup=self._price_lookup)
            self._refresh_cache(db=db)
            return ledger
        positions = self._build_positions_from_cache()
        market_value = round(sum(float(position.get('marketValue') or 0.0) for position in positions), 8)
        equity = round(float(self.balance) + market_value, 8)
        net_pnl = round(equity - float(self.starting_balance), 8)
        total_unrealized = round(sum(float(position.get('pnl') or 0.0) for position in positions), 8)
        realized_pnl = round(net_pnl - total_unrealized, 8)
        return {
            'balance': float(self.balance),
            'startingBalance': float(self.starting_balance),
            'marketValue': market_value,
            'equity': equity,
            'totalPnL': total_unrealized,
            'realizedPnL': realized_pnl,
            'netPnL': net_pnl,
            'returnPct': round((net_pnl / float(self.starting_balance)) * 100, 8) if float(self.starting_balance) > 0 else 0.0,
            'trades': list(self.trades),
            'positions': positions,
        }


kraken_service = KrakenAPIService()
crypto_ledger = CryptoPaperLedger()
