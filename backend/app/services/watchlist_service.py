from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.position import Position
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.models.watchlist_ui_context import WatchlistUiContext
from app.models.watchlist_upload import WatchlistUpload
from app.services.kraken_service import crypto_ledger

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

        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        upload_id = f'wlu_{uuid.uuid4().hex[:20]}'
        scan_id = f'scan_{uuid.uuid4().hex[:20]}'
        expires_at = freshness['generatedAtUtc'] + timedelta(hours=max(1, int(settings.WATCHLIST_DEFAULT_EXPIRY_HOURS)))
        now = freshness['observedAtUtc']

        next_symbols = {symbol.symbol.upper() for symbol in parsed.bot_payload.symbols}
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
                row.symbol.upper()
                for row in db.query(WatchlistSymbol).filter(WatchlistSymbol.upload_id == active_upload.upload_id).all()
            }

        open_symbols = self._get_open_symbols(db, scope)
        candidate_rows = (
            db.query(WatchlistSymbol)
            .filter(
                WatchlistSymbol.scope == scope,
                WatchlistSymbol.monitoring_status.in_([ACTIVE, MANAGED_ONLY]),
            )
            .all()
        )

        changed = 0
        for row in candidate_rows:
            next_status = self._resolve_row_status(
                row_upload_id=row.upload_id,
                active_upload_id=active_upload.upload_id if active_upload else None,
                symbol=row.symbol,
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

    def get_monitoring_snapshot(
        self,
        db: Session,
        *,
        scope: WATCHLIST_SCOPE | None = None,
        include_inactive: bool = False,
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
            rows = [self._serialize_monitor_row(symbol_row, monitor_row) for monitor_row, symbol_row in pairs]
            next_eval = min(
                (row['monitoring']['nextEvaluationAtUtc'] for row in rows if row.get('monitoring') and row['monitoring']['nextEvaluationAtUtc']),
                default=None,
            )
            last_eval = max(
                (row['monitoring']['lastEvaluatedAtUtc'] for row in rows if row.get('monitoring') and row['monitoring']['lastEvaluatedAtUtc']),
                default=None,
            )
            active_upload = self._get_latest_upload_row(db, scope=scope_value, active_only=True)
            result[scope_value] = {
                'scope': scope_value,
                'capturedAtUtc': observed_at.isoformat(),
                'activeUploadId': active_upload.upload_id if active_upload else None,
                'summary': {
                    'total': len(rows),
                    'activeCount': sum(1 for row in rows if row['monitoringStatus'] == ACTIVE),
                    'managedOnlyCount': sum(1 for row in rows if row['monitoringStatus'] == MANAGED_ONLY),
                    'inactiveCount': sum(1 for row in rows if row['monitoringStatus'] == INACTIVE),
                    'pendingEvaluationCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == PENDING_EVALUATION),
                    'monitorOnlyCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == MONITOR_ONLY),
                    'inactiveDecisionCount': sum(1 for row in rows if row.get('monitoring') and row['monitoring']['latestDecisionState'] == INACTIVE_DECISION),
                    'nextEvaluationAtUtc': next_eval,
                    'lastEvaluatedAtUtc': last_eval,
                },
                'rows': rows,
            }
        return result[scope] if scope is not None else result

    def get_managed_only_rows(self, db: Session, *, scope: WATCHLIST_SCOPE) -> list[WatchlistSymbol]:
        return (
            db.query(WatchlistSymbol)
            .filter(
                WatchlistSymbol.scope == scope,
                WatchlistSymbol.monitoring_status == MANAGED_ONLY,
            )
            .order_by(WatchlistSymbol.priority_rank.asc(), WatchlistSymbol.id.asc())
            .all()
        )

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
        status_counts = self.get_scope_status_counts(db, scope=upload.scope)
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
            'statusSummary': {
                'activeCount': status_counts.get(ACTIVE, 0),
                'managedOnlyCount': status_counts.get(MANAGED_ONLY, 0),
                'inactiveCount': status_counts.get(INACTIVE, 0),
            },
            'monitoringSummary': self.get_monitoring_snapshot(db, scope=upload.scope)['summary'],
            'uiPayload': {
                'summary': (ui_context.summary_json if ui_context else {}),
                'providerLimitations': (ui_context.provider_limitations_json if ui_context else []),
                'symbolContext': (ui_context.symbol_context_json if ui_context else {}),
            },
        }

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
            .filter(
                WatchlistSymbol.scope == scope,
                WatchlistSymbol.monitoring_status.in_([ACTIVE, MANAGED_ONLY]),
            )
            .all()
        )
        for row in candidate_rows:
            symbol = str(row.symbol or '').upper()
            if symbol in next_symbols:
                row.monitoring_status = INACTIVE
            elif symbol in open_symbols:
                row.monitoring_status = MANAGED_ONLY
            else:
                row.monitoring_status = INACTIVE
            self._upsert_monitor_state(db, row, observed_at=observed_at)

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

    def _get_open_stock_symbols(self, db: Session) -> set[str]:
        rows = db.query(Position).filter(Position.is_open.is_(True)).all()
        return {str(row.ticker or '').upper() for row in rows if str(row.ticker or '').strip()}

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
            open_symbols.add(raw_pair)
            if '/' in raw_pair:
                open_symbols.add(raw_pair.split('/', 1)[0])
        return open_symbols

    def _resolve_row_status(
        self,
        *,
        row_upload_id: str,
        active_upload_id: str | None,
        symbol: str,
        active_symbols: set[str],
        open_symbols: set[str],
    ) -> str:
        symbol_value = str(symbol or '').upper()
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
        next_evaluation_at = self._calculate_next_evaluation_at(observed_at, interval_seconds) if row.monitoring_status != INACTIVE else None
        context = {
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
                decision_context_json=context,
                required_timeframes_json=row.bot_timeframes,
                evaluation_interval_seconds=interval_seconds,
                last_decision_at_utc=observed_at,
                last_evaluated_at_utc=None,
                next_evaluation_at_utc=next_evaluation_at,
                last_market_data_at_utc=None,
            )
            db.add(monitor_state)
            return True

        if monitor_state.monitoring_status != row.monitoring_status:
            monitor_state.monitoring_status = row.monitoring_status
            changed = True
        if monitor_state.latest_decision_state != decision_state:
            monitor_state.latest_decision_state = decision_state
            changed = True
        if monitor_state.latest_decision_reason != decision_reason:
            monitor_state.latest_decision_reason = decision_reason
            changed = True
        if monitor_state.decision_context_json != context:
            monitor_state.decision_context_json = context
            changed = True
        if monitor_state.required_timeframes_json != row.bot_timeframes:
            monitor_state.required_timeframes_json = row.bot_timeframes
            changed = True
        if monitor_state.evaluation_interval_seconds != interval_seconds:
            monitor_state.evaluation_interval_seconds = interval_seconds
            changed = True
        if monitor_state.next_evaluation_at_utc != next_evaluation_at:
            monitor_state.next_evaluation_at_utc = next_evaluation_at
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

    @staticmethod
    def _calculate_next_evaluation_at(reference_time: datetime, interval_seconds: int | None) -> datetime | None:
        if interval_seconds is None:
            return None
        base_time = _normalize_dt(reference_time)
        epoch = int(base_time.timestamp())
        next_boundary = ((epoch // interval_seconds) + 1) * interval_seconds
        return datetime.fromtimestamp(next_boundary + MONITORING_OFFSET_SECONDS, tz=UTC)

    def _serialize_monitor_row(self, row: WatchlistSymbol, monitor_state: WatchlistMonitorState | None) -> dict[str, Any]:
        symbol_payload = self._serialize_symbol_row(row, monitor_state=monitor_state)
        symbol_payload['managedOnly'] = row.monitoring_status == MANAGED_ONLY
        return symbol_payload

    @staticmethod
    def _serialize_symbol_row(row: WatchlistSymbol, monitor_state: WatchlistMonitorState | None = None) -> dict[str, Any]:
        monitoring_payload = None
        if monitor_state is not None:
            monitoring_payload = {
                'latestDecisionState': monitor_state.latest_decision_state,
                'latestDecisionReason': monitor_state.latest_decision_reason,
                'decisionContext': monitor_state.decision_context_json or {},
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


watchlist_service = WatchlistService()
