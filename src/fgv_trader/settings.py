"""Configuration checks for the port; strategy defaults live in config.yaml."""
import json
import math
import os
from importlib.util import find_spec
from datetime import time
from zoneinfo import ZoneInfo

from fgv_trader.strategy import FGVStrategy
from fgv_trader.stops import settings as stop_settings


STRATEGY_KEYS = (
    'risk_reward_ratio', 'signal_c2_min_range_pct', 'signal_c3_min_close_position',
    'signal_c2_min_relative_volume', 'early_entry_filter_minutes',
    'early_entry_stop_risk_min_pct', 'early_entry_stop_risk_max_pct',
)


def validate_settings(config):
    errors = []
    try:
        fgv = config.fgv
        stops = stop_settings(config)
        for key in ('enabled', 'exits_enabled', 'record_observations'):
            if not isinstance(stops[key], bool):
                raise ValueError(f'stop_policy.{key} must be boolean')
        for key, low, high in (('confirmation_observations', 2, 5), ('atr_period', 3, 100), ('atr_min_periods', 3, 100)):
            value = stops[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f'stop_policy.{key} must be an integer in [{low}, {high}]')
        if stops['atr_min_periods'] > stops['atr_period']:
            raise ValueError('atr_min_periods cannot exceed atr_period')
        for key in ('confirmation_seconds', 'confirmation_max_gap_seconds', 'atr_multiplier', 'max_buffer_r', 'emergency_extra_r'):
            value = stops[key]
            if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f'stop_policy.{key} must be finite and positive')
        if not 0 < stops['confirmation_seconds'] <= stops['confirmation_max_gap_seconds'] <= 60:
            raise ValueError('stop confirmation timing must be ordered and <= 60 seconds')
        if stops['max_buffer_r'] + stops['emergency_extra_r'] > 0.5:
            raise ValueError('stop buffer plus emergency extension cannot exceed 0.5 R')
        initial = float(fgv.get('initial_usdc_per_wallet', 100))
        if not isinstance(fgv.get('prefund_wallets', False), bool):
            raise ValueError('prefund_wallets must be boolean')
        if not math.isfinite(initial) or initial < float(fgv['min_order_usdc']):
            raise ValueError('initial_usdc_per_wallet must be finite and >= min_order_usdc')
        sizing = fgv.get('confidence_sizing', {})
        if not isinstance(sizing.get('enabled', False), bool):
            raise ValueError('confidence_sizing.enabled must be boolean')
        if sizing.get('enabled'):
            minimum = float(sizing['min_multiplier'])
            maximum = float(sizing['max_multiplier'])
            maximum_probability = float(sizing['max_probability'])
            if (not math.isfinite(minimum) or not math.isfinite(maximum) or
                    not 0 < minimum <= maximum):
                raise ValueError('confidence sizing multipliers must satisfy 0 < min <= max')
            if initial * minimum < float(fgv['min_order_usdc']):
                raise ValueError('minimum confidence allocation must meet min_order_usdc')
            if (not math.isfinite(maximum_probability) or
                    not float(fgv['min_win_probability']) < maximum_probability <= 1):
                raise ValueError('confidence sizing max_probability must be above the entry threshold and <= 1')
        FGVStrategy(**{key: fgv[key] for key in STRATEGY_KEYS})
        for key in ('allocation_pct_per_trade', 'risk_pct_per_trade'):
            if not 0 < float(fgv[key]) <= 1:
                raise ValueError(f'{key} must be in (0, 1]')
        if not 0 <= float(fgv['min_win_probability']) <= 1:
            raise ValueError('min_win_probability must be in [0, 1]')
        if int(fgv['max_concurrent_positions']) < 1 or float(fgv['min_order_usdc']) < 5:
            raise ValueError('positive position limit and minimum order >= $5 required')
        if float(fgv['reserve_usdc']) < 0:
            raise ValueError('reserve_usdc cannot be negative')
        if fgv.get('one_trade_per_symbol_per_day') is not True:
            raise ValueError('FGV parity requires one_trade_per_symbol_per_day: true')
        for value in fgv.values():
            if isinstance(value, (float, int)) and not math.isfinite(value):
                raise ValueError('FGV parameters must be finite')
        for key in ('exchange_timezone', 'local_timezone'):
            ZoneInfo(config.session_time[key])
        times = [time.fromisoformat(config.session_time[k]) for k in (
            'market_open', 'first_15m_complete', 'scan_end', 'force_exit', 'market_close')]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError('session times must be strictly ordered')
        if config.market_data.get('provider') != 'backpack':
            raise ValueError('market_data.provider must be backpack')
        if config.market_data.get('alpaca_fallback'):
            if not os.getenv('ALPACA_API_KEY') or not os.getenv('ALPACA_API_SECRET'):
                raise ValueError('Alpaca fallback needs ALPACA_API_KEY and ALPACA_API_SECRET')
            if find_spec('alpaca') is None:
                raise ValueError('Alpaca fallback needs the optional alpaca-py package')
        for key in ('bar_poll_interval_seconds', 'price_poll_interval_seconds', 'max_price_age_seconds'):
            if float(config.market_data[key]) <= 0:
                raise ValueError(f'{key} must be positive')
        workers = config.market_data.get('candle_workers', 8)
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 32:
            raise ValueError('candle_workers must be an integer from 1 to 32')
        age = float(config.market_data.get('max_candle_snapshot_age_seconds', 120))
        if not math.isfinite(age) or not 0 < age < 300:
            raise ValueError('max_candle_snapshot_age_seconds must be positive and less than 300')
        if float(config.execution['reconcile_interval_seconds']) <= 0:
            raise ValueError('reconcile interval must be positive')
        if not isinstance(config.execution.get('allow_entry_below_original_stop', False), bool):
            raise ValueError('allow_entry_below_original_stop must be boolean')
        entry_buffer = config.execution.get('max_entry_above_trigger_r', 0)
        if (isinstance(entry_buffer, bool) or not math.isfinite(float(entry_buffer))
                or not 0 <= float(entry_buffer) <= 0.25):
            raise ValueError('max_entry_above_trigger_r must be finite and in [0, 0.25]')
        for key, default in (('entry_max_age_seconds', 60), ('min_entry_reward_risk', 1.5),
                             ('max_execution_quote_age_seconds', 5), ('stuck_order_alert_seconds', 60)):
            value = float(config.execution.get(key, default))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{key} must be finite and positive')
        carry_sessions = config.execution.get('losing_time_exit_max_sessions', 0)
        if (isinstance(carry_sessions, bool) or not isinstance(carry_sessions, int)
                or not 0 <= carry_sessions <= 10):
            raise ValueError('losing_time_exit_max_sessions must be an integer in [0, 10]')
        if isinstance(config.max_loss_traders, bool) or not isinstance(config.max_loss_traders, int) or config.max_loss_traders < 1:
            raise ValueError('max_loss_traders must be a positive integer')
        if int(config.execution['confirmations']) < 1:
            raise ValueError('confirmations must be >= 1')
        if config.order_expiry_days <= 0:
            raise ValueError('order_expiry_days must be positive')
        path = config.project_root / fgv['win_probability_model_path']
        model = json.loads(path.read_text())
        if model.get('model_version') != 2 or model.get('feature_names') != fgv['win_probability_features']:
            raise ValueError('probability model version/features do not match configuration')
        if config.paper_database_path.resolve() == config.database_path.resolve():
            raise ValueError('paper and live databases must be separate')
        if float(config.paper.get('initial_usdc', 1000)) <= 0:
            raise ValueError('paper initial_usdc must be positive')
        if not 0 <= float(config.paper.get('slippage_bps', 5)) < 10000:
            raise ValueError('paper slippage_bps must be in [0, 10000)')
    except (KeyError, ValueError, TypeError, OSError) as exc:
        errors.append(f'FGV configuration: {exc}')
    return errors
