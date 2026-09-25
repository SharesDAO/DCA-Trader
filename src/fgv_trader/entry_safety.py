"""Execution-time entry gates, independent of the original candle strategy."""
import math
import time


def now_timestamp():
    return time.time()


def rejection(op, price=None, now=None):
    guard = op.get('entry_guard')
    if not guard:
        return None  # Low-level maintenance/tests; Engine supplies all new entries.
    now = now_timestamp() if now is None else now
    if now >= guard['expires_at']:
        return 'ENTRY_EXPIRED'
    if price is None:
        return None
    if not math.isfinite(price) or price <= 0:
        return 'INVALID_EXECUTION_QUOTE'
    signal = guard['signal']
    entry_floor = (guard.get('risk_stop', signal['stop_loss'])
                   if guard.get('allow_entry_below_original_stop', False)
                   else signal['stop_loss'])
    below_entry_floor = price <= entry_floor
    allow_below_risk_stop = guard.get('allow_entry_at_or_below_risk_stop', False)
    if below_entry_floor and not allow_below_risk_stop:
        return 'ENTRY_AT_OR_BELOW_STOP'
    if 'max_entry_price' in guard:
        if price > guard['max_entry_price']:
            return 'ENTRY_ABOVE_TRIGGER'
    elif price >= signal['trigger_low']:
        return 'ENTRY_ABOVE_TRIGGER'
    risk_stop = (guard['reward_risk_stop']
                 if price > signal['stop_loss'] and 'reward_risk_stop' in guard else
                 guard.get('risk_stop', signal['stop_loss']))
    # At/below the configured risk floor there is no positive stop distance, so
    # the stop-based reward/risk ratio is undefined.  The explicit deep-entry
    # override admits that case without weakening the ratio check for any quote
    # above the floor.
    if not (price <= risk_stop and allow_below_risk_stop):
        rr = (signal['take_profit'] - price) / (price - risk_stop)
        if rr < guard['min_reward_risk']:
            return 'ENTRY_REWARD_RISK_TOO_LOW'
    return None
