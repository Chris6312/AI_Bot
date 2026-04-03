from __future__ import annotations

from app.services.kraken_service import KrakenAPIService


ASSET_PAIRS_RESULT = {
    'XXBTZUSD': {
        'altname': 'XBTUSD',
        'wsname': 'XBT/USD',
        'base': 'XXBT',
        'quote': 'ZUSD',
    },
    'AAVEUSD': {
        'altname': 'AAVEUSD',
        'wsname': 'AAVE/USD',
        'base': 'AAVE',
        'quote': 'USD',
    },
    'CHZUSD': {
        'altname': 'CHZUSD',
        'wsname': 'CHZ/USD',
        'base': 'CHZ',
        'quote': 'USD',
    },
    'SHIBUSD': {
        'altname': 'SHIBUSD',
        'wsname': 'SHIB/USD',
        'base': 'SHIB',
        'quote': 'USD',
    },
    'OPUSD': {
        'altname': 'OPUSD',
        'wsname': 'OP/USD',
        'base': 'OP',
        'quote': 'USD',
    },
}


def test_resolve_pair_accepts_display_aliases_and_bitcoin_synonym(monkeypatch) -> None:
    service = KrakenAPIService()
    monkeypatch.setattr(service, '_api_call', lambda endpoint, params=None: ASSET_PAIRS_RESULT if endpoint == 'AssetPairs' else None)

    btc_pair = service.resolve_pair('BTC/USD')
    aave_pair = service.resolve_pair('AAVE/USD')
    shib_pair = service.resolve_pair('SHIBUSD')

    assert btc_pair is not None
    assert btc_pair.rest_pair == 'XBTUSD'
    assert btc_pair.display_pair == 'BTC/USD'

    assert aave_pair is not None
    assert aave_pair.rest_pair == 'AAVEUSD'
    assert aave_pair.display_pair == 'AAVE/USD'

    assert shib_pair is not None
    assert shib_pair.display_pair == 'SHIB/USD'


def test_get_supported_pairs_builds_dynamic_assetpairs_map(monkeypatch) -> None:
    service = KrakenAPIService()
    monkeypatch.setattr(service, '_api_call', lambda endpoint, params=None: ASSET_PAIRS_RESULT if endpoint == 'AssetPairs' else None)

    pairs = service.get_supported_pairs()

    assert pairs['BTC/USD'] == 'XBTUSD'
    assert pairs['AAVE/USD'] == 'AAVEUSD'
    assert pairs['CHZ/USD'] == 'CHZUSD'
    assert pairs['SHIB/USD'] == 'SHIBUSD'
    assert pairs['OP/USD'] == 'OPUSD'



def test_resolve_pair_negative_cache_suppresses_repeat_refresh(monkeypatch) -> None:
    service = KrakenAPIService()
    calls = {'count': 0}

    def fake_api(endpoint, params=None):
        if endpoint == 'AssetPairs':
            calls['count'] += 1
            return ASSET_PAIRS_RESULT
        return None

    monkeypatch.setattr(service, '_api_call', fake_api)

    service.get_supported_pairs()
    assert calls['count'] == 1

    assert service.resolve_pair('NOTREAL/USD') is None
    assert service.resolve_pair('NOTREAL/USD') is None
    assert calls['count'] == 2
