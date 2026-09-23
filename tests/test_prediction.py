from datetime import datetime, timezone

from fgv_trader.models import FGVSignal
from fgv_trader.prediction import WinProbabilityEstimator


def signal(symbol="TSLA", risk=1.0):
    timestamp = datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc)
    return FGVSignal(
        symbol=symbol,
        session_date="2026-05-04",
        range_high=101,
        trigger_low=100,
        stop_loss=100 - risk,
        take_profit=100 + 1.5 * risk,
        risk=risk,
        c1_time=timestamp,
        c2_time=timestamp,
        c3_time=timestamp,
    )


def test_probability_starts_at_neutral_prior_and_learns_outcomes():
    model = WinProbabilityEstimator()
    winner = signal("WIN", risk=0.8)
    loser = signal("LOSS", risk=2.5)

    assert model.predict(winner, 45) == 0.5
    for _ in range(20):
        model.observe(winner, 45, True)
        model.observe(loser, 120, False)

    assert model.predict(winner, 45) > 0.5
    assert model.predict(loser, 120) < 0.5


def test_probability_model_round_trip(tmp_path):
    path = tmp_path / "model.json"
    model = WinProbabilityEstimator()
    model.observe(signal(), 45, True)

    model.save(path)
    restored = WinProbabilityEstimator.load(path)

    assert restored.to_dict() == model.to_dict()


def test_probability_model_learns_numeric_feature_direction():
    model = WinProbabilityEstimator(feature_names=["momentum"])

    for _ in range(50):
        model.observe({"momentum": 2.0}, True)
        model.observe({"momentum": -2.0}, False)

    assert model.predict({"momentum": 2.0}) > model.predict({"momentum": -2.0})
