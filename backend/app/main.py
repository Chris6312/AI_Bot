"""Main application entry point"""
import asyncio
import logging
from fastapi import FastAPI
from app.core.config import settings
from app.services.discord_bot import start_discord_bot
from app.routers import crypto
from pydantic import BaseModel

# Configure logging
logging.basicConfig(
    level=settings.LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

app = FastAPI(title="AI Trading Bot", version="2.0.0")
app.include_router(crypto.router, prefix="/api")

@app.get("/")
async def root():
    return {"status": "online", "version": "2.0.0"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

async def main():
    """Main application startup"""
    logger.info("Starting AI Trading Bot...")
    logger.info(f"Environment: {settings.APP_ENV}")
    logger.info(f"Tradier: {settings.TRADIER_BASE_URL}")
    
    # Start Discord bot
    await start_discord_bot()

if __name__ == "__main__":
    asyncio.run(main())

@app.get("/api/stocks/positions")
async def get_stock_positions():
    """Get stock positions - integrate with your Tradier code"""
    return []  # Replace with your actual Tradier integration

@app.get("/api/stocks/history")
async def get_stock_history():
    """Get stock trade history"""
    return []  # Replace with your actual data

@app.get("/api/stocks/account")
async def get_stock_account():
    """Get Tradier account info"""
    return {
        "buyingPower": 0,
        "portfolioValue": 0
    }  # Replace with your actual Tradier data

@app.get("/api/ai/decisions")
async def get_ai_decisions(limit: int = 50):
    """Get AI decisions from Discord"""
    return []  # Replace with your actual Discord decision log
    
@app.get("/api/status")
async def get_status():
    """Get bot status"""
    return {
        "running": True,
        "mode": "MIXED",
        "stockMode": "PAPER",  # Change to LIVE when ready
        "cryptoMode": "PAPER",
        "safetyRequireMarketHours": settings.SAFETY_REQUIRE_MARKET_HOURS if hasattr(settings, 'SAFETY_REQUIRE_MARKET_HOURS') else True,
        "lastHeartbeat": "2026-03-28T14:33:00Z"
    }

@app.post("/api/control/toggle")
async def toggle_bot(request: dict):
    """Toggle bot on/off"""
    enabled = request.get("enabled", False)
    logger.info(f"Bot toggle requested: {enabled}")
    # TODO: Implement actual bot start/stop logic
    return {"success": True, "running": enabled}

@app.post("/api/control/safety-override")
async def toggle_safety(request: dict):
    """Toggle safety override"""
    enabled = request.get("enabled", False)
    logger.info(f"Safety override toggle: {enabled}")
    # TODO: Implement safety override logic
    return {"success": True, "safetyRequireMarketHours": enabled}

@app.get("/api/market-status")
async def get_market_status():
    """Get market status"""
    from datetime import datetime
    import pytz
    
    # Get current ET time
    et_tz = pytz.timezone('America/New_York')
    now_et = datetime.now(et_tz)
    
    # Market hours: 9:30 AM - 4:00 PM ET, Monday-Friday
    is_weekday = now_et.weekday() < 5
    is_market_hours = 9 <= now_et.hour < 16 or (now_et.hour == 9 and now_et.minute >= 30)
    is_market_open = is_weekday and is_market_hours
    
    return {
        "stock": {
            "isOpen": is_market_open,
            "nextOpen": "2026-03-31T09:30:00-04:00",
            "nextClose": "2026-03-28T16:00:00-04:00"
        },
        "crypto": {
            "isOpen": True  # Crypto markets 24/7
        }
    }