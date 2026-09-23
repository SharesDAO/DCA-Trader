import json
from datetime import date, timedelta
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from backtest_current import Assumptions, Replay, Tape, bounds, completed_prefix, sessions
from config import Config
from fgv_trader.models import Candle, FGVSignal


@pytest.fixture
def replay(tmp_path):
    config=Config()
    inputs=dict(fgv=dict(config.fgv,max_concurrent_positions=10),stop_policy=config.stop_policy,
                execution=config.execution,market_data=config.market_data,max_loss_traders=2,
                _directory=str(tmp_path),model=json.loads((config.project_root/config.fgv['win_probability_model_path']).read_text()))
    return Replay(inputs,Assumptions(buy_delay_seconds=15,sell_delay_seconds=10,slippage_bps=0))


def setup(replay):
    now=bounds(date(2026,7,10))+timedelta(minutes=60)
    times=[now-timedelta(minutes=20-5*i) for i in range(4)]
    bars=[Candle('TSLA',t,100,101,99,100,10) for t in times]
    sig=FGVSignal('TSLA',now.date().isoformat(),101,100,98,103,2,*times[:3])
    assert replay.select('TSLA',sig,.5,bars,now)
    return now


def quotes(now,price):
    return {'TSLA':(price,now.timestamp())}


def test_tape_never_uses_unfinished_second_or_future_quote():
    tape=Tape([dict(start='2026-07-10 13:30:00',close='100'),dict(start='2026-07-10 13:30:05',close='200')])
    start=bounds(date(2026,7,10)).timestamp()
    assert tape.quote(start) is None
    assert tape.quote(start+1)==(100,start+1)
    assert tape.quote(start+5)==(100,start+1)
    assert tape.quote(start+6)==(200,start+6)
    assert tape.quote(start+37) is None


def test_coarse_prefix_excludes_current_candle():
    opening=bounds(date(2026,7,10))
    bars=[Candle('TSLA',opening+timedelta(minutes=5*i),100,101,99,100,10) for i in range(7)]
    assert len(completed_prefix(bars,opening,opening+timedelta(minutes=30)))==5
    assert len(completed_prefix(bars,opening,opening+timedelta(minutes=30,seconds=5)))==6
    assert completed_prefix(bars[1:],opening,opening+timedelta(minutes=30,seconds=5))==[]


def test_wallet_budget_reuse_and_initial_capital(replay):
    now=setup(replay)
    assert replay.vault==9950
    assert replay.wallets[0]['cash']==50
    replay.progress(now+timedelta(seconds=10),quotes(now,99))
    assert replay.active['TSLA']['state']=='BUY_PENDING'
    now+=timedelta(seconds=15)
    replay.progress(now,quotes(now,99))
    assert replay.active['TSLA']['state']=='OPEN'
    assert replay.active['TSLA']['cost']==50
    replay.progress(now+timedelta(seconds=5),quotes(now,104))
    assert replay.active['TSLA']['state']=='SELL_PENDING'
    replay.progress(now+timedelta(seconds=15),quotes(now,104))
    assert not replay.active
    assert replay.summary()['net_profit']>0
    wallet=replay.select_wallet()
    assert wallet['id']==1
    assert wallet['cash']>50
    assert replay.vault==9950


def test_entry_expiry_releases_wallet_without_spending(replay):
    now=setup(replay)
    replay.progress(now+timedelta(seconds=15),quotes(now,101))
    assert replay.rejections['ABOVE_TRIGGER_RETRY_TICKS']==1
    replay.progress(now+timedelta(seconds=300),quotes(now,99))
    assert not replay.active
    assert replay.wallets[0]['cash']==50
    assert replay.summary()['ending_equity']==10000
    assert replay.rejections['ENTRY_EXPIRED']==1


def test_entry_deadline_uses_configured_scan_end(tmp_path, replay):
    replay = Replay(replay.inputs, replay.a, scan_end_minutes=180)
    now = bounds(date(2026,7,10)) + timedelta(minutes=179)
    times = [now-timedelta(minutes=20-5*i) for i in range(4)]
    bars = [Candle('TSLA',t,100,101,99,100,10) for t in times]
    sig = FGVSignal('TSLA',now.date().isoformat(),101,100,98,103,2,*times[:3])
    assert replay.select('TSLA',sig,.5,bars,now)
    expected = bounds(now.date()) + timedelta(minutes=180)
    assert replay.active['TSLA']['entry_guard']['expires_at'] == expected.timestamp()


def test_confirmation_and_emergency_reuse_production_stop_rules(replay):
    now=setup(replay)+timedelta(seconds=15)
    replay.progress(now,quotes(now,99))
    for seconds in [5,10]:
        observed=now+timedelta(seconds=seconds)
        replay.progress(observed,quotes(observed,97.4))
    assert replay.active['TSLA']['stop_subtype']=='confirmed_buffered'
    observed=now+timedelta(seconds=20)
    replay.progress(observed,quotes(observed,97.2))
    assert replay.summary()['ending_equity']<10000


def test_retirement_returns_remaining_cash_after_second_loss(replay):
    for _ in range(2):
        now=setup(replay)+timedelta(seconds=15)
        replay.progress(now,quotes(now,99))
        now+=timedelta(seconds=5)
        replay.progress(now,quotes(now,96))
        replay.progress(now+timedelta(seconds=10),quotes(now,96))
    assert len(replay.wallets)==1
    assert replay.wallets[0]['state']=='abandoned'
    assert replay.wallets[0]['cash']==0
    assert replay.summary()['wallets_retired']==1


def test_fee_accounting_and_holiday(replay):
    replay.a.fee_per_side=.05
    now=setup(replay)+timedelta(seconds=15)
    replay.progress(now,quotes(now,99))
    replay.progress(now+timedelta(seconds=5),quotes(now,104))
    replay.progress(now+timedelta(seconds=15),quotes(now,104))
    assert replay.trades[0]['modeled_fees']==.1
    assert replay.summary()['net_profit']==pytest.approx(replay.trades[0]['pnl_after_slippage']-.1)
    assert date(2026,9,7) not in list(sessions())


@pytest.mark.parametrize('exit_kind',['target','time'])
def test_no_stop_exits_ignores_emergency_but_preserves_other_exits(replay,exit_kind):
    replay.a.stop_exits_enabled=False
    now=setup(replay)+timedelta(seconds=15)
    replay.progress(now,quotes(now,99))
    for seconds in (5,10,15):
        observed=now+timedelta(seconds=seconds)
        replay.progress(observed,quotes(observed,90))
        assert replay.active['TSLA']['state']=='OPEN'
    observed=now+timedelta(seconds=20) if exit_kind=='target' else bounds(now.date())+timedelta(minutes=375)
    price=104 if exit_kind=='target' else 90
    replay.progress(observed,quotes(observed,price))
    assert replay.active['TSLA']['exit_reason']==('TAKE_PROFIT' if exit_kind=='target' else 'FORCE_EXIT')
    replay.progress(observed+timedelta(seconds=10),quotes(observed,price))
    assert not replay.active
    assert replay.trades[0]['stop_subtype'] is None
    replay.summary()


def test_no_stop_exits_does_not_change_entry_filters(replay):
    replay.a.stop_exits_enabled=False
    now=setup(replay)+timedelta(seconds=15)
    replay.progress(now,quotes(now,97))
    assert replay.rejections['ENTRY_AT_OR_BELOW_STOP']==1
    assert not replay.active
