from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.control_plane import require_admin_token
from app.services.watchlist_service import WatchlistValidationError, watchlist_service

router = APIRouter(prefix='/watchlists', tags=['watchlists'])


@router.post('/ingest')
async def ingest_watchlist(
    payload: dict[str, Any],
    _: bool = Depends(require_admin_token),
    db: Session = Depends(get_db),
):
    try:
        return watchlist_service.ingest_watchlist(db, payload, source='api')
    except WatchlistValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/latest')
async def get_latest_watchlist(
    scope: Literal['stocks_only', 'crypto_only'] | None = Query(default=None),
    db: Session = Depends(get_db),
):
    payload = watchlist_service.get_latest_upload(db, scope=scope, active_only=False)
    if scope is not None and not payload:
        raise HTTPException(status_code=404, detail=f'No watchlist found for scope {scope}.')
    return payload


@router.get('/active')
async def get_active_watchlist(
    scope: Literal['stocks_only', 'crypto_only'] | None = Query(default=None),
    db: Session = Depends(get_db),
):
    payload = watchlist_service.get_latest_upload(db, scope=scope, active_only=True)
    if scope is not None and not payload:
        raise HTTPException(status_code=404, detail=f'No active watchlist found for scope {scope}.')
    return payload
