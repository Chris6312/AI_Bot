from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, get_db
from app.main import app
from app.models.position import Position
from app.models.watchlist_monitor_state import WatchlistMonitorState
from app.models.watchlist_symbol import WatchlistSymbol
from app.models.watchlist_upload import WatchlistUpload
from app.services.execution_lifecycle import execution_lifecycle
from app.services.kraken_service import crypto_ledger
from app.services.watchlist_service import watchlist_service


@contextmanager
def build_session_factory(tmp_path) -> Iterator[sessionmaker]:
    db_path = tmp_path / 'phase8_frozen_management.db'
    engine = create_engine(
        f'sqlite:///{db_path}',
        connect_args={'check_same_thread': False},
    )
    SessionFactory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    try:
        yield SessionFactory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _make_upload(upload_id: str, scope: str = 'stocks_only') -> WatchlistUpload:
    return WatchlistUpload(
        upload_id=upload_id,
        scan_id=f'scan-{upload_id}',
        schema_version='bot_watchlist_v3',
        provider='claude_tradier_mcp',
        scope=scope,
        source='test',
        payload_hash=f'hash-{upload_id}',
        generated_at_utc=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        received_at_utc=datetime(2026, 5, 1, 10, 1, tzinfo=UTC),
        watchlist_expires_at_utc=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
        validation_status='ACCEPTED',
        market_regime='risk_on',
        selected_count=1,
        is_active=True,
        validation_result_json={},
        raw_payload_json={},
        bot_payload_json={},
    )


def _make_stock_symbol(upload_id: str, symbol: str, exit_template: str, max_hold_hours: int = 48) -> WatchlistSymbol:
    return WatchlistSymbol(
        upload_id=upload_id,
        scope='stocks_only',
        symbol=symbol,
        quote_currency='USD',
        asset_class='stock',
        enabled=True,
        trade_direction='long',
        priority_rank=1,
        tier='tier_2',
        bias='bullish',
        setup_template='breakout_retest',
        bot_timeframes=['15m', '1h'],
        exit_template=exit_template,
        max_hold_hours=max_hold_hours,
        risk_flags=[],
        monitoring_status='ACTIVE',
    )


def _make_crypto_symbol(upload_id: str, symbol: str, exit_template: str) -> WatchlistSymbol:
    return WatchlistSymbol(
        upload_id=upload_id,
        scope='crypto_only',
        symbol=symbol,
        quote_currency='USD',
        asset_class='crypto',
        enabled=True,
        trade_direction='long',
        priority_rank=1,
        tier='tier_2',
        bias='bullish',
        setup_template='trend_continuation',
        bot_timeframes=['15m', '1h'],
        exit_template=exit_template,
        max_hold_hours=72,
        risk_flags=[],
        monitoring_status='ACTIVE',
    )


def _make_monitor_state(row: WatchlistSymbol, **kwargs) -> WatchlistMonitorState:
    defaults = dict(
        watchlist_symbol_id=row.id,
        upload_id=row.upload_id,
        scope=row.scope,
        symbol=row.symbol,
        monitoring_status='ACTIVE',
        latest_decision_state='PENDING_EVALUATION',
        latest_decision_reason=None,
        decision_context_json={'exitTemplate': row.exit_template},
        required_timeframes_json=row.bot_timeframes or [],
        evaluation_interval_seconds=900,
        last_decision_at_utc=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        last_evaluated_at_utc=None,
        next_evaluation_at_utc=datetime(2026, 5, 1, 10, 15, tzinfo=UTC),
        last_market_data_at_utc=None,
    )
    defaults.update(kwargs)
    return WatchlistMonitorState(**defaults)


# ---------------------------------------------------------------------------
# Test 1 — frozen exit template survives watchlist replacement (stock path)
# ---------------------------------------------------------------------------

def test_frozen_exit_template_survives_watchlist_replacement_stock(tmp_path) -> None:
    """Position.frozen_exit_template='scale_out_then_trail' overrides a newer watchlist row
    with exit_template='first_failed_follow_through'. The state map must report scaleOutReady=True
    when the profit target is reached, not followThroughFailed=True."""
    with build_session_factory(tmp_path) as SessionFactory:
        observed_at = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        entry_time = observed_at - timedelta(hours=3)

        db = SessionFactory()
        upload = _make_upload('upl-stock-freeze-1', scope='stocks_only')
        db.add(upload)
        db.flush()

        symbol_row = _make_stock_symbol(upload.upload_id, 'AAPL', exit_template='first_failed_follow_through')
        db.add(symbol_row)
        db.flush()

        position = Position(
            account_id=None,
            ticker='AAPL',
            shares=10,
            avg_entry_price=150.0,
            current_price=165.0,
            stop_loss=140.0,
            profit_target=162.0,
            trailing_stop=155.0,
            peak_price=165.0,
            strategy='WATCHLIST_ENTRY',
            entry_time=entry_time,
            entry_reasoning={},
            is_open=True,
            frozen_exit_template='scale_out_then_trail',
            frozen_max_hold_hours=48,
            frozen_management_policy_version='upl-stock-freeze-1',
            entry_watchlist_upload_id='upl-stock-freeze-1',
        )
        db.add(position)
        db.commit()

        state_map = watchlist_service._build_stock_position_state_map(
            db, observed_at=observed_at, broker_enrichment=False
        )
        db.close()

    assert 'AAPL' in state_map
    ps = state_map['AAPL']
    assert ps['hasOpenPosition'] is True
    assert ps['scaleOutReady'] is True, 'scale_out_then_trail + profit_target_reached must yield scaleOutReady=True'
    assert ps['followThroughFailed'] is False, 'frozen template overrides watchlist row; follow_through must not fire'


# ---------------------------------------------------------------------------
# Test 2 — milestone carry-forward on watchlist upload
# ---------------------------------------------------------------------------

def test_milestone_carry_forward_on_watchlist_upload(tmp_path) -> None:
    """When a second upload creates a new WatchlistSymbol for the same (scope, symbol),
    _upsert_monitor_state must copy all 11 milestone fields to the new WatchlistMonitorState."""
    with build_session_factory(tmp_path) as SessionFactory:
        observed_at = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
        tp_ts = datetime(2026, 5, 1, 13, 0, tzinfo=UTC)
        be_ts = datetime(2026, 5, 1, 13, 5, tzinfo=UTC)

        db = SessionFactory()
        upload_v1 = _make_upload('upl-carry-v1', scope='stocks_only')
        db.add(upload_v1)
        db.flush()

        row_v1 = _make_stock_symbol(upload_v1.upload_id, 'MSFT', exit_template='trail_after_impulse')
        db.add(row_v1)
        db.flush()

        ms_v1 = _make_monitor_state(
            row_v1,
            protection_mode_high_water='BREAK_EVEN_PROMOTED',
            tp_touched_at_utc=tp_ts,
            break_even_promoted_at_utc=be_ts,
            promoted_protective_floor=305.5,
            highest_protective_floor=310.0,
            peak_price_since_entry=320.0,
            impulse_trailing_stop=298.0,
            scale_out_taken=False,
        )
        db.add(ms_v1)
        db.commit()

        upload_v2 = _make_upload('upl-carry-v2', scope='stocks_only')
        db.add(upload_v2)
        db.flush()

        row_v2 = _make_stock_symbol(upload_v2.upload_id, 'MSFT', exit_template='trail_after_impulse')
        db.add(row_v2)
        db.flush()

        watchlist_service._upsert_monitor_state(db, row_v2, observed_at=observed_at)
        db.commit()

        ms_v2 = (
            db.query(WatchlistMonitorState)
            .filter(WatchlistMonitorState.watchlist_symbol_id == row_v2.id)
            .first()
        )
        db.close()

    assert ms_v2 is not None
    assert ms_v2.protection_mode_high_water == 'BREAK_EVEN_PROMOTED'
    assert ms_v2.tp_touched_at_utc is not None
    assert ms_v2.promoted_protective_floor == pytest.approx(305.5)
    assert ms_v2.peak_price_since_entry == pytest.approx(320.0)
    assert ms_v2.impulse_trailing_stop == pytest.approx(298.0)


# ---------------------------------------------------------------------------
# Test 3 — advance_position_milestones is monotonic
# ---------------------------------------------------------------------------

def test_advance_milestones_monotonic(tmp_path) -> None:
    """_apply_milestone_updates advances protection_mode_high_water and never downgrades it,
    even when called with a lower-ranked position state."""
    with build_session_factory(tmp_path) as SessionFactory:
        observed_at = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)

        db = SessionFactory()
        upload = _make_upload('upl-mono-1', scope='stocks_only')
        db.add(upload)
        db.flush()

        row = _make_stock_symbol(upload.upload_id, 'NVDA', exit_template='trail_after_impulse')
        db.add(row)
        db.flush()

        ms = _make_monitor_state(row)
        db.add(ms)
        db.commit()

        assert ms.protection_mode_high_water is None

        # Advance with BREAK_EVEN_PROMOTED state
        position_state_be = {
            'hasOpenPosition': True,
            'protectionMode': 'BREAK_EVEN_PROMOTED',
            'tpTouchedAtUtc': observed_at.isoformat(),
            'promotedProtectiveFloor': 410.0,
            'peakPrice': 430.0,
            'trailingStop': 400.0,
            'feeAdjustedBreakEven': 405.0,
            'impulseTrailArmed': False,
            'impulseTrailingStop': None,
            'scaleOutAlreadyTaken': False,
            'strongerMarginReached': False,
        }
        watchlist_service._apply_milestone_updates(ms, position_state_be, observed_at=observed_at)
        db.commit()

        assert ms.protection_mode_high_water == 'BREAK_EVEN_PROMOTED'
        assert ms.tp_touched_at_utc is not None
        assert ms.promoted_protective_floor == pytest.approx(410.0)
        assert ms.peak_price_since_entry == pytest.approx(430.0)
        assert ms.last_management_evaluated_at_utc is not None

        # Now call again with a lower-ranked INITIAL_RISK state — must NOT downgrade
        later_at = observed_at + timedelta(minutes=30)
        position_state_ir = {
            'hasOpenPosition': True,
            'protectionMode': 'INITIAL_RISK',
            'tpTouchedAtUtc': None,
            'promotedProtectiveFloor': 390.0,
            'peakPrice': 395.0,
            'trailingStop': 385.0,
            'feeAdjustedBreakEven': 405.0,
            'impulseTrailArmed': False,
            'impulseTrailingStop': None,
            'scaleOutAlreadyTaken': False,
            'strongerMarginReached': False,
        }
        watchlist_service._apply_milestone_updates(ms, position_state_ir, observed_at=later_at)
        db.commit()

        assert ms.protection_mode_high_water == 'BREAK_EVEN_PROMOTED', 'protection_mode_high_water must not downgrade'
        assert ms.tp_touched_at_utc is not None, 'tp_touched_at_utc must not be cleared'
        assert ms.promoted_protective_floor == pytest.approx(410.0), 'promoted floor must not decrease'
        assert ms.peak_price_since_entry == pytest.approx(430.0), 'peak price must not decrease'
        db.close()


# ---------------------------------------------------------------------------
# Test 4 — crypto inspect uses frozen exit template from intent context
# ---------------------------------------------------------------------------

def test_crypto_inspect_uses_frozen_exit_template_from_intent(tmp_path, monkeypatch) -> None:
    """intent.context_json['watchlist']['exitTemplate'] takes priority over
    the current watch_symbol.exit_template in _build_crypto_payload."""
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        upload = _make_upload('upl-crypto-frozen', scope='crypto_only')
        db.add(upload)
        db.flush()

        # Current watchlist row has a different (newer) exit template
        row = _make_crypto_symbol(upload.upload_id, 'BTC/USD', exit_template='first_failed_follow_through')
        db.add(row)
        db.flush()

        ms = _make_monitor_state(row)
        db.add(ms)
        db.flush()

        # Intent was created at fill time with the frozen exit template
        intent = execution_lifecycle.create_order_intent(
            db,
            account_id='paper-crypto-ledger',
            asset_class='crypto',
            symbol='BTC/USD',
            side='BUY',
            requested_quantity=0.001,
            requested_price=65000.0,
            execution_source='WATCHLIST_MONITOR_ENTRY',
            context={
                'mode': 'PAPER',
                'watchlist': {
                    'uploadId': upload.upload_id,
                    'scope': 'crypto_only',
                    'exitTemplate': 'trail_after_impulse',
                    'maxHoldHours': 72,
                    'botTimeframes': ['15m', '1h'],
                },
                'ohlcvPair': 'XBTUSD',
                'displayPair': 'BTC/USD',
            },
        )
        intent.status = 'FILLED'
        intent.filled_quantity = 0.001
        intent.avg_fill_price = 65000.0
        db.commit()

        monkeypatch.setattr(
            crypto_ledger,
            'get_positions',
            lambda: [
                {
                    'pair': 'BTC/USD',
                    'amount': 0.001,
                    'avgPrice': 65000.0,
                    'currentPrice': 67000.0,
                    'marketValue': 67.0,
                    'costBasis': 65.0,
                    'pnl': 2.0,
                    'pnlPercent': 3.07,
                    'entryTimeUtc': '2026-05-01T10:00:00+00:00',
                    'realizedPnl': 0.0,
                }
            ],
        )

        def override_get_db():
            local_db = SessionFactory()
            try:
                yield local_db
            finally:
                local_db.close()

        app.dependency_overrides[get_db] = override_get_db
        try:
            client = TestClient(app)
            response = client.get('/api/positions/inspect', params={'asset_class': 'crypto', 'symbol': 'BTC/USD'})
        finally:
            app.dependency_overrides.clear()
            db.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload['assetClass'] == 'crypto'
    exit_plan = payload.get('exitPlan', {})
    assert exit_plan.get('template') == 'trail_after_impulse', (
        f"Expected exit template from intent context ('trail_after_impulse'), "
        f"got {exit_plan.get('template')!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — _merge_protection_state_monotonic never downgrades
# ---------------------------------------------------------------------------

def test_no_downgrade_after_tp_touch() -> None:
    """_merge_protection_state_monotonic must preserve the higher protection mode and
    tpTouchedAtUtc from milestone state even when the live position state has lower rank."""
    tp_ts = '2026-05-01T13:00:00+00:00'
    milestone_state = {
        'protectionModeHighWater': 'BREAK_EVEN_PROMOTED',
        'tpTouchedAtUtc': tp_ts,
        'promotedProtectiveFloor': 305.5,
        'peakPriceSinceEntry': 320.0,
        'scaleOutTaken': False,
        'strongerMarginPromotedAtUtc': None,
        'impulseTrailingStop': None,
    }
    live_state = {
        'hasOpenPosition': True,
        'protectionMode': 'INITIAL_RISK',
        'tpTouchedAtUtc': None,
        'promotedProtectiveFloor': 290.0,
        'peakPrice': 295.0,
        'scaleOutReady': True,
        'scaleOutAlreadyTaken': False,
        'impulseTrailingStop': None,
    }
    merged = watchlist_service._merge_protection_state_monotonic(live_state, milestone_state)

    assert merged['protectionMode'] == 'BREAK_EVEN_PROMOTED', 'protection mode must not downgrade'
    assert merged['tpTouchedAtUtc'] == tp_ts, 'tpTouchedAtUtc must be preserved from milestone'
    assert merged['promotedProtectiveFloor'] == pytest.approx(305.5), 'floor must not decrease'
    assert merged['peakPrice'] == pytest.approx(320.0), 'peak price must not decrease'
    # scaleOutReady must remain True since scaleOutTaken=False in milestone
    assert merged.get('scaleOutReady') is True
    # original live fields not touched by milestone must be preserved
    assert merged['hasOpenPosition'] is True
