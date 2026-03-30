from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.control_plane import require_admin_token
from app.services.template_evaluator import template_evaluation_service
from app.services.watchlist_monitoring import watchlist_monitoring_orchestrator
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


@router.post('/reconcile-status')
async def reconcile_watchlist_status(
    scope: Literal['stocks_only', 'crypto_only'] = Query(...),
    _: bool = Depends(require_admin_token),
    db: Session = Depends(get_db),
):
    return watchlist_service.reconcile_scope_statuses(db, scope=scope)


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

@router.get('/monitoring')
async def get_watchlist_monitoring(
    scope: Literal['stocks_only', 'crypto_only'] | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    payload = watchlist_service.get_monitoring_snapshot(db, scope=scope, include_inactive=include_inactive)
    if scope is not None and not payload:
        raise HTTPException(status_code=404, detail=f'No watchlist monitoring snapshot found for scope {scope}.')
    return payload




@router.get('/orchestration')
async def get_watchlist_orchestration_status(
    scope: Literal['stocks_only', 'crypto_only'] | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return watchlist_monitoring_orchestrator.get_runtime_status(db, scope=scope)


@router.post('/run-due')
async def run_due_watchlist_monitoring(
    scope: Literal['stocks_only', 'crypto_only'] | None = Query(default=None),
    limit_per_scope: int = Query(default=25, ge=1, le=100),
    _: bool = Depends(require_admin_token),
    db: Session = Depends(get_db),
):
    return watchlist_monitoring_orchestrator.run_due_once(db, scope=scope, limit_per_scope=limit_per_scope)


@router.get('/exit-readiness')
async def get_watchlist_exit_readiness(
    scope: Literal['stocks_only', 'crypto_only'] | None = Query(default=None),
    expiring_within_hours: int = Query(default=24, ge=1, le=240),
    db: Session = Depends(get_db),
):
    return watchlist_service.get_exit_readiness_snapshot(
        db,
        scope=scope,
        expiring_within_hours=expiring_within_hours,
    )



@router.post('/evaluate')
async def evaluate_watchlist_monitoring(
    scope: Literal['stocks_only', 'crypto_only'] = Query(...),
    limit: int = Query(default=25, ge=1, le=100),
    force: bool = Query(default=False),
    _: bool = Depends(require_admin_token),
    db: Session = Depends(get_db),
):
    return template_evaluation_service.evaluate_scope(db, scope=scope, limit=limit, force=force)
