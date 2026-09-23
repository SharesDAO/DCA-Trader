from unittest.mock import MagicMock

from config import Config
from sharesdao_client import SharesDAOClient


def test_pool_catalog_retains_mint_mode_and_noneligible_metadata():
    client = SharesDAOClient()
    response = MagicMock(status_code=200)
    response.json.return_value = [
        dict(symbol='GOOD', blockchain=6, mint_mode=3, token_id='0xgood'),
        dict(symbol='OLD', blockchain=6, mint_mode=2, token_id='0xold'),
    ]
    client.session.post = MagicMock(return_value=response)
    pools = client.get_pool_list()
    assert pools['GOOD']['mint_mode'] == 3
    assert pools['OLD']['mint_mode'] == 2
    config = Config()
    config.stock_filter = []
    config.set_trading_stocks(pools)
    assert set(config.trading_stocks) == {'GOOD'}
    assert 'OLD' in client.stock_pools  # Retained for outstanding holdings.


def test_all_stocks_excludes_other_missing_and_malformed_modes():
    config = Config()
    config.stock_filter = []
    pools = {str(i): {'mint_mode': mode} for i, mode in enumerate([3, '3', 0, 1, 2, 4, None, 'invalid'])}
    pools['missing'] = {}
    config.set_trading_stocks(pools)
    assert set(config.trading_stocks) == {'0', '1'}


def test_allowlist_cannot_override_mint_mode_filter():
    config = Config()
    config.stock_filter = ['GOOD', 'BAD']
    config.set_trading_stocks({'GOOD': {'mint_mode': 3}, 'BAD': {'mint_mode': 2}, 'OTHER': {'mint_mode': 3}})
    assert set(config.trading_stocks) == {'GOOD'}
