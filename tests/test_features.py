from datetime import datetime, timedelta, timezone

from fgv_trader.features import TechnicalFeatureCalculator
from fgv_trader.models import Candle, FGVSignal


def build_bar(index: int, close: float, volume: float = 100) -> Candle:
    return Candle(
        symbol="TSLA",
        timestamp=datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=5 * index),
        open=close - 0.2,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=volume,
    )


def test_features_use_only_bars_through_signal_time():
    bars = [build_bar(index, 100 + index) for index in range(10)]
    signal = FGVSignal(
        symbol="TSLA",
        session_date="2026-05-04",
        range_high=104,
        trigger_low=104,
        stop_loss=102,
        take_profit=107,
        risk=2,
        c1_time=bars[3].timestamp,
        c2_time=bars[4].timestamp,
        c3_time=bars[5].timestamp,
    )
    first_15m = Candle("TSLA", bars[0].timestamp, 100, 103, 99, 102, 300)
    calculator = TechnicalFeatureCalculator()

    original = calculator.calculate(signal, first_15m, bars, bars, 45)
    changed_future = bars[:6] + [build_bar(index, 1_000) for index in range(6, 10)]
    changed = calculator.calculate(signal, first_15m, changed_future, changed_future, 45)

    assert original == changed
    assert original.values["stock_above_vwap"] == 1
    assert original.values["stock_ema9_over_ema20_pct"] > 0
