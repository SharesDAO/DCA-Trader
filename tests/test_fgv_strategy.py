from datetime import datetime, timezone

from fgv_trader.models import Candle, FGVSignal
from fgv_trader.strategy import FGVStrategy


def candle(symbol, minute, high, low, close):
    return Candle(
        symbol=symbol,
        timestamp=datetime(2026, 5, 4, 13, minute, tzinfo=timezone.utc),
        open=low,
        high=high,
        low=low,
        close=close,
    )


def test_builds_signal_from_three_candle_pattern():
    first_15m = candle("TSLA", 45, high=100, low=95, close=98)
    five_minute = [
        candle("TSLA", 45, high=99, low=96, close=98),
        candle("TSLA", 50, high=101, low=97, close=99),
        candle("TSLA", 55, high=104, low=98, close=102),
    ]

    signal = FGVStrategy().build_signal("TSLA", "2026-05-04", first_15m, five_minute)

    assert signal is not None
    assert signal.range_high == 100
    assert signal.trigger_low == 98
    assert signal.stop_loss == 96
    assert signal.take_profit == 102


def test_waits_for_price_below_third_candle_low_before_buy():
    first_15m = candle("TSLA", 45, high=100, low=95, close=98)
    signal = FGVStrategy().build_signal(
        "TSLA",
        "2026-05-04",
        first_15m,
        [
            candle("TSLA", 45, high=99, low=96, close=98),
            candle("TSLA", 50, high=101, low=97, close=99),
            candle("TSLA", 55, high=104, low=98, close=102),
        ],
    )

    assert signal is not None
    assert not FGVStrategy.should_market_buy(signal, 98.0)
    assert FGVStrategy.should_market_buy(signal, 97.99)


def test_ignores_signal_when_target_hit_before_pullback():
    first_15m = candle("TSLA", 45, high=100, low=95, close=98)
    signal = FGVStrategy().build_signal(
        "TSLA",
        "2026-05-04",
        first_15m,
        [
            candle("TSLA", 45, high=99, low=96, close=98),
            candle("TSLA", 50, high=101, low=97, close=99),
            candle("TSLA", 55, high=104, low=98, close=102),
            Candle(
                "TSLA",
                datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc),
                open=101,
                high=102,
                low=100,
                close=101,
            ),
            Candle(
                "TSLA",
                datetime(2026, 5, 4, 14, 5, tzinfo=timezone.utc),
                open=99,
                high=101,
                low=97.9,
                close=99,
            ),
        ],
    )

    assert signal is None


def test_requires_configured_second_signal_bar_range():
    first_15m = candle("TSLA", 45, high=100, low=95, close=98)
    signal = FGVStrategy(signal_c2_min_range_pct=2.0).build_signal(
        "TSLA",
        "2026-05-04",
        first_15m,
        [
            candle("TSLA", 45, high=99, low=96, close=98),
            candle("TSLA", 50, high=101, low=100, close=99),
            candle("TSLA", 55, high=104, low=98, close=102),
        ],
    )

    assert signal is None


def test_accepts_second_signal_bar_range_at_configured_threshold():
    first_15m = candle("TSLA", 45, high=100, low=95, close=98)
    signal = FGVStrategy(signal_c2_min_range_pct=2.0).build_signal(
        "TSLA",
        "2026-05-04",
        first_15m,
        [
            candle("TSLA", 45, high=99, low=96, close=98),
            candle("TSLA", 50, high=102, low=99, close=99),
            candle("TSLA", 55, high=104, low=98, close=102),
        ],
    )

    assert signal is not None


def test_requires_third_signal_bar_to_close_above_open():
    first_15m = candle("TSLA", 45, high=100, low=95, close=98)
    signal = FGVStrategy().build_signal(
        "TSLA",
        "2026-05-04",
        first_15m,
        [
            candle("TSLA", 45, high=99, low=96, close=98),
            candle("TSLA", 50, high=101, low=97, close=99),
            Candle(
                "TSLA",
                datetime(2026, 5, 4, 13, 55, tzinfo=timezone.utc),
                open=103,
                high=104,
                low=98,
                close=102,
            ),
        ],
    )

    assert signal is None


def test_requires_third_signal_bar_close_in_configured_upper_range():
    first_15m = candle("TSLA", 45, high=100, low=95, close=98)
    bars = [
        candle("TSLA", 45, high=99, low=96, close=98),
        candle("TSLA", 50, high=101, low=97, close=99),
        Candle(
            "TSLA",
            datetime(2026, 5, 4, 13, 55, tzinfo=timezone.utc),
            open=100,
            high=104,
            low=98,
            close=102,
        ),
    ]

    assert FGVStrategy(signal_c3_min_close_position=0.75).build_signal(
        "TSLA", "2026-05-04", first_15m, bars
    ) is None

    bars[-1] = Candle(
        "TSLA",
        datetime(2026, 5, 4, 13, 55, tzinfo=timezone.utc),
        open=100,
        high=104,
        low=98,
        close=102.5,
    )
    assert FGVStrategy(signal_c3_min_close_position=0.75).build_signal(
        "TSLA", "2026-05-04", first_15m, bars
    ) is not None


def test_requires_second_signal_bar_relative_volume_against_prior_median():
    first_15m = candle("TSLA", 45, high=100, low=95, close=98)
    bars = [
        Candle("TSLA", datetime(2026, 5, 4, 13, 40, tzinfo=timezone.utc), 96, 98, 95, 97, 100),
        Candle("TSLA", datetime(2026, 5, 4, 13, 45, tzinfo=timezone.utc), 97, 99, 96, 98, 200),
        Candle("TSLA", datetime(2026, 5, 4, 13, 50, tzinfo=timezone.utc), 98, 101, 97, 99, 225),
        Candle("TSLA", datetime(2026, 5, 4, 13, 55, tzinfo=timezone.utc), 99, 104, 98, 103, 300),
    ]

    rejected = FGVStrategy(signal_c2_min_relative_volume=1.6).build_signal(
        "TSLA", "2026-05-04", first_15m, bars
    )
    accepted = FGVStrategy(signal_c2_min_relative_volume=1.5).build_signal(
        "TSLA", "2026-05-04", first_15m, bars
    )

    assert rejected is None
    assert accepted is not None
    assert accepted.c2_relative_volume == 1.5
    assert accepted.c2_range_pct > 4
    assert accepted.c3_close_position > 0.8


def test_skips_only_configured_early_entry_stop_risk_band():
    strategy = FGVStrategy(
        early_entry_filter_minutes=60,
        early_entry_stop_risk_min_pct=1.0,
        early_entry_stop_risk_max_pct=2.0,
    )
    signal = FGVSignal(
        symbol="TSLA",
        session_date="2026-05-04",
        range_high=101,
        trigger_low=100,
        stop_loss=98,
        take_profit=103,
        risk=2,
        c1_time=datetime(2026, 5, 4, 13, 45, tzinfo=timezone.utc),
        c2_time=datetime(2026, 5, 4, 13, 50, tzinfo=timezone.utc),
        c3_time=datetime(2026, 5, 4, 13, 55, tzinfo=timezone.utc),
    )

    assert strategy.should_skip_entry(signal, 59)
    assert not strategy.should_skip_entry(signal, 60)
    assert not strategy.should_skip_entry(signal.__class__(**{**signal.__dict__, "risk": 1}), 59)
