"""
Crypto API Router
Handles all crypto-related endpoints (Kraken integration)
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.control_plane import ensure_execution_armed, require_admin_token
from app.services.kraken_service import crypto_ledger, kraken_service
from app.services.pre_trade_gate import pre_trade_gate

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
    """Get tradable crypto pairs from Kraken AssetPairs."""
    return [
        {"display": display, "ohlcv": ohlcv}
        for display, ohlcv in kraken_service.get_supported_pairs().items()
    ]


@router.post("/trade")
async def execute_crypto_trade(
    trade: CryptoTradeRequest,
    _: bool = Depends(require_admin_token),
    db: Session = Depends(get_db),
):
    """Execute a paper crypto trade"""
    try:
        ensure_execution_armed()

        trade_side = trade.side.upper().strip()
        if trade_side not in {'BUY', 'SELL'}:
            raise HTTPException(status_code=400, detail=f'Invalid side: {trade.side}')

        resolved_pair = kraken_service.resolve_pair(trade.pair)
        if resolved_pair is None:
            raise HTTPException(status_code=400, detail=f"Invalid pair: {trade.pair}")

        ledger = crypto_ledger.get_ledger()
        gate = await pre_trade_gate.evaluate_crypto_order(
            pair=trade.pair,
            amount=trade.amount,
            account={
                'cash': ledger['balance'],
                'buyingPower': ledger['balance'],
                'portfolioValue': ledger.get('equity', ledger['balance']),
            },
            db=db,
            execution_source='HTTP_ADMIN_CRYPTO',
            decision_context={'requestedSide': trade_side},
        )
        if not gate.allowed:
            raise HTTPException(status_code=400, detail=gate.rejection_reason)

        ohlcv_pair = resolved_pair.rest_pair
        execution_price = trade.price or float(gate.market_data.get('currentPrice') or 0.0)
        if execution_price <= 0:
            raise HTTPException(status_code=400, detail='Current market price unavailable after pre-trade gate approval.')

        result = crypto_ledger.execute_trade(
            pair=trade.pair,
            ohlcv_pair=ohlcv_pair,
            side=trade_side,
            amount=trade.amount,
            price=execution_price,
        )

        if result.get('status') == 'REJECTED':
            raise HTTPException(status_code=400, detail=result.get('reason'))

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
