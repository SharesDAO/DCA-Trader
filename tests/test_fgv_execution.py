import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from eth_account import Account
from web3.exceptions import TransactionNotFound
from web3 import Web3
from hexbytes import HexBytes

from config import Config
from database import Database
from fgv_trader.execution import LiveBroker
from fgv_trader.runtime import PAPER_KEY
from fgv_trader.store import Store


@pytest.fixture
def live(tmp_path):
    config = Config()
    account = Account.create()
    vault = Account.create()
    config.vault_address = vault.address
    config.vault_private_key = vault.key.hex()
    asset_address = '0x' + '22' * 20
    usdc_address = '0x' + '33' * 20
    destination = '0x' + '44' * 20
    config.mint_address = config.burn_address = destination
    config.trading_stocks = {'TSLA': dict(asset_id=asset_address, pool_id='test-pool')}
    db = Database(str(tmp_path / 'live-test.db'), PAPER_KEY)
    db.create_wallet(account.address, account.key.hex(), config.blockchain, 'TSLA')
    store = Store(db, config.blockchain)
    trade = dict(id='trade', symbol='TSLA', session='2026-09-04', wallet=account.address,
                 state='BUY_PENDING', amount=100)
    store.save_trade(trade)

    def contract(address, decimals):
        c = MagicMock()
        c.address = address
        c.functions.decimals.return_value.call.return_value = decimals
        c.functions.balanceOf.return_value.call.return_value = 10000 * 10 ** decimals
        c.functions.transfer.return_value._encode_transaction_data.return_value = '0x' + '00' * 68
        return c
    usdc = contract(usdc_address, 6)
    stock = contract(asset_address, 18)
    chain = MagicMock()
    chain.chain_id = 42161
    chain.usdc_contract = usdc
    chain.usdc_decimals = 6
    chain.get_token_contract.return_value = stock
    chain.get_nonce.return_value = 0
    chain.build_eip1559_transaction.side_effect = lambda tx: dict(tx, gasPrice=1000000000)
    chain.w3.eth.estimate_gas.return_value = 100000
    chain.w3.eth.get_transaction_receipt.side_effect = TransactionNotFound('not mined')
    api = MagicMock()
    api.get_stock_price.return_value = 100
    api.get_stock_sell_price.return_value = 99
    return LiveBroker(store, chain, config, api), store, trade


@pytest.mark.parametrize('side,amount', [('buy', 100), ('sell', 1)])
def test_market_memo_and_signed_journal_precede_broadcast(live, side, amount):
    broker, store, trade = live
    op = store.new_operation(trade, side, amount, expiry_days=1)
    broadcasts = []
    def broadcast(raw):
        saved = store.operations()[0]
        assert saved['state'] == 'SIGNED'
        assert saved['raw_tx'].removeprefix('0x') == raw.hex()
        assert saved['memo']['type'] == 'MARKET'
        assert saved['memo']['customer_id'] == op['id']
        assert saved['tx_hash']
        broadcasts.append(raw)
        raise TimeoutError('Response lost after broadcast')
    broker.w3.eth.send_raw_transaction.side_effect = broadcast
    broker.step(op)
    recovered = store.operations()[0]
    broker.step(recovered)
    assert len(broadcasts) == 2
    assert broadcasts[0] == broadcasts[1]
    broker.blockchain.get_nonce.assert_called_once()
    quote_fn = broker.api.get_stock_price if side == 'buy' else broker.api.get_stock_sell_price
    quote_fn.assert_called_once_with('TSLA', slippage=0)


def test_terminal_record_requires_matching_customer_and_chain_receipt(live, monkeypatch):
    broker, store, trade = live
    op = store.new_operation(trade, 'buy', 100, expiry_days=1)
    op['tx_hash'] = '0x1234'
    records = [dict(customer_id='someone-else', crypto_tx_id='0x1234', status=4, closed_tx_id='0x5678')]
    response = MagicMock()
    response.json.side_effect = lambda: records
    monkeypatch.setattr('fgv_trader.execution.requests.post', lambda *a, **k: response)
    broker._confirmed_receipt = MagicMock(return_value={'status': 1})
    broker._incoming = MagicMock(side_effect=lambda r, token, wallet: .95 if token != broker.contract('USDC') else 2)
    assert broker.settlement(op)['state'] == 'SUBMITTED'
    broker._confirmed_receipt.assert_not_called()
    records[0]['customer_id'] = op['id']
    result = broker.settlement(op)
    assert result['state'] == 'SETTLED'
    assert result['quantity'] == .95
    assert result['cost'] == 98


def test_cash_in_wallet_is_not_a_fill(live, monkeypatch):
    broker, store, trade = live
    op = store.new_operation(trade, 'sell', 1, expiry_days=1)
    op['tx_hash'] = '0x1234'
    response = MagicMock()
    response.json.return_value = []
    monkeypatch.setattr('fgv_trader.execution.requests.post', lambda *a, **k: response)
    assert broker.balance(trade['wallet']) == 10000
    assert broker.settlement(op)['state'] == 'SUBMITTED'


def test_full_refund_closes_order_without_creating_position(live, monkeypatch):
    broker, store, trade = live
    op = store.new_operation(trade, 'buy', 100, expiry_days=1)
    op['tx_hash'] = '0x1234'
    def post(*args, **kwargs):
        records = [] if kwargs['json']['status'] == 4 else [dict(
            customer_id=op['id'], crypto_tx_id='1234', status=3, closed_tx_id='0x5678')]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: records)
    monkeypatch.setattr('fgv_trader.execution.requests.post', post)
    broker._confirmed_receipt = lambda tx: {'status': 1}
    broker._incoming = lambda receipt, token, wallet: 100 if token == broker.contract('USDC') else 0
    result = broker.settlement(op)
    assert result['state'] == 'SETTLED'
    assert result['quantity'] == 0
    assert result['cost'] == 0


def test_incoming_transfers_use_token_recipient_and_actual_units(live):
    broker, store, trade = live
    token = broker.contract('USDC')
    destination = HexBytes('0x' + trade['wallet'].removeprefix('0x').lower().zfill(64))
    event = dict(address=token.address, topics=[Web3.keccak(text='Transfer(address,address,uint256)'),
                 HexBytes('0x' + '00' * 32), destination], data=HexBytes((1234567).to_bytes(32, 'big')))
    wrong_token = dict(event, address='0x' + '55' * 20)
    wrong_wallet = dict(event, topics=[event['topics'][0], event['topics'][1], HexBytes('0x' + '11' * 32)])
    assert broker._incoming({'logs': [event, wrong_token, wrong_wallet]}, token, trade['wallet']) == 1.234567


@pytest.mark.parametrize('existing,expected', [(167779269, 12110212), (179889480, 1), (179889481, 0)])
def test_funding_subtracts_integer_units_without_losing_micro_usdc(live, existing, expected):
    broker, store, trade = live
    token = broker.contract('USDC')
    token.functions.balanceOf.side_effect = lambda address: SimpleNamespace(
        call=lambda: existing if address == trade['wallet'] else 184889536)
    op = store.new_operation(trade, 'fund', 179.889481)
    result = broker._prepare(op)
    if expected:
        token.functions.transfer.assert_called_once_with(trade['wallet'], expected)
        assert op['state'] == 'SIGNED'
    else:
        assert result == {'state': 'SETTLED', 'transferred': 0}
        token.functions.transfer.assert_not_called()


def test_buy_spends_usdc_budget_with_fractional_shares(live):
    broker, store, trade = live
    broker.api.get_stock_price.return_value = 400
    broker.contract('USDC').functions.balanceOf.return_value.call.return_value = 100_000_000
    op = store.new_operation(trade, 'buy', 100, expiry_days=1)
    broker._prepare(op)
    assert op['memo']['offer'] == 100_000_000
    assert op['memo']['request'] == 250_000_000_000_000_000
    assert op['estimated_quantity'] == .25


@pytest.mark.parametrize('wallet_units', [179889480, 200123456])
def test_buy_uses_entire_wallet_balance_and_persists_actual_budget(live, wallet_units):
    broker, store, trade = live
    broker.contract('USDC').functions.balanceOf.return_value.call.return_value = wallet_units
    op = store.new_operation(trade, 'buy', 179.889481, expiry_days=1)
    broker._prepare(op)
    assert op['memo']['offer'] == wallet_units
    assert op['amount'] == wallet_units / 1_000_000
    assert op['planned_amount'] == 179.889481
    assert store.operations()[0]['amount'] == op['amount']
    assert store.db.get_wallet_orders(trade['wallet'])[0]['amount_usdc'] == op['amount']


@pytest.mark.parametrize('wallet_units', [0, 1, 179889481])
def test_retirement_sweeps_exact_wallet_units(live, wallet_units):
    broker, store, trade=live
    token=broker.contract('USDC')
    token.functions.balanceOf.return_value.call.return_value=wallet_units
    op=store.new_operation(trade,'sweep',0,retirement=True,sweep_all=True)
    result=broker._prepare(op)
    if wallet_units:
        token.functions.transfer.assert_called_once_with(broker.vault,wallet_units)
        assert op['state']=='SIGNED'
    else:
        assert result==dict(state='SETTLED',transferred=0)
        token.functions.transfer.assert_not_called()


@pytest.mark.parametrize('wallet_units', [791248021466619987, 1, 123456789012345678901])
def test_sell_all_uses_exact_chain_units_and_replays_without_resizing(live, wallet_units):
    broker, store, trade=live
    token=broker.contract('TSLA')
    token.functions.balanceOf.return_value.call.return_value=wallet_units
    op=store.new_operation(trade,'sell',.79124802146662,expiry_days=1)
    broker.step(op)
    saved=store.operations()[0]
    assert saved['amount_units']==wallet_units
    assert saved['memo']['offer']==wallet_units
    token.functions.transfer.assert_called_once_with(broker.config.burn_address,wallet_units)
    raw=saved['raw_tx']
    token.functions.balanceOf.return_value.call.return_value=0
    broker.step(saved)
    assert store.operations()[0]['raw_tx']==raw
    broker.blockchain.get_nonce.assert_called_once()
    assert broker.w3.eth.send_raw_transaction.call_count==2


def test_zero_share_balance_does_not_guess_a_fill(live):
    broker, store, trade=live
    broker.contract('TSLA').functions.balanceOf.return_value.call.return_value=0
    op=store.new_operation(trade,'sell',1,expiry_days=1)
    with pytest.raises(ValueError,match='reconciliation required'):
        broker.step(op)
    assert store.operations()[0]['state']=='PREPARED'
    broker.w3.eth.send_raw_transaction.assert_not_called()


def test_live_above_trigger_retries_same_operation_after_restart(live):
    import time
    broker,store,trade=live
    guard=dict(expires_at=time.time()+60,min_reward_risk=1.5,
               signal=dict(stop_loss=98,trigger_low=100,take_profit=103))
    op=store.new_operation(trade,'buy',100,expiry_days=1,entry_guard=guard)
    result = broker.step(op)
    assert result==dict(state='PREPARED',error='ENTRY_ABOVE_TRIGGER')
    store.finish_operation(op, result)
    broker.blockchain.get_nonce.assert_not_called()
    broker.w3.eth.send_raw_transaction.assert_not_called()
    recovered = store.operations()[0]
    store.finish_operation(recovered, broker.step(recovered))
    assert recovered['entry_guard'] == guard
    assert recovered['id'] == op['id']
    broker.api.get_stock_price.return_value = 99
    recovered = store.operations()[0]
    store.finish_operation(recovered, broker.step(recovered))
    assert recovered['state'] == 'SIGNED'
    assert 'error' not in recovered
    assert len(store.operations()) == 1
    broker.blockchain.get_nonce.assert_called_once()
    assert broker.api.get_stock_price.call_count == 3


@pytest.mark.parametrize('price,expired,reason', [
    (100, True, 'ENTRY_EXPIRED'),
    (97, False, 'ENTRY_AT_OR_BELOW_STOP'),
    (99, False, 'ENTRY_REWARD_RISK_TOO_LOW'),
])
def test_retry_still_obeys_other_entry_guards(live, monkeypatch, price, expired, reason):
    broker, store, trade = live
    clock = [90]
    monkeypatch.setattr('fgv_trader.entry_safety.now_timestamp', lambda: clock[0])
    monkeypatch.setattr('fgv_trader.execution.now_timestamp', lambda: clock[0])
    guard = dict(expires_at=100, min_reward_risk=5,
                 signal=dict(stop_loss=98, trigger_low=100, take_profit=103))
    op = store.new_operation(trade, 'buy', 100, expiry_days=1, entry_guard=guard)
    store.finish_operation(op, broker.step(op))
    assert op['state'] == 'PREPARED'
    clock[0] = 100 if expired else 91
    broker.api.get_stock_price.return_value = price
    assert broker.step(store.operations()[0]) == dict(state='FAILED', error=reason)
    broker.blockchain.get_nonce.assert_not_called()
    broker.w3.eth.send_raw_transaction.assert_not_called()


def test_entry_expiring_during_preparation_never_signs(live,monkeypatch):
    broker,store,trade=live
    clock=[90]
    monkeypatch.setattr('fgv_trader.entry_safety.now_timestamp',lambda:clock[0])
    monkeypatch.setattr('fgv_trader.execution.now_timestamp',lambda:clock[0])
    broker.api.get_stock_price.return_value=99
    def estimate(tx):
        clock[0]=101
        return 100000
    broker.w3.eth.estimate_gas.side_effect=estimate
    guard=dict(expires_at=100,min_reward_risk=1.5,signal=dict(stop_loss=98,trigger_low=100,take_profit=103))
    op=store.new_operation(trade,'buy',100,expiry_days=1,entry_guard=guard)
    result=broker.step(op)
    assert result['state']=='FAILED'
    broker.blockchain.get_nonce.assert_not_called()
    broker.w3.eth.send_raw_transaction.assert_not_called()


def test_signed_entry_is_rebroadcast_even_after_deadline(live,monkeypatch):
    broker,store,trade=live
    clock=[90]
    monkeypatch.setattr('fgv_trader.entry_safety.now_timestamp',lambda:clock[0])
    monkeypatch.setattr('fgv_trader.execution.now_timestamp',lambda:clock[0])
    broker.api.get_stock_price.return_value=99
    guard=dict(expires_at=100,min_reward_risk=1.5,signal=dict(stop_loss=98,trigger_low=100,take_profit=103))
    op=store.new_operation(trade,'buy',100,expiry_days=1,entry_guard=guard)
    broker.step(op)
    saved=store.operations()[0]
    clock[0]=200
    result=broker.step(saved)
    assert result['state']=='SIGNED'
    broker.blockchain.get_nonce.assert_called_once()
    calls=broker.w3.eth.send_raw_transaction.call_args_list
    assert calls[0]==calls[1]


@pytest.mark.parametrize('invalid', [None,'sender','customer_id','status'])
def test_chain_sell_settlement_requires_authenticated_order_memo(live,invalid):
    broker,store,trade=live
    op=store.new_operation(trade,'sell',1,expiry_days=1)
    txhash=HexBytes('0x'+'12'*32)
    token=broker.contract('USDC')
    memo=dict(customer_id=op['id'],symbol='TSLA',side='SELL',status='COMPLETED')
    sender=broker.config.burn_address
    if invalid=='sender':sender='0x'+'66'*20
    if invalid in ('customer_id','status'):memo[invalid]='wrong'
    payload=bytes.fromhex('a9059cbb'+trade['wallet'][2:].zfill(64)+(99000000).to_bytes(32,'big').hex())+json.dumps(memo).encode()
    broker.w3.eth.get_transaction.return_value=dict(input=HexBytes(payload),to=token.address,**{'from':sender})
    broker.w3.eth.block_number=120
    broker.w3.eth.get_logs.return_value=[dict(transactionHash=txhash)]
    broker._confirmed_receipt=lambda tx:dict(status=1)
    broker._incoming=lambda r,t,w:99 if t==token else 0
    result=broker.chain_sell_settlement(op,{'blockNumber':100})
    if invalid:
        assert result is None
    else:
        assert result['state']=='SETTLED'
        assert result['proceeds']==99
        assert result['settlement_detection']=='authenticated_chain_memo'


@pytest.mark.parametrize('invalid',[None,'sender','recipient','token','unconfirmed','amount'])
def test_chain_buy_delivery_verifies_mint_and_counts_exact_units_once(live,invalid):
    broker,store,trade=live
    op=store.new_operation(trade,'buy',100,expiry_days=1)
    token=broker.contract('TSLA')
    raw=791248021466619987
    txhash=HexBytes('0x'+'77'*32)
    recipient=trade['wallet'] if invalid!='recipient' else '0x'+'88'*20
    sender=broker.config.mint_address if invalid!='sender' else '0x'+'99'*20
    target=token.address if invalid!='token' else '0x'+'66'*20
    data=bytes.fromhex('40c10f19'+recipient[2:].zfill(64))+(raw+(1 if invalid=='amount' else 0)).to_bytes(32,'big')
    broker.w3.eth.get_transaction.return_value=dict(input=HexBytes(data),to=target,**{'from':sender})
    broker.w3.eth.block_number=120
    event=dict(transactionHash=txhash,address=token.address,data=HexBytes(raw.to_bytes(32,'big')),
        topics=[Web3.keccak(text='Transfer(address,address,uint256)'),HexBytes('0x'+'00'*32),
                HexBytes('0x'+trade['wallet'][2:].zfill(64))])
    broker.w3.eth.get_logs.return_value=[event]
    broker._confirmed_receipt=lambda tx:None if invalid=='unconfirmed' else dict(status=1,logs=[event],blockNumber=105)
    result=broker.chain_buy_delivery(op,{'blockNumber':100})
    if invalid:
        assert result is None
    else:
        assert result['state']=='SUBMITTED'
        assert result['delivered_units']==raw
        assert result['delivery_transactions']=={txhash.hex() if txhash.hex().startswith('0x') else '0x'+txhash.hex():raw}
        op.update(result)
        assert broker.chain_buy_delivery(op,{'blockNumber':100}) is None
        assert op['delivered_units']==raw


def test_delivery_is_returned_before_slow_or_failing_api(live):
    broker,store,trade=live
    op=store.new_operation(trade,'buy',100,expiry_days=1)
    op.update(state='SUBMITTED',tx_hash='0x1234')
    broker._confirmed_receipt=lambda tx:dict(status=1,blockNumber=100,gasUsed=1,effectiveGasPrice=1)
    broker.chain_buy_delivery=MagicMock(return_value=dict(state='SUBMITTED',delivered_units=123,delivery_decimals=18))
    broker.settlement=MagicMock(side_effect=AssertionError('Do not block delivery monitoring on API'))
    result=broker.step(op)
    assert result['delivered_units']==123
    broker.settlement.assert_not_called()


def test_multiple_pending_buys_prevent_ambiguous_mint_attribution(live):
    broker,store,trade=live
    first=store.new_operation(trade,'buy',100,expiry_days=1)
    store.new_operation(trade,'buy',100,expiry_days=1)
    assert broker.chain_buy_delivery(first,{'blockNumber':100}) is None
    broker.w3.eth.get_logs.assert_not_called()
