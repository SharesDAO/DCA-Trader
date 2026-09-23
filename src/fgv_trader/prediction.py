from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fgv_trader.features import SignalFeatures
from fgv_trader.models import FGVSignal


DEFAULT_FEATURES = ["stop_risk_pct", "entry_minutes"]


@dataclass
class OutcomeStats:
    wins: int = 0
    total: int = 0

    def observe(self, won: bool) -> None:
        self.total += 1
        self.wins += int(won)


@dataclass
class RunningStats:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def observe(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)

    def standardize(self, value: float) -> float:
        if self.count < 2:
            return 0.0
        standard_deviation = math.sqrt(self.m2 / (self.count - 1))
        if standard_deviation <= 1e-12:
            return 0.0
        return max(-5.0, min(5.0, (value - self.mean) / standard_deviation))


@dataclass
class WinProbabilityEstimator:
    feature_names: list[str] = field(default_factory=lambda: list(DEFAULT_FEATURES))
    learning_rate: float = 0.05
    l2: float = 0.001
    bias: float = 0.0
    weights: dict[str, float] = field(default_factory=dict)
    normalization: dict[str, RunningStats] = field(default_factory=dict)
    total: OutcomeStats = field(default_factory=OutcomeStats)

    def __post_init__(self) -> None:
        self.feature_names = list(dict.fromkeys(self.feature_names))
        for name in self.feature_names:
            self.weights.setdefault(name, 0.0)
            self.normalization.setdefault(name, RunningStats())

    def predict(
        self,
        features: SignalFeatures | dict[str, float] | FGVSignal,
        entry_minutes_after_open: int | None = None,
    ) -> float:
        values = self._values(features, entry_minutes_after_open)
        score = self.bias
        for name in self.feature_names:
            score += self.weights[name] * self.normalization[name].standardize(float(values.get(name, 0.0)))
        return max(0.05, min(0.95, self._sigmoid(score)))

    def observe(
        self,
        features: SignalFeatures | dict[str, float] | FGVSignal,
        entry_minutes_after_open: int | bool | None = None,
        won: bool | None = None,
    ) -> None:
        if won is None:
            won = bool(entry_minutes_after_open)
            entry_minutes_after_open = None
        values = self._values(
            features,
            int(entry_minutes_after_open) if entry_minutes_after_open is not None else None,
        )
        probability = self.predict(values)
        error = float(won) - probability
        rate = self.learning_rate / math.sqrt(1 + self.total.total / 200)
        self.bias += rate * error
        for name in self.feature_names:
            value = float(values.get(name, 0.0))
            standardized = self.normalization[name].standardize(value)
            self.weights[name] += rate * (error * standardized - self.l2 * self.weights[name])
            self.normalization[name].observe(value)
        self.total.observe(bool(won))

    def fit(self, examples: list[tuple[dict[str, float], bool]], epochs: int = 400) -> None:
        self.bias = 0.0
        self.weights = {name: 0.0 for name in self.feature_names}
        self.normalization = {name: RunningStats() for name in self.feature_names}
        self.total = OutcomeStats()
        if not examples:
            return
        for values, won in examples:
            for name in self.feature_names:
                self.normalization[name].observe(float(values.get(name, 0.0)))
            self.total.observe(won)
        prevalence = max(0.01, min(0.99, self.total.wins / self.total.total))
        self.bias = math.log(prevalence / (1 - prevalence))
        for _ in range(epochs):
            bias_gradient = 0.0
            weight_gradients = {name: 0.0 for name in self.feature_names}
            for values, won in examples:
                probability = self.predict(values)
                error = float(won) - probability
                bias_gradient += error
                for name in self.feature_names:
                    standardized = self.normalization[name].standardize(float(values.get(name, 0.0)))
                    weight_gradients[name] += error * standardized
            scale = 1 / len(examples)
            self.bias += self.learning_rate * bias_gradient * scale
            for name in self.feature_names:
                self.weights[name] += self.learning_rate * (
                    weight_gradients[name] * scale - self.l2 * self.weights[name]
                )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def load(
        cls,
        path: Path,
        feature_names: list[str] | None = None,
    ) -> "WinProbabilityEstimator":
        if not path.exists():
            return cls(feature_names=feature_names or list(DEFAULT_FEATURES))
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data.get("model_version", 0)) != 2:
            return cls(feature_names=feature_names or list(DEFAULT_FEATURES))
        saved_features = [str(name) for name in data.get("feature_names", [])]
        requested_features = feature_names or saved_features or list(DEFAULT_FEATURES)
        if feature_names is not None and requested_features != saved_features:
            return cls(feature_names=requested_features)
        model = cls(
            feature_names=requested_features,
            learning_rate=float(data.get("learning_rate", 0.05)),
            l2=float(data.get("l2", 0.001)),
            bias=float(data.get("bias", 0.0)),
            weights={key: float(value) for key, value in data.get("weights", {}).items()},
        )
        model.total = OutcomeStats(**data.get("total", {}))
        model.normalization = {
            key: RunningStats(
                count=int(value.get("count", 0)),
                mean=float(value.get("mean", 0.0)),
                m2=float(value.get("m2", 0.0)),
            )
            for key, value in data.get("normalization", {}).items()
        }
        model.__post_init__()
        return model

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": 2,
            "feature_names": self.feature_names,
            "learning_rate": self.learning_rate,
            "l2": self.l2,
            "bias": self.bias,
            "weights": self.weights,
            "normalization": {
                key: {"count": stats.count, "mean": stats.mean, "m2": stats.m2}
                for key, stats in self.normalization.items()
            },
            "total": {"wins": self.total.wins, "total": self.total.total},
        }

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            return 1 / (1 + math.exp(-min(value, 60)))
        exponent = math.exp(max(value, -60))
        return exponent / (1 + exponent)

    @staticmethod
    def _values(
        features: SignalFeatures | dict[str, float] | FGVSignal,
        entry_minutes_after_open: int | None,
    ) -> dict[str, float]:
        if isinstance(features, SignalFeatures):
            return features.values
        if isinstance(features, dict):
            return features
        risk_pct = features.risk / features.trigger_low * 100 if features.trigger_low > 0 else 0
        return {
            "stop_risk_pct": risk_pct,
            "entry_minutes": float(entry_minutes_after_open or 0),
        }
