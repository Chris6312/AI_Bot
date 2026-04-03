from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base, get_db
from app.main import app
from app.services.kraken_service import KrakenPairMetadata
from app.services.pre_trade_gate import PreTradeGateDecision, pre_trade_gate
from app.services.safety_validator import safety_validator

UTC = timezone.utc


@contextmanager
def build_session_factory(tmp_path) -> Iterator[sessionmaker]:
    db_path = tmp_path / 'phase3_gate.db'
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


@pytest.mark.asyncio
async def test_stock_gate_rejects_stale_quote(monkeypatch, tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()

        monkeypatch.setattr('app.services.pre_trade_gate.get_execution_gate_status', lambda: type('Gate', (), {'allowed': True, 'state': 'ARMED', 'reason': '', 'status_code': 200})())
        monkeypatch.setattr('app.services.pre_trade_gate.tradier_client.is_ready', lambda mode=None: True)
        monkeypatch.setattr('app.services.pre_trade_gate.tradier_client.get_quote_sync', lambda symbol, mode=None: {'symbol': symbol, '_fetched_at_utc': datetime.now(UTC).isoformat()})
        monkeypatch.setattr(
            'app.services.pre_trade_gate.trade_validator.validate_stock_trade_with_quote',
            lambda ticker, shares, mode='PAPER', quote=None: {
                'valid': True,
                'reason': 'ok',
                'price': 101.25,
                'volume': 500000,
                'spread_pct': 0.1,
                'trade_value': 506.25,
                'quote_fetched_at': (datetime.now(UTC) - timedelta(seconds=120)).isoformat(),
                'quote_age_seconds': 120.0,
            },
        )

        called = {'count': 0}

        async def fake_safety(*args, **kwargs):
            called['count'] += 1
            return {'safe': True}

        monkeypatch.setattr(pre_trade_gate.safety, 'validate', fake_safety)

        decision = await pre_trade_gate.evaluate_stock_order(
            ticker='AAPL',
            shares=5,
            mode='PAPER',
            account={'accountId': 'paper-1', 'cash': 10000, 'buyingPower': 10000, 'portfolioValue': 10000},
            db=db,
            execution_source='TEST',
            decision_context={},
        )

        assert decision.allowed is False
        assert 'stale' in decision.rejection_reason.lower()
        assert called['count'] == 0


@pytest.mark.asyncio
async def test_stock_gate_allows_fresh_quote_and_safety(monkeypatch, tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()

        monkeypatch.setattr('app.services.pre_trade_gate.get_execution_gate_status', lambda: type('Gate', (), {'allowed': True, 'state': 'ARMED', 'reason': '', 'status_code': 200})())
        monkeypatch.setattr('app.services.pre_trade_gate.tradier_client.is_ready', lambda mode=None: True)
        monkeypatch.setattr('app.services.pre_trade_gate.tradier_client._credentials_for_mode', lambda mode=None: {'account_id': 'paper-1'})
        monkeypatch.setattr('app.services.pre_trade_gate.tradier_client.get_quote_sync', lambda symbol, mode=None: {'symbol': symbol, '_fetched_at_utc': datetime.now(UTC).isoformat()})
        monkeypatch.setattr(
            'app.services.pre_trade_gate.trade_validator.validate_stock_trade_with_quote',
            lambda ticker, shares, mode='PAPER', quote=None: {
                'valid': True,
                'reason': 'ok',
                'price': 101.25,
                'volume': 500000,
                'spread_pct': 0.1,
                'trade_value': 506.25,
                'quote_fetched_at': datetime.now(UTC).isoformat(),
                'quote_age_seconds': 1.5,
            },
        )

        async def fake_safety(*args, **kwargs):
            return {'safe': True}

        monkeypatch.setattr(pre_trade_gate.safety, 'validate', fake_safety)

        decision = await pre_trade_gate.evaluate_stock_order(
            ticker='AAPL',
            shares=5,
            mode='PAPER',
            account={'accountId': 'paper-1', 'cash': 10000, 'buyingPower': 10000, 'portfolioValue': 10000},
            db=db,
            execution_source='TEST',
            decision_context={'vix': 12},
        )

        assert decision.allowed is True
        assert decision.market_data['currentPrice'] == 101.25
        assert decision.risk_data['estimatedValue'] == 506.25


@pytest.mark.asyncio
async def test_crypto_gate_rejects_candle_gap(monkeypatch, tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()

        now = datetime.now(UTC)
        candles = []
        base = int((now - timedelta(minutes=120)).timestamp())
        for idx in range(20):
            gap = 300 if idx != 10 else 1800
            base += gap
            candles.append({'timestamp': base, 'open': 1, 'high': 1, 'low': 1, 'close': 1})

        monkeypatch.setattr('app.services.pre_trade_gate.get_execution_gate_status', lambda: type('Gate', (), {'allowed': True, 'state': 'ARMED', 'reason': '', 'status_code': 200})())
        monkeypatch.setattr('app.services.pre_trade_gate.kraken_service.resolve_pair', lambda pair: KrakenPairMetadata(display_pair='BTC/USD', rest_pair='XBTUSD', pair_key='XXBTZUSD', ws_pair='XBT/USD', altname='XBTUSD'))
        monkeypatch.setattr('app.services.pre_trade_gate.kraken_service.get_ticker', lambda pair: {'c': ['100'], 'v': ['0', '5000'], '_fetched_at_utc': now.isoformat()})
        monkeypatch.setattr('app.services.pre_trade_gate.kraken_service.get_ohlc', lambda pair, interval=5, limit=20: candles)
        monkeypatch.setattr(
            'app.services.pre_trade_gate.trade_validator.validate_crypto_trade_with_market_data',
            lambda pair, amount, ticker=None, candles=None: {
                'valid': True,
                'reason': 'ok',
                'price': 100.0,
                'volume_usd': 500000.0,
                'spread_pct': 0.2,
                'trade_value': 200.0,
                'ticker_fetched_at': now.isoformat(),
                'ticker_age_seconds': 1.0,
            },
        )

        async def fake_safety(*args, **kwargs):
            return {'safe': True}

        monkeypatch.setattr(pre_trade_gate.safety, 'validate', fake_safety)

        decision = await pre_trade_gate.evaluate_crypto_order(
            pair='BTC/USD',
            amount=2.0,
            account={'cash': 10000, 'buyingPower': 10000, 'portfolioValue': 10000},
            db=db,
            execution_source='TEST',
            decision_context={},
        )

        assert decision.allowed is False
        assert 'continuity' in decision.rejection_reason.lower()


def test_crypto_route_returns_gate_rejection(monkeypatch, tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        def override_get_db() -> Iterator[Session]:
            db = SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db

        from app.core.config import settings

        original_token = settings.ADMIN_API_TOKEN
        settings.ADMIN_API_TOKEN = 'unit-token'

        monkeypatch.setattr('app.routers.crypto.ensure_execution_armed', lambda: None)
        monkeypatch.setattr('app.routers.crypto.crypto_ledger.get_ledger', lambda: {'balance': 10000.0, 'equity': 10000.0})
        monkeypatch.setattr('app.routers.crypto.kraken_service.resolve_pair', lambda pair: KrakenPairMetadata(display_pair='BTC/USD', rest_pair='XBTUSD', pair_key='XXBTZUSD', ws_pair='XBT/USD', altname='XBTUSD'))

        async def fake_gate(**kwargs):
            return PreTradeGateDecision(
                allowed=False,
                asset_class='crypto',
                symbol='BTC/USD',
                state='REJECTED',
                rejection_reason='Synthetic gate rejection',
            )

        monkeypatch.setattr('app.routers.crypto.pre_trade_gate.evaluate_crypto_order', fake_gate)

        client = TestClient(app)
        response = client.post(
            '/api/crypto/trade',
            headers={'X-Admin-Token': 'unit-token'},
            json={'pair': 'BTC/USD', 'side': 'BUY', 'amount': 0.5},
        )

        assert response.status_code == 400
        assert response.json()['detail'] == 'Synthetic gate rejection'

        settings.ADMIN_API_TOKEN = original_token
        app.dependency_overrides.clear()


def test_safety_validator_rejects_missing_vix_for_live_stock(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        result = safety_validator.validate_sync(
            {
                'candidates': [{'ticker': 'AAPL', 'shares': 5, 'estimated_value': 500.0, 'price': 100.0}],
                'vix': None,
                'enforce_vix': True,
                'require_market_hours': False,
            },
            {'accountId': 'live-1', 'cash': 10000.0, 'buyingPower': 10000.0, 'portfolioValue': 10000.0},
            db,
            account_id='live-1',
            asset_class='stock',
        )

        assert result['safe'] is False
        assert 'vix unavailable' in result['reason'].lower()
