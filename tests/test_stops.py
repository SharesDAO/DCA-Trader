from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from config import Config
from fgv_trader.entry_safety import rejection
from fgv_trader.models import Candle
from fgv_trader.settings import validate_settings
from fgv_trader.stops import DEFAULTS, build_policy, stop_decision


NOW = datetime(2026, 9, 10, 14, 30, tzinfo=timezone.utc)


def history(count=15, spread=2):
    return [Candle('TSLA', NOW - timedelta(minutes=5 * (count-i)),
                   100, 100 + spread/2, 100-spread/2, 100, 10) for i in range(count)]


def policy(bars=None):
    return build_policy(SimpleNamespace(stop_loss=98, trigger_low=100),
                        history() if bars is None else bars, NOW, dict(DEFAULTS, enabled=True))


def trade():
    return dict(signal=dict(stop_loss=98), stop_policy=policy())


def observe(t, price, seconds=0, quote_seconds=None):
    now = NOW + timedelta(seconds=seconds)
    quote_at = (NOW + timedelta(seconds=seconds if quote_seconds is None else quote_seconds)).timestamp()
    return stop_decision(t, price, quote_at, now)


def test_atr_buffer_cap_and_emergency():
    p = policy(history(spread=8))
    assert p['atr'] == 8
    assert p['atr_samples'] == 14
    assert p['buffer'] == .5
    assert p['buffered_stop'] == 97.5
    assert p['emergency_stop'] == 97
    p = policy(history(4, spread=1))
    assert p['atr_samples'] == 3
    assert p['buffer'] == .25
    assert p['emergency_stop'] == 97.25


def test_true_range_includes_previous_close_gap():
    bars = history(4, spread=2)
    bars[-1] = Candle('TSLA', bars[-1].timestamp, 110, 111, 109, 110, 10)
    assert policy(bars)['atr'] == 5  # (2 + 2 + 11) / 3


@pytest.mark.parametrize('kind', ['missing', 'short', 'gapped', 'stale', 'incomplete', 'invalid'])
def test_bad_atr_falls_back_to_original_immediate_stop(kind):
    bars = history(4)
    if kind == 'missing': bars = []
    if kind == 'short': bars = bars[:3]
    if kind == 'gapped': bars = history(6)[::2] + [history(6)[-1]]
    if kind == 'stale':
        bars = [Candle(b.symbol, b.timestamp-timedelta(hours=1), b.open,b.high,b.low,b.close,b.volume) for b in bars]
    if kind == 'incomplete':
        bars = [Candle(b.symbol,b.timestamp+timedelta(minutes=1),b.open,b.high,b.low,b.close,b.volume) for b in bars]
    if kind == 'invalid':
        bars[-1] = Candle('TSLA', bars[-1].timestamp,100,float('nan'),99,100,10)
    p = policy(bars)
    assert not p['enabled']
    assert p['emergency_stop'] == 98
    assert observe(dict(signal=dict(stop_loss=98), stop_policy=p), 97.9) == 'original'


def test_two_distinct_observations_survive_serialization():
    import json
    t = trade()
    assert observe(t,97.4) is None
    t = json.loads(json.dumps(t))
    assert observe(t,97.4,5) == 'confirmed_buffered'


def test_identical_cached_quote_and_fast_observations_do_not_confirm():
    t = trade()
    assert observe(t,97.4) is None
    assert observe(t,97.4,5,0) is None
    assert t['stop_confirmation']['count'] == 1
    assert observe(t,97.4,5,5) == 'confirmed_buffered'
    t = trade()
    assert observe(t,97.4) is None
    assert observe(t,97.4,1) is None
    assert observe(t,97.4,4) is None
    assert observe(t,97.4,5) == 'confirmed_buffered'


@pytest.mark.parametrize('reset', ['recovery', 'missing', 'gap'])
def test_confirmation_resets(reset):
    t = trade()
    assert observe(t,97.4) is None
    if reset == 'recovery': assert observe(t,97.5,5) is None
    if reset == 'missing': assert stop_decision(t,None,None,NOW+timedelta(seconds=5)) is None
    seconds = 20 if reset == 'gap' else 10
    assert observe(t,97.4,seconds) is None
    assert t['stop_confirmation']['count'] == 1
    assert observe(t,97.4,seconds+5) == 'confirmed_buffered'


def test_emergency_immediate_and_buffer_ignores_small_dip():
    assert observe(trade(),97.8) is None
    assert observe(trade(),97) == 'emergency'
    assert observe(dict(signal=dict(stop_loss=98)),97.8) == 'original'


def test_entry_reward_risk_uses_emergency_not_original_stop():
    op = dict(entry_guard=dict(expires_at=NOW.timestamp()+60, min_reward_risk=1.5,
                              signal=dict(stop_loss=98,trigger_low=100,take_profit=103)))
    assert rejection(op,99.8,now=NOW.timestamp()) is None
    op['entry_guard']['risk_stop'] = 97
    assert rejection(op,99.8,now=NOW.timestamp()) == 'ENTRY_REWARD_RISK_TOO_LOW'
    assert rejection(op,99,now=NOW.timestamp()) is None
    assert rejection(op,97.9,now=NOW.timestamp()) == 'ENTRY_AT_OR_BELOW_STOP'


def test_invalid_entry_floor_config_rejected():
    config = Config()
    config.execution['allow_entry_below_original_stop'] = 'yes'
    assert validate_settings(config)


@pytest.mark.parametrize('value', [True, -0.01, 0.26, float('nan')])
def test_invalid_above_trigger_buffer_rejected(value):
    config = Config()
    config.execution['max_entry_above_trigger_r'] = value
    assert validate_settings(config)


@pytest.mark.parametrize('minimum,maximum', [
    (0, 1.5),
    (1, .5),
    (.5, float('nan')),
])
def test_invalid_confidence_sizing_rejected(minimum, maximum):
    config = Config()
    config.fgv['confidence_sizing'].update(min_multiplier=minimum, max_multiplier=maximum)
    assert validate_settings(config)


def test_confidence_sizing_probability_range_rejected():
    config = Config()
    config.fgv['confidence_sizing']['max_probability'] = config.fgv['min_win_probability']
    assert validate_settings(config)


@pytest.mark.parametrize('changes', [dict(confirmation_observations=1),dict(confirmation_observations=True),
    dict(confirmation_seconds=0),dict(confirmation_max_gap_seconds=2),dict(atr_period=2),
    dict(atr_min_periods=15),dict(atr_multiplier=float('nan')),dict(max_buffer_r=.3),
    dict(emergency_extra_r=-1),dict(enabled='yes'),dict(exits_enabled='false'),dict(record_observations='yes'),
    dict(prefund_wallets='yes')])
def test_invalid_stop_config_rejected(changes):
    config = Config()
    if 'prefund_wallets' in changes:
        config.fgv.update(changes)
    else:
        config.stop_policy = dict(DEFAULTS, **changes)
    assert validate_settings(config)


def test_current_config_validates():
    assert validate_settings(Config()) == []
