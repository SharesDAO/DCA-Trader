from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sharesdao_client import SharesDAOClient
from trade_manager import TradeManager
from utils.stock_units import pool_ratio, tokens_to_shares, shares_to_tokens


@pytest.mark.parametrize('conversion,shares', [('10','1'), ('0.1','100'), ('1','10')])
def test_wallet_tokens_and_actual_shares(conversion, shares):
    assert tokens_to_shares('10', conversion) == Decimal(shares)
    assert shares_to_tokens(shares, conversion) == 10


@pytest.mark.parametrize('conversion', [0, -1, 'NaN', 'Infinity', '', None])
def test_invalid_pool_ratio_is_not_assumed_one(conversion):
    with pytest.raises((ValueError, ArithmeticError)):
        pool_ratio({'token_per_share': conversion})


def test_exact_ratio_and_legacy_missing_field():
    assert pool_ratio({}) == 1
    assert pool_ratio({'token_per_share': 1, 'token_per_share_exact': '0.999999999999999999'}) == Decimal('0.999999999999999999')
    assert tokens_to_shares('9007199254740993.000000001', '10') == Decimal('900719925474099.3000000001')


def test_catalog_preserves_current_ratio_and_excludes_other_network():
    client = SharesDAOClient(blockchain='arbitrum')
    client.session.post = MagicMock(return_value=MagicMock(status_code=200))
    client.session.post.return_value.json.return_value = [
        dict(symbol='MSTU', blockchain=6, token_id='{"arbitrum":"0xcorrect","base":"0xother"}', token_per_share=10, token_per_share_exact='10'),
        dict(symbol='WRONG', blockchain=6, token_id='{"base":"0xother"}', token_per_share=10),
    ]
    pools = client.get_pool_list()
    assert pools['MSTU']['asset_id'] == '0xcorrect'
    assert client.get_token_per_share('MSTU') == 10
    assert 'WRONG' not in pools


def test_execution_price_refreshes_conversion_and_invalidates_bad_quotes():
    client = SharesDAOClient()
    client.stock_pools = {'MSTU': {'pool_id': 'pool', 'token_per_share': 1}}
    response = MagicMock(status_code=200)
    response.json.return_value = dict(buy_price='40', sell_price='41', token_per_share=10)
    client.session.get = MagicMock(return_value=response)
    assert client.get_stock_price('MSTU', slippage=0) == 40
    assert client.get_token_per_share('MSTU') == 10
    response.json.return_value['token_per_share'] = 0
    assert client.get_stock_sell_price('MSTU') is None
    assert client.get_stock_price('MSTU') is None


@pytest.mark.parametrize('conversion', ['10', '0.1'])
def test_legacy_order_quantities_stay_tokens_and_price_is_per_token(conversion):
    api = MagicMock()
    api.get_stock_price.return_value = 40
    api.get_token_per_share.return_value = Decimal(conversion)
    db, chain, wallets = MagicMock(), MagicMock(), MagicMock()
    db.get_wallet.return_value = {'private_key': 'test-only'}
    cfg = SimpleNamespace(get_stock_token_address=lambda symbol: 'token', mint_address='mint', burn_address='burn',
                          order_expiry_days=1, liquid_mode=False, sell_slippage=0)
    manager = TradeManager(db, chain, api, wallets, cfg)
    assert manager.place_buy_order('wallet', 'MSTU', 40, dry_run=True)
    bought = chain.submit_buy_order.call_args.kwargs
    assert bought['stock_quantity'] == pytest.approx(40 / 40.20 * float(conversion))
    assert manager.place_sell_order('wallet', 'MSTU', float(conversion), order_type='MARKET', dry_run=True)
    sold = chain.submit_sell_order.call_args.kwargs
    assert sold['stock_quantity'] == float(conversion)
    assert sold['usdc_amount'] == pytest.approx(40)


def test_legacy_portfolio_values_actual_shares_without_changing_cost():
    import asyncio
    from utils.portfolio import PortfolioCalculator
    db, api, chain = MagicMock(), MagicMock(), MagicMock()
    cfg = SimpleNamespace(portfolio_cache_refresh=1, vault_address='vault')
    wallets = MagicMock()
    wallets.get_active_wallets.return_value = [dict(address='wallet', assigned_stock='MSTU')]
    db.get_pending_orders.return_value = []
    db.get_all_positions.return_value = [dict(wallet_address='wallet', quantity=10, total_cost_usdc=40)]
    api.get_stock_price.return_value = 40
    api.get_token_per_share.return_value = Decimal('10')
    chain.get_usdc_balance.return_value = 0
    result = asyncio.run(PortfolioCalculator(db, cfg, api).calculate_total_usd_value(wallets, chain))
    assert result['total_value'] == 40
