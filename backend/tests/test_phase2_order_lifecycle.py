from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base, get_db
from app.main import app
from app.models.account import Account  # noqa: F401
from app.models.order_event import OrderEvent
from app.models.order_intent import OrderIntent
from app.models.position import Position
from app.models.trade import Trade
from app.services.execution_lifecycle import execution_lifecycle
from app.services.tradier_client import tradier_client


@contextmanager
def build_session_factory(tmp_path) -> Iterator[sessionmaker]:
    db_path = tmp_path / 'phase2_lifecycle.db'
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


def _create_intent(db: Session) -> OrderIntent:
    return execution_lifecycle.create_order_intent(
        db,
        account_id='paper-123',
        asset_class='stock',
        symbol='AAPL',
        side='BUY',
        requested_quantity=5,
        requested_price=100.0,
        execution_source='TEST_SUITE',
        context={'mode': 'PAPER'},
    )


def test_materialize_stock_fill_requires_confirmed_fill(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        intent = _create_intent(db)
        execution_lifecycle.record_submission(db, intent, {'order': {'id': 'ord-1', 'status': 'open', 'quantity': 5}})
        intent = execution_lifecycle.refresh_from_order_snapshot(
            db,
            intent,
            {'order': {'id': 'ord-1', 'status': 'open', 'quantity': 5, 'exec_quantity': 0}},
        )

        fill_record = execution_lifecycle.materialize_stock_fill(
            db,
            intent,
            strategy='AI_SCREENING',
            stop_loss=98.5,
            profit_target=102.5,
            trailing_stop=97.0,
            current_price=100.0,
        )

        assert fill_record is None
        assert db.query(Position).count() == 0
        assert db.query(Trade).count() == 0
        assert intent.status == 'SUBMITTED'


def test_materialize_stock_fill_uses_confirmed_partial_quantity_only(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        intent = _create_intent(db)
        execution_lifecycle.record_submission(db, intent, {'order': {'id': 'ord-2', 'status': 'open', 'quantity': 5}})
        intent = execution_lifecycle.refresh_from_order_snapshot(
            db,
            intent,
            {
                'order': {
                    'id': 'ord-2',
                    'status': 'open',
                    'quantity': 5,
                    'exec_quantity': 3,
                    'avg_fill_price': 101.25,
                }
            },
        )

        fill_record = execution_lifecycle.materialize_stock_fill(
            db,
            intent,
            strategy='AI_SCREENING',
            stop_loss=99.0,
            profit_target=104.0,
            trailing_stop=98.0,
            current_price=101.25,
        )

        position = db.query(Position).one()
        trade = db.query(Trade).one()
        db.refresh(intent)

        assert fill_record is not None
        assert fill_record['filled_shares'] == 3
        assert position.shares == 3
        assert trade.shares == 3
        assert intent.status == 'PARTIALLY_FILLED'
        assert intent.position_id == position.id
        assert intent.trade_id == trade.id


def test_stock_history_endpoint_returns_lifecycle_events(tmp_path) -> None:
    with build_session_factory(tmp_path) as SessionFactory:
        db = SessionFactory()
        intent = _create_intent(db)
        execution_lifecycle.record_submission(db, intent, {'order': {'id': 'ord-3', 'status': 'open', 'quantity': 5}})
        execution_lifecycle.refresh_from_order_snapshot(
            db,
            intent,
            {
                'order': {
                    'id': 'ord-3',
                    'status': 'filled',
                    'quantity': 5,
                    'exec_quantity': 5,
                    'avg_fill_price': 102.5,
                }
            },
        )

        def override_get_db() -> Iterator[Session]:
            session = SessionFactory()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_get_db
        try:
            with TestClient(app) as client:
                response = client.get('/api/stocks/history')
        finally:
            app.dependency_overrides.pop(get_db, None)
            db.close()

        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]['symbol'] == 'AAPL'
        assert payload[0]['status'] == 'FILLED'
        assert [event['eventType'] for event in payload[0]['events']] == [
            'INTENT_CREATED',
            'ORDER_SUBMITTED',
            'ORDER_STATUS_UPDATED',
        ]


def test_tradier_normalize_order_response_reads_nested_fill_fields() -> None:
    normalized = tradier_client.normalize_order_response(
        {
            'order': {
                'id': 'ord-9',
                'status': 'filled',
                'quantity': '7',
                'exec_quantity': '7',
                'avg_fill_price': '123.45',
            }
        }
    )

    assert normalized['id'] == 'ord-9'
    assert normalized['status'] == 'FILLED'
    assert normalized['requested_quantity'] == 7.0
    assert normalized['filled_quantity'] == 7.0
    assert normalized['avg_fill_price'] == 123.45
