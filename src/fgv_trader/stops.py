"""Frozen, bounded volatility stops and restart-safe observation confirmation."""
import math
from datetime import timedelta


DEFAULTS = dict(enabled=False, exits_enabled=True, confirmation_observations=2, confirmation_seconds=5,
                confirmation_max_gap_seconds=15, atr_period=14, atr_min_periods=3,
                atr_multiplier=0.25, max_buffer_r=0.25, emergency_extra_r=0.25,
                record_observations=True)


def settings(config):
    return dict(DEFAULTS, **getattr(config, 'stop_policy', {}))


def build_policy(signal, bars, now, options):
    original = signal.stop_loss
    policy = dict(version=1, enabled=False, original_stop=original,
                  buffered_stop=original, emergency_stop=original,
                  frozen_at=now.isoformat(), fallback='disabled')
    if not options['enabled']:
        return policy
    risk = signal.trigger_low - original
    history = sorted((b for b in bars if b.timestamp.date() == now.date()
                      and b.timestamp + timedelta(minutes=5) <= now), key=lambda b: b.timestamp)
    history = history[-(options['atr_period'] + 1):]
    policy['fallback'] = 'missing_or_stale_atr'
    if (len(history) < options['atr_min_periods'] + 1 or risk <= 0
            or (now - history[-1].timestamp - timedelta(minutes=5)).total_seconds() > 420):
        return policy
    ranges = []
    for previous, bar in zip(history, history[1:]):
        if bar.timestamp - previous.timestamp != timedelta(minutes=5):
            return policy
        values = (previous.close, bar.high, bar.low, bar.close, bar.open)
        if not all(math.isfinite(v) and v > 0 for v in values) or not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            return policy
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close)))
    atr = sum(ranges) / len(ranges)
    buffer = min(atr * options['atr_multiplier'], risk * options['max_buffer_r'])
    buffered = original - buffer
    emergency = buffered - risk * options['emergency_extra_r']
    if emergency <= 0:
        policy['fallback'] = 'invalid_emergency_stop'
        return policy
    policy.update(enabled=True, fallback=None, atr=atr, atr_samples=len(ranges),
                  atr_last_bar=history[-1].timestamp.isoformat(), original_risk=risk,
                  buffer=buffer, buffered_stop=buffered, emergency_stop=emergency,
                  confirmation_observations=options['confirmation_observations'],
                  confirmation_seconds=options['confirmation_seconds'],
                  confirmation_max_gap_seconds=options['confirmation_max_gap_seconds'])
    return policy


def stop_decision(trade, price, quote_at, now):
    """Return a stop subtype, preserving STOP_LOSS as the accounting exit reason."""
    policy = trade.get('stop_policy', {})
    if not policy.get('enabled'):
        return 'original' if price is not None and price < trade['signal']['stop_loss'] else None
    state = trade.setdefault('stop_confirmation', {})
    stamp = now.timestamp()
    if price is None or quote_at is None:
        state.update(count=0, first_at=None)
        return None
    if not math.isfinite(price) or price <= 0:
        state.update(count=0, first_at=None)
        return None
    if price <= policy['emergency_stop']:
        return 'emergency'
    if quote_at <= state.get('last_quote_at', float('-inf')):
        return None  # Polling the same cached quote is not another observation.
    previous_at = state.get('last_at', stamp)
    state.update(last_quote_at=quote_at, last_at=stamp)
    if price >= policy['buffered_stop']:
        state.update(count=0, first_at=None)
        return None
    if stamp - previous_at > policy['confirmation_max_gap_seconds']:
        state.update(count=0, first_at=None)
    if not state.get('count'):
        state.update(count=1, first_at=stamp, counted_at=stamp)
    elif stamp - state['counted_at'] >= policy['confirmation_seconds']:
        state.update(count=state['count'] + 1, counted_at=stamp)
    if state['count'] >= policy['confirmation_observations']:
        return 'confirmed_buffered'
    return None
