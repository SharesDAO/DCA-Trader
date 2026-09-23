from __future__ import annotations

from collections.abc import Iterable
from statistics import median

from fgv_trader.models import Candle, FGVSignal


class FGVStrategy:
    def __init__(
        self,
        risk_reward_ratio: float = 2.0,
        signal_c2_min_range_pct: float = 0.0,
        signal_c3_min_close_position: float = 0.0,
        signal_c2_min_relative_volume: float = 0.0,
        early_entry_filter_minutes: int = 0,
        early_entry_stop_risk_min_pct: float = 0.0,
        early_entry_stop_risk_max_pct: float = 0.0,
    ):
        if risk_reward_ratio <= 0:
            raise ValueError("risk_reward_ratio must be positive")
        if signal_c2_min_range_pct < 0:
            raise ValueError("signal_c2_min_range_pct cannot be negative")
        if not 0 <= signal_c3_min_close_position <= 1:
            raise ValueError("signal_c3_min_close_position must be in [0, 1]")
        if signal_c2_min_relative_volume < 0:
            raise ValueError("signal_c2_min_relative_volume cannot be negative")
        if early_entry_filter_minutes < 0:
            raise ValueError("early_entry_filter_minutes cannot be negative")
        if early_entry_stop_risk_min_pct < 0:
            raise ValueError("early_entry_stop_risk_min_pct cannot be negative")
        if early_entry_stop_risk_max_pct < early_entry_stop_risk_min_pct:
            raise ValueError("early_entry_stop_risk_max_pct cannot be below the minimum")
        self.risk_reward_ratio = risk_reward_ratio
        self.signal_c2_min_range_pct = signal_c2_min_range_pct
        self.signal_c3_min_close_position = signal_c3_min_close_position
        self.signal_c2_min_relative_volume = signal_c2_min_relative_volume
        self.early_entry_filter_minutes = early_entry_filter_minutes
        self.early_entry_stop_risk_min_pct = early_entry_stop_risk_min_pct
        self.early_entry_stop_risk_max_pct = early_entry_stop_risk_max_pct

    def build_signal(
        self,
        symbol: str,
        session_date: str,
        first_15m: Candle,
        five_minute_candles: Iterable[Candle],
    ) -> FGVSignal | None:
        candles = sorted(five_minute_candles, key=lambda candle: candle.timestamp)
        if len(candles) < 3:
            return None

        range_high = first_15m.high
        for index in range(2, len(candles)):
            c1 = candles[index - 2]
            c2 = candles[index - 1]
            c3 = candles[index]
            if not self._is_signal_triplet(c1, c2, c3, range_high):
                continue
            if not self._is_expansion_bar(c2):
                continue
            if c3.close <= c3.open:
                continue
            c3_close_position = self._close_position(c3)
            if c3_close_position < self.signal_c3_min_close_position:
                continue
            c2_relative_volume = self._relative_volume(c2, candles[: index - 1])
            if self.signal_c2_min_relative_volume > 0 and c2_relative_volume < self.signal_c2_min_relative_volume:
                continue

            trigger_low = c3.low
            stop_loss = c1.low
            risk = trigger_low - stop_loss
            if risk <= 0:
                continue
            take_profit = trigger_low + self.risk_reward_ratio * risk
            if self._target_hit_before_pullback(candles[index + 1 :], trigger_low, take_profit):
                continue

            return FGVSignal(
                symbol=symbol,
                session_date=session_date,
                range_high=range_high,
                trigger_low=trigger_low,
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk=risk,
                c1_time=c1.timestamp,
                c2_time=c2.timestamp,
                c3_time=c3.timestamp,
                c2_range_pct=self._range_pct(c2),
                c2_body_pct=self._body_pct(c2),
                c2_relative_volume=c2_relative_volume,
                c3_body_pct=self._body_pct(c3),
                c3_close_position=c3_close_position,
            )
        return None

    @staticmethod
    def should_market_buy(signal: FGVSignal, latest_price: float) -> bool:
        return latest_price < signal.trigger_low

    @staticmethod
    def should_stop_loss(signal: FGVSignal, latest_price: float) -> bool:
        return latest_price < signal.stop_loss

    @staticmethod
    def should_take_profit(signal: FGVSignal, latest_price: float) -> bool:
        return latest_price >= signal.take_profit

    def should_skip_entry(self, signal: FGVSignal, entry_minutes_after_open: int) -> bool:
        if self.early_entry_filter_minutes <= 0 or entry_minutes_after_open >= self.early_entry_filter_minutes:
            return False
        risk_pct = signal.risk / signal.trigger_low * 100 if signal.trigger_low > 0 else 0
        return self.early_entry_stop_risk_min_pct < risk_pct <= self.early_entry_stop_risk_max_pct

    @staticmethod
    def _is_signal_triplet(c1: Candle, c2: Candle, c3: Candle, range_high: float) -> bool:
        return (
            c1.high < range_high
            and c2.low < range_high
            and c3.close > range_high
        )

    def _is_expansion_bar(self, candle: Candle) -> bool:
        if self.signal_c2_min_range_pct <= 0:
            return True
        return self._range_pct(candle) >= self.signal_c2_min_range_pct

    @staticmethod
    def _range_pct(candle: Candle) -> float:
        if candle.low <= 0:
            return 0
        return (candle.high - candle.low) / candle.low * 100

    @staticmethod
    def _body_pct(candle: Candle) -> float:
        if candle.low <= 0:
            return 0
        return abs(candle.close - candle.open) / candle.low * 100

    @staticmethod
    def _close_position(candle: Candle) -> float:
        candle_range = candle.high - candle.low
        if candle_range <= 0:
            return 0
        return (candle.close - candle.low) / candle_range

    @staticmethod
    def _relative_volume(candle: Candle, previous_candles: Iterable[Candle]) -> float:
        previous_volumes = [item.volume for item in previous_candles if item.volume > 0]
        baseline = median(previous_volumes) if previous_volumes else 0
        return candle.volume / baseline if baseline > 0 else 0

    @staticmethod
    def _target_hit_before_pullback(candles: Iterable[Candle], trigger_low: float, take_profit: float) -> bool:
        for candle in candles:
            if candle.high >= take_profit:
                return True
            if candle.low < trigger_low:
                return False
        return False
