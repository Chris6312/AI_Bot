from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.account import Account
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.trade import Trade
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.models.watchlist_ui_context import WatchlistUiContext
from app.models.watchlist_upload import WatchlistUpload
from app.services.kraken_service import crypto_ledger, kraken_service
from app.services.market_sessions import calculate_next_scope_evaluation_at, get_scope_session_status
from app.services.runtime_state import runtime_state
from app.services.tradier_client import tradier_client

ALLOWED_SETUP_TEMPLATES = {
    'breakout_retest',
    'pullback_reclaim',
    'trend_continuation',
    'mean_reversion_bounce',
    'range_breakout',
}
ALLOWED_EXIT_TEMPLATES = {
    'scale_out_then_trail',
    'first_failed_follow_through',
    'sell_into_strength',
    'trail_after_impulse',
    'time_stop_with_structure_check',
}
ALLOWED_BIAS = {'bullish', 'bearish', 'neutral'}
ALLOWED_MARKET_REGIME = {'risk_on', 'mixed', 'risk_off'}
ALLOWED_TIERS = {'tier_1', 'tier_2', 'tier_3'}
ALLOWED_TIMEFRAMES = {'5m', '15m', '1h', '4h', '1d'}
ALLOWED_STOCK_RISK_FLAGS = {
    'earnings_nearby',
    'headline_sensitive',
    'high_beta',
    'parabolic_recent_move',
    'weak_follow_through',
    'mean_reversion_only',
    'crowded_trade',
    'low_conviction_news',
    'reversal_not_confirmed',
    'gap_risk',
    'low_liquidity',
}
ALLOWED_CRYPTO_RISK_FLAGS = {
    'headline_sensitive',
    'high_beta',
    'parabolic_recent_move',
    'weak_follow_through',
    'mean_reversion_only',
    'crowded_trade',
    'low_conviction_news',
    'reversal_not_confirmed',
}
TIMEFRAME_INTERVAL_SECONDS = {
    '5m': 300,
    '15m': 900,
    '1h': 3600,
    '4h': 14400,
    '1d': 86400,
}
WATCHLIST_SCOPE = Literal['stocks_only', 'crypto_only']
ACTIVE = 'ACTIVE'
MANAGED_ONLY = 'MANAGED_ONLY'
INACTIVE = 'INACTIVE'
PENDING_EVALUATION = 'PENDING_EVALUATION'
MONITOR_ONLY = 'MONITOR_ONLY'
INACTIVE_DECISION = 'INACTIVE'
MONITORING_OFFSET_SECONDS = 20
PROFIT_TARGET_SCALE_OUT_TEMPLATES = {'scale_out_then_trail', 'sell_into_strength'}
FOLLOW_THROUGH_EXIT_TEMPLATES = {'first_failed_follow_through'}
IMPULSE_TRAIL_TEMPLATES = {'trail_after_impulse'}
IMPULSE_TRAIL_STOP_FACTOR = 0.5
POSITION_MIRROR_SYNC_SOURCE = 'broker_position_mirror'
VALIDATION_ACCEPTED_STATUSES = {'accepted', 'valid'}
REPLAY_REJECTED = 'rejected'


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def _execution_safe_symbol_payload(symbol: BaseWatchlistSymbol | dict[str, Any]) -> dict[str, Any]:
    if isinstance(symbol, BaseWatchlistSymbol):
        payload = symbol.model_dump(mode='json')
    else:
        payload = dict(symbol or {})
    return {
        'symbol': str(payload.get('symbol') or '').strip().upper(),
        'quote_currency': str(payload.get('quote_currency') or '').strip().upper(),
        'asset_class': str(payload.get('asset_class') or '').strip().lower(),
        'enabled': bool(payload.get('enabled', False)),
        'trade_direction': str(payload.get('trade_direction') or '').strip().lower(),
        'priority_rank': int(payload.get('priority_rank') or 0),
        'tier': str(payload.get('tier') or '').strip(),
        'bias': str(payload.get('bias') or '').strip(),
        'setup_template': str(payload.get('setup_template') or '').strip(),
        'bot_timeframes': sorted({str(item).strip() for item in (payload.get('bot_timeframes') or []) if str(item).strip()}),
        'exit_template': str(payload.get('exit_template') or '').strip(),
        'max_hold_hours': int(payload.get('max_hold_hours') or 0),
        'risk_flags': sorted({str(item).strip() for item in (payload.get('risk_flags') or []) if str(item).strip()}),
    }


def _execution_safe_payload(parsed: ParsedWatchlist) -> dict[str, Any]:
    symbols = sorted(
        (_execution_safe_symbol_payload(symbol) for symbol in parsed.bot_payload.symbols),
        key=lambda item: (item['priority_rank'], item['symbol'], item['quote_currency']),
    )
    return {
        'schema_version': parsed.schema_version,
        'provider': parsed.provider,
        'scope': parsed.scope,
        'market_regime': parsed.bot_payload.market_regime,
        'symbols': symbols,
    }


class WatchlistValidationError(ValueError):
    pass


class _BaseModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class WatchlistSummary(_BaseModel):
    selected_count: int = Field(ge=0)
    primary_focus: list[str] = Field(default_factory=list)
    regime_note: str


class WatchlistUiPayload(_BaseModel):
    summary: WatchlistSummary
    provider_limitations: list[str] = Field(default_factory=list)
    symbol_context: dict[str, dict[str, Any]] = Field(default_factory=dict)


class BaseWatchlistSymbol(_BaseModel):
    symbol: str
    quote_currency: str
    asset_class: str
    enabled: bool
    trade_direction: str
    priority_rank: int = Field(ge=1)
    tier: str
    bias: str
    setup_template: str
    bot_timeframes: list[str] = Field(min_length=1)
    exit_template: str
    max_hold_hours: int = Field(ge=1, le=240)
    risk_flags: list[str] = Field(default_factory=list)

    @field_validator('symbol', 'quote_currency', 'asset_class', 'trade_direction', 'tier', 'bias', 'setup_template', 'exit_template')
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError('Value cannot be blank.')
        return cleaned

    @field_validator('trade_direction')
    @classmethod
    def _validate_trade_direction(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {'long'}:
            raise ValueError('Only long trade_direction is supported in vNext watchlists.')
        return normalized

    @field_validator('tier')
    @classmethod
    def _validate_tier(cls, value: str) -> str:
        if value not in ALLOWED_TIERS:
            raise ValueError(f'Unsupported tier: {value}')
        return value

    @field_validator('bias')
    @classmethod
    def _validate_bias(cls, value: str) -> str:
        if value not in ALLOWED_BIAS:
            raise ValueError(f'Unsupported bias: {value}')
        return value

    @field_validator('setup_template')
    @classmethod
    def _validate_setup_template(cls, value: str) -> str:
        if value not in ALLOWED_SETUP_TEMPLATES:
            raise ValueError(f'Unsupported setup_template: {value}')
        return value

    @field_validator('exit_template')
    @classmethod
    def _validate_exit_template(cls, value: str) -> str:
        if value not in ALLOWED_EXIT_TEMPLATES:
            raise ValueError(f'Unsupported exit_template: {value}')
        return value

    @field_validator('bot_timeframes')
    @classmethod
    def _validate_bot_timeframes(cls, value: list[str]) -> list[str]:
        cleaned = []
        for timeframe in value:
            timeframe_value = timeframe.strip()
            if timeframe_value not in ALLOWED_TIMEFRAMES:
                raise ValueError(f'Unsupported bot timeframe: {timeframe_value}')
            if timeframe_value not in cleaned:
                cleaned.append(timeframe_value)
        return cleaned


class StockWatchlistSymbol(BaseWatchlistSymbol):
    @field_validator('asset_class')
    @classmethod
    def _validate_asset_class(cls, value: str) -> str:
        if value != 'stock':
            raise ValueError('Stock watchlists require asset_class=stock.')
        return value

    @field_validator('risk_flags')
    @classmethod
    def _validate_risk_flags(cls, value: list[str]) -> list[str]:
        for flag in value:
            if flag not in ALLOWED_STOCK_RISK_FLAGS:
                raise ValueError(f'Unsupported stock risk flag: {flag}')
        return value


class CryptoWatchlistSymbol(BaseWatchlistSymbol):
    @field_validator('asset_class')
    @classmethod
    def _validate_asset_class(cls, value: str) -> str:
        if value != 'crypto':
            raise ValueError('Crypto watchlists require asset_class=crypto.')
        return value

    @field_validator('risk_flags')
    @classmethod
    def _validate_risk_flags(cls, value: list[str]) -> list[str]:
        for flag in value:
            if flag not in ALLOWED_CRYPTO_RISK_FLAGS:
                raise ValueError(f'Unsupported crypto risk flag: {flag}')
        return value


class StockBotPayload(_BaseModel):
    market_regime: str
    symbols: list[StockWatchlistSymbol] = Field(min_length=1, max_length=12)

    @field_validator('market_regime')
    @classmethod
    def _validate_market_regime(cls, value: str) -> str:
        if value not in ALLOWED_MARKET_REGIME:
            raise ValueError(f'Unsupported market_regime: {value}')
        return value


class CryptoBotPayload(_BaseModel):
    market_regime: str
    symbols: list[CryptoWatchlistSymbol] = Field(min_length=1, max_length=12)

    @field_validator('market_regime')
    @classmethod
    def _validate_market_regime(cls, value: str) -> str:
        if value not in ALLOWED_MARKET_REGIME:
            raise ValueError(f'Unsupported market_regime: {value}')
        return value


class StockWatchlistPayload(_BaseModel):
    schema_version: Literal['bot_stock_watchlist_v1']
    generated_at_utc: datetime
    provider: str
    scope: Literal['stocks_only']
    bot_payload: StockBotPayload
    ui_payload: WatchlistUiPayload

    @model_validator(mode='after')
    def _cross_validate(self):
        _validate_ui_consistency(self.ui_payload, self.bot_payload.symbols)
        return self


class CryptoWatchlistPayload(_BaseModel):
    schema_version: Literal['bot_watchlist_v3']
    generated_at_utc: datetime
    provider: str
    scope: Literal['crypto_only']
    bot_payload: CryptoBotPayload
    ui_payload: WatchlistUiPayload

    @model_validator(mode='after')
    def _cross_validate(self):
        _validate_ui_consistency(self.ui_payload, self.bot_payload.symbols)
        return self


ParsedWatchlist = StockWatchlistPayload | CryptoWatchlistPayload


def _validate_ui_consistency(ui_payload: WatchlistUiPayload, symbols: list[BaseWatchlistSymbol]) -> None:
    included_symbols = [item.symbol for item in symbols]
    if ui_payload.summary.selected_count != len(symbols):
        raise ValueError('ui_payload.summary.selected_count must match bot_payload.symbols length.')
    if len(set(included_symbols)) != len(included_symbols):
        raise ValueError('Duplicate symbols are not allowed in bot_payload.symbols.')
    if len({item.priority_rank for item in symbols}) != len(symbols):
        raise ValueError('priority_rank values must be unique.')

    missing_focus = [symbol for symbol in ui_payload.summary.primary_focus if symbol not in included_symbols]
    if missing_focus:
        raise ValueError(f'primary_focus contains symbols not present in bot_payload.symbols: {missing_focus}')

    unknown_context = [symbol for symbol in ui_payload.symbol_context if symbol not in included_symbols]
    if unknown_context:
        raise ValueError(f'symbol_context contains unknown symbols: {unknown_context}')


def _normalize_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decision_for_status(status: str) -> tuple[str, str]:
    if status == ACTIVE:
        return PENDING_EVALUATION, 'Awaiting first deterministic template evaluation.'
    if status == MANAGED_ONLY:
        return MONITOR_ONLY, 'Symbol is no longer eligible for new entries but still has an open position to manage.'
    return INACTIVE_DECISION, 'Symbol is inactive and no longer scheduled for evaluation.'


class WatchlistService:
    def parse_payload(self, payload: dict[str, Any]) -> ParsedWatchlist:
        schema_version = str(payload.get('schema_version', '')).strip()
        try:
            if schema_version == 'bot_stock_watchlist_v1':
                return StockWatchlistPayload.model_validate(payload)
            if schema_version == 'bot_watchlist_v3':
                return CryptoWatchlistPayload.model_validate(payload)
        except ValidationError as exc:
            raise WatchlistValidationError(exc.json()) from exc
        raise WatchlistValidationError(f'Unsupported watchlist schema_version: {schema_version or "(missing)"}')

    def validate_freshness(self, generated_at: datetime) -> dict[str, Any]:
        generated_at_utc = _normalize_dt(generated_at)
        now = datetime.now(UTC)
        age_seconds = (now - generated_at_utc).total_seconds()
        max_age = max(60, int(settings.WATCHLIST_MAX_AGE_SECONDS))
        if age_seconds < -300:
            raise WatchlistValidationError('Watchlist generated_at_utc is in the future.')
        if age_seconds > max_age:
            raise WatchlistValidationError(f'Watchlist payload is stale ({int(age_seconds)}s old).')
        return {
            'generatedAtUtc': generated_at_utc,
            'observedAtUtc': now,
            'ageSeconds': max(0, int(age_seconds)),
            'maxAgeSeconds': max_age,
        }

    def ingest_watchlist(
        self,
        db: Session,
        payload: dict[str, Any],
        *,
        source: str,
        source_user_id: str | None = None,
        source_channel_id: str | None = None,
        source_message_id: str | None = None,
    ) -> dict[str, Any]:
        parsed = self.parse_payload(payload)
        freshness = self.validate_freshness(parsed.generated_at_utc)

        payload_hash = _stable_hash(payload)
        execution_payload = _execution_safe_payload(parsed)
        execution_payload_hash = _stable_hash(execution_payload)
        upload_id = f'wlu_{uuid.uuid4().hex[:20]}'
        scan_id = f'scan_{uuid.uuid4().hex[:20]}'
        expires_at = freshness['generatedAtUtc'] + timedelta(hours=max(1, int(settings.WATCHLIST_DEFAULT_EXPIRY_HOURS)))
        now = freshness['observedAtUtc']

        replay_conflict = self._detect_replay_conflict(
            db,
            scope=parsed.scope,
            payload_hash=payload_hash,
            execution_payload_hash=execution_payload_hash,
            observed_at=now,
        )
        if replay_conflict is not None:
            self._record_rejected_upload(
                db,
                upload_id=upload_id,
                scan_id=scan_id,
                parsed=parsed,
                raw_payload=payload,
                payload_hash=payload_hash,
                execution_payload_hash=execution_payload_hash,
                freshness=freshness,
                source=source,
                source_user_id=source_user_id,
                source_channel_id=source_channel_id,
                source_message_id=source_message_id,
                rejection_reason=replay_conflict['reason'],
                rejection_validation={
                    'freshness': {
                        'generatedAtUtc': freshness['generatedAtUtc'].isoformat(),
                        'observedAtUtc': freshness['observedAtUtc'].isoformat(),
                        'ageSeconds': freshness['ageSeconds'],
                        'maxAgeSeconds': freshness['maxAgeSeconds'],
                    },
                    'selectedCount': len(parsed.bot_payload.symbols),
                    'primaryFocus': parsed.ui_payload.summary.primary_focus,
                    'replay': replay_conflict,
                },
            )
            raise WatchlistValidationError(replay_conflict['reason'])

        next_symbols = {
            self._normalize_scope_symbol(
                scope=parsed.scope,
                symbol=symbol.symbol,
                quote_currency=symbol.quote_currency,
            )
            for symbol in parsed.bot_payload.symbols
        }
        next_symbols.discard('')
        self._deactivate_scope_uploads(db, parsed.scope)
        self._reconcile_rows_before_new_upload(db, parsed.scope, next_symbols, observed_at=now)

        upload = WatchlistUpload(
            upload_id=upload_id,
            scan_id=scan_id,
            schema_version=parsed.schema_version,
            provider=parsed.provider,
            scope=parsed.scope,
            source=source,
            source_user_id=source_user_id,
            source_channel_id=source_channel_id,
            source_message_id=source_message_id,
            payload_hash=payload_hash,
            generated_at_utc=freshness['generatedAtUtc'],
            received_at_utc=now,
            watchlist_expires_at_utc=expires_at,
            validation_status='valid',
            rejection_reason=None,
            market_regime=parsed.bot_payload.market_regime,
            selected_count=len(parsed.bot_payload.symbols),
            is_active=True,
            validation_result_json={
                'freshness': {
                    'generatedAtUtc': freshness['generatedAtUtc'].isoformat(),
                    'observedAtUtc': freshness['observedAtUtc'].isoformat(),
                    'ageSeconds': freshness['ageSeconds'],
                    'maxAgeSeconds': freshness['maxAgeSeconds'],
                },
                'selectedCount': len(parsed.bot_payload.symbols),
                'primaryFocus': parsed.ui_payload.summary.primary_focus,
                'replay': {
                    'windowSeconds': max(60, int(settings.WATCHLIST_REPLAY_WINDOW_SECONDS)),
                    'payloadHash': payload_hash,
                    'executionPayloadHash': execution_payload_hash,
                    'status': 'accepted',
                },
            },
            raw_payload_json=payload,
            bot_payload_json=parsed.bot_payload.model_dump(mode='json'),
        )
        db.add(upload)
        db.flush()

        symbol_rows: list[WatchlistSymbol] = []
        for symbol in parsed.bot_payload.symbols:
            row = WatchlistSymbol(
                upload_id=upload_id,
                scope=parsed.scope,
                symbol=symbol.symbol,
                quote_currency=symbol.quote_currency,
                asset_class=symbol.asset_class,
                enabled=symbol.enabled,
                trade_direction=symbol.trade_direction,
                priority_rank=symbol.priority_rank,
                tier=symbol.tier,
                bias=symbol.bias,
                setup_template=symbol.setup_template,
                bot_timeframes=symbol.bot_timeframes,
                exit_template=symbol.exit_template,
                max_hold_hours=symbol.max_hold_hours,
                risk_flags=symbol.risk_flags,
                monitoring_status=ACTIVE,
            )
            db.add(row)
            symbol_rows.append(row)
        db.flush()

        for row in symbol_rows:
            self._upsert_monitor_state(db, row, observed_at=now)

        db.add(
            WatchlistUiContext(
                upload_id=upload_id,
                summary_json=parsed.ui_payload.summary.model_dump(mode='json'),
                provider_limitations_json=parsed.ui_payload.provider_limitations,
                symbol_context_json=parsed.ui_payload.symbol_context,
            )
        )

        db.commit()
        db.refresh(upload)
        return self.serialize_upload(db, upload)

    def reconcile_scope_statuses(self, db: Session, *, scope: WATCHLIST_SCOPE) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        self._backfill_missing_monitor_states(db, scope=scope, observed_at=observed_at)
        active_upload = self._get_latest_upload_row(db, scope=scope, active_only=True)
        active_symbols: set[str] = set()
        if active_upload is not None:
            active_symbols = {
                self._normalize_scope_symbol(
                    scope=scope,
                    symbol=row.symbol,
                    quote_currency=row.quote_currency,
                )
                for row in db.query(WatchlistSymbol).filter(WatchlistSymbol.upload_id == active_upload.upload_id).all()
            }
            active_symbols.discard('')

        broker_positions: dict[str, dict[str, Any]] | None = None
        if scope == 'stocks_only':
            broker_positions = self._get_open_stock_broker_positions()
            self._sync_stock_position_mirror_from_broker(db, observed_at=observed_at, broker_positions=broker_positions)
            open_symbols = self._get_open_stock_symbols(db, broker_positions=broker_positions)
        else:
            open_symbols = self._get_open_symbols(db, scope)
        candidate_rows = db.query(WatchlistSymbol).filter(WatchlistSymbol.scope == scope).all()

        changed = 0
        for row in candidate_rows:
            next_status = self._resolve_row_status(
                scope=scope,
                row_upload_id=row.upload_id,
                active_upload_id=active_upload.upload_id if active_upload else None,
                symbol=row.symbol,
                quote_currency=row.quote_currency,
                active_symbols=active_symbols,
                open_symbols=open_symbols,
            )
            if row.monitoring_status != next_status:
                row.monitoring_status = next_status
                changed += 1
            self._upsert_monitor_state(db, row, observed_at=observed_at)

        if changed:
            db.commit()
            if active_upload is not None:
                db.refresh(active_upload)
        else:
            db.rollback()

        active_upload = self._get_latest_upload_row(db, scope=scope, active_only=True)
        managed_only_rows = self.get_managed_only_rows(db, scope=scope)
        status_counts = self.get_scope_status_counts(db, scope=scope)
        return {
            'scope': scope,
            'activeUploadId': active_upload.upload_id if active_upload else None,
            'managedOnlyCount': len(managed_only_rows),
            'statusCounts': status_counts,
            'changedRows': changed,
        }

    def get_latest_upload(self, db: Session, *, scope: WATCHLIST_SCOPE | None = None, active_only: bool = False) -> Any:
        query = db.query(WatchlistUpload)
        if scope is not None:
            query = query.filter(WatchlistUpload.scope == scope)
        if active_only:
            query = query.filter(WatchlistUpload.is_active.is_(True))
        uploads = query.order_by(WatchlistUpload.received_at_utc.desc(), WatchlistUpload.id.desc()).all()
        if scope is not None:
            upload = uploads[0] if uploads else None
            return self.serialize_upload(db, upload) if upload else None
        grouped: dict[str, dict[str, Any]] = {}
        for upload in uploads:
            if upload.scope in grouped:
                continue
            grouped[upload.scope] = self.serialize_upload(db, upload)
        return grouped

    def get_ai_decision_feed(self, db: Session, *, limit: int = 50) -> list[dict[str, Any]]:
        requested_limit = max(1, min(int(limit or 50), 500))
        upload_fetch_limit = max(3, min(requested_limit, 25))
        uploads = (
            db.query(WatchlistUpload)
            .order_by(WatchlistUpload.received_at_utc.desc(), WatchlistUpload.id.desc())
            .limit(upload_fetch_limit)
            .all()
        )

        decisions: list[dict[str, Any]] = []
        for upload in uploads:
            if len(decisions) >= requested_limit:
                break

            ui_context = (
                db.query(WatchlistUiContext)
                .filter(WatchlistUiContext.upload_id == upload.upload_id)
                .order_by(WatchlistUiContext.id.desc())
                .first()
            )
            summary_json = (ui_context.summary_json if ui_context else {}) or {}
            symbol_context_json = (ui_context.symbol_context_json if ui_context else {}) or {}
            primary_focus = {
                str(item or '').upper()
                for item in summary_json.get('primary_focus', [])
                if str(item or '').strip()
            }
            symbols = (
                db.query(WatchlistSymbol)
                .filter(WatchlistSymbol.upload_id == upload.upload_id)
                .order_by(WatchlistSymbol.priority_rank.asc(), WatchlistSymbol.id.asc())
                .all()
            )
            for row in symbols:
                if len(decisions) >= requested_limit:
                    break

                symbol_key = str(row.symbol or '').upper()
                context = symbol_context_json.get(row.symbol) or symbol_context_json.get(symbol_key) or {}
                reasoning = self._build_ai_decision_reasoning(
                    upload=upload,
                    row=row,
                    symbol_context=context if isinstance(context, dict) else {},
                    summary_json=summary_json if isinstance(summary_json, dict) else {},
                )
                decisions.append(
                    {
                        'id': f'{upload.upload_id}:{symbol_key}:{row.priority_rank}',
                        'timestamp': (
                            upload.received_at_utc or upload.generated_at_utc or datetime.now(UTC)
                        ).isoformat(),
                        'type': 'SCREENING',
                        'market': 'CRYPTO' if upload.scope == 'crypto_only' else 'STOCK',
                        'symbol': row.symbol,
                        'confidence': self._estimate_ai_confidence(row, primary_focus=primary_focus),
                        'reasoning': reasoning,
                        'executed': False,
                        'rejected': not self._is_accepted_validation_status(upload.validation_status),
                        'rejectionReason': upload.rejection_reason,
                        'vix': None,
                    }
                )

        return decisions[:requested_limit]

    def get_monitoring_snapshot(
        self,
        db: Session,
        *,
        scope: WATCHLIST_SCOPE | None = None,
        include_inactive: bool = False,
        broker_enrichment: bool = False,
    ) -> Any:
        observed_at = datetime.now(UTC)
        scopes: list[WATCHLIST_SCOPE] = [scope] if scope is not None else ['stocks_only', 'crypto_only']
        result: dict[str, Any] = {}
        for scope_value in scopes:
            self._backfill_missing_monitor_states(db, scope=scope_value, observed_at=observed_at)
            query = (
                db.query(WatchlistMonitorState, WatchlistSymbol)
                .join(WatchlistSymbol, WatchlistSymbol.id == WatchlistMonitorState.watchlist_symbol_id)
                .filter(WatchlistMonitorState.scope == scope_value)
            )
            if not include_inactive:
                query = query.filter(WatchlistMonitorState.monitoring_status.in_([ACTIVE, MANAGED_ONLY]))
            pairs = query.order_by(
                WatchlistMonitorState.monitoring_status.asc(),
                WatchlistSymbol.priority_rank.asc(),
                WatchlistSymbol.id.asc(),
            ).all()
            active_upload = self._get_latest_upload_row(db, scope=scope_value, active_only=True)
            effective_broker_enrichment = broker_enrichment or scope_value == 'stocks_only'
            position_state_map = self._build_position_state_map(
                db,
                scope=scope_value,
                observed_at=observed_at,
                broker_enrichment=effective_broker_enrichment,
            )
            rows = self._build_deduped_monitoring_rows(
                pairs,
                position_state_map=position_state_map,
                broker_enrichment=broker_enrichment,
                active_upload_id=active_upload.upload_id if active_upload else None,
            )
            next_eval = min(
                (row['monitoring']['nextEvaluationAtUtc'] for row in rows if row.get('monitoring') and row['monitoring']['nextEvaluationAtUtc']),
                default=None,
            )
            last_eval = max(
                (row['monitoring']['lastEvaluatedAtUtc'] for row in rows if row.get('monitoring') and row['monitoring']['lastEvaluatedAtUtc']),
                default=None,
            )
            entry_submitted_states = {'SUBMISSION_PENDING', 'ENTRY_SUBMITTED'}
            entry_rejected_states = {'GATE_REJECTED', 'SUBMISSION_REJECTED'}
            summary = {
                    'total': len(rows),
                    'activeCount': sum(1 for row in rows if row['monitoringStatus'] == ACTIVE),
                    'managedOnlyCount': sum(1 for row in rows if row['monitoringStatus'] == MANAGED_ONLY),
                    'inactiveCount': sum(1 for row in rows if row['monitoringStatus'] == INACTIVE),
                    'pendingEvaluationCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == PENDING_EVALUATION),
                    'entryCandidateCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'ENTRY_CANDIDATE'),
                    'entrySubmittedCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] in entry_submitted_states),
                    'entryFilledCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'ENTRY_FILLED'),
                    'entryRejectedCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] in entry_rejected_states),
                    'entrySkippedCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'SKIPPED'),
                    'gateRejectedCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'GATE_REJECTED'),
                    'submissionRejectedCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'SUBMISSION_REJECTED'),
                    'waitingForSetupCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'WAITING_FOR_SETUP'),
                    'dataStaleCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'DATA_STALE'),
                    'dataUnavailableCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'DATA_UNAVAILABLE'),
                    'biasConflictCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'BIAS_CONFLICT'),
                    'evaluationBlockedCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'EVALUATION_BLOCKED'),
                    'monitorOnlyCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == MONITOR_ONLY),
                    'skippedCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == 'SKIPPED'),
                    'inactiveDecisionCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == INACTIVE_DECISION),
                    'openPositionCount': sum(1 for row in rows if row.get('positionState', {}).get('hasOpenPosition')),
                    'expiredPositionCount': sum(1 for row in rows if row.get('positionState', {}).get('positionExpired')),
                    'protectiveExitPendingCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('protectiveExitPending')
                    ),
                    'stopLossBreachedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('stopLossBreached')
                    ),
                    'trailingStopBreachedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('trailingStopBreached')
                    ),
                    'profitTargetReachedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('profitTargetReached')
                    ),
                    'scaleOutReadyCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('scaleOutReady')
                    ),
                    'followThroughFailedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('followThroughFailed')
                    ),
                    'impulseTrailArmedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('impulseTrailArmed')
                    ),
                    'timeStopExtendedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('timeStopExtended')
                    ),
                    'expiringWithin24hCount': sum(
                        1
                        for row in rows
                        if row.get('positionState', {}).get('hasOpenPosition')
                        and row.get('positionState', {}).get('positionExpired') is False
                        and row.get('positionState', {}).get('hoursUntilExpiry') is not None
                        and float(row['positionState']['hoursUntilExpiry']) <= 24.0
                    ),
                    'nextEvaluationAtUtc': next_eval,
                    'lastEvaluatedAtUtc': last_eval,
                }
            active_upload_received_at = active_upload.received_at_utc.isoformat() if active_upload and active_upload.received_at_utc else None
            watchlist_expires_at = active_upload.watchlist_expires_at_utc.isoformat() if active_upload and active_upload.watchlist_expires_at_utc else None
            watchlist_expired = bool(
                active_upload
                and active_upload.watchlist_expires_at_utc
                and _normalize_dt(active_upload.watchlist_expires_at_utc) <= observed_at
            )
            data_warning_count = int(summary['dataStaleCount'] + summary['dataUnavailableCount'] + summary['evaluationBlockedCount'])
            supervision_only = bool(summary['managedOnlyCount'] > 0 and summary['activeCount'] <= 0)
            fresh_entry_state = 'READY'
            fresh_entries_ready = bool(active_upload is not None and not watchlist_expired and summary['activeCount'] > 0)
            review_state = 'OK'
            review_reason = ''
            operational_state = 'HEALTHY'
            operational_reason = ''
            operational_impairment = False
            scope_truth_ready = True

            if active_upload is None:
                fresh_entry_state = 'MISSING'
                if summary['openPositionCount'] > 0 or summary['managedOnlyCount'] > 0:
                    scope_truth_state = 'PAUSED'
                    scope_truth_reason = 'No active watchlist is loaded for fresh entries. Existing positions remain under supervision.'
                    review_state = 'REVIEW'
                    review_reason = 'Fresh entries are paused until a new watchlist is loaded for this scope.'
                else:
                    scope_truth_state = 'MISSING'
                    scope_truth_reason = 'No active watchlist upload is loaded for this scope.'
                    scope_truth_ready = False
                    review_state = 'REVIEW'
                    review_reason = scope_truth_reason
            elif watchlist_expired:
                fresh_entry_state = 'STALE'
                scope_truth_state = 'STALE'
                scope_truth_reason = 'Active watchlist freshness has expired for fresh entries. Supervision remains active.'
                review_state = 'REVIEW'
                review_reason = 'Fresh entries are stale until a newer watchlist is loaded.'
            else:
                if summary['activeCount'] <= 0:
                    fresh_entry_state = 'PAUSED'
                if summary['managedOnlyCount'] > 0:
                    scope_truth_state = 'DEGRADED'
                    scope_truth_ready = False
                    if supervision_only:
                        fresh_entries_ready = False
                        scope_truth_reason = 'Scope is supervision-only. Managed-only rows remain, but no fresh-entry symbols are eligible.'
                    else:
                        scope_truth_reason = 'Scope has supervision-only rows that require review before fresh-entry monitoring is considered healthy.'
                    review_state = 'REVIEW'
                    review_reason = (
                        'Fresh entries are paused because this scope is currently supervision-only.'
                        if supervision_only
                        else 'Some rows are supervision-only while other symbols remain eligible for fresh-entry evaluation.'
                    )
                elif data_warning_count > 0:
                    scope_truth_state = 'HEALTHY'
                    scope_truth_reason = 'Scope is healthy. Some rows are waiting on normal data or evaluation review.'
                    review_state = 'REVIEW'
                    review_reason = 'One or more rows are waiting on data, setup confirmation, or another valid review gate.'
                else:
                    scope_truth_state = 'HEALTHY'
                    scope_truth_reason = 'Scope is healthy and monitoring is operating normally.'

            scope_truth = {
                'scope': scope_value,
                'state': scope_truth_state,
                'ready': scope_truth_ready and not operational_impairment,
                'reason': scope_truth_reason,
                'freshnessState': 'MISSING' if active_upload is None else ('STALE' if watchlist_expired else 'FRESH'),
                'freshEntryState': fresh_entry_state,
                'freshEntriesReady': fresh_entries_ready,
                'reviewState': review_state,
                'reviewReason': review_reason,
                'operationalState': operational_state,
                'operationalReason': operational_reason,
                'operationalImpairment': operational_impairment,
                'supervisionOnly': supervision_only,
                'activeUploadId': active_upload.upload_id if active_upload else None,
                'activeUploadReceivedAtUtc': active_upload_received_at,
                'watchlistExpiresAtUtc': watchlist_expires_at,
                'watchlistExpired': watchlist_expired,
                'activeSymbolCount': int(summary['activeCount']),
                'managedOnlyCount': int(summary['managedOnlyCount']),
                'openPositionCount': int(summary['openPositionCount']),
                'dataWarningCount': data_warning_count,
            }
            result[scope_value] = {
                'scope': scope_value,
                'capturedAtUtc': observed_at.isoformat(),
                'activeUploadId': active_upload.upload_id if active_upload else None,
                'scopeTruth': scope_truth,
                'summary': summary,
                'rows': rows,
            }
        return result[scope] if scope is not None else result


    def get_exit_readiness_snapshot(
        self,
        db: Session,
        *,
        scope: WATCHLIST_SCOPE | None = None,
        expiring_within_hours: int = 24,
        broker_enrichment: bool = False,
    ) -> Any:
        monitoring_snapshot = self.get_monitoring_snapshot(db, scope=scope, include_inactive=False, broker_enrichment=broker_enrichment)
        scopes: dict[str, Any] = monitoring_snapshot if scope is None else {scope: monitoring_snapshot}
        result: dict[str, Any] = {}
        for scope_value, snapshot in scopes.items():
            rows = [row for row in snapshot['rows'] if row.get('positionState', {}).get('hasOpenPosition')]
            due_rows = [row for row in rows if row.get('positionState', {}).get('positionExpired')]
            expiring_rows = [
                row
                for row in rows
                if row.get('positionState', {}).get('positionExpired') is False
                and row.get('positionState', {}).get('hoursUntilExpiry') is not None
                and float(row['positionState']['hoursUntilExpiry']) <= float(expiring_within_hours)
            ]
            result[scope_value] = {
                'scope': scope_value,
                'capturedAtUtc': snapshot['capturedAtUtc'],
                'activeUploadId': snapshot['activeUploadId'],
                'expiringWithinHours': expiring_within_hours,
                'summary': {
                    'openPositionCount': len(rows),
                    'expiredPositionCount': len(due_rows),
                    'expiringWithinWindowCount': len(expiring_rows),
                    'protectiveExitPendingCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('protectiveExitPending')
                    ),
                    'stopLossBreachedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('stopLossBreached')
                    ),
                    'trailingStopBreachedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('trailingStopBreached')
                    ),
                    'profitTargetReachedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('profitTargetReached')
                    ),
                    'scaleOutReadyCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('scaleOutReady')
                    ),
                    'followThroughFailedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('followThroughFailed')
                    ),
                    'impulseTrailArmedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('impulseTrailArmed')
                    ),
                    'timeStopExtendedCount': sum(
                        1 for row in rows if row.get('positionState', {}).get('timeStopExtended')
                    ),
                    'managedOnlyOpenCount': sum(1 for row in rows if row.get('managedOnly')),
                },
                'rows': rows,
            }
        return result[scope] if scope is not None else result

    def get_managed_only_rows(self, db: Session, *, scope: WATCHLIST_SCOPE) -> list[WatchlistSymbol]:
        rows = (
            db.query(WatchlistSymbol)
            .filter(
                WatchlistSymbol.scope == scope,
                WatchlistSymbol.monitoring_status == MANAGED_ONLY,
            )
            .order_by(WatchlistSymbol.priority_rank.asc(), WatchlistSymbol.id.desc())
            .all()
        )
        deduped: list[WatchlistSymbol] = []
        seen: set[str] = set()
        for row in rows:
            key = self._normalize_scope_symbol(
                scope=scope,
                symbol=row.symbol,
                quote_currency=getattr(row, 'quote_currency', None),
            )
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped

    def _crypto_symbol_aliases(self, symbol: str | None, quote_currency: str | None = None) -> set[str]:
        raw = str(symbol or '').upper().strip()
        if not raw:
            return set()

        normalized_quote = ''.join(char for char in str(quote_currency or 'USD').upper().strip() if char.isalnum()) or 'USD'
        aliases: set[str] = set()
        resolver_candidates: set[str] = set()

        def add_alias(value: str | None, *, assume_quote_pair: bool = False) -> None:
            normalized = str(value or '').upper().strip()
            if not normalized:
                return

            aliases.add(normalized)
            compact = ''.join(char for char in normalized if char.isalnum())
            if compact:
                aliases.add(compact)
                resolver_candidates.add(compact)

            resolver_candidates.add(normalized)

            if '/' in normalized:
                base, _, quote = normalized.partition('/')
                if base:
                    aliases.add(base)
                if base and quote:
                    aliases.add(f'{base}/{quote}')
                    aliases.add(f'{base}{quote}')
                    resolver_candidates.add(f'{base}/{quote}')
                    resolver_candidates.add(f'{base}{quote}')
            elif normalized.endswith(normalized_quote) and len(normalized) > len(normalized_quote):
                base = normalized[:-len(normalized_quote)]
                if base:
                    aliases.add(base)
                    aliases.add(f'{base}/{normalized_quote}')
                    aliases.add(f'{base}{normalized_quote}')
                    resolver_candidates.add(f'{base}/{normalized_quote}')
                    resolver_candidates.add(f'{base}{normalized_quote}')
            elif assume_quote_pair and compact:
                aliases.add(f'{compact}/{normalized_quote}')
                aliases.add(f'{compact}{normalized_quote}')
                resolver_candidates.add(f'{compact}/{normalized_quote}')
                resolver_candidates.add(f'{compact}{normalized_quote}')

        add_alias(raw, assume_quote_pair=True)

        resolved_fields: set[str] = set()
        for candidate in list(resolver_candidates):
            resolved = kraken_service.resolve_pair(candidate)
            if resolved is None:
                continue
            resolved_fields.update(
                value
                for value in (
                    resolved.display_pair,
                    resolved.ws_pair,
                    resolved.altname,
                    resolved.rest_pair,
                    resolved.pair_key,
                )
                if value
            )

        for value in resolved_fields:
            add_alias(value)

        return {alias for alias in aliases if alias}

    def _normalize_crypto_symbol_key(self, symbol: str | None, quote_currency: str | None = None) -> str:
        raw = str(symbol or '').upper().strip()
        if not raw:
            return ''

        normalized_quote = ''.join(char for char in str(quote_currency or 'USD').upper().strip() if char.isalnum()) or 'USD'
        compact = ''.join(char for char in raw if char.isalnum())
        resolver_candidates = [raw]
        if compact:
            resolver_candidates.append(compact)
            resolver_candidates.append(f'{compact}/{normalized_quote}')
            resolver_candidates.append(f'{compact}{normalized_quote}')

        for candidate in resolver_candidates:
            resolved = kraken_service.resolve_pair(candidate)
            if resolved is None:
                continue
            display_pair = str(resolved.display_pair or '').upper().strip()
            if '/' in display_pair:
                return display_pair.split('/', 1)[0]
            if display_pair:
                return ''.join(char for char in display_pair if char.isalnum())

        if '/' in raw:
            return raw.split('/', 1)[0]
        if compact.endswith(normalized_quote) and len(compact) > len(normalized_quote):
            return compact[:-len(normalized_quote)]
        return compact

    def _normalize_scope_symbol(self, *, scope: WATCHLIST_SCOPE, symbol: str | None, quote_currency: str | None = None) -> str:
        raw = str(symbol or '').upper().strip()
        if not raw:
            return ''
        if scope != 'crypto_only':
            return ''.join(char for char in raw if char.isalnum())
        return self._normalize_crypto_symbol_key(raw, quote_currency)

    def _build_status_summary(
        self,
        *,
        scope: WATCHLIST_SCOPE,
        active_symbols: list[WatchlistSymbol],
        managed_only_symbols: list[WatchlistSymbol],
        historical_rows: list[WatchlistSymbol],
    ) -> dict[str, int]:
        selected_keys = {
            self._normalize_scope_symbol(scope=scope, symbol=row.symbol, quote_currency=row.quote_currency)
            for row in active_symbols
        }
        selected_keys.discard('')

        healthy_keys = {
            self._normalize_scope_symbol(scope=scope, symbol=row.symbol, quote_currency=row.quote_currency)
            for row in active_symbols
            if row.monitoring_status == ACTIVE
        }
        healthy_keys.discard('')

        managed_only_keys = {
            self._normalize_scope_symbol(scope=scope, symbol=row.symbol, quote_currency=row.quote_currency)
            for row in managed_only_symbols
        }
        managed_only_keys.discard('')
        managed_only_keys -= selected_keys

        unmanaged_keys = {
            self._normalize_scope_symbol(scope=scope, symbol=row.symbol, quote_currency=row.quote_currency)
            for row in historical_rows
            if row.monitoring_status == INACTIVE
        }
        unmanaged_keys.discard('')
        unmanaged_keys -= selected_keys
        unmanaged_keys -= managed_only_keys

        return {
            'activeCount': len(healthy_keys),
            'managedOnlyCount': len(managed_only_keys),
            'inactiveCount': len(unmanaged_keys),
        }

    def get_scope_status_counts(self, db: Session, *, scope: WATCHLIST_SCOPE) -> dict[str, int]:
        rows = db.query(WatchlistSymbol).filter(WatchlistSymbol.scope == scope).all()
        counts = {ACTIVE: 0, MANAGED_ONLY: 0, INACTIVE: 0}
        for row in rows:
            counts[row.monitoring_status] = counts.get(row.monitoring_status, 0) + 1
        return counts

    def serialize_upload(self, db: Session, upload: WatchlistUpload | None) -> dict[str, Any]:
        if upload is None:
            return {}
        self._backfill_missing_monitor_states(db, scope=upload.scope, observed_at=datetime.now(UTC))
        symbols = (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.upload_id == upload.upload_id)
            .order_by(WatchlistSymbol.priority_rank.asc(), WatchlistSymbol.id.asc())
            .all()
        )
        monitor_states = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.watchlist_symbol_id.in_([row.id for row in symbols] or [-1]))
            .all()
        )
        monitor_by_symbol_id = {row.watchlist_symbol_id: row for row in monitor_states}
        ui_context = (
            db.query(WatchlistUiContext)
            .filter(WatchlistUiContext.upload_id == upload.upload_id)
            .order_by(WatchlistUiContext.id.desc())
            .first()
        )
        symbol_rows = [self._serialize_symbol_row(row, monitor_state=monitor_by_symbol_id.get(row.id)) for row in symbols]
        managed_only_rows = self.get_managed_only_rows(db, scope=upload.scope) if upload.is_active else []
        managed_monitor_states = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.watchlist_symbol_id.in_([row.id for row in managed_only_rows] or [-1]))
            .all()
        )
        managed_monitor_by_symbol_id = {row.watchlist_symbol_id: row for row in managed_monitor_states}
        managed_only_symbols = [
            self._serialize_symbol_row(row, monitor_state=managed_monitor_by_symbol_id.get(row.id))
            for row in managed_only_rows
            if row.upload_id != upload.upload_id
        ]
        historical_rows = db.query(WatchlistSymbol).filter(WatchlistSymbol.scope == upload.scope).all()
        status_summary = self._build_status_summary(
            scope=upload.scope,
            active_symbols=symbols,
            managed_only_symbols=[row for row in managed_only_rows if row.upload_id != upload.upload_id],
            historical_rows=historical_rows,
        )
        return {
            'uploadId': upload.upload_id,
            'scanId': upload.scan_id,
            'schemaVersion': upload.schema_version,
            'provider': upload.provider,
            'scope': upload.scope,
            'source': upload.source,
            'sourceUserId': upload.source_user_id,
            'sourceChannelId': upload.source_channel_id,
            'sourceMessageId': upload.source_message_id,
            'payloadHash': upload.payload_hash,
            'generatedAtUtc': upload.generated_at_utc.isoformat() if upload.generated_at_utc else None,
            'receivedAtUtc': upload.received_at_utc.isoformat() if upload.received_at_utc else None,
            'watchlistExpiresAtUtc': upload.watchlist_expires_at_utc.isoformat() if upload.watchlist_expires_at_utc else None,
            'validationStatus': upload.validation_status,
            'rejectionReason': upload.rejection_reason,
            'marketRegime': upload.market_regime,
            'selectedCount': upload.selected_count,
            'isActive': upload.is_active,
            'validation': upload.validation_result_json or {},
            'symbols': symbol_rows,
            'managedOnlySymbols': managed_only_symbols,
            'statusSummary': status_summary,
            'monitoringSummary': self.get_monitoring_snapshot(db, scope=upload.scope)['summary'],
            'uiPayload': {
                'summary': (ui_context.summary_json if ui_context else {}),
                'providerLimitations': (ui_context.provider_limitations_json if ui_context else []),
                'symbolContext': (ui_context.symbol_context_json if ui_context else {}),
            },
        }

    def _detect_replay_conflict(
        self,
        db: Session,
        *,
        scope: WATCHLIST_SCOPE,
        payload_hash: str,
        execution_payload_hash: str,
        observed_at: datetime,
    ) -> dict[str, Any] | None:
        window_seconds = max(60, int(settings.WATCHLIST_REPLAY_WINDOW_SECONDS))
        cutoff = observed_at - timedelta(seconds=window_seconds)
        recent_uploads = (
            db.query(WatchlistUpload)
            .filter(
                WatchlistUpload.scope == scope,
                WatchlistUpload.received_at_utc >= cutoff,
            )
            .order_by(WatchlistUpload.received_at_utc.desc(), WatchlistUpload.id.desc())
            .all()
        )
        for upload in recent_uploads:
            existing_payload_hash = str(upload.payload_hash or '').strip()
            existing_execution_payload_hash = self._get_upload_execution_payload_hash(upload)
            if existing_payload_hash == payload_hash:
                return self._build_replay_conflict_payload(
                    upload=upload,
                    observed_at=observed_at,
                    window_seconds=window_seconds,
                    replay_type='exact_duplicate',
                    reason='Duplicate watchlist payload suppressed within replay window.',
                )
            if existing_execution_payload_hash and existing_execution_payload_hash == execution_payload_hash:
                return self._build_replay_conflict_payload(
                    upload=upload,
                    observed_at=observed_at,
                    window_seconds=window_seconds,
                    replay_type='execution_duplicate',
                    reason='Equivalent execution-safe watchlist payload suppressed within replay window.',
                )
        return None

    def _record_rejected_upload(
        self,
        db: Session,
        *,
        upload_id: str,
        scan_id: str,
        parsed: ParsedWatchlist,
        raw_payload: dict[str, Any],
        payload_hash: str,
        execution_payload_hash: str,
        freshness: dict[str, Any],
        source: str,
        source_user_id: str | None,
        source_channel_id: str | None,
        source_message_id: str | None,
        rejection_reason: str,
        rejection_validation: dict[str, Any],
    ) -> None:
        upload = WatchlistUpload(
            upload_id=upload_id,
            scan_id=scan_id,
            schema_version=parsed.schema_version,
            provider=parsed.provider,
            scope=parsed.scope,
            source=source,
            source_user_id=source_user_id,
            source_channel_id=source_channel_id,
            source_message_id=source_message_id,
            payload_hash=payload_hash,
            generated_at_utc=freshness['generatedAtUtc'],
            received_at_utc=freshness['observedAtUtc'],
            watchlist_expires_at_utc=freshness['generatedAtUtc'] + timedelta(hours=max(1, int(settings.WATCHLIST_DEFAULT_EXPIRY_HOURS))),
            validation_status=REPLAY_REJECTED,
            rejection_reason=rejection_reason,
            market_regime=parsed.bot_payload.market_regime,
            selected_count=len(parsed.bot_payload.symbols),
            is_active=False,
            validation_result_json={
                **(rejection_validation or {}),
                'replay': {
                    **((rejection_validation or {}).get('replay') or {}),
                    'windowSeconds': max(60, int(settings.WATCHLIST_REPLAY_WINDOW_SECONDS)),
                    'payloadHash': payload_hash,
                    'executionPayloadHash': execution_payload_hash,
                    'status': 'rejected',
                },
            },
            raw_payload_json=raw_payload,
            bot_payload_json=parsed.bot_payload.model_dump(mode='json'),
        )
        db.add(upload)
        db.commit()

    @staticmethod
    def _build_replay_conflict_payload(
        *,
        upload: WatchlistUpload,
        observed_at: datetime,
        window_seconds: int,
        replay_type: str,
        reason: str,
    ) -> dict[str, Any]:
        received_at = _normalize_dt(upload.received_at_utc) if upload.received_at_utc else observed_at
        return {
            'status': 'rejected',
            'reason': reason,
            'type': replay_type,
            'windowSeconds': window_seconds,
            'duplicateOfUploadId': upload.upload_id,
            'duplicateOfReceivedAtUtc': received_at.isoformat(),
            'duplicateOfValidationStatus': upload.validation_status,
            'ageSeconds': max(0, int((observed_at - received_at).total_seconds())),
        }

    @staticmethod
    def _get_upload_execution_payload_hash(upload: WatchlistUpload) -> str:
        validation_payload = upload.validation_result_json or {}
        replay_payload = validation_payload.get('replay') if isinstance(validation_payload, dict) else {}
        replay_hash = ''
        if isinstance(replay_payload, dict):
            replay_hash = str(replay_payload.get('executionPayloadHash') or '').strip()
        if replay_hash:
            return replay_hash

        raw_payload = upload.raw_payload_json if isinstance(upload.raw_payload_json, dict) else {}
        schema_version = str(raw_payload.get('schema_version') or '').strip()
        if schema_version not in {'bot_stock_watchlist_v1', 'bot_watchlist_v3'}:
            return ''
        try:
            parsed = watchlist_service.parse_payload(raw_payload)
        except WatchlistValidationError:
            return ''
        return _stable_hash(_execution_safe_payload(parsed))

    def _deactivate_scope_uploads(self, db: Session, scope: WATCHLIST_SCOPE) -> None:
        db.query(WatchlistUpload).filter(
            WatchlistUpload.scope == scope,
            WatchlistUpload.is_active.is_(True),
        ).update({'is_active': False}, synchronize_session=False)

    def _reconcile_rows_before_new_upload(
        self,
        db: Session,
        scope: WATCHLIST_SCOPE,
        next_symbols: set[str],
        *,
        observed_at: datetime,
    ) -> None:
        open_symbols = self._get_open_symbols(db, scope)
        candidate_rows = (
            db.query(WatchlistSymbol)
            .filter(WatchlistSymbol.scope == scope)
            .order_by(WatchlistSymbol.id.desc())
            .all()
        )
        managed_only_assigned: set[str] = set()
        for row in candidate_rows:
            symbol = self._normalize_scope_symbol(scope=scope, symbol=row.symbol, quote_currency=row.quote_currency)
            if symbol in next_symbols:
                row.monitoring_status = INACTIVE
            elif symbol in open_symbols and symbol not in managed_only_assigned:
                row.monitoring_status = MANAGED_ONLY
                managed_only_assigned.add(symbol)
            else:
                row.monitoring_status = INACTIVE
            self._upsert_monitor_state(db, row, observed_at=observed_at)

    def _build_deduped_monitoring_rows(
        self,
        pairs: list[tuple[WatchlistMonitorState, WatchlistSymbol]],
        *,
        position_state_map: dict[str, dict[str, Any]],
        broker_enrichment: bool,
        active_upload_id: str | None,
    ) -> list[dict[str, Any]]:
        selected: dict[str, tuple[WatchlistMonitorState, WatchlistSymbol]] = {}
        for monitor_row, symbol_row in pairs:
            symbol_key = self._normalize_scope_symbol(
                scope=symbol_row.scope,
                symbol=symbol_row.symbol,
                quote_currency=symbol_row.quote_currency,
            )
            if not symbol_key:
                continue
            current = selected.get(symbol_key)
            if current is None or self._prefer_monitor_pair(
                candidate=(monitor_row, symbol_row),
                current=current,
                active_upload_id=active_upload_id,
            ):
                selected[symbol_key] = (monitor_row, symbol_row)

        ordered_pairs = sorted(
            selected.values(),
            key=lambda item: (
                self._monitor_status_sort_key(item[0].monitoring_status),
                item[1].priority_rank if item[1].priority_rank is not None else 999999,
                -(item[1].id or 0),
            ),
        )
        return [
            self._serialize_monitor_row(
                symbol_row,
                monitor_row,
                position_state=position_state_map.get(
                    self._normalize_scope_symbol(
                        scope=symbol_row.scope,
                        symbol=symbol_row.symbol,
                        quote_currency=symbol_row.quote_currency,
                    )
                ),
                broker_enrichment=broker_enrichment,
            )
            for monitor_row, symbol_row in ordered_pairs
        ]

    @staticmethod
    def _monitor_status_sort_key(status: str | None) -> int:
        order = {ACTIVE: 0, MANAGED_ONLY: 1, INACTIVE: 2}
        return order.get(str(status or '').upper(), 99)

    def _prefer_monitor_pair(
        self,
        *,
        candidate: tuple[WatchlistMonitorState, WatchlistSymbol],
        current: tuple[WatchlistMonitorState, WatchlistSymbol],
        active_upload_id: str | None,
    ) -> bool:
        candidate_monitor, candidate_symbol = candidate
        current_monitor, current_symbol = current
        candidate_active = bool(active_upload_id and candidate_symbol.upload_id == active_upload_id)
        current_active = bool(active_upload_id and current_symbol.upload_id == active_upload_id)
        if candidate_active != current_active:
            return candidate_active

        candidate_status = self._monitor_status_sort_key(candidate_monitor.monitoring_status)
        current_status = self._monitor_status_sort_key(current_monitor.monitoring_status)
        if candidate_status != current_status:
            return candidate_status < current_status

        candidate_eval = candidate_monitor.last_evaluated_at_utc or candidate_monitor.last_decision_at_utc
        current_eval = current_monitor.last_evaluated_at_utc or current_monitor.last_decision_at_utc
        if candidate_eval and current_eval and candidate_eval != current_eval:
            return candidate_eval > current_eval
        if candidate_eval and not current_eval:
            return True
        if current_eval and not candidate_eval:
            return False

        return (candidate_symbol.id or 0) > (current_symbol.id or 0)

    def _get_latest_upload_row(
        self,
        db: Session,
        *,
        scope: WATCHLIST_SCOPE,
        active_only: bool,
    ) -> WatchlistUpload | None:
        query = db.query(WatchlistUpload).filter(WatchlistUpload.scope == scope)
        if active_only:
            query = query.filter(WatchlistUpload.is_active.is_(True))
        return query.order_by(WatchlistUpload.received_at_utc.desc(), WatchlistUpload.id.desc()).first()

    def _get_open_symbols(self, db: Session, scope: WATCHLIST_SCOPE) -> set[str]:
        if scope == 'stocks_only':
            return self._get_open_stock_symbols(db)
        return self._get_open_crypto_symbols()

    def _get_open_stock_symbols(
        self,
        db: Session,
        *,
        broker_positions: dict[str, dict[str, Any]] | None = None,
    ) -> set[str]:
        open_symbols = {
            str(row.ticker or '').upper()
            for row in db.query(Position).filter(Position.is_open.is_(True)).all()
            if str(row.ticker or '').strip()
        }
        if broker_positions is None:
            broker_positions = self._get_open_stock_broker_positions()
        open_symbols.update(broker_positions.keys())
        return open_symbols

    def _get_open_stock_broker_symbols(self) -> set[str]:
        return set(self._get_open_stock_broker_positions().keys())

    def _get_open_stock_broker_positions(self) -> dict[str, dict[str, Any]]:
        try:
            mode = runtime_state.get().stock_mode
            try:
                positions = tradier_client.get_positions_snapshot(mode, include_quotes=False)
            except TypeError:
                positions = tradier_client.get_positions_snapshot(mode)
        except Exception:
            return {}

        broker_positions: dict[str, dict[str, Any]] = {}
        for position in positions:
            symbol = str(position.get('symbol') or '').upper().strip()
            shares = int(round(float(position.get('shares') or 0))) if position.get('shares') is not None else 0
            if not symbol or shares <= 0:
                continue
            broker_positions[symbol] = position
        return broker_positions


    @staticmethod
    def _is_broker_sync_position(position: Position) -> bool:
        reasoning = position.entry_reasoning if isinstance(position.entry_reasoning, dict) else {}
        return str(reasoning.get('syncSource') or '').strip().lower() == POSITION_MIRROR_SYNC_SOURCE

    def _sync_stock_position_mirror_from_broker(
        self,
        db: Session,
        *,
        observed_at: datetime,
        broker_positions: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        if broker_positions is None:
            broker_positions = self._get_open_stock_broker_positions()

        open_rows = (
            db.query(Position)
            .filter(Position.is_open.is_(True))
            .order_by(Position.id.desc())
            .all()
        )
        rows_by_symbol: dict[str, list[Position]] = {}
        for row in open_rows:
            symbol = str(row.ticker or '').upper().strip()
            if not symbol:
                continue
            rows_by_symbol.setdefault(symbol, []).append(row)

        inserted = 0
        updated = 0
        closed = 0
        repaired = 0

        for symbol, rows in rows_by_symbol.items():
            if symbol in broker_positions:
                continue
            synthetic_rows = [row for row in rows if self._is_broker_sync_position(row)]
            for row in synthetic_rows:
                if row.is_open:
                    row.is_open = False
                    row.shares = 0
                    row.updated_at = observed_at
                    closed += 1

        for symbol, broker_position in broker_positions.items():
            rows = rows_by_symbol.get(symbol, [])
            synthetic_row = next((row for row in rows if self._is_broker_sync_position(row)), None)
            if synthetic_row is not None:
                if self._apply_broker_snapshot_to_position_row(
                    synthetic_row,
                    broker_position,
                    observed_at=observed_at,
                    reconciliation_source='broker_position_mirror',
                ):
                    updated += 1
                    repaired += 1
                continue

            if rows:
                if len(rows) == 1 and self._apply_broker_snapshot_to_position_row(
                    rows[0],
                    broker_position,
                    observed_at=observed_at,
                    reconciliation_source='broker_position_mirror',
                ):
                    updated += 1
                    repaired += 1
                continue

            watchlist_row = (
                db.query(WatchlistSymbol)
                .filter(WatchlistSymbol.scope == 'stocks_only', WatchlistSymbol.symbol == symbol)
                .order_by(WatchlistSymbol.id.desc())
                .first()
            )
            seed = self._resolve_stock_position_seed(
                db,
                symbol=symbol,
                watchlist_row=watchlist_row,
                broker_position=broker_position,
                observed_at=observed_at,
            )
            entry_price = seed['avgEntryPrice']
            current_price = seed['currentPrice']
            entry_reasoning = dict(seed['entryReasoning'] or {})
            entry_reasoning['syncSource'] = 'broker_position_mirror'
            entry_reasoning['syncedAtUtc'] = observed_at.isoformat()
            entry_reasoning['reconciliation'] = {
                'event': 'POSITION_RESTORED_FROM_BROKER',
                'observedAtUtc': observed_at.isoformat(),
                'symbol': symbol,
                'brokerSnapshot': {
                    'shares': seed['shares'],
                    'avgPrice': seed['avgEntryPrice'],
                    'currentPrice': current_price,
                    'unrealizedPnl': seed['unrealizedPnl'],
                    'unrealizedPnlPct': seed['unrealizedPnlPct'],
                },
            }
            new_row = Position(
                account_id=seed['accountId'],
                ticker=symbol,
                shares=seed['shares'],
                avg_entry_price=entry_price,
                current_price=current_price,
                unrealized_pnl=seed['unrealizedPnl'],
                unrealized_pnl_pct=seed['unrealizedPnlPct'],
                strategy=seed['strategy'],
                entry_time=seed['entryTime'],
                entry_reasoning=entry_reasoning,
                stop_loss=seed['stopLoss'],
                profit_target=seed['profitTarget'],
                peak_price=max(float(current_price or 0.0), float(entry_price or 0.0)),
                trailing_stop=seed['trailingStop'],
                is_open=True,
                execution_id=seed['executionId'],
            )
            db.add(new_row)
            inserted += 1
            repaired += 1

        if inserted or updated or closed:
            db.commit()
        else:
            db.rollback()
        return {
            'inserted': inserted,
            'updated': updated,
            'closed': closed,
            'repaired': repaired,
        }

    def _apply_broker_snapshot_to_position_row(
        self,
        row: Position,
        broker_position: dict[str, Any],
        *,
        observed_at: datetime,
        reconciliation_source: str | None = None,
    ) -> bool:
        changed = False
        broker_shares = int(round(float(broker_position.get('shares') or 0.0))) if broker_position.get('shares') is not None else 0
        broker_avg = float(broker_position.get('avgPrice') or 0.0) if broker_position.get('avgPrice') is not None else None
        broker_current = float(broker_position.get('currentPrice') or 0.0) if broker_position.get('currentPrice') is not None else None
        broker_pnl = float(broker_position.get('pnl') or 0.0) if broker_position.get('pnl') is not None else None
        broker_pnl_pct = float(broker_position.get('pnlPercent') or 0.0) if broker_position.get('pnlPercent') is not None else None

        updates = {
            'shares': broker_shares,
            'avg_entry_price': broker_avg,
            'current_price': broker_current,
            'unrealized_pnl': broker_pnl,
            'unrealized_pnl_pct': broker_pnl_pct,
        }
        for field_name, field_value in updates.items():
            if getattr(row, field_name) != field_value and field_value is not None:
                setattr(row, field_name, field_value)
                changed = True

        reference_price = max(float(row.peak_price or 0.0), float(broker_current or 0.0), float(broker_avg or 0.0))
        if reference_price > float(row.peak_price or 0.0):
            row.peak_price = reference_price
            changed = True

        if reconciliation_source:
            entry_reasoning = dict(row.entry_reasoning or {})
            entry_reasoning['syncSource'] = reconciliation_source
            entry_reasoning['syncedAtUtc'] = observed_at.isoformat()
            entry_reasoning['reconciliation'] = {
                'event': 'POSITION_RESTORED_FROM_BROKER',
                'observedAtUtc': observed_at.isoformat(),
                'brokerSnapshot': {
                    'shares': broker_shares,
                    'avgPrice': broker_avg,
                    'currentPrice': broker_current,
                    'unrealizedPnl': broker_pnl,
                    'unrealizedPnlPct': broker_pnl_pct,
                },
            }
            if row.entry_reasoning != entry_reasoning:
                row.entry_reasoning = entry_reasoning
                changed = True

        if changed:
            row.updated_at = observed_at
        return changed

    def _resolve_existing_account_id(
        self,
        db: Session,
        *,
        candidate_account_id: Any,
    ) -> str | None:
        account_id = str(candidate_account_id or '').strip()
        if not account_id:
            return None
        existing = (
            db.query(Account.account_id)
            .filter(Account.account_id == account_id)
            .first()
        )
        return account_id if existing is not None else None

    def _resolve_stock_position_seed(
        self,
        db: Session,
        *,
        symbol: str,
        watchlist_row: WatchlistSymbol | None,
        broker_position: dict[str, Any],
        observed_at: datetime,
    ) -> dict[str, Any]:
        latest_buy_intent = (
            db.query(OrderIntent)
            .filter(
                OrderIntent.asset_class == 'stock',
                OrderIntent.symbol == symbol,
                OrderIntent.side.in_(['BUY', 'buy']),
                OrderIntent.status.in_(['FILLED', 'PARTIALLY_FILLED']),
            )
            .order_by(OrderIntent.last_fill_at.desc(), OrderIntent.first_fill_at.desc(), OrderIntent.submitted_at.desc(), OrderIntent.created_at.desc())
            .first()
        )
        avg_entry_price = float(broker_position.get('avgPrice') or 0.0) if broker_position.get('avgPrice') is not None else 0.0
        current_price = float(broker_position.get('currentPrice') or 0.0) if broker_position.get('currentPrice') is not None else avg_entry_price
        entry_time = observed_at
        if latest_buy_intent is not None:
            entry_time = latest_buy_intent.last_fill_at or latest_buy_intent.first_fill_at or latest_buy_intent.submitted_at or latest_buy_intent.created_at or observed_at

        seeded_account_id = latest_buy_intent.account_id if latest_buy_intent is not None else None
        resolved_account_id = self._resolve_existing_account_id(db, candidate_account_id=seeded_account_id)
        seed_account_missing = bool(seeded_account_id) and resolved_account_id is None

        strategy = str(watchlist_row.setup_template or '').strip() if watchlist_row is not None else ''
        if not strategy:
            strategy = 'WATCHLIST_ENTRY'
        stop_loss = avg_entry_price * (1 - settings.STOP_LOSS_PCT) if avg_entry_price > 0 else 0.0
        profit_target = avg_entry_price * (1 + settings.PROFIT_TARGET_PCT) if avg_entry_price > 0 else 0.0
        trailing_stop = avg_entry_price * (1 - settings.TRAILING_STOP_PCT) if avg_entry_price > 0 else None
        entry_reasoning = {
            'syncSource': POSITION_MIRROR_SYNC_SOURCE,
            'syncedAtUtc': observed_at.isoformat(),
            'brokerSnapshot': {
                'shares': int(round(float(broker_position.get('shares') or 0.0))),
                'avgPrice': avg_entry_price,
                'currentPrice': current_price,
                'marketValue': float(broker_position.get('marketValue') or 0.0),
                'pnl': float(broker_position.get('pnl') or 0.0),
                'pnlPercent': float(broker_position.get('pnlPercent') or 0.0),
            },
            'watchlist': {
                'setupTemplate': watchlist_row.setup_template if watchlist_row is not None else None,
                'exitTemplate': watchlist_row.exit_template if watchlist_row is not None else None,
                'maxHoldHours': watchlist_row.max_hold_hours if watchlist_row is not None else None,
            },
        }
        if latest_buy_intent is not None:
            entry_reasoning['seedIntentId'] = latest_buy_intent.intent_id
            entry_reasoning['seedIntentStatus'] = latest_buy_intent.status
            entry_reasoning['seedExecutionSource'] = latest_buy_intent.execution_source
        if seeded_account_id:
            entry_reasoning['seedAccountId'] = seeded_account_id
        if seed_account_missing:
            entry_reasoning['seedAccountMissingFromAccounts'] = True
        return {
            'accountId': resolved_account_id,
            'shares': int(round(float(broker_position.get('shares') or 0.0))),
            'avgEntryPrice': avg_entry_price,
            'currentPrice': current_price,
            'unrealizedPnl': float(broker_position.get('pnl') or 0.0) if broker_position.get('pnl') is not None else None,
            'unrealizedPnlPct': float(broker_position.get('pnlPercent') or 0.0) if broker_position.get('pnlPercent') is not None else None,
            'strategy': strategy,
            'entryTime': entry_time,
            'entryReasoning': entry_reasoning,
            'stopLoss': float(stop_loss),
            'profitTarget': float(profit_target),
            'trailingStop': float(trailing_stop) if trailing_stop is not None else None,
            'executionId': latest_buy_intent.intent_id if latest_buy_intent is not None else None,
        }

    def _get_open_crypto_symbols(self) -> set[str]:
        open_symbols: set[str] = set()
        try:
            positions = crypto_ledger.get_positions()
        except Exception:
            positions = []
        for position in positions:
            raw_pair = str(position.get('pair') or position.get('symbol') or '').upper().strip()
            if not raw_pair:
                continue
            normalized = self._normalize_crypto_symbol_key(raw_pair)
            if normalized:
                open_symbols.add(normalized)
        return open_symbols

    def _has_open_crypto_position(self, symbol: str | None, quote_currency: str | None = None) -> bool:
        normalized = self._normalize_crypto_symbol_key(symbol, quote_currency)
        return bool(normalized and normalized in self._get_open_crypto_symbols())

    def _resolve_row_status(
        self,
        *,
        scope: WATCHLIST_SCOPE,
        row_upload_id: str,
        active_upload_id: str | None,
        symbol: str,
        quote_currency: str | None,
        active_symbols: set[str],
        open_symbols: set[str],
    ) -> str:
        symbol_value = self._normalize_scope_symbol(scope=scope, symbol=symbol, quote_currency=quote_currency)
        if active_upload_id and row_upload_id == active_upload_id:
            return ACTIVE
        if symbol_value in open_symbols and symbol_value not in active_symbols:
            return MANAGED_ONLY
        return INACTIVE

    def _backfill_missing_monitor_states(self, db: Session, *, scope: WATCHLIST_SCOPE, observed_at: datetime) -> None:
        rows = db.query(WatchlistSymbol).filter(WatchlistSymbol.scope == scope).all()
        changed = False
        for row in rows:
            if self._upsert_monitor_state(db, row, observed_at=observed_at):
                changed = True
        if changed:
            db.commit()

    def _upsert_monitor_state(self, db: Session, row: WatchlistSymbol, *, observed_at: datetime) -> bool:
        monitor_state = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.watchlist_symbol_id == row.id)
            .first()
        )
        decision_state, decision_reason = _decision_for_status(row.monitoring_status)
        interval_seconds = self._calculate_evaluation_interval_seconds(row.bot_timeframes or [])
        next_evaluation_at = self._calculate_next_evaluation_at(row.scope, observed_at, interval_seconds) if row.monitoring_status != INACTIVE else None
        base_context = {
            'setupTemplate': row.setup_template,
            'exitTemplate': row.exit_template,
            'botTimeframes': row.bot_timeframes,
            'tradeDirection': row.trade_direction,
            'bias': row.bias,
            'tier': row.tier,
            'riskFlags': row.risk_flags,
            'maxHoldHours': row.max_hold_hours,
        }
        changed = False
        if monitor_state is None:
            monitor_state = WatchlistMonitorState(
                watchlist_symbol_id=row.id,
                upload_id=row.upload_id,
                scope=row.scope,
                symbol=row.symbol,
                monitoring_status=row.monitoring_status,
                latest_decision_state=decision_state,
                latest_decision_reason=decision_reason,
                decision_context_json=base_context,
                required_timeframes_json=row.bot_timeframes,
                evaluation_interval_seconds=interval_seconds,
                last_decision_at_utc=observed_at,
                last_evaluated_at_utc=None,
                next_evaluation_at_utc=next_evaluation_at,
                last_market_data_at_utc=None,
            )
            db.add(monitor_state)
            return True

        status_changed = monitor_state.monitoring_status != row.monitoring_status
        if status_changed:
            monitor_state.monitoring_status = row.monitoring_status
            changed = True

        required_timeframes_changed = monitor_state.required_timeframes_json != row.bot_timeframes
        interval_changed = monitor_state.evaluation_interval_seconds != interval_seconds
        should_reset_decision = status_changed or row.monitoring_status in {MANAGED_ONLY, INACTIVE}
        if should_reset_decision and monitor_state.latest_decision_state != decision_state:
            monitor_state.latest_decision_state = decision_state
            changed = True
        if should_reset_decision and monitor_state.latest_decision_reason != decision_reason:
            monitor_state.latest_decision_reason = decision_reason
            changed = True
        merged_context = dict(monitor_state.decision_context_json or {})
        merged_context.update(base_context)
        if monitor_state.decision_context_json != merged_context:
            monitor_state.decision_context_json = merged_context
            changed = True
        if required_timeframes_changed:
            monitor_state.required_timeframes_json = row.bot_timeframes
            changed = True
        if interval_changed:
            monitor_state.evaluation_interval_seconds = interval_seconds
            changed = True

        next_evaluation_update = monitor_state.next_evaluation_at_utc
        if row.monitoring_status == INACTIVE:
            next_evaluation_update = None
        elif status_changed or required_timeframes_changed or interval_changed or monitor_state.next_evaluation_at_utc is None:
            next_evaluation_update = next_evaluation_at

        if monitor_state.next_evaluation_at_utc != next_evaluation_update:
            monitor_state.next_evaluation_at_utc = next_evaluation_update
            changed = True
        if changed:
            monitor_state.upload_id = row.upload_id
            monitor_state.scope = row.scope
            monitor_state.symbol = row.symbol
            monitor_state.last_decision_at_utc = observed_at
        return changed

    @staticmethod
    def _calculate_evaluation_interval_seconds(timeframes: list[str]) -> int | None:
        intervals = [TIMEFRAME_INTERVAL_SECONDS[item] for item in timeframes if item in TIMEFRAME_INTERVAL_SECONDS]
        if not intervals:
            return None
        return min(intervals)

    def _calculate_next_evaluation_at(self, scope: WATCHLIST_SCOPE, reference_time: datetime, interval_seconds: int | None) -> datetime | None:
        return calculate_next_scope_evaluation_at(scope, reference_time, interval_seconds)

    def _serialize_monitor_row(
        self,
        row: WatchlistSymbol,
        monitor_state: WatchlistMonitorState | None,
        *,
        position_state: dict[str, Any] | None = None,
        broker_enrichment: bool = False,
    ) -> dict[str, Any]:
        symbol_payload = self._serialize_symbol_row(row, monitor_state=monitor_state)
        position_state = position_state or {}
        monitoring_payload = symbol_payload.get('monitoring') or {}
        if position_state.get('hasOpenPosition'):
            protective_reasons = list(position_state.get('protectiveExitReasons') or [])
            if position_state.get('protectiveExitPending'):
                symbol_payload['monitoringStatus'] = 'EXIT_PENDING'
                monitoring_payload['latestDecisionState'] = 'EXIT_PENDING'
                monitoring_payload['latestDecisionReason'] = ', '.join(protective_reasons) if protective_reasons else 'Protective exit threshold breached for the open position.'
            elif position_state.get('positionExpired'):
                symbol_payload['monitoringStatus'] = 'EXIT_PENDING'
                monitoring_payload['latestDecisionState'] = 'EXIT_PENDING'
                monitoring_payload['latestDecisionReason'] = 'Open position exceeded max hold duration and is pending exit management.'
            elif position_state.get('scaleOutReady'):
                symbol_payload['monitoringStatus'] = 'EXIT_PENDING'
                monitoring_payload['latestDecisionState'] = 'EXIT_PENDING'
                monitoring_payload['latestDecisionReason'] = 'Profit target reached and the position is ready for scale-out management.'
            elif position_state.get('followThroughFailed'):
                symbol_payload['monitoringStatus'] = 'EXIT_PENDING'
                monitoring_payload['latestDecisionState'] = 'EXIT_PENDING'
                monitoring_payload['latestDecisionReason'] = 'First failed follow-through exit condition triggered for the open position.'
            elif row.scope == 'crypto_only' and row.monitoring_status == ACTIVE:
                monitoring_payload['latestDecisionState'] = 'SKIPPED'
                monitoring_payload['latestDecisionReason'] = 'OPEN_POSITION_EXISTS'
        derived_monitoring_status = self._derive_runtime_monitoring_status(row, position_state) if broker_enrichment else None
        if derived_monitoring_status is not None:
            symbol_payload['monitoringStatus'] = derived_monitoring_status
            if derived_monitoring_status == 'EXIT_PENDING':
                monitoring_payload['latestDecisionState'] = 'EXIT_PENDING'
                monitoring_payload['latestDecisionReason'] = 'Broker already has an open stock exit order for this symbol.'
            elif derived_monitoring_status == 'WAITING_FOR_MARKET_OPEN':
                monitoring_payload['latestDecisionState'] = 'WAITING_FOR_MARKET_OPEN'
                monitoring_payload['latestDecisionReason'] = 'Stock exit is queued, but the market session is currently closed.'
        if monitoring_payload:
            symbol_payload['monitoring'] = monitoring_payload
        symbol_payload['managedOnly'] = row.monitoring_status == MANAGED_ONLY
        symbol_payload['positionState'] = position_state or {
            'hasOpenPosition': False,
            'basePositionExpiresAtUtc': None,
            'positionExpiresAtUtc': None,
            'positionExpired': False,
            'hoursUntilExpiry': None,
            'hoursSinceEntry': None,
            'followThroughWindowHours': None,
            'followThroughFailed': False,
            'timeStopStructureCheckPassed': False,
            'timeStopExtended': False,
            'timeStopExtensionHours': None,
            'timeStopExtendedUntilUtc': None,
            'exitDeadlineSource': None,
            'protectiveExitPending': False,
            'protectiveExitReasons': [],
            'stopLossBreached': False,
            'trailingStopBreached': False,
            'profitTargetReached': False,
            'scaleOutReady': False,
            'scaleOutAlreadyTaken': False,
            'impulseTrailArmed': False,
            'impulseTrailingStop': None,
            'peakPrice': None,
        }
        return symbol_payload

    def _build_position_state_map(
        self,
        db: Session,
        *,
        scope: WATCHLIST_SCOPE,
        observed_at: datetime,
        broker_enrichment: bool = False,
    ) -> dict[str, dict[str, Any]]:
        if scope == 'stocks_only':
            return self._build_stock_position_state_map(db, observed_at=observed_at, broker_enrichment=broker_enrichment)
        return self._build_crypto_position_state_map(db, observed_at=observed_at)

    def _build_stock_position_state_map(self, db: Session, *, observed_at: datetime, broker_enrichment: bool = False) -> dict[str, dict[str, Any]]:
        broker_positions = self._get_open_stock_broker_positions() if broker_enrichment else {}
        if broker_enrichment:
            self._sync_stock_position_mirror_from_broker(db, observed_at=observed_at, broker_positions=broker_positions)
        state_map: dict[str, dict[str, Any]] = {}
        positions = (
            db.query(Position)
            .filter(Position.is_open.is_(True))
            .order_by(Position.entry_time.asc(), Position.id.asc())
            .all()
        )
        for position in positions:
            symbol = str(position.ticker or '').upper().strip()
            if not symbol:
                continue
            entry_time = position.entry_time
            if entry_time is not None and entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=UTC)
            watchlist_row = (
                db.query(WatchlistSymbol)
                .filter(WatchlistSymbol.scope == 'stocks_only', WatchlistSymbol.symbol == symbol)
                .order_by(WatchlistSymbol.id.desc())
                .first()
            )
            max_hold_hours = int(watchlist_row.max_hold_hours) if watchlist_row is not None and watchlist_row.max_hold_hours is not None else None
            exit_template = str(watchlist_row.exit_template or '').strip().lower() if watchlist_row is not None and watchlist_row.exit_template is not None else ''
            trade = self._resolve_trade_for_position(db, position)
            profit_scale_out_taken = self._trade_has_partial_trigger(trade, 'PROFIT_TARGET_REACHED')
            base_expires_at = entry_time + timedelta(hours=max_hold_hours) if entry_time is not None and max_hold_hours is not None else None
            hours_since_entry = None
            if entry_time is not None:
                hours_since_entry = round((observed_at - entry_time).total_seconds() / 3600.0, 2)
            avg_entry_price = float(position.avg_entry_price or 0.0) if position.avg_entry_price is not None else None
            current_price = float(position.current_price or 0.0) if position.current_price is not None else None
            stop_loss = float(position.stop_loss or 0.0) if position.stop_loss is not None else None
            trailing_stop = float(position.trailing_stop or 0.0) if position.trailing_stop is not None else None
            peak_price = float(position.peak_price or 0.0) if position.peak_price is not None else None
            stop_loss_breached = bool(
                current_price is not None and stop_loss is not None and stop_loss > 0 and current_price <= stop_loss
            )
            trailing_stop_breached = bool(
                current_price is not None
                and trailing_stop is not None
                and trailing_stop > 0
                and current_price <= trailing_stop
            )
            protective_exit_reasons: list[str] = []
            if stop_loss_breached:
                protective_exit_reasons.append('STOP_LOSS_BREACH')
            if trailing_stop_breached:
                protective_exit_reasons.append('TRAILING_STOP_BREACH')
            profit_target = float(position.profit_target or 0.0) if position.profit_target is not None else None
            profit_target_reached = bool(
                current_price is not None
                and profit_target is not None
                and profit_target > 0
                and current_price >= profit_target
            )
            scale_out_ready = bool(
                profit_target_reached
                and exit_template in PROFIT_TARGET_SCALE_OUT_TEMPLATES
                and not profit_scale_out_taken
            )
            follow_through_window_hours = self._resolve_follow_through_window_hours(max_hold_hours)
            follow_through_failed = bool(
                exit_template in FOLLOW_THROUGH_EXIT_TEMPLATES
                and avg_entry_price is not None
                and avg_entry_price > 0
                and current_price is not None
                and current_price < avg_entry_price
                and hours_since_entry is not None
                and follow_through_window_hours is not None
                and 1.0 <= hours_since_entry <= follow_through_window_hours
                and not stop_loss_breached
                and not trailing_stop_breached
            )
            impulse_trail_armed = bool(
                exit_template in IMPULSE_TRAIL_TEMPLATES
                and profit_target_reached
            )
            impulse_reference_price = max(float(peak_price or 0.0), float(current_price or 0.0))
            impulse_trailing_stop = self._calculate_impulse_trailing_stop(impulse_reference_price) if impulse_trail_armed else None
            (
                effective_expires_at,
                position_expired,
                hours_until_expiry,
                time_stop_structure_check_passed,
                time_stop_extended,
                time_stop_extension_hours,
                time_stop_extended_until,
                exit_deadline_source,
            ) = self._apply_structure_time_stop(
                exit_template=exit_template,
                base_expires_at=base_expires_at,
                observed_at=observed_at,
                max_hold_hours=max_hold_hours,
                avg_entry_price=avg_entry_price,
                current_price=current_price,
                stop_loss_breached=stop_loss_breached,
                trailing_stop_breached=trailing_stop_breached,
            )
            is_broker_sync_position = self._is_broker_sync_position(position)
            state_map[symbol] = {
                'hasOpenPosition': True,
                'accountId': None if is_broker_sync_position else position.account_id,
                'positionId': None if is_broker_sync_position else position.id,
                'positionSource': 'broker' if is_broker_sync_position else 'database',
                'positionSyncGap': is_broker_sync_position,
                'shares': int(position.shares or 0),
                'avgEntryPrice': avg_entry_price,
                'currentPrice': current_price,
                'stopLoss': stop_loss,
                'profitTarget': profit_target,
                'trailingStop': trailing_stop,
                'peakPrice': peak_price,
                'profitTargetReached': profit_target_reached,
                'scaleOutReady': scale_out_ready,
                'scaleOutAlreadyTaken': profit_scale_out_taken,
                'impulseTrailArmed': impulse_trail_armed,
                'impulseTrailingStop': impulse_trailing_stop,
                'entryTimeUtc': entry_time.isoformat() if entry_time else None,
                'maxHoldHours': max_hold_hours,
                'basePositionExpiresAtUtc': base_expires_at.isoformat() if base_expires_at else None,
                'positionExpiresAtUtc': effective_expires_at.isoformat() if effective_expires_at else None,
                'positionExpired': position_expired,
                'hoursUntilExpiry': hours_until_expiry,
                'hoursSinceEntry': hours_since_entry,
                'followThroughWindowHours': follow_through_window_hours,
                'followThroughFailed': follow_through_failed,
                'timeStopStructureCheckPassed': time_stop_structure_check_passed,
                'timeStopExtended': time_stop_extended,
                'timeStopExtensionHours': time_stop_extension_hours,
                'timeStopExtendedUntilUtc': time_stop_extended_until.isoformat() if time_stop_extended_until else None,
                'exitDeadlineSource': exit_deadline_source,
                'protectiveExitPending': bool(protective_exit_reasons),
                'protectiveExitReasons': protective_exit_reasons,
                'stopLossBreached': stop_loss_breached,
                'trailingStopBreached': trailing_stop_breached,
            }

        for symbol, broker_position in broker_positions.items():
            if symbol in state_map:
                continue
            watchlist_row = (
                db.query(WatchlistSymbol)
                .filter(WatchlistSymbol.scope == 'stocks_only', WatchlistSymbol.symbol == symbol)
                .order_by(WatchlistSymbol.id.desc())
                .first()
            )
            max_hold_hours = int(watchlist_row.max_hold_hours) if watchlist_row is not None and watchlist_row.max_hold_hours is not None else None
            current_price = float(broker_position.get('currentPrice') or 0.0) if broker_position.get('currentPrice') is not None else None
            avg_entry_price = float(broker_position.get('avgPrice') or 0.0) if broker_position.get('avgPrice') is not None else None
            state_map[symbol] = {
                'hasOpenPosition': True,
                'accountId': None,
                'positionId': None,
                'positionSource': 'broker',
                'positionSyncGap': True,
                'shares': int(round(float(broker_position.get('shares') or 0.0))),
                'avgEntryPrice': avg_entry_price,
                'currentPrice': current_price,
                'marketValue': float(broker_position.get('marketValue') or 0.0),
                'unrealizedPnl': float(broker_position.get('pnl') or 0.0),
                'unrealizedPnlPct': float(broker_position.get('pnlPercent') or 0.0),
                'stopLoss': None,
                'profitTarget': None,
                'trailingStop': None,
                'peakPrice': None,
                'profitTargetReached': False,
                'scaleOutReady': False,
                'scaleOutAlreadyTaken': False,
                'impulseTrailArmed': False,
                'impulseTrailingStop': None,
                'entryTimeUtc': None,
                'maxHoldHours': max_hold_hours,
                'basePositionExpiresAtUtc': None,
                'positionExpiresAtUtc': None,
                'positionExpired': False,
                'hoursUntilExpiry': None,
                'hoursSinceEntry': None,
                'followThroughWindowHours': self._resolve_follow_through_window_hours(max_hold_hours),
                'followThroughFailed': False,
                'timeStopStructureCheckPassed': False,
                'timeStopExtended': False,
                'timeStopExtensionHours': None,
                'timeStopExtendedUntilUtc': None,
                'exitDeadlineSource': 'BROKER_ONLY_POSITION',
                'protectiveExitPending': False,
                'protectiveExitReasons': ['BROKER_POSITION_NOT_SYNCED_TO_DB'],
                'stopLossBreached': False,
                'trailingStopBreached': False,
            }
        return state_map

    @staticmethod
    def _resolve_trade_for_position(db: Session, position: Position) -> Trade | None:
        return (
            db.query(Trade)
            .filter(
                Trade.account_id == position.account_id,
                Trade.ticker == position.ticker,
            )
            .order_by(Trade.entry_time.desc(), Trade.id.desc())
            .first()
        )

    @staticmethod
    def _trade_has_partial_trigger(trade: Trade | None, trigger: str) -> bool:
        if trade is None or not isinstance(trade.exit_reasoning, dict):
            return False
        partial_exits = trade.exit_reasoning.get('partialExits', [])
        if not isinstance(partial_exits, list):
            return False
        for event in partial_exits:
            if isinstance(event, dict) and str(event.get('trigger') or '').upper() == str(trigger).upper():
                return True
        return False


    @staticmethod
    def _resolve_follow_through_window_hours(max_hold_hours: int | None) -> float | None:
        if max_hold_hours is None:
            return 24.0
        return round(max(2.0, min(24.0, float(max_hold_hours) / 2.0)), 2)

    @staticmethod
    def _resolve_structure_extension_hours(max_hold_hours: int | None) -> float | None:
        if max_hold_hours is None:
            return 4.0
        return round(max(1.0, min(8.0, float(max_hold_hours) / 6.0)), 2)

    @classmethod
    def _apply_structure_time_stop(
        cls,
        *,
        exit_template: str,
        base_expires_at: datetime | None,
        observed_at: datetime,
        max_hold_hours: int | None,
        avg_entry_price: float | None,
        current_price: float | None,
        stop_loss_breached: bool,
        trailing_stop_breached: bool,
    ) -> tuple[datetime | None, bool, float | None, bool, bool, float | None, datetime | None, str | None]:
        effective_expires_at = base_expires_at
        position_expired = False
        hours_until_expiry = None
        time_stop_structure_check_passed = False
        time_stop_extended = False
        time_stop_extension_hours = None
        time_stop_extended_until = None
        exit_deadline_source = 'watchlist_max_hold' if base_expires_at is not None else None

        if base_expires_at is not None and exit_template == 'time_stop_with_structure_check':
            time_stop_extension_hours = cls._resolve_structure_extension_hours(max_hold_hours)
            structure_intact = bool(
                observed_at >= base_expires_at
                and avg_entry_price is not None
                and avg_entry_price > 0
                and current_price is not None
                and current_price >= avg_entry_price
                and not stop_loss_breached
                and not trailing_stop_breached
            )
            time_stop_structure_check_passed = structure_intact
            if structure_intact and time_stop_extension_hours is not None:
                time_stop_extended_until = base_expires_at + timedelta(hours=time_stop_extension_hours)
                if observed_at < time_stop_extended_until:
                    effective_expires_at = time_stop_extended_until
                    time_stop_extended = True
                    exit_deadline_source = 'watchlist_max_hold_structure_extension'

        if effective_expires_at is not None:
            hours_until_expiry = round((effective_expires_at - observed_at).total_seconds() / 3600.0, 2)
            position_expired = effective_expires_at <= observed_at

        return (
            effective_expires_at,
            position_expired,
            hours_until_expiry,
            time_stop_structure_check_passed,
            time_stop_extended,
            time_stop_extension_hours,
            time_stop_extended_until,
            exit_deadline_source,
        )

    @staticmethod
    def _calculate_impulse_trailing_stop(reference_price: float | None) -> float | None:
        if reference_price is None or reference_price <= 0:
            return None
        return round(reference_price * (1.0 - (float(settings.TRAILING_STOP_PCT) * IMPULSE_TRAIL_STOP_FACTOR)), 4)

    @staticmethod
    def _resolve_crypto_exit_level(position: dict[str, Any], key: str, fallback: float | None) -> float | None:
        aliases = {
            key,
            str(key or '').strip(),
            str(key or '').strip().lower(),
            str(key or '').strip().upper(),
        }
        if key == 'stopLoss':
            aliases.update({'stop_loss', 'stoploss', 'StopLoss'})
        elif key == 'profitTarget':
            aliases.update({'profit_target', 'profittarget', 'ProfitTarget'})
        elif key == 'trailingStop':
            aliases.update({'trailing_stop', 'trailingstop', 'TrailingStop'})
        elif key == 'peakPrice':
            aliases.update({'peak_price', 'peakprice', 'PeakPrice'})

        value = None
        for alias in aliases:
            if alias in position and position.get(alias) is not None:
                value = position.get(alias)
                break
        if value is None:
            return fallback
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return fallback
        if parsed <= 0:
            return fallback
        return parsed

    def _build_crypto_position_state_map(self, db: Session, *, observed_at: datetime) -> dict[str, dict[str, Any]]:
        state_map: dict[str, dict[str, Any]] = {}
        try:
            positions = crypto_ledger.get_positions()
        except Exception:
            positions = []
        for position in positions:
            raw_symbol = str(position.get('pair') or position.get('symbol') or '').upper().strip()
            if not raw_symbol:
                continue

            normalized_symbol = self._normalize_crypto_symbol_key(raw_symbol)
            if not normalized_symbol:
                continue

            latest_row = None
            for row_candidate in db.query(WatchlistSymbol).filter(WatchlistSymbol.scope == 'crypto_only').order_by(WatchlistSymbol.id.desc()).all():
                candidate_key = self._normalize_scope_symbol(
                    scope='crypto_only',
                    symbol=row_candidate.symbol,
                    quote_currency=row_candidate.quote_currency,
                )
                if candidate_key == normalized_symbol:
                    latest_row = row_candidate
                    break

            max_hold_hours = int(latest_row.max_hold_hours) if latest_row is not None and latest_row.max_hold_hours is not None else None
            exit_template = str(latest_row.exit_template or '').strip().lower() if latest_row is not None and latest_row.exit_template is not None else ''

            entry_time_raw = position.get('entryTimeUtc')
            entry_time = None
            if entry_time_raw:
                try:
                    entry_time = datetime.fromisoformat(str(entry_time_raw).replace('Z', '+00:00'))
                    if entry_time.tzinfo is None:
                        entry_time = entry_time.replace(tzinfo=UTC)
                except ValueError:
                    entry_time = None

            avg_entry_price = float(position.get('avgPrice') or 0.0) if position.get('avgPrice') is not None else None
            current_price = float(position.get('currentPrice') or 0.0) if position.get('currentPrice') is not None else None
            base_expires_at = entry_time + timedelta(hours=max_hold_hours) if entry_time is not None and max_hold_hours is not None else None
            hours_since_entry = None
            if entry_time is not None:
                hours_since_entry = round((observed_at - entry_time).total_seconds() / 3600.0, 2)

            # Prefer explicit stop-loss / target / trailing levels already stored
            # on the crypto ledger position. Fall back to config-derived levels
            # from avg entry when the ledger does not provide them.
            explicit_peak_price = self._resolve_crypto_exit_level(position, 'peakPrice', None)
            explicit_stop_loss = self._resolve_crypto_exit_level(position, 'stopLoss', None)
            explicit_profit_target = self._resolve_crypto_exit_level(position, 'profitTarget', None)
            explicit_trailing_stop = self._resolve_crypto_exit_level(position, 'trailingStop', None)
            stop_loss: float | None = explicit_stop_loss
            profit_target: float | None = explicit_profit_target
            trailing_stop: float | None = explicit_trailing_stop
            peak_price: float | None = explicit_peak_price
            if avg_entry_price is not None and avg_entry_price > 0:
                derived_stop_loss = round(avg_entry_price * (1.0 - float(settings.STOP_LOSS_PCT)), 8)
                derived_profit_target = round(avg_entry_price * (1.0 + float(settings.PROFIT_TARGET_PCT)), 8)
                derived_peak_price = max(avg_entry_price, float(current_price or 0.0))
                derived_trailing_stop = round(derived_peak_price * (1.0 - float(settings.TRAILING_STOP_PCT)), 8)
                peak_price = peak_price if peak_price is not None else derived_peak_price
                stop_loss = stop_loss if stop_loss is not None else derived_stop_loss
                profit_target = profit_target if profit_target is not None else derived_profit_target
                trailing_stop = trailing_stop if trailing_stop is not None else derived_trailing_stop

            stop_loss_breached = bool(
                current_price is not None
                and stop_loss is not None
                and stop_loss > 0
                and current_price <= stop_loss
            )
            trailing_stop_breached = bool(
                current_price is not None
                and trailing_stop is not None
                and trailing_stop > 0
                and current_price <= trailing_stop
            )
            protective_exit_reasons: list[str] = []
            if stop_loss_breached:
                protective_exit_reasons.append('STOP_LOSS_BREACH')
            if trailing_stop_breached:
                protective_exit_reasons.append('TRAILING_STOP_BREACH')

            profit_target_reached = bool(
                current_price is not None
                and profit_target is not None
                and profit_target > 0
                and current_price >= profit_target
            )
            scale_out_ready = bool(
                profit_target_reached
                and exit_template in PROFIT_TARGET_SCALE_OUT_TEMPLATES
            )
            impulse_trail_armed = bool(
                exit_template in IMPULSE_TRAIL_TEMPLATES
                and profit_target_reached
            )
            impulse_reference_price = max(float(peak_price or 0.0), float(current_price or 0.0))
            impulse_trailing_stop = self._calculate_impulse_trailing_stop(impulse_reference_price) if impulse_trail_armed else None

            follow_through_window_hours = self._resolve_follow_through_window_hours(max_hold_hours)
            follow_through_failed = bool(
                exit_template in FOLLOW_THROUGH_EXIT_TEMPLATES
                and avg_entry_price is not None
                and avg_entry_price > 0
                and current_price is not None
                and current_price < avg_entry_price
                and hours_since_entry is not None
                and follow_through_window_hours is not None
                and 1.0 <= hours_since_entry <= follow_through_window_hours
                and not stop_loss_breached
                and not trailing_stop_breached
            )

            (
                effective_expires_at,
                position_expired,
                hours_until_expiry,
                time_stop_structure_check_passed,
                time_stop_extended,
                time_stop_extension_hours,
                time_stop_extended_until,
                exit_deadline_source,
            ) = self._apply_structure_time_stop(
                exit_template=exit_template,
                base_expires_at=base_expires_at,
                observed_at=observed_at,
                max_hold_hours=max_hold_hours,
                avg_entry_price=avg_entry_price,
                current_price=current_price,
                stop_loss_breached=stop_loss_breached,
                trailing_stop_breached=trailing_stop_breached,
            )

            payload = {
                'hasOpenPosition': True,
                'pair': position.get('pair') or position.get('symbol'),
                'amount': float(position.get('amount') or 0.0),
                'avgEntryPrice': avg_entry_price,
                'currentPrice': current_price,
                'marketValue': float(position.get('marketValue') or 0.0) if position.get('marketValue') is not None else None,
                'costBasis': float(position.get('costBasis') or 0.0) if position.get('costBasis') is not None else None,
                'pnl': float(position.get('pnl') or 0.0) if position.get('pnl') is not None else None,
                'pnlPercent': float(position.get('pnlPercent') or 0.0) if position.get('pnlPercent') is not None else None,
                'realizedPnl': float(position.get('realizedPnl') or 0.0) if position.get('realizedPnl') is not None else None,
                'entryTimeUtc': entry_time.isoformat() if entry_time else None,
                'maxHoldHours': max_hold_hours,
                'basePositionExpiresAtUtc': base_expires_at.isoformat() if base_expires_at else None,
                'positionExpiresAtUtc': effective_expires_at.isoformat() if effective_expires_at else None,
                'positionExpired': position_expired,
                'hoursUntilExpiry': hours_until_expiry,
                'hoursSinceEntry': hours_since_entry,
                'followThroughWindowHours': follow_through_window_hours,
                'followThroughFailed': follow_through_failed,
                'timeStopStructureCheckPassed': time_stop_structure_check_passed,
                'timeStopExtended': time_stop_extended,
                'timeStopExtensionHours': time_stop_extension_hours,
                'timeStopExtendedUntilUtc': time_stop_extended_until.isoformat() if time_stop_extended_until else None,
                'exitDeadlineSource': exit_deadline_source or ('unavailable_crypto_ledger' if entry_time is None else 'watchlist_max_hold'),
                'stopLoss': stop_loss,
                'profitTarget': profit_target,
                'trailingStop': trailing_stop,
                'peakPrice': peak_price,
                'protectiveExitPending': bool(protective_exit_reasons),
                'protectiveExitReasons': protective_exit_reasons,
                'stopLossBreached': stop_loss_breached,
                'trailingStopBreached': trailing_stop_breached,
                'profitTargetReached': profit_target_reached,
                'scaleOutReady': scale_out_ready,
                'scaleOutAlreadyTaken': False,
                'impulseTrailArmed': impulse_trail_armed,
                'impulseTrailingStop': impulse_trailing_stop,
                'observedAtUtc': observed_at.isoformat(),
            }
            state_map[normalized_symbol] = payload
        return state_map

    def _derive_runtime_monitoring_status(self, row: WatchlistSymbol, position_state: dict[str, Any]) -> str | None:
        if row.scope != 'stocks_only' or not position_state.get('hasOpenPosition'):
            return None
        symbol = str(row.symbol or '').upper().strip()
        mode = runtime_state.get().stock_mode
        try:
            open_orders = tradier_client.get_orders_sync(
                mode=mode,
                symbol=symbol,
                side='SELL',
                statuses=['OPEN', 'PENDING', 'SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED', 'NEW'],
                timeout=max(1.0, float(settings.TRADIER_POSITIONS_TIMEOUT_SECONDS)),
            )
        except Exception:
            open_orders = []
        if open_orders:
            return 'EXIT_PENDING'
        if position_state.get('protectiveExitPending') or position_state.get('positionExpired') or position_state.get('scaleOutReady') or position_state.get('followThroughFailed'):
            session = get_scope_session_status(row.scope, datetime.now(UTC))
            if not bool(getattr(session, 'session_open', False)):
                return 'WAITING_FOR_MARKET_OPEN'
        return None

    @staticmethod
    def _serialize_symbol_row(row: WatchlistSymbol, monitor_state: WatchlistMonitorState | None = None) -> dict[str, Any]:
        monitoring_payload = None
        if monitor_state is not None:
            decision_context = monitor_state.decision_context_json or {}
            reentry_blocked_until = decision_context.get('reentryBlockedUntilUtc')
            monitoring_payload = {
                'latestDecisionState': monitor_state.latest_decision_state,
                'latestDecisionReason': monitor_state.latest_decision_reason,
                'decisionContext': decision_context,
                'reentryBlockedUntilUtc': reentry_blocked_until,
                'cooldownActive': bool(reentry_blocked_until),
                'requiredTimeframes': monitor_state.required_timeframes_json or [],
                'evaluationIntervalSeconds': monitor_state.evaluation_interval_seconds,
                'lastDecisionAtUtc': monitor_state.last_decision_at_utc.isoformat() if monitor_state.last_decision_at_utc else None,
                'lastEvaluatedAtUtc': monitor_state.last_evaluated_at_utc.isoformat() if monitor_state.last_evaluated_at_utc else None,
                'nextEvaluationAtUtc': monitor_state.next_evaluation_at_utc.isoformat() if monitor_state.next_evaluation_at_utc else None,
                'lastMarketDataAtUtc': monitor_state.last_market_data_at_utc.isoformat() if monitor_state.last_market_data_at_utc else None,
            }
        return {
            'symbol': row.symbol,
            'quoteCurrency': row.quote_currency,
            'assetClass': row.asset_class,
            'enabled': row.enabled,
            'tradeDirection': row.trade_direction,
            'priorityRank': row.priority_rank,
            'tier': row.tier,
            'bias': row.bias,
            'setupTemplate': row.setup_template,
            'botTimeframes': row.bot_timeframes,
            'exitTemplate': row.exit_template,
            'maxHoldHours': row.max_hold_hours,
            'riskFlags': row.risk_flags,
            'monitoringStatus': row.monitoring_status,
            'uploadId': row.upload_id,
            'monitoring': monitoring_payload,
        }

    @staticmethod
    def _is_accepted_validation_status(status: str | None) -> bool:
        normalized = str(status or '').strip().lower()
        return normalized in VALIDATION_ACCEPTED_STATUSES

    @classmethod
    def _estimate_ai_confidence(cls, row: WatchlistSymbol, *, primary_focus: set[str]) -> float:
        tier_base = {
            'tier_1': 0.86,
            'tier_2': 0.74,
            'tier_3': 0.62,
        }
        symbol_key = str(row.symbol or '').upper()
        base = tier_base.get(str(row.tier or '').strip().lower(), 0.66)
        priority_bonus = max(0.0, 0.05 - max(row.priority_rank - 1, 0) * 0.01)
        focus_bonus = 0.03 if symbol_key in primary_focus else 0.0
        return round(min(0.95, max(0.51, base + priority_bonus + focus_bonus)), 2)

    @classmethod
    def _build_ai_decision_reasoning(
        cls,
        *,
        upload: WatchlistUpload,
        row: WatchlistSymbol,
        symbol_context: dict[str, Any],
        summary_json: dict[str, Any],
    ) -> str:
        context_bits = []
        for key in ('thesis', 'why_now', 'notes', 'scan_reason', 'role', 'sector', 'radar_bucket'):
            value = symbol_context.get(key)
            if isinstance(value, str) and value.strip():
                label = key.replace('_', ' ')
                context_bits.append(f'{label}: {value.strip()}')
        if context_bits:
            return ' | '.join(context_bits)

        regime_note = summary_json.get('regime_note') if isinstance(summary_json, dict) else None
        fallback = [
            f'Watchlist upload {upload.schema_version} from {upload.provider}',
            f'setup {row.setup_template}',
            f'exit {row.exit_template}',
            f'tier {row.tier}',
        ]
        if isinstance(regime_note, str) and regime_note.strip():
            fallback.append(regime_note.strip())
        return ' · '.join(fallback)


watchlist_service = WatchlistService()
