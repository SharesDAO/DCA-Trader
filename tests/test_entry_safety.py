import pytest

from fgv_trader.entry_safety import rejection


def guarded():
    return {'entry_guard': {'expires_at': 100, 'min_reward_risk': 1.5,
                           'signal': {'stop_loss': 98, 'trigger_low': 100, 'take_profit': 103}}}


@pytest.mark.parametrize('price,expected', [(99,None),(98,'ENTRY_AT_OR_BELOW_STOP'),
    (97,'ENTRY_AT_OR_BELOW_STOP'),(100,'ENTRY_ABOVE_TRIGGER'),(101,'ENTRY_ABOVE_TRIGGER'),
    (float('nan'),'INVALID_EXECUTION_QUOTE')])
def test_entry_price_checks(price,expected):
    assert rejection(guarded(),price,now=90)==expected


def test_deadline_and_custom_reward_risk():
    assert rejection(guarded(),99,now=100)=='ENTRY_EXPIRED'
    op=guarded();op['entry_guard']['min_reward_risk']=5
    assert rejection(op,99,now=90)=='ENTRY_REWARD_RISK_TOO_LOW'


def test_configured_deep_pullback_uses_emergency_floor():
    op=guarded()
    op['entry_guard'].update(allow_entry_below_original_stop=True,risk_stop=97)
    assert rejection(op,97.9,now=90) is None
    assert rejection(op,97,now=90)=='ENTRY_AT_OR_BELOW_STOP'


def test_above_trigger_buffer_and_reward_risk_both_apply():
    op=guarded()
    op['entry_guard'].update(risk_stop=97,reward_risk_stop=98,
                             max_entry_price=100.1,max_entry_above_trigger_r=.05,
                             min_reward_risk=1.4)
    assert rejection(op,100.04,now=90) is None
    assert rejection(op,100.1,now=90)=='ENTRY_REWARD_RISK_TOO_LOW'
    assert rejection(op,100.11,now=90)=='ENTRY_ABOVE_TRIGGER'
