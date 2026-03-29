"""
Crypto API Router
Handles all crypto-related endpoints (Kraken integration)
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.services.control_plane import ensure_execution_armed, require_admin_token
from app.services.kraken_service import TOP_30_PAIRS, crypto_ledger, kraken_service
from app.services.trade_validator import trade_validator

router = APIRouter(prefix="/crypto", tags=["crypto"])


class CryptoTradeRequest(BaseModel):
    pair: str  # Display format: BTC/USD
    side: str  # BUY or SELL
    amount: float
    price: Optional[float] = None


@router.get("/positions")
async def get_crypto_positions():
    """Get current crypto positions (paper)"""
    try:
        return crypto_ledger.get_positions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_crypto_history(limit: int = Query(50, ge=1, le=500)):
    """Get crypto trade history (paper)"""
    try:
        ledger = crypto_ledger.get_ledger()
        trades = ledger['trades'][-limit:]
        return list(reversed(trades))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/paper-ledger")
async def get_paper_ledger():
    """Get full paper trading ledger"""
    try:
        return crypto_ledger.get_ledger()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prices")
async def get_crypto_prices(pairs: str = Query(..., description="Comma-separated OHLCV pairs")):
    """Get current prices for crypto pairs"""
    try:
        pair_list = pairs.split(',')
        prices = kraken_service.get_prices(pair_list)
        return prices
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/candles")
async def get_crypto_candles(
    pair: str = Query(..., description="OHLCV pair (e.g., XBTUSD)"),
    interval: int = Query(5, description="Interval in minutes"),
    limit: int = Query(100, ge=1, le=720)
):
    """Get OHLC candle data for a crypto pair"""
    try:
        candles = kraken_service.get_ohlc(pair, interval, limit)
        return candles
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pairs")
async def get_top_pairs():
    """Get top 15 liquid crypto pairs"""
    return [
        {"display": display, "ohlcv": ohlcv}
        for display, ohlcv in TOP_30_PAIRS.items()
    ]


@router.post("/trade")
async def execute_crypto_trade(trade: CryptoTradeRequest, _: bool = Depends(require_admin_token)):
    """Execute a paper crypto trade"""
    try:
        ensure_execution_armed()

        trade_side = trade.side.upper().strip()
        if trade_side not in {'BUY', 'SELL'}:
            raise HTTPException(status_code=400, detail=f'Invalid side: {trade.side}')

        if trade.pair not in TOP_30_PAIRS:
            raise HTTPException(status_code=400, detail=f"Invalid pair: {trade.pair}")

        is_valid, validation_message = trade_validator.validate_crypto_trade(trade.pair, trade.amount)
        if not is_valid:
            raise HTTPException(status_code=400, detail=validation_message)

        ohlcv_pair = TOP_30_PAIRS[trade.pair]
        result = crypto_ledger.execute_trade(
            pair=trade.pair,
            ohlcv_pair=ohlcv_pair,
            side=trade_side,
            amount=trade.amount,
            price=trade.price
        )

        if result.get('status') == 'REJECTED':
            raise HTTPException(status_code=400, detail=result.get('reason'))

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
