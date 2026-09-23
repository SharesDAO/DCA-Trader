from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median

from fgv_trader.models import Candle, FGVSignal


@dataclass(frozen=True)
class SignalFeatures:
    values: dict[str, float]


class TechnicalFeatureCalculator:
    def calculate(
        self,
        signal: FGVSignal,
        first_15m: Candle,
        stock_bars: list[Candle],
        spy_bars: list[Candle],
        entry_minutes_after_open: int,
        market_open: datetime | None = None,
    ) -> SignalFeatures:
        stock_history = self._history(stock_bars, signal.c3_time)
        spy_history = self._history(spy_bars, signal.c3_time)
        values = {
            "entry_minutes": float(entry_minutes_after_open),
            "signal_minutes": (
                (signal.c3_time - market_open).total_seconds() / 60
                if market_open is not None
                else 0.0
            ),
            "stop_risk_pct": self._pct(signal.risk, signal.trigger_low),
            "first15_range_pct": self._pct(first_15m.high - first_15m.low, first_15m.low),
            "breakout_pct": self._pct(self._last_close(stock_history) - signal.range_high, signal.range_high),
            "c2_range_pct": signal.c2_range_pct,
            "c2_body_pct": signal.c2_body_pct,
            "c2_relative_volume": signal.c2_relative_volume,
            "c3_body_pct": signal.c3_body_pct,
            "c3_close_position": signal.c3_close_position,
        }
        values.update(self._market_features("stock", stock_history))
        values.update(self._market_features("spy", spy_history))
        return SignalFeatures(values)

    @staticmethod
    def _history(bars: list[Candle], through: datetime) -> list[Candle]:
        return sorted((bar for bar in bars if bar.timestamp <= through), key=lambda bar: bar.timestamp)

    def _market_features(self, prefix: str, bars: list[Candle]) -> dict[str, float]:
        closes = [bar.close for bar in bars]
        last_close = self._last_close(bars)
        ema9 = self._ema(closes, 9)
        ema20 = self._ema(closes, 20)
        ema9_three_bars_ago = self._ema(closes[:-3], 9) if len(closes) > 3 else 0
        vwap = self._vwap(bars)
        previous_volumes = [bar.volume for bar in bars[:-1] if bar.volume > 0]
        median_volume = median(previous_volumes) if previous_volumes else 0
        latest_volume_ratio = bars[-1].volume / median_volume if bars and median_volume > 0 else 0
        macd, signal = self._macd(closes)
        return {
            f"{prefix}_history_bars": float(len(bars)),
            f"{prefix}_above_vwap": float(vwap > 0 and last_close > vwap),
            f"{prefix}_vwap_distance_pct": self._pct(last_close - vwap, vwap),
            f"{prefix}_ema9_over_ema20_pct": self._pct(ema9 - ema20, ema20),
            f"{prefix}_ema9_slope_pct": self._pct(ema9 - ema9_three_bars_ago, ema9_three_bars_ago),
            f"{prefix}_rsi14": self._rsi(closes, 14),
            f"{prefix}_macd_hist_pct": self._pct(macd - signal, last_close),
            f"{prefix}_atr14_pct": self._pct(self._atr(bars, 14), last_close),
            f"{prefix}_return_15m_pct": self._return_pct(closes, 3),
            f"{prefix}_return_30m_pct": self._return_pct(closes, 6),
            f"{prefix}_latest_relative_volume": latest_volume_ratio,
        }

    @staticmethod
    def _last_close(bars: list[Candle]) -> float:
        return bars[-1].close if bars else 0

    @staticmethod
    def _pct(numerator: float, denominator: float) -> float:
        return numerator / denominator * 100 if denominator else 0

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        if not values:
            return 0
        alpha = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = alpha * value + (1 - alpha) * result
        return result

    @staticmethod
    def _vwap(bars: list[Candle]) -> float:
        volume = sum(bar.volume for bar in bars if bar.volume > 0)
        if volume <= 0:
            return 0
        dollar_volume = sum(
            ((bar.high + bar.low + bar.close) / 3) * bar.volume
            for bar in bars
            if bar.volume > 0
        )
        return dollar_volume / volume

    @classmethod
    def _macd(cls, closes: list[float]) -> tuple[float, float]:
        if not closes:
            return 0, 0
        macd_values = []
        for index in range(1, len(closes) + 1):
            history = closes[:index]
            macd_values.append(cls._ema(history, 12) - cls._ema(history, 26))
        return macd_values[-1], cls._ema(macd_values, 9)

    @staticmethod
    def _rsi(closes: list[float], period: int) -> float:
        if len(closes) < 2:
            return 50
        changes = [current - previous for previous, current in zip(closes, closes[1:])]
        sample = changes[-period:]
        average_gain = mean(max(change, 0) for change in sample)
        average_loss = mean(max(-change, 0) for change in sample)
        if average_loss == 0:
            return 100 if average_gain > 0 else 50
        return 100 - 100 / (1 + average_gain / average_loss)

    @staticmethod
    def _atr(bars: list[Candle], period: int) -> float:
        if not bars:
            return 0
        true_ranges = []
        previous_close = bars[0].close
        for bar in bars:
            true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))
            previous_close = bar.close
        return mean(true_ranges[-period:])

    @classmethod
    def _return_pct(cls, closes: list[float], periods: int) -> float:
        if len(closes) <= periods:
            return 0
        return cls._pct(closes[-1] - closes[-1 - periods], closes[-1 - periods])
