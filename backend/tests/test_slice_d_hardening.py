from __future__ import annotations

from app.services.position_sizer import PositionSizer
from app.services.tradier_client import TradierClient, _extract_collection


def test_position_sizer_respects_existing_open_slots_and_symbol_exposure() -> None:
    sizer = PositionSizer()

    positions = sizer.calculate_stock_positions(
        [{'ticker': 'AAPL'}],
        20000.0,
        prices={'AAPL': 100.0},
        current_open_positions=max(sizer.max_positions - 1, 0),
        current_symbol_exposure={'AAPL': 200.0},
    )

    assert len(positions) == 1
    assert positions[0]['estimated_value'] == 2000.0
    assert positions[0]['shares'] == 20

    no_symbol_capacity = sizer.calculate_stock_positions(
        [{'ticker': 'AAPL'}],
        20000.0,
        prices={'AAPL': 100.0},
        current_open_positions=0,
        current_symbol_exposure={'AAPL': 5000.0},
    )
    assert no_symbol_capacity == []

    no_slots = sizer.calculate_stock_positions(
        [{'ticker': 'MSFT'}],
        20000.0,
        prices={'MSFT': 100.0},
        current_open_positions=sizer.max_positions,
    )
    assert no_slots == []


def test_tradier_normalizes_null_and_dict_payload_variants() -> None:
    client = TradierClient()

    assert client.normalize_orders_response(None) == []

    dict_orders = client.normalize_orders_response({'id': '1', 'symbol': 'AAPL', 'side': 'buy', 'status': 'open', 'quantity': '2'})
    assert len(dict_orders) == 1
    assert dict_orders[0]['symbol'] == 'AAPL'
    assert dict_orders[0]['requested_quantity'] == 2.0

    assert _extract_collection({'positions': None}, 'positions', 'position') == []
    assert _extract_collection({'positions': {'position': {'symbol': 'AAPL'}}}, 'positions', 'position') == [{'symbol': 'AAPL'}]
    assert _extract_collection({'orders': [{'order': {'id': '1'}}]}, 'orders', 'order') == [{'id': '1'}]
