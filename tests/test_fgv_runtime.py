import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from config import Config
from database import Database
from fgv_trader.execution import PaperBroker
from fgv_trader.models import Candle, FGVSignal
from fgv_trader.runtime import Engine, PAPER_KEY, READY_SYMBOL
from fgv_trader.store import Store


class Prices:
    price = 99.0

    def get_latest_prices(self, symbols):
        return {s: self.price for s in symbols} if self.price else {}


@pytest.fixture
def system(tmp_path, monkeypatch):
    config = Config()
    config.stop_policy['exits_enabled'] = True
    config.fgv['initial_usdc_per_wallet'] = 100
    config.fgv['max_concurrent_positions'] = 2
    config.fgv['prefund_wallets'] = False
    config.fgv['confidence_sizing']['enabled'] = False
    config.trading_stocks = {'TSLA': {}}
    db = Database(str(tmp_path / 'paper.db'), PAPER_KEY)
    store = Store(db, config.blockchain)
    market = Prices()
    broker = PaperBroker(store, config, market)
    engine = Engine(config, store, broker, market)
    fixed = datetime(2026, 9, 4, 14, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(engine.clock, 'now_exchange', lambda: engine.clock.to_exchange(fixed))
    monkeypatch.setattr('fgv_trader.entry_safety.now_timestamp', lambda: fixed.timestamp())
    engine.balances = broker.snapshot()
    return engine, store, broker, market


def signal(symbol='TSLA'):
    stamp = datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)
    return FGVSignal(symbol, '2026-09-04', 101, 100, 98, 103, 2, stamp, stamp, stamp)


async def cycles(engine, count=8, now=None):
    now = now or engine.clock.to_exchange(datetime(2026, 9, 4, 14, 30, tzinfo=timezone.utc))
    for _ in range(count):
        await engine.tick(now=now, scan=False)
        if engine.jobs:
            await asyncio.gather(*(task for _, task in engine.jobs.values()))


def test_above_trigger_entry_remains_pending_then_fills_once(system):
    engine, store, broker, market = system
    engine.enter(signal(), 100, .6)
    market.price = 101
    asyncio.run(cycles(engine))
    trade = store.trades()[0]
    buy = next(o for o in store.operations() if o['kind'] == 'buy')
    assert trade['state'] == 'BUY_PENDING'
    assert buy['state'] == 'PREPARED'
    assert buy['error'] == 'ENTRY_ABOVE_TRIGGER'
    assert broker.balance(trade['wallet'], 'USDC') == 100
    assert broker.balance(trade['wallet'], 'TSLA') == 0
    original_guard = buy['entry_guard'].copy()
    assert original_guard['max_entry_above_trigger_r'] == .05
    assert original_guard['max_entry_price'] == pytest.approx(100.1)
    assert original_guard['reward_risk_stop'] == 98
    market.price = 99
    restarted = Engine(engine.config, store, broker, market)
    asyncio.run(cycles(restarted))
    assert store.trades()[0]['state'] == 'OPEN'
    buys = [o for o in store.operations() if o['kind'] == 'buy']
    assert len(buys) == 1
    assert buys[0]['id'] == buy['id']
    assert buys[0]['entry_guard'] == original_guard
    assert 'error' not in buys[0]
    assert broker.balance(trade['wallet'], 'USDC') == 0


def test_small_above_trigger_quote_fills_and_is_recorded(system):
    engine, store, _, market = system
    market.price = 100.03  # 100.080015 after paper slippage: below both effective caps.
    engine.enter(signal(), 100, .6)
    asyncio.run(cycles(engine, count=12))
    trade = store.trades()[0]
    buy = next(o for o in store.operations() if o['kind'] == 'buy')
    assert trade['state'] == 'OPEN'
    assert buy['entry_above_trigger'] is True
    assert 0 < buy['entry_buffer_r_used'] < .05


def test_above_trigger_retry_expires_without_spending(system):
    engine, store, broker, market = system
    engine.enter(signal(), 100, .6)
    market.price = 101
    asyncio.run(cycles(engine))
    trade = store.trades()[0]
    deadline = datetime.fromtimestamp(trade['entry_guard']['expires_at'], timezone.utc)
    asyncio.run(cycles(engine, now=deadline))
    assert store.trades()[0]['state'] == 'FAILED'
    buy = next(o for o in store.operations() if o['kind'] == 'buy')
    assert buy['state'] == 'FAILED'
    assert buy['error'] == 'ENTRY_EXPIRED'
    assert broker.balance(trade['wallet'], 'USDC') == 100
    assert broker.balance(trade['wallet'], 'TSLA') == 0


def test_paper_full_lifecycle_and_restart_without_duplicate_spend(system):
    engine, store, broker, market = system
    engine.enter(signal(), 100, .6)
    asyncio.run(cycles(engine))
    trade = store.trades()[0]
    assert trade['state'] == 'OPEN'
    assert broker.balance(broker.vault) == 900
    assert len([o for o in store.operations() if o['kind'] == 'buy']) == 1
    assert not [o for o in store.operations() if o['kind'] == 'sell']
    assert store.has_traded('TSLA', '2026-09-04')

    restarted = Engine(engine.config, store, broker, market)
    market.price = 104
    asyncio.run(cycles(restarted))
    trade = store.trades()[0]
    assert trade['state'] == 'CLOSED'
    assert trade['exit_reason'] == 'TAKE_PROFIT'
    assert trade['realized_pnl'] > 0
    assert store.db.get_all_positions() == []
    orders = store.db.get_wallet_orders(trade['wallet'])
    assert len(orders) == 2
    assert all(o['status'] == 'filled' for o in orders)
    assert all(o['order_type'] == 'MARKET' for o in store.operations() if o['kind'] in ('buy', 'sell'))
    # A replay after a commit must not change balances.
    before = broker.snapshot()
    buy = next(o for o in store.operations() if o['kind'] == 'buy')
    broker.step(buy)
    assert before == broker.snapshot()


def test_buffered_live_policy_lifecycle_and_after_sale_telemetry(system):
    import json
    from fgv_trader.stops import DEFAULTS
    engine, store, broker, market = system
    now = engine.clock.now_exchange()
    engine.stop_settings = dict(DEFAULTS, enabled=True)
    engine.bars['TSLA'] = [Candle('TSLA',now-timedelta(minutes=5*i),100,101,99,100,10)
                           for i in (4,3,2,1)]
    engine.enter(signal(),100,.6)
    asyncio.run(cycles(engine))
    t = store.trades()[0]
    assert t['state'] == 'OPEN'
    assert t['stop_policy']['buffered_stop'] == 97.5
    assert t['entry_guard']['risk_stop'] == 97
    market.price = 97.4
    asyncio.run(cycles(engine,count=1,now=now+timedelta(seconds=5)))
    assert store.trades()[0]['state'] == 'OPEN'
    assert store.trades()[0]['stop_confirmation']['count'] == 1
    restarted = Engine(engine.config,store,broker,market)
    asyncio.run(cycles(restarted,now=now+timedelta(seconds=10)))
    t = store.trades()[0]
    assert t['state'] == 'CLOSED'
    assert t['exit_reason'] == 'STOP_LOSS'
    assert t['stop_exit_detail']['subtype'] == 'confirmed_buffered'
    assert len([o for o in store.operations() if o['kind']=='sell']) == 1
    market.price = 104
    asyncio.run(cycles(restarted,now=now+timedelta(seconds=15)))
    with store.db.get_connection() as conn:
        rows = conn.execute('SELECT * FROM fgv_stop_observations ORDER BY quote_at').fetchall()
    assert len(rows) == 4  # One per distinct feed observation, not per repeated test tick.
    assert rows[-1]['price'] == 104
    assert json.loads(rows[-1]['payload'])['state'] == 'CLOSED'
    assert json.loads(rows[-1]['payload'])['policy']['emergency_stop'] == 97


@pytest.mark.parametrize('force', [True,False])
def test_emergency_or_session_exit_does_not_wait_for_confirmation(system,force):
    from fgv_trader.stops import DEFAULTS
    engine,store,broker,market = system
    now = engine.clock.now_exchange()
    engine.stop_settings = dict(DEFAULTS, enabled=True)
    engine.bars['TSLA'] = [Candle('TSLA',now-timedelta(minutes=5*i),100,101,99,100,10)
                           for i in (4,3,2,1)]
    engine.enter(signal(),100,.6)
    asyncio.run(cycles(engine))
    market.price = None if force else 96
    when = engine.clock.session_for(now.date()).force_exit if force else now+timedelta(seconds=5)
    engine.advance(when)
    t = store.trades()[0]
    assert t['state'] == 'SELL_PENDING'
    assert t['exit_reason'] == ('FORCE_EXIT_15M_BEFORE_CLOSE' if force else 'STOP_LOSS')
    if not force:
        assert t['stop_exit_detail']['subtype'] == 'emergency'


def test_telemetry_failure_does_not_block_order_scheduling(system,monkeypatch):
    engine,store,broker,market = system
    monkeypatch.setattr(engine,'record_stop_observations',MagicMock(side_effect=RuntimeError('disk failure')))
    engine.enter(signal(),100,.6)
    asyncio.run(cycles(engine))
    assert store.trades()[0]['state'] == 'OPEN'


def test_pre_upgrade_position_keeps_original_stop_after_restart(system):
    engine,store,broker,market = system
    engine.enter(signal(),100,.6)
    asyncio.run(cycles(engine))
    t = store.trades()[0]
    t.pop('stop_policy')
    store.save_trade(t)
    engine.config.stop_policy['enabled'] = True
    restarted = Engine(engine.config,store,broker,market)
    market.price = 97.8
    restarted.advance(engine.clock.now_exchange())
    t = store.trades()[0]
    assert t['state'] == 'SELL_PENDING'
    assert t['stop_exit_detail']['subtype'] == 'original'


def test_stop_telemetry_stops_after_cutoff_grace(system):
    engine,store,broker,market = system
    engine.enter(signal(),100,.6)
    asyncio.run(cycles(engine))
    with store.db.get_connection() as conn:
        before = conn.execute('SELECT count(*) FROM fgv_stop_observations').fetchone()[0]
    cutoff = engine.clock.session_for(engine.clock.now_exchange().date()).force_exit
    engine.record_stop_observations(cutoff+timedelta(minutes=2))
    with store.db.get_connection() as conn:
        assert conn.execute('SELECT count(*) FROM fgv_stop_observations').fetchone()[0] == before


@pytest.mark.parametrize('legacy',[True,False])
@pytest.mark.parametrize('exit_kind',['target','time'])
def test_global_disable_stop_exits_applies_to_existing_positions(system,legacy,exit_kind):
    engine,store,broker,market=system
    now=engine.clock.now_exchange()
    engine.bars['TSLA']=[Candle('TSLA',now-timedelta(minutes=5*i),100,101,99,100,10) for i in (4,3,2,1)]
    engine.enter(signal(),100,.6)
    asyncio.run(cycles(engine))
    trade=store.trades()[0]
    if legacy:
        trade.pop('stop_policy')
    trade['stop_confirmation']={'count':1}
    store.save_trade(trade)
    engine.config.stop_policy['exits_enabled']=False
    restarted=Engine(engine.config,store,broker,market)
    market.price=90
    for seconds in (5,10,15):
        restarted.advance(now+timedelta(seconds=seconds))
    trade=store.trades()[0]
    assert trade['state']=='OPEN'
    assert not trade['stop_exits_enabled']
    assert 'stop_confirmation' not in trade
    assert not [o for o in store.operations() if o['kind']=='sell']
    if exit_kind=='target':
        market.price=104
        restarted.advance(now+timedelta(seconds=20))
    else:
        market.price=None
        restarted.advance(restarted.clock.session_for(now.date()).force_exit)
    trade=store.trades()[0]
    assert trade['state']=='SELL_PENDING'
    assert trade['exit_reason']==('TAKE_PROFIT' if exit_kind=='target' else 'FORCE_EXIT_15M_BEFORE_CLOSE')


def test_disabling_stops_preserves_already_pending_exit(system):
    engine,store,broker,market=system
    engine.enter(signal(),100,.6)
    asyncio.run(cycles(engine))
    market.price=90
    engine.advance(engine.clock.now_exchange())
    sell=next(o for o in store.operations() if o['kind']=='sell')
    engine.config.stop_policy['exits_enabled']=False
    restarted=Engine(engine.config,store,broker,market)
    asyncio.run(cycles(restarted))
    assert store.trades()[0]['state']=='CLOSED'
    assert [o['id'] for o in store.operations() if o['kind']=='sell']==[sell['id']]


def test_below_stop_entry_rejects_when_at_fallback_emergency_floor(system):
    engine, store, broker, market = system
    market.price = 97
    assert engine.strategy.should_market_buy(signal(), market.price)
    engine.enter(signal(), 100, .6)
    asyncio.run(cycles(engine, count=12))
    assert store.trades()[0]['state'] == 'FAILED'
    buy = next(o for o in store.operations() if o['kind'] == 'buy')
    assert buy['error'] == 'ENTRY_AT_OR_BELOW_STOP'


def test_enabled_policy_allows_entry_below_original_stop(system):
    engine, store, broker, market = system
    now = engine.clock.now_exchange()
    engine.bars['TSLA'] = [Candle('TSLA', now-timedelta(minutes=20-5*i),
                                    99, 100, 98, 99, 10) for i in range(4)]
    market.price = 97.5
    engine.enter(signal(), 100, .6)
    asyncio.run(cycles(engine, count=12))
    trade = store.trades()[0]
    assert trade['stop_policy']['emergency_stop'] == 97
    assert trade['state'] == 'OPEN'
    assert next(o for o in store.operations() if o['kind'] == 'buy')['state'] == 'SETTLED'


def test_forced_exit_is_requested_even_when_quote_missing(system):
    engine, store, broker, market = system
    engine.enter(signal(), 100, .6)
    asyncio.run(cycles(engine))
    market.price = None
    now = engine.clock.to_exchange(datetime(2026, 9, 4, 19, 45, tzinfo=timezone.utc))
    engine.advance(now)
    trade = store.trades()[0]
    assert trade['state'] == 'SELL_PENDING'
    assert trade['exit_reason'] == 'FORCE_EXIT_15M_BEFORE_CLOSE'


def test_losing_time_exit_carries_for_three_additional_sessions(system):
    engine, store, _, market = system
    engine.enter(signal(), 100, .6)
    asyncio.run(cycles(engine))
    cutoff = engine.clock.session_for(datetime(2026, 9, 4).date()).force_exit
    market.price = 99
    engine.advance(cutoff)
    trade = store.trades()[0]
    assert trade['state'] == 'OPEN'
    assert trade['time_exit_carry']['max_additional_sessions'] == 3
    for days in (3, 4):  # Monday and Tuesday after the Friday entry.
        engine.advance(engine.clock.session_for((cutoff + timedelta(days=days)).date()).force_exit)
        assert store.trades()[0]['state'] == 'OPEN'
    engine.advance(engine.clock.session_for((cutoff + timedelta(days=5)).date()).force_exit)
    trade = store.trades()[0]
    assert trade['state'] == 'SELL_PENDING'
    assert trade['exit_reason'] == 'FORCE_EXIT_MAX_HOLD_3_SESSIONS'


def test_carried_loser_exits_at_intermediate_cutoff_after_recovery(system):
    engine, store, _, market = system
    engine.enter(signal(), 100, .6)
    asyncio.run(cycles(engine))
    cutoff = engine.clock.session_for(datetime(2026, 9, 4).date()).force_exit
    market.price = 99
    engine.advance(cutoff)
    assert store.trades()[0]['state'] == 'OPEN'
    market.price = 100
    next_cutoff = engine.clock.session_for((cutoff + timedelta(days=3)).date()).force_exit
    engine.advance(next_cutoff)
    trade = store.trades()[0]
    assert trade['state'] == 'SELL_PENDING'
    assert trade['exit_reason'] == 'CARRIED_POSITION_RECOVERY'


def test_delayed_buy_does_not_receive_losing_time_exit_extension(system):
    engine, store, _, market = system
    engine.enter(signal(), 100, .6)
    asyncio.run(cycles(engine))
    cutoff = engine.clock.session_for(datetime(2026, 9, 4).date()).force_exit
    trade = store.trades()[0]
    trade['opened_at'] = (cutoff + timedelta(minutes=1)).isoformat()
    store.save_trade(trade)
    market.price = 99
    engine.advance(cutoff + timedelta(minutes=2))
    trade = store.trades()[0]
    assert trade['state'] == 'SELL_PENDING'
    assert trade['exit_reason'] == 'FORCE_EXIT_15M_BEFORE_CLOSE'


def test_partial_sell_keeps_remaining_position_and_realized_cost(system):
    engine, store, broker, market = system
    engine.enter(signal(), 100, .6)
    asyncio.run(cycles(engine))
    trade = store.trades()[0]
    before_qty = trade['quantity']
    market.price = 104
    engine.advance(engine.clock.to_exchange(datetime(2026, 9, 4, 14, 30, tzinfo=timezone.utc)))
    sell = next(o for o in store.operations(trade['id']) if o['kind'] == 'sell')
    store.finish_operation(sell, dict(state='SETTLED', quantity=before_qty / 2, proceeds=55, remaining=before_qty / 2))
    engine.advance(engine.clock.to_exchange(datetime(2026, 9, 4, 14, 30, tzinfo=timezone.utc)))
    trade = store.trades()[0]
    assert trade['state'] == 'OPEN'
    assert trade['quantity'] == pytest.approx(before_qty / 2)
    assert trade['cost'] == pytest.approx(50)
    assert trade['realized_pnl'] == pytest.approx(5)


def test_new_reservations_reduce_available_cash(system):
    engine, store, broker, market = system
    amount = engine.risk.trade_amount(engine.available_usdc(), .02)
    assert amount == 497.5
    engine.enter(signal(), amount, .6)
    assert engine.available_usdc() == pytest.approx(900)
    second = engine.risk.trade_amount(engine.available_usdc(), .02)
    assert second == pytest.approx(447.5)


def test_missing_model_is_configuration_error(tmp_path):
    config = Config()
    config.fgv['win_probability_model_path'] = str(tmp_path / 'missing.json')
    assert any('FGV configuration' in e for e in config.validate())


def test_fixed_source_schedule_is_preserved(system):
    engine, *_ = system
    session = engine.clock.session_for(datetime(2026, 9, 4).date())
    assert session.scan_end.strftime('%H:%M') == '11:30'
    assert session.force_exit.strftime('%H:%M') == '15:45'


def test_ranked_candidates_use_original_sizing_and_pending_slot_limit(system):
    engine, store, broker, market = system
    symbols = ['AAA', 'BBB', 'CCC']
    engine.symbols = symbols
    start = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
    now = engine.clock.to_exchange(start + timedelta(hours=1, seconds=1))
    engine.data_session = '2026-09-04'
    for symbol_name in symbols:
        engine.first[symbol_name] = [Candle(symbol_name, start, 100, 101, 98, 100)]
        engine.bars[symbol_name] = [Candle(symbol_name, start + timedelta(minutes=i * 5), 100, 101, 98, 100)
                                    for i in range(12)]
    engine.strategy.build_signal = lambda symbol_name, *args: signal(symbol_name)
    engine.model.predict = MagicMock(side_effect=[.6, .8, .7])
    engine.scan(now)
    trades = store.trades()
    assert [t['symbol'] for t in trades] == ['BBB', 'CCC']
    assert [t['amount'] for t in trades] == [100, 100]
    assert all(t['state'] == 'FUNDING' for t in trades)
    assert store.has_traded('BBB', '2026-09-04')


def test_linear_confidence_sizing_and_prefund_maximum(system):
    engine, *_ = system
    engine.config.fgv['confidence_sizing']['enabled'] = True
    expected = [
        (.45, 50),
        (.50, 60),
        (.60, 80),
        (.70, 100),
        (.80, 120),
        (.90, 140),
        (.95, 150),
        (1.00, 150),
    ]
    assert [engine.confidence_allocation(p)[0] for p, _ in expected] == [amount for _, amount in expected]
    assert engine.prefund_amount() == 150


def test_confidence_sizing_is_fully_configuration_driven(system):
    engine, *_ = system
    engine.config.fgv['initial_usdc_per_wallet'] = 80
    engine.config.fgv['min_win_probability'] = .40
    engine.config.fgv['confidence_sizing'] = dict(
        enabled=True, min_multiplier=.25, max_multiplier=1.25, max_probability=.90)
    assert engine.confidence_allocation(.40)[0] == 20
    assert engine.confidence_allocation(.65)[0] == 60
    assert engine.confidence_allocation(.90)[0] == 100
    assert engine.prefund_amount() == 100


def test_confidence_sized_buy_spends_exact_allocation_and_keeps_remainder(system):
    engine, store, broker, _ = system
    engine.config.fgv['confidence_sizing']['enabled'] = True
    enable_prefunding(engine)
    engine.ensure_prefunded_wallets()
    asyncio.run(cycles(engine, count=4))
    assert engine.enter(signal(), 999, .45)
    trade = next(t for t in store.trades(active=True))
    assert trade['amount'] == 50
    assert trade['desired_amount'] == 50
    assert trade['allocation_multiplier'] == .5
    buy = next(o for o in store.operations(trade['id']) if o['kind'] == 'buy')
    assert buy['spend_exact'] is True
    asyncio.run(cycles(engine))
    trade = next(t for t in store.trades() if not t.get('maintenance'))
    assert trade['cost'] == 50
    assert broker.balance(trade['wallet'], 'USDC') == 100


def test_liquidation_cancels_unsigned_entry_and_never_buys(system):
    engine, store, broker, market = system
    engine.enter(signal(), 100, .6)
    store.set_liquidating()
    asyncio.run(cycles(engine))
    assert store.trades()[0]['state'] == 'FAILED'
    assert store.operations() == []
    assert broker.balance(broker.vault) == 1000
    store.resume_entries()
    assert not store.liquidating()


def test_maintenance_reserves_idle_wallet_until_transfer_settles(system):
    engine, store, broker, market = system
    address = broker.create_wallet('TSLA')
    wallet = store.db.get_wallet(address)
    maintenance = store.maintenance_trade(wallet)
    op = store.new_operation(maintenance, 'sweep', 1)
    assert address in store.busy_wallets()
    with pytest.raises(ValueError):
        store.resume_entries()
    store.finish_operation(op, dict(state='FAILED'))
    assert address not in store.busy_wallets()


def test_source_parameters_and_model_features(system):
    engine, *_ = system
    assert engine.strategy.risk_reward_ratio == 1.5
    assert engine.strategy.signal_c2_min_range_pct == 1.0
    assert engine.strategy.early_entry_filter_minutes == 60
    assert engine.strategy.early_entry_stop_risk_min_pct == 1.0
    assert engine.strategy.early_entry_stop_risk_max_pct == 2.0
    assert engine.config.fgv['min_win_probability'] == .45
    assert engine.config.execution['losing_time_exit_max_sessions'] == 3
    assert engine.model.feature_names == ['stop_risk_pct', 'entry_minutes', 'signal_minutes']
    assert engine.model.total.total == 1646


def test_scan_uses_snapshot_boundary_and_rejects_old_snapshot(system):
    engine, store, *_ = system
    start = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
    engine.data_session = '2026-09-04'
    engine.data_asof = start + timedelta(minutes=59, seconds=59)
    engine.first['TSLA'] = [Candle('TSLA', start, 100, 101, 98, 100)]
    engine.bars['TSLA'] = [Candle('TSLA', start + timedelta(minutes=i*5), 100, 101, 98, 100) for i in range(11)]
    engine.strategy.build_signal = MagicMock(return_value=None)
    engine.scan(engine.clock.to_exchange(start + timedelta(minutes=60, seconds=10)))
    engine.strategy.build_signal.assert_called_once()
    assert engine.scan_diagnostics['no_fgv_setup'] == 1
    engine.scan(engine.clock.to_exchange(start + timedelta(minutes=63)))
    assert engine.scan_diagnostics['status'] == 'stale_candle_snapshot'
    assert store.trades() == []


def test_refresh_aggregates_opening_range_from_one_interval(system):
    engine, _, _, market = system
    start = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
    market.get_bars = MagicMock(return_value={'TSLA': [
        Candle('TSLA', start + timedelta(minutes=i*5), 100+i, 103+i, 99-i, 102+i, 10+i)
        for i in range(3)]})
    result = engine.refresh(engine.clock.to_exchange(start + timedelta(minutes=16)))
    first = result[1]['TSLA'][0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (100, 105, 97, 104, 33)
    assert market.get_bars.call_count == 1
    assert market.get_bars.call_args.args[-1] == 5


def test_paper_buy_spends_wallet_excess_and_replay_is_idempotent(system):
    engine, store, broker, market = system
    wallet = broker.create_wallet('TSLA')
    with store.db.get_connection() as conn:
        conn.execute('INSERT OR REPLACE INTO fgv_paper_balances VALUES(?,?,?)', (wallet, 'USDC', 150.25))
    trade = dict(id='whole-wallet', symbol='TSLA', session='2026-09-04', wallet=wallet,
                 state='BUY_PENDING', amount=100)
    store.save_trade(trade)
    op = store.new_operation(trade, 'buy', 100, expiry_days=1)
    result = broker.step(op)
    store.finish_operation(op, result)
    assert result['cost'] == 150.25
    assert broker.balance(wallet) == 0
    assert store.db.get_wallet_orders(wallet)[0]['amount_usdc'] == 150.25
    before = broker.balance(wallet, 'TSLA')
    broker.step(op)
    assert broker.balance(wallet, 'TSLA') == before


def closed_trade(store, wallet, ident, pnl):
    trade = dict(id=ident, symbol='TSLA', session=ident, wallet=wallet, state='CLOSED',
                 amount=100, cost=0, quantity=0, realized_pnl=pnl)
    store.save_trade(trade)
    return trade


def test_loss_count_is_cumulative_backfilled_and_restart_safe(system):
    engine, store, broker, _ = system
    wallet = broker.create_wallet('TSLA')
    closed_trade(store, wallet, 'loss-one', -3)
    store.record_closed_losses(2)
    store.record_closed_losses(2)
    assert store.db.get_wallet(wallet)['loss_count'] == 1
    closed_trade(store, wallet, 'win', 5)
    store.record_closed_losses(2)
    assert store.db.get_wallet(wallet)['loss_count'] == 1
    closed_trade(store, wallet, 'loss-two', -1)
    store.record_closed_losses(2)
    assert store.db.get_wallet(wallet)['loss_count'] == 2
    assert store.db.get_wallet(wallet)['status'] == 'retiring'
    store.record_closed_losses(2)
    assert store.db.get_wallet(wallet)['loss_count'] == 2


def test_retirement_waits_for_open_position_then_sweeps_in_order(system):
    engine, store, broker, _ = system
    wallet = broker.create_wallet('TSLA')
    closed_trade(store, wallet, 'loss-one', -3)
    closed_trade(store, wallet, 'loss-two', -1)
    current = closed_trade(store, wallet, 'current', 0)
    current.update(state='OPEN',quantity=1,cost=100)
    store.save_trade(current)
    engine.retire_wallets()
    assert store.db.get_wallet(wallet)['status'] == 'retiring'
    assert not store.operations()
    current.update(state='CLOSED',quantity=0,cost=0)
    store.save_trade(current)
    with store.db.get_connection() as conn:
        conn.execute('INSERT OR REPLACE INTO fgv_paper_balances VALUES(?,?,?)',(wallet,'USDC',97))
        conn.execute('INSERT OR REPLACE INTO fgv_paper_balances VALUES(?,?,?)',(wallet,'NATIVE',.001))
    before = broker.balance(broker.vault)
    for kind in ('gas', 'sweep', 'collect'):
        engine.retire_wallets()
        ops = store.operations()
        assert ops[-1]['kind'] == kind
        assert store.db.get_wallet(wallet)['status'] == 'retiring'
        engine.retire_wallets()
        assert len(store.operations()) == len(ops)
        store.finish_operation(ops[-1],broker.step(ops[-1]))
    engine.retire_wallets()
    assert store.db.get_wallet(wallet)['status'] == 'abandoned'
    assert broker.balance(wallet) == 0
    assert broker.balance(wallet,'NATIVE') == 0
    assert broker.balance(broker.vault) == before + 97
    engine.retire_wallets()
    assert len(store.operations()) == 3
    engine.balances = broker.snapshot()
    engine.enter(signal(),100,.6)
    assert store.trades(active=True)[0]['wallet'] != wallet


def test_failed_retirement_cleanup_is_not_reused(system):
    engine, store, broker, _ = system
    wallet = broker.create_wallet('TSLA')
    closed_trade(store,wallet,'loss-one',-1)
    closed_trade(store,wallet,'loss-two',-1)
    engine.retire_wallets()
    op=store.operations()[0]
    store.finish_operation(op,dict(state='FAILED'))
    engine.retire_wallets()
    assert len(store.operations()) == 1
    assert store.db.get_wallet(wallet)['status'] == 'retiring'
    engine.balances={wallet:100,broker.vault:50}
    assert engine.available_usdc()==50


def test_partial_loss_and_pending_orders_do_not_trigger_cleanup(system):
    engine, store, broker, _ = system
    wallet=broker.create_wallet('TSLA')
    trade=closed_trade(store,wallet,'partial',-2)
    trade.update(state='OPEN',quantity=.5,cost=50)
    store.save_trade(trade)
    store.record_closed_losses(2)
    assert store.db.get_wallet(wallet)['loss_count']==0
    trade.update(state='CLOSED',quantity=0,cost=0)
    store.save_trade(trade)
    closed_trade(store,wallet,'second',-1)
    pending=store.new_operation(trade,'sell',.5,expiry_days=1)
    engine.retire_wallets()
    assert store.db.get_wallet(wallet)['status']=='retiring'
    assert len(store.operations())==1
    store.finish_operation(pending,dict(state='FAILED'))
    engine.retire_wallets()
    assert store.operations()[-1]['kind']=='gas'


@pytest.mark.parametrize('remaining',[65,150])
def test_reused_wallet_spends_remaining_without_topup(system,remaining):
    engine,store,broker,market=system
    wallet=broker.create_wallet('TSLA')
    with store.db.get_connection() as conn:
        conn.execute('INSERT OR REPLACE INTO fgv_paper_balances VALUES(?,?,?)',(wallet,'USDC',remaining))
    engine.balances=broker.snapshot()
    before=broker.balance(broker.vault)
    engine.enter(signal(),100,.6)
    trade=store.trades()[0]
    assert trade['wallet']==wallet
    assert trade['amount']==remaining
    assert trade['reused_wallet']
    asyncio.run(cycles(engine))
    assert broker.balance(broker.vault)==before
    assert next(o for o in store.operations() if o['kind']=='fund')['transferred']==0
    assert store.trades()[0]['cost']==remaining


def test_new_wallet_uses_fixed_funding_and_keeps_reserve(system):
    engine,store,broker,_=system
    engine.balances={broker.vault:104}
    engine.enter(signal(),999,.6)
    assert not store.trades()
    engine.balances={broker.vault:105}
    engine.enter(signal(),999,.6)
    assert store.trades()[0]['amount']==100
    assert engine.balances[broker.vault]==5


def enable_prefunding(engine):
    engine.config.fgv['prefund_wallets'] = True
    engine.prefund_enabled = True


def test_prefunding_creates_target_and_is_restart_idempotent(system):
    engine,store,broker,_=system
    enable_prefunding(engine)
    engine.ensure_prefunded_wallets()
    pending=store.db.get_wallets_by_status(engine.config.blockchain,'pending_funding')
    assert len(pending)==2
    assert all(w['assigned_stock']==READY_SYMBOL for w in pending)
    assert len([o for o in store.operations() if o.get('prefund')])==4
    assert engine.balances[broker.vault]==800
    restarted=Engine(engine.config,store,broker,engine.market)
    restarted.balances=restarted.apply_funding_reservations(broker.snapshot())
    assert restarted.balances[broker.vault]==800
    restarted.ensure_prefunded_wallets()
    assert len(store.db.get_wallets_by_status(engine.config.blockchain,'pending_funding'))==2
    assert len([o for o in store.operations() if o.get('prefund')])==4
    asyncio.run(cycles(restarted,count=4))
    active=store.db.get_active_wallets(engine.config.blockchain)
    assert len(active)==2
    assert all(w['assigned_stock']==READY_SYMBOL for w in active)
    assert all(broker.balance(w['address'])==100 for w in active)
    assert broker.balance(broker.vault)==800


def test_prefunded_entry_creates_buy_immediately_without_funding_ops(system):
    engine,store,broker,_=system
    enable_prefunding(engine)
    engine.ensure_prefunded_wallets()
    asyncio.run(cycles(engine,count=4))
    ready=[w for w in store.db.get_active_wallets(engine.config.blockchain) if w['assigned_stock']==READY_SYMBOL]
    wallet=max(ready,key=lambda w:engine.balances[w['address']])
    assert engine.enter(signal(),100,.6)
    trade=next(t for t in store.trades(active=True))
    assert trade['state']=='BUY_PENDING'
    assert trade['wallet']==wallet['address']
    assert store.db.get_wallet(wallet['address'])['assigned_stock']=='TSLA'
    ops=store.operations(trade['id'])
    assert [o['kind'] for o in ops]==['buy']
    assert ops[0]['created_at']>=trade['decision_at']
    prefund=next(t for t in store.trades() if t.get('prefund') and t['wallet']==wallet['address'])
    assert prefund.get('consumed_at')


def test_prefunding_defers_selection_when_no_ready_wallet(system):
    engine,store,_,_=system
    enable_prefunding(engine)
    assert engine.enter(signal(),100,.6) is False
    assert not [t for t in store.trades() if not t.get('maintenance')]


def test_prefunding_respects_vault_reserve_and_reuses_remaining_cash(system):
    engine,store,broker,_=system
    enable_prefunding(engine)
    engine.balances={broker.vault:204}
    engine.ensure_prefunded_wallets()
    assert len(store.db.get_wallets_by_status(engine.config.blockchain,'pending_funding'))==1
    assert engine.balances[broker.vault]==104
    asyncio.run(cycles(engine,count=4))
    wallet=next(w for w in store.db.get_active_wallets(engine.config.blockchain)
                if w['assigned_stock']==READY_SYMBOL)
    with store.db.get_connection() as conn:
        conn.execute('UPDATE fgv_paper_balances SET amount=65 WHERE address=? AND asset=?',(wallet['address'],'USDC'))
    engine.balances=broker.snapshot()
    assert engine.enter(signal(),999,.6)
    trade=next(t for t in store.trades(active=True))
    assert trade['amount']==65
    assert not [o for o in store.operations(trade['id']) if o['kind'] in ('fund','gas')]


def test_rearm_adds_only_gas_and_makes_wallet_ready(system):
    engine,store,broker,_=system
    enable_prefunding(engine)
    wallet=broker.create_wallet('OLD')
    with store.db.get_connection() as conn:
        conn.execute('INSERT OR REPLACE INTO fgv_paper_balances VALUES(?,?,?)',(wallet,'USDC',65))
    engine.balances=broker.snapshot()
    engine.ensure_prefunded_wallets()
    rearm=[o for o in store.operations() if o.get('rearm') and o['wallet']==wallet]
    assert len(rearm)==1 and rearm[0]['kind']=='gas'
    before=broker.balance(wallet)
    asyncio.run(cycles(engine,count=3))
    assert store.db.get_wallet(wallet)['assigned_stock']==READY_SYMBOL
    assert broker.balance(wallet)==before


def test_pending_wallet_creation_crash_is_resumed_and_failure_quarantined(system):
    engine,store,broker,_=system
    enable_prefunding(engine)
    address=broker.create_wallet(READY_SYMBOL,status='pending_funding')
    engine.balances=broker.snapshot()
    engine.ensure_prefunded_wallets()
    ops=[o for o in store.operations() if o['wallet']==address]
    assert sorted(o['kind'] for o in ops)==['fund','gas']
    store.finish_operation(ops[0],dict(state='FAILED',error='test'))
    store.finish_operation(ops[1],dict(state='SETTLED'))
    engine.ensure_prefunded_wallets()
    assert store.db.get_wallet(address)['status']=='prefund_failed'


def test_orphan_pending_wallet_reserves_cash_before_recovery(system):
    engine,store,broker,_=system
    enable_prefunding(engine)
    address=broker.create_wallet(READY_SYMBOL,status='pending_funding')
    engine.balances=broker.snapshot()
    engine.ensure_prefunded_wallets()
    # The recovered wallet and one newly-created target slot reserve $100 each.
    assert engine.balances[broker.vault]==800
    assert len(store.db.get_wallets_by_status(engine.config.blockchain,'pending_funding'))==2


def test_unfundable_orphan_does_not_count_as_ready_capacity(system):
    engine,store,broker,_=system
    enable_prefunding(engine)
    address=broker.create_wallet(READY_SYMBOL,status='pending_funding')
    engine.balances={broker.vault:104,address:0}
    engine.ensure_prefunded_wallets()
    assert not store.operations()
    assert store.db.get_wallet(address)['status']=='pending_funding'


def test_entry_deadline_expires_without_new_funding(system):
    engine,store,broker,_=system
    now=engine.clock.now_exchange()
    engine.enter(signal(),100,.6,now=now)
    assert store.trades()[0]['entry_guard']['expires_at']==now.timestamp()+300
    engine.advance(now+timedelta(seconds=301))
    assert store.trades()[0]['state']=='FAILED'
    assert store.operations()==[]
    assert broker.balance(broker.vault)==1000


def test_entry_deadline_is_clamped_to_session_cutoff(system):
    engine,store,_,_=system
    now=engine.clock.to_exchange(datetime(2026,9,4,15,29,50,tzinfo=timezone.utc))
    engine.enter(signal(),100,.6,now=now)
    assert store.trades()[0]['entry_guard']['expires_at']==now.timestamp()+10


def test_expired_signed_buy_remains_reserved_for_reconciliation(system):
    engine,store,_,_=system
    now=engine.clock.now_exchange()
    engine.enter(signal(),100,.6,now=now)
    trade=store.trades()[0]
    trade['state']='BUY_PENDING';store.save_trade(trade)
    op=store.new_operation(trade,'buy',100,expiry_days=1,entry_guard=trade['entry_guard'])
    op.update(state='SIGNED',tx_hash='0x1234',raw_tx='0xabcd');store.save_operation(op)
    engine.advance(now+timedelta(hours=2))
    assert store.trades()[0]['state']=='BUY_PENDING'
    assert store.operations()[0]['raw_tx']=='0xabcd'
    assert store.operations()[0]['state']=='SIGNED'


def test_partial_onchain_delivery_is_protected_and_late_delivery_not_reused(system):
    engine,store,broker,market=system
    now=engine.clock.now_exchange()
    engine.enter(signal(),100,.6,now=now)
    trade=store.trades()[0];trade['state']='BUY_PENDING';store.save_trade(trade)
    buy=store.new_operation(trade,'buy',100,expiry_days=1)
    store.finish_operation(buy,dict(state='SUBMITTED',delivered_units=5*10**17,delivery_decimals=18))
    market.price=97
    engine.advance(now)
    trade=store.trades()[0]
    assert trade['state']=='SELL_PENDING'
    assert trade['quantity']==.5
    assert trade['cost_provisional']
    sell=next(o for o in store.operations() if o['kind']=='sell')
    store.finish_operation(sell,dict(state='SETTLED',quantity=.5,proceeds=48))
    engine.advance(now)
    assert store.trades()[0]['state']=='BUY_PENDING'
    assert trade['wallet'] in store.busy_wallets()
    # A later mint must reopen protection, without counting the first mint twice.
    store.finish_operation(buy,dict(delivered_units=10**18))
    restarted=Engine(engine.config,store,broker,market)
    restarted.advance(now)
    trade=store.trades()[0]
    assert trade['state']=='SELL_PENDING'
    assert trade['quantity']==.5
    assert trade['cost']==50
    sell2=[o for o in store.operations() if o['kind']=='sell'][-1]
    assert sell2['id']!=sell['id']
    store.finish_operation(sell2,dict(state='SETTLED',quantity=.5,proceeds=48))
    restarted.advance(now)
    assert store.trades()[0]['state']=='BUY_PENDING'
    store.finish_operation(buy,dict(state='SETTLED',quantity=1,cost=100))
    restarted.advance(now)
    trade=store.trades()[0]
    assert trade['state']=='CLOSED'
    assert trade['realized_pnl']==-4
    assert not trade['cost_provisional']
