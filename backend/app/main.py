"""AI Bot - Main FastAPI Application

Hybrid stock + crypto API aligned to the system design doc.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.routers import crypto
from app.services.kraken_service import crypto_ledger
from app.services.runtime_state import runtime_state
from app.services.tradier_client import tradier_client

# Import Discord bot
from app.services.discord_bot import start_discord_bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan events for startup/shutdown
    """
    # Startup: Start Discord bot
    logger.info("🤖 Starting Discord bot...")
    discord_task = asyncio.create_task(start_discord_bot())
    
    yield  # Application is running
    
    # Shutdown: Cancel Discord bot
    logger.info("Shutting down Discord bot...")
    discord_task.cancel()
    try:
        await discord_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title='AI Trading Bot API',
    description='Stock (Tradier) and Crypto (Kraken) AI Trading System',
    version='3.0.0',
    lifespan=lifespan,  # Add lifespan manager
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:5173', 'http://localhost:3000'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.include_router(crypto.router, prefix='/api')


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
async def health():
    return {'status': 'healthy'}


@app.get('/api/status')
async def get_status():
    state = runtime_state.touch()
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
    }


@app.post('/api/control/toggle')
async def toggle_bot(request: ToggleRequest):
    state = runtime_state.set_running(request.enabled)
    return {'success': True, 'running': state.running, 'lastHeartbeat': state.last_heartbeat}


@app.post('/api/control/stock-mode')
async def set_stock_mode(request: StockModeRequest):
    try:
        state = runtime_state.set_stock_mode(request.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'stockMode': state.stock_mode, 'lastHeartbeat': state.last_heartbeat}


@app.post('/api/control/safety-override')
async def toggle_safety(request: ToggleRequest):
    state = runtime_state.set_safety_require_market_hours(request.enabled)
    return {
        'success': True,
        'safetyRequireMarketHours': state.safety_require_market_hours,
        'lastHeartbeat': state.last_heartbeat,
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


@app.get('/api/stocks/history')
async def get_stock_history(limit: int = Query(50, ge=1, le=500)):
    # Placeholder until order history ingestion is added.
    return []


@app.get('/api/ai/decisions')
async def get_ai_decisions(limit: int = Query(50, ge=1, le=500)):
    # Placeholder until Discord/webhook audit log storage is added.
    return []


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