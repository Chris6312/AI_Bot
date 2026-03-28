"""
AI Bot - Main FastAPI Application
Stock (Tradier) + Crypto (Kraken) Trading System
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

# Import routers
from app.routers import crypto

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI Trading Bot API",
    description="Stock (Tradier) and Crypto (Kraken) AI Trading System",
    version="2.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(crypto.router, prefix="/api")

@app.get("/")
async def root():
    return {
        "name": "AI Trading Bot",
        "version": "2.0.0",
        "markets": ["stocks", "crypto"],
        "status": "online"
    }

@app.get("/api/status")
async def get_status():
    """Get bot status"""
    return {
        "running": True,
        "mode": "MIXED",
        "stockMode": "LIVE",
        "cryptoMode": "PAPER",
        "safetyRequireMarketHours": True,
        "lastHeartbeat": "2026-03-28T12:00:00Z"
    }

@app.get("/api/market-status")
async def get_market_status():
    """Get market status"""
    from datetime import datetime
    now = datetime.now()
    
    # Simple market hours check (9:30 AM - 4:00 PM ET)
    is_market_open = 9 <= now.hour < 16
    
    return {
        "stock": {
            "isOpen": is_market_open,
            "nextOpen": "2026-03-29T09:30:00-04:00",
            "nextClose": "2026-03-28T16:00:00-04:00"
        },
        "crypto": {
            "isOpen": True  # Crypto markets always open
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
