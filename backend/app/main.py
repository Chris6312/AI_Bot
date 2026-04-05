"""AI Bot - Main FastAPI Application

Hybrid stock + crypto API aligned to the system design doc.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.routers import crypto, watchlists
from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.services.control_plane import get_control_plane_status, require_admin_token
from app.services.execution_lifecycle import execution_lifecycle
from app.services.kraken_service import crypto_ledger
from app.services.position_inspect import PositionInspectNotFound, position_inspect_service
from app.services.position_reconciliation import position_reconciliation_service
from app.services.runtime_visibility import runtime_visibility_service
from app.services.runtime_state import runtime_state
from app.services.watchlist_monitoring import watchlist_monitoring_orchestrator
from app.services.watchlist_service import watchlist_service
from app.services.watchlist_exit_worker import watchlist_exit_worker
from app.services.tradier_client import tradier_client
from app.services.trade_history import TradeHistoryFilters, trade_history_service


logging.basicConfig(level=logging.INFO)

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')


def _is_test_runtime() -> bool:
    raw_skip = str(os.getenv("APP_SKIP_BACKGROUND_TASKS", "")).strip().lower()
    return (
        os.getenv("PYTEST_CURRENT_TEST") is not None
        or raw_skip in {"1", "true", "yes", "on"}
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan events for startup/shutdown.

    During pytest/TestClient runs, skip background startup services so tests do not
    boot long-running workers or external integrations.
    """
    discord_task: asyncio.Task | None = None
    watchlist_monitor_task: asyncio.Task | None = None
    watchlist_exit_task: asyncio.Task | None = None

    if _is_test_runtime():
        logger.info('Test runtime detected. Skipping background startup services.')
        yield
        return

    logger.info('🔄 Priming Kraken AssetPairs cache for startup...')

    def _run_startup_reconciliation() -> dict[str, object]:
        db = SessionLocal()
        try:
            return position_reconciliation_service.reconcile_all(db)
        finally:
            db.close()

    try:
        startup_reconciliation = await asyncio.to_thread(_run_startup_reconciliation)
        startup_refresh = await watchlist_monitoring_orchestrator.bootstrap_startup_state(
            refresh_crypto_monitor_state=bool(settings.WATCHLIST_MONITOR_ENABLED),
        )
        logger.info('Startup position reconciliation complete: %s', startup_reconciliation)
        logger.info(
            'Startup crypto refresh complete: asset_pairs=%s evaluated=%s data_unavailable=%s waiting=%s entry_candidates=%s',
            startup_refresh['assetPairCount'],
            startup_refresh['evaluatedCount'],
            startup_refresh.get('evaluationSummary', {}).get('dataUnavailableCount', 0),
            startup_refresh.get('evaluationSummary', {}).get('waitingForSetupCount', 0),
            startup_refresh.get('evaluationSummary', {}).get('entryCandidateCount', 0),
        )
    except Exception as exc:
        logger.warning('Startup Kraken/crypto bootstrap failed: %s', exc)

    if settings.WATCHLIST_MONITOR_ENABLED:
        logger.info('🛰️ Starting watchlist monitoring orchestrator...')
        watchlist_monitor_task = asyncio.create_task(
            watchlist_monitoring_orchestrator.run_loop()
        )
    else:
        logger.info('Watchlist monitoring orchestrator startup skipped because it is disabled.')

    if settings.WATCHLIST_EXIT_WORKER_ENABLED:
        logger.info('⏳ Starting watchlist exit worker orchestrator...')
        watchlist_exit_task = asyncio.create_task(watchlist_exit_worker.run_loop())
    else:
        logger.info('Watchlist exit worker startup skipped because it is disabled.')

    if settings.DISCORD_BOT_TOKEN:
        try:
            from app.services.discord_bot import start_discord_bot
        except ModuleNotFoundError as exc:
            logger.warning('Discord bot startup skipped because dependency import failed: %s', exc)
        else:
            logger.info('🤖 Starting Discord bot...')
            discord_task = asyncio.create_task(start_discord_bot())
    else:
        logger.info('Discord bot startup skipped because DISCORD_BOT_TOKEN is not configured.')

    yield

    if watchlist_monitor_task is not None:
        logger.info('Shutting down watchlist monitoring orchestrator...')
        watchlist_monitor_task.cancel()
        try:
            await watchlist_monitor_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning('Watchlist monitoring orchestrator shutdown raised: %s', exc)

    if watchlist_exit_task is not None:
        logger.info('Shutting down watchlist exit worker orchestrator...')
        watchlist_exit_task.cancel()
        try:
            await watchlist_exit_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning('Watchlist exit worker shutdown raised: %s', exc)

    if discord_task is not None:
        logger.info('Shutting down Discord bot...')
        discord_task.cancel()
        try:
            await discord_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning('Discord bot shutdown raised: %s', exc)


app = FastAPI(
    title='AI Trading Bot API',
    description='Stock (Tradier) and Crypto (Kraken) AI Trading System',
    version='3.0.0',
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:5173', 'http://localhost:3000'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(crypto.router, prefix='/api')
app.include_router(watchlists.router, prefix='/api')


class ToggleRequest(BaseModel):
    enabled: bool


class StockModeRequest(BaseModel):
    mode: Literal['PAPER', 'LIVE']


@app.get('/')
async def root():
    return {
        'name': 'AI Trading Bot',
        'version': '3.0.0',
        'markets': ['stocks', 'crypto'],
        'status': 'online',
    }


@app.get('/health')
async def health(force_refresh: bool = Query(False)):
    runtime_visibility = runtime_visibility_service.get_runtime_snapshot(limit=5, force_refresh=force_refresh)
    truth_board = runtime_visibility['truthBoard']
    return {
        'status': 'healthy' if truth_board['supervisionReady'] else 'degraded',
        'reason': truth_board['reason'],
        'truthBoard': truth_board,
        'dependencies': runtime_visibility['dependencies'],
    }


@app.get('/ready')
async def ready(force_refresh: bool = Query(False)):
    runtime_visibility = runtime_visibility_service.get_runtime_snapshot(limit=5, force_refresh=force_refresh)
    truth_board = runtime_visibility['truthBoard']
    readiness_ok = bool(truth_board['freshEntryReady'])
    return {
        'status': 'ready' if readiness_ok else 'degraded',
        'reason': truth_board['reason'],
        'controlPlane': runtime_visibility['controlPlane'],
        'executionGate': runtime_visibility['executionGate'],
        'dependencies': runtime_visibility['dependencies'],
        'truthBoard': truth_board,
        'stockCapabilities': {
            'paperReady': tradier_client.is_ready('PAPER'),
            'liveReady': tradier_client.is_ready('LIVE'),
        },
        'cryptoCapabilities': {
            'paperReady': True,
            'liveReady': False,
        },
    }


@app.get('/api/status')
async def get_status():
    state = runtime_state.get()
    runtime_visibility = runtime_visibility_service.get_runtime_snapshot(limit=5)
    return {
        'running': state.running,
        'mode': state.stock_mode,
        'stockMode': state.stock_mode,
        'cryptoMode': state.crypto_mode,
        'safetyRequireMarketHours': state.safety_require_market_hours,
        'lastHeartbeat': state.last_heartbeat,
        'stockCapabilities': {
            'paperReady': tradier_client.is_ready('PAPER'),
            'liveReady': tradier_client.is_ready('LIVE'),
        },
        'cryptoCapabilities': {
            'paperReady': True,
            'liveReady': False,
        },
        'controlPlane': runtime_visibility['controlPlane'],
        'executionGate': runtime_visibility['executionGate'],
        'runtimeVisibility': {
            'gateSummary': runtime_visibility['gate']['summary'],
            'dependencySummary': runtime_visibility['dependencies']['summary'],
            'truthBoard': runtime_visibility['truthBoard'],
            'lastDecision': runtime_visibility['gate']['summary']['lastDecision'],
            'lastRejected': runtime_visibility['gate']['summary']['lastRejected'],
        },
    }


@app.get('/api/control-state')
async def get_control_state():
    return get_control_plane_status()


@app.get('/api/runtime-visibility')
async def get_runtime_visibility(
    limit: int = Query(10, ge=1, le=50),
    force_refresh: bool = Query(False),
):
    return runtime_visibility_service.get_runtime_snapshot(limit=limit, force_refresh=force_refresh)


@app.post('/api/control/toggle')
async def toggle_bot(request: ToggleRequest, _: bool = Depends(require_admin_token)):
    state = runtime_state.set_running(request.enabled)
    return {'success': True, 'running': state.running, 'lastHeartbeat': state.last_heartbeat}


@app.post('/api/control/stock-mode')
async def set_stock_mode(request: StockModeRequest, _: bool = Depends(require_admin_token)):
    try:
        state = runtime_state.set_stock_mode(request.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'stockMode': state.stock_mode, 'lastHeartbeat': state.last_heartbeat}


@app.post('/api/control/safety-override')
async def toggle_safety(request: ToggleRequest, _: bool = Depends(require_admin_token)):
    state = runtime_state.set_safety_require_market_hours(request.enabled)
    return {
        'success': True,
        'safetyRequireMarketHours': state.safety_require_market_hours,
        'lastHeartbeat': state.last_heartbeat,
    }


def _position_symbol_key(symbol: str | None) -> str:
    return ''.join(ch for ch in str(symbol or '').upper() if ch.isalnum())


def _serialize_unified_stock_position(*, broker_row: dict[str, Any] | None, db_row: Position | None) -> dict[str, Any]:
    symbol = str((broker_row or {}).get('symbol') or (db_row.ticker if db_row else '') or '').upper()
    broker_present = broker_row is not None
    db_present = db_row is not None
    source_status = 'aligned' if broker_present and db_present else 'broker_only' if broker_present else 'db_only'
    quantity = float((broker_row or {}).get('shares') or (db_row.shares if db_row else 0) or 0)
    avg_price = (broker_row or {}).get('avgPrice')
    if avg_price is None and db_row is not None:
        avg_price = float(db_row.avg_entry_price or 0.0) if db_row.avg_entry_price is not None else None
    current_price = (broker_row or {}).get('currentPrice')
    if current_price is None and db_row is not None:
        current_price = float(db_row.current_price or 0.0) if db_row.current_price is not None else None
    market_value = (broker_row or {}).get('marketValue')
    if market_value is None:
        base_price = current_price if current_price is not None else avg_price
        market_value = float(quantity) * float(base_price or 0.0)
    pnl = (broker_row or {}).get('pnl')
    if pnl is None and db_row is not None:
        pnl = float(db_row.unrealized_pnl or 0.0) if db_row.unrealized_pnl is not None else None
    pnl_pct = (broker_row or {}).get('pnlPercent')
    if pnl_pct is None and db_row is not None:
        pnl_pct = float(db_row.unrealized_pnl_pct or 0.0) if db_row.unrealized_pnl_pct is not None else None

    return {
        'assetClass': 'stock',
        'symbol': symbol,
        'displaySymbol': symbol,
        'quantity': float(quantity),
        'quantityUnit': 'shares',
        'avgPrice': float(avg_price or 0.0),
        'currentPrice': float(current_price or 0.0),
        'marketValue': float(market_value or 0.0),
        'pnl': float(pnl or 0.0),
        'pnlPercent': float(pnl_pct or 0.0),
        'inspectSymbol': symbol,
        'inspectAssetClass': 'stock',
        'sourceStatus': source_status,
        'sourceDetail': 'Broker + DB mirror' if source_status == 'aligned' else 'Broker only' if source_status == 'broker_only' else 'DB mirror only',
        'entryTime': db_row.entry_time.isoformat() if db_row and db_row.entry_time else None,
    }


def _serialize_unified_crypto_position(row: dict[str, Any]) -> dict[str, Any]:
    pair = str(row.get('pair') or '').upper()
    return {
        'assetClass': 'crypto',
        'symbol': pair,
        'displaySymbol': pair,
        'quantity': float(row.get('amount') or 0.0),
        'quantityUnit': 'units',
        'avgPrice': float(row.get('avgPrice') or 0.0),
        'currentPrice': float(row.get('currentPrice') or 0.0),
        'marketValue': float(row.get('marketValue') or 0.0),
        'pnl': float(row.get('pnl') or 0.0),
        'pnlPercent': float(row.get('pnlPercent') or 0.0),
        'inspectSymbol': pair,
        'inspectAssetClass': 'crypto',
        'sourceStatus': 'ledger',
        'sourceDetail': 'Crypto paper ledger',
        'entryTime': row.get('entryTimeUtc'),
    }


@app.get('/api/stocks/account')
async def get_stock_account():
    mode = runtime_state.get().stock_mode
    try:
        return tradier_client.get_account_snapshot(mode)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Failed to fetch Tradier account: {exc}') from exc


@app.get('/api/stocks/positions')
async def get_stock_positions():
    mode = runtime_state.get().stock_mode
    try:
        return tradier_client.get_positions_snapshot(mode)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Failed to fetch Tradier positions: {exc}') from exc


@app.get('/api/stocks/db-positions')
async def get_stock_db_positions(db: Session = Depends(get_db)):
    rows = (
        db.query(Position)
        .filter(Position.is_open.is_(True))
        .order_by(
            Position.entry_time.desc(),
            Position.created_at.desc(),
            Position.id.desc(),
        )
        .all()
    )
    return [
        {
            'ticker': str(row.ticker or '').upper(),
            'accountId': row.account_id,
            'shares': int(row.shares or 0),
            'avgEntryPrice': float(row.avg_entry_price or 0.0) if row.avg_entry_price is not None else None,
            'currentPrice': float(row.current_price or 0.0) if row.current_price is not None else None,
            'unrealizedPnl': float(row.unrealized_pnl or 0.0) if row.unrealized_pnl is not None else None,
            'unrealizedPnlPct': float(row.unrealized_pnl_pct or 0.0) if row.unrealized_pnl_pct is not None else None,
            'strategy': row.strategy,
            'entryTime': row.entry_time.isoformat() if row.entry_time else None,
            'entryReasoning': row.entry_reasoning or {},
            'stopLoss': float(row.stop_loss or 0.0) if row.stop_loss is not None else None,
            'profitTarget': float(row.profit_target or 0.0) if row.profit_target is not None else None,
            'peakPrice': float(row.peak_price or 0.0) if row.peak_price is not None else None,
            'trailingStop': float(row.trailing_stop or 0.0) if row.trailing_stop is not None else None,
            'isOpen': bool(row.is_open),
            'executionId': row.execution_id,
            'createdAt': row.created_at.isoformat() if row.created_at else None,
            'updatedAt': row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


@app.get('/api/positions/unified')
async def get_unified_positions(db: Session = Depends(get_db)):
    mode = runtime_state.get().stock_mode
    try:
        broker_positions = tradier_client.get_positions_snapshot(mode)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Failed to fetch Tradier positions: {exc}') from exc

    db_rows = (
        db.query(Position)
        .filter(Position.is_open.is_(True))
        .order_by(
            Position.entry_time.desc(),
            Position.created_at.desc(),
            Position.id.desc(),
        )
        .all()
    )
    db_positions_by_symbol: dict[str, Position] = {}
    for row in db_rows:
        key = _position_symbol_key(row.ticker)
        if key and key not in db_positions_by_symbol:
            db_positions_by_symbol[key] = row

    broker_positions_by_symbol: dict[str, dict[str, Any]] = {}
    for row in broker_positions:
        key = _position_symbol_key(str(row.get('symbol') or ''))
        if key and key not in broker_positions_by_symbol:
            broker_positions_by_symbol[key] = row

    stock_keys = set(db_positions_by_symbol.keys()) | set(broker_positions_by_symbol.keys())
    stock_rows = [
        _serialize_unified_stock_position(
            broker_row=broker_positions_by_symbol.get(key),
            db_row=db_positions_by_symbol.get(key),
        )
        for key in stock_keys
    ]
    crypto_rows = [_serialize_unified_crypto_position(row) for row in crypto_ledger.get_positions()]
    rows = sorted(stock_rows + crypto_rows, key=lambda row: (-float(row.get('marketValue') or 0.0), str(row.get('displaySymbol') or '')))
    stock_drift_count = sum(1 for row in stock_rows if row['sourceStatus'] != 'aligned')
    return {
        'rows': rows,
        'summary': {
            'totalCount': len(rows),
            'stockCount': len(stock_rows),
            'cryptoCount': len(crypto_rows),
            'stockDriftCount': stock_drift_count,
            'alignedStockCount': sum(1 for row in stock_rows if row['sourceStatus'] == 'aligned'),
        },
    }


@app.get('/api/positions/inspect')
async def get_position_inspect(
    asset_class: Literal['stock', 'crypto'] = Query(...),
    symbol: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
):
    try:
        return position_inspect_service.get_inspect_payload(
            db,
            asset_class=asset_class,
            symbol=symbol,
        )
    except PositionInspectNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.message) from exc


@app.get('/api/stocks/history')
async def get_stock_history(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    intents = (
        db.query(OrderIntent)
        .filter(OrderIntent.asset_class == 'stock')
        .order_by(OrderIntent.created_at.desc(), OrderIntent.id.desc())
        .limit(limit)
        .all()
    )
    return [execution_lifecycle.serialize_intent(intent, db=db) for intent in intents]


@app.get('/api/ai/decisions')
async def get_ai_decisions(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return watchlist_service.get_ai_decision_feed(db, limit=limit)




@app.get('/api/trade-history')
async def get_trade_history(
    mode: Literal['PAPER', 'LIVE', 'ALL'] = Query('ALL'),
    asset_class: Literal['stock', 'crypto', 'all'] = Query('all'),
    symbol: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    def _parse_date(value: str | None, *, end_of_day: bool) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f'Invalid date: {value}') from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET)
        else:
            parsed = parsed.astimezone(ET)
        if end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
        return parsed.astimezone(ZoneInfo('UTC'))

    filters = TradeHistoryFilters(
        mode=None if mode == 'ALL' else mode,
        asset_class=None if asset_class == 'all' else asset_class,
        symbol=symbol,
        date_from=_parse_date(date_from, end_of_day=False),
        date_to=_parse_date(date_to, end_of_day=True),
    )
    return trade_history_service.get_closed_trade_history(db, filters=filters)


@app.get('/api/market-status')
async def get_market_status():
    now_et = datetime.now(ET)
    weekday = now_et.weekday() < 5
    market_open_today = datetime.combine(now_et.date(), time(9, 30), tzinfo=ET)
    market_close_today = datetime.combine(now_et.date(), time(16, 0), tzinfo=ET)
    is_open = weekday and market_open_today <= now_et <= market_close_today

    next_open = market_open_today
    if is_open:
        next_close = market_close_today
    else:
        next_close = market_close_today if weekday else datetime.combine(now_et.date(), time(16, 0), tzinfo=ET)
        while next_open <= now_et or next_open.weekday() >= 5:
            next_open = datetime.combine(next_open.date() + timedelta(days=1), time(9, 30), tzinfo=ET)
            next_close = datetime.combine(next_open.date(), time(16, 0), tzinfo=ET)

    return {
        'stock': {
            'isOpen': is_open,
            'nextOpen': next_open.isoformat(),
            'nextClose': next_close.isoformat(),
        },
        'crypto': {'isOpen': True},
    }


@app.get('/api/dashboard/summary')
async def get_dashboard_summary():
    state = runtime_state.get()
    stock_account = tradier_client.get_account_snapshot(state.stock_mode)
    crypto_ledger_snapshot = crypto_ledger.get_ledger()
    stock_equity = float(stock_account.get('portfolioValue', 0.0))
    crypto_equity = float(crypto_ledger_snapshot.get('equity', 0.0))
    stock_pnl = float(stock_account.get('unrealizedPnL', 0.0))
    crypto_pnl = float(crypto_ledger_snapshot.get('totalPnL', 0.0))
    return {
        'stockMode': state.stock_mode,
        'stockEquity': stock_equity,
        'cryptoEquity': crypto_equity,
        'totalEquity': stock_equity + crypto_equity,
        'openPnL': stock_pnl + crypto_pnl,
    }


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='0.0.0.0', port=8000)

