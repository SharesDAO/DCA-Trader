"""Retrain and gate the FGV probability model on current execution semantics.

This script reads only the dedicated public Backpack backtest cache.  It labels
independent symbol/session opportunities with the current entry guards, no stop
exits, take-profit/time exits, and modeled execution delay/slippage.  The latest
20% of sessions are held out until the final candidate-versus-incumbent gate.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys

from backtest_current import (
    Assumptions,
    END,
    START,
    ET,
    bounds,
    candles,
    coarse_candidates,
    completed_prefix,
    download_fine,
    first_range,
    sessions,
)
from backtest_data import HistoricalData

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import Config
from fgv_trader.entry_safety import rejection
from fgv_trader.features import TechnicalFeatureCalculator
from fgv_trader.prediction import WinProbabilityEstimator
from fgv_trader.settings import STRATEGY_KEYS
from fgv_trader.stops import DEFAULTS, build_policy
from fgv_trader.strategy import FGVStrategy


@dataclass(frozen=True)
class Example:
    symbol: str
    session_date: str
    selected_at: str
    entry_time: str
    exit_time: str
    exit_reason: str
    net_pnl: float
    won: bool
    values: dict[str, float]


def grouped_bars(rows):
    output = {}
    for bar in candles("SPY", rows):
        output.setdefault(bar.timestamp.astimezone(ET).date(), []).append(bar)
    return output


def simulate(symbol, day, bars, tape, spy_bars, inputs, assumptions):
    opening = bounds(day)
    first = first_range(symbol, bars, opening)
    if first is None:
        return None, "MISSING_OPENING_RANGE"
    strategy = FGVStrategy(**{key: inputs["fgv"][key] for key in STRATEGY_KEYS})
    calculator = TechnicalFeatureCalculator()
    cached = None
    for seconds in range(900, 7200, 5):
        now = opening + timedelta(seconds=seconds)
        quote = tape.quote(now.timestamp(), inputs["market_data"]["max_price_age_seconds"])
        if quote is None:
            continue
        key = (seconds - 1) // 300
        if cached is None or cached[0] != key:
            prefix = completed_prefix(bars, opening, now)
            signal = strategy.build_signal(symbol, day.isoformat(), first, prefix[3:]) if prefix else None
            cached = key, signal, prefix
        _, signal, prefix = cached
        if signal is None or not strategy.should_market_buy(signal, quote[0]):
            continue
        entry_minutes = seconds // 60
        if strategy.should_skip_entry(signal, entry_minutes):
            continue

        options = dict(DEFAULTS, **inputs["stop_policy"])
        policy = build_policy(signal, prefix, now, options)
        guard = {
            "signal": asdict(signal),
            "risk_stop": policy["emergency_stop"],
            "reward_risk_stop": signal.stop_loss,
            "max_entry_price": signal.trigger_low
            + inputs["execution"].get("max_entry_above_trigger_r", 0) * signal.risk,
            "max_entry_above_trigger_r": inputs["execution"].get("max_entry_above_trigger_r", 0),
            "allow_entry_below_original_stop": inputs["execution"].get(
                "allow_entry_below_original_stop", False
            ),
            "expires_at": min(
                now.timestamp() + inputs["execution"]["entry_max_age_seconds"],
                (opening + timedelta(minutes=120)).timestamp(),
            ),
            "min_reward_risk": inputs["execution"]["min_entry_reward_risk"],
        }
        spy_prefix = completed_prefix(spy_bars, opening, now)
        values = calculator.calculate(signal, first, prefix, spy_prefix, entry_minutes, opening).values
        due = now + timedelta(seconds=assumptions.buy_delay_seconds)
        entered = None
        failure = "ENTRY_EXPIRED"
        while due.timestamp() < guard["expires_at"]:
            entry_quote = tape.quote(due.timestamp(), inputs["market_data"]["max_price_age_seconds"])
            if entry_quote is None:
                due += timedelta(seconds=5)
                continue
            entry_price = entry_quote[0] * (1 + assumptions.slippage_bps / 10_000)
            failure = rejection({"entry_guard": guard}, entry_price, due.timestamp())
            if failure == "ENTRY_ABOVE_TRIGGER":
                due += timedelta(seconds=5)
                continue
            if failure:
                break
            entered = due, entry_price
            break
        if entered is None:
            if failure == "ENTRY_ABOVE_TRIGGER":
                failure = "ENTRY_EXPIRED"
            return None, failure

        entry_time, entry_price = entered
        quantity = assumptions.wallet_fund / entry_price
        force_exit = opening + timedelta(minutes=375)
        check = entry_time + timedelta(seconds=5)
        exit_reason = "FORCE_EXIT"
        trigger = force_exit
        while check < force_exit:
            current = tape.quote(check.timestamp(), inputs["market_data"]["max_price_age_seconds"])
            if current is not None and current[0] >= signal.take_profit:
                exit_reason = "TAKE_PROFIT"
                trigger = check
                break
            check += timedelta(seconds=5)
        exit_time = trigger + timedelta(seconds=assumptions.sell_delay_seconds)
        exit_quote = tape.quote(exit_time.timestamp(), inputs["market_data"]["max_price_age_seconds"])
        if exit_quote is None:
            return None, "MISSING_EXIT_QUOTE"
        exit_price = exit_quote[0] * (1 - assumptions.slippage_bps / 10_000)
        proceeds = math.floor(quantity * exit_price * 1_000_000) / 1_000_000
        pnl = proceeds - assumptions.wallet_fund - 2 * assumptions.fee_per_side
        return Example(
            symbol=symbol,
            session_date=day.isoformat(),
            selected_at=now.isoformat(),
            entry_time=entry_time.isoformat(),
            exit_time=exit_time.isoformat(),
            exit_reason=exit_reason,
            net_pnl=pnl,
            won=pnl > 0,
            values=values,
        ), None
    return None, "NO_ACTIONABLE_PULLBACK"


def date_split(examples):
    dates = sorted({example.session_date for example in examples})
    train_end = max(1, int(len(dates) * 0.6))
    validation_end = max(train_end + 1, int(len(dates) * 0.8))
    train_dates = set(dates[:train_end])
    validation_dates = set(dates[train_end:validation_end])
    return (
        [example for example in examples if example.session_date in train_dates],
        [example for example in examples if example.session_date in validation_dates],
        [example for example in examples if example.session_date not in train_dates | validation_dates],
    )


def fit(features, examples, epochs=300):
    model = WinProbabilityEstimator(feature_names=features)
    model.fit([(example.values, example.won) for example in examples], epochs=epochs)
    return model


def auc(scored):
    positives = sum(won for _, won in scored)
    negatives = len(scored) - positives
    if not positives or not negatives:
        return 0.5
    ordered = sorted(scored, key=lambda item: item[0])
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        rank_sum += (index + 1 + end) / 2 * sum(won for _, won in ordered[index:end])
        index = end
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def metrics(model, examples):
    scored = [(model.predict(example.values), example.won) for example in examples]
    probabilities = [probability for probability, _ in scored]
    top = sorted(scored, reverse=True)[: max(1, len(scored) // 3)]
    return {
        "count": len(scored),
        "win_rate": sum(won for _, won in scored) / len(scored),
        "auc": auc(scored),
        "log_loss": -sum(
            math.log(max(1e-9, probability if won else 1 - probability))
            for probability, won in scored
        ) / len(scored),
        "top_third_win_rate": sum(won for _, won in top) / len(top),
        "probability_min": min(probabilities),
        "probability_max": max(probabilities),
    }


def portfolio(model, examples, threshold, max_positions=10):
    selected = []
    for day in sorted({example.session_date for example in examples}):
        candidates = sorted(
            ((model.predict(example.values), example) for example in examples if example.session_date == day),
            key=lambda item: (item[1].selected_at, -item[0], item[1].symbol),
        )
        day_selected = []
        for probability, example in candidates:
            if probability < threshold:
                continue
            open_count = sum(prior.entry_time <= example.entry_time < prior.exit_time for prior in day_selected)
            if open_count >= max_positions:
                continue
            day_selected.append(example)
            selected.append(example)
    return {
        "threshold": threshold,
        "trades": len(selected),
        "wins": sum(example.won for example in selected),
        "win_rate": sum(example.won for example in selected) / len(selected) if selected else 0,
        "net_pnl": sum(example.net_pnl for example in selected),
    }


def choose_threshold(model, validation, max_positions):
    probabilities = sorted(model.predict(example.values) for example in validation)
    candidates = {0.45}
    for coverage in (0.2, 0.3, 0.4, 0.5, 0.6, 0.8):
        candidates.add(probabilities[min(len(probabilities) - 1, int(len(probabilities) * (1 - coverage)))])
    results = [portfolio(model, validation, threshold, max_positions) for threshold in candidates]
    minimum = max(10, int(len(validation) * 0.1))
    eligible = [result for result in results if result["trades"] >= minimum]
    return max(eligible or results, key=lambda result: (result["net_pnl"], result["win_rate"], result["trades"]))


def select_features(train, validation, baseline):
    candidates = [name for name in train[0].values if not name.endswith("_history_bars") and name not in baseline]
    dates = sorted({example.session_date for example in train + validation})
    midpoint = max(1, len(dates) // 2)
    folds = [
        (set(dates[:midpoint]), set(dates[midpoint:])),
        (set(dates[: int(len(dates) * 0.75)]), set(dates[int(len(dates) * 0.75) :])),
    ]

    def score(features):
        values = []
        for fit_dates, score_dates in folds:
            fitted = fit(features, [e for e in train + validation if e.session_date in fit_dates], 200)
            values.append(metrics(fitted, [e for e in train + validation if e.session_date in score_dates])["auc"])
        return values

    selected = list(baseline)
    best = score(selected)
    history = [{"features": list(selected), "fold_auc": best, "mean_auc": sum(best) / len(best)}]
    while candidates and len(selected) < len(baseline) + 3:
        trials = []
        for name in candidates:
            fold_scores = score(selected + [name])
            trials.append((sum(fold_scores) / len(fold_scores), name, fold_scores))
        mean_auc, name, fold_scores = max(trials)
        history.append({"candidate": name, "fold_auc": fold_scores, "mean_auc": mean_auc})
        if mean_auc < sum(best) / len(best) + 0.01 or any(new < old - 0.005 for new, old in zip(fold_scores, best)):
            break
        selected.append(name)
        candidates.remove(name)
        best = fold_scores
    return selected, history


def write_examples(path, examples):
    metadata = ["symbol", "session_date", "selected_at", "entry_time", "exit_time", "exit_reason", "net_pnl", "won"]
    features = list(examples[0].values)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=metadata + features)
        writer.writeheader()
        for example in examples:
            row = {key: getattr(example, key) for key in metadata}
            row["won"] = int(example.won)
            row.update(example.values)
            writer.writerow(row)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default="reports/backtests/2026-07-10_2026-09-09")
    parser.add_argument("--output", default="reports/retraining/2026-09-11")
    parser.add_argument("--deploy", action="store_true", help="Replace the configured model only if the gate passes")
    args = parser.parse_args()
    directory = Path(args.directory)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    data = HistoricalData(directory)
    inputs = json.loads((directory / "inputs.json").read_text())
    config = Config()
    inputs["execution"] = dict(
        inputs["execution"],
        entry_max_age_seconds=config.execution["entry_max_age_seconds"],
        allow_entry_below_original_stop=config.execution.get("allow_entry_below_original_stop", False),
        max_entry_above_trigger_r=config.execution.get("max_entry_above_trigger_r", 0),
        min_entry_reward_risk=config.execution["min_entry_reward_risk"],
    )
    inputs["stop_policy"] = dict(inputs["stop_policy"], exits_enabled=False)
    inputs["fgv"] = dict(inputs["fgv"], max_concurrent_positions=config.fgv["max_concurrent_positions"])
    coarse = coarse_candidates(data, inputs)
    tapes = download_fine(data, coarse)
    spy = grouped_bars(data.fetch("SPY", "5m", START, END))
    assumptions = Assumptions(stop_exits_enabled=False, wallet_fund=50, slippage_bps=5, buy_delay_seconds=15, sell_delay_seconds=10)
    examples = []
    rejected = {}
    for day in sessions():
        for symbol, bars in sorted(coarse.get(day, {}).items()):
            tape = tapes.get(day, {}).get(symbol)
            if tape is None or day not in spy:
                rejected["MISSING_DATA"] = rejected.get("MISSING_DATA", 0) + 1
                continue
            example, reason = simulate(symbol, day, bars, tape, spy[day], inputs, assumptions)
            if example is None:
                rejected[reason] = rejected.get(reason, 0) + 1
            else:
                examples.append(example)
    examples.sort(key=lambda example: (example.session_date, example.selected_at, example.symbol))
    if examples:
        write_examples(output / "examples.csv", examples)
    if len(examples) < 100:
        report = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "data_period": [START.isoformat(), END.isoformat()],
            "examples": len(examples),
            "wins": sum(example.won for example in examples),
            "rejected": rejected,
            "minimum_examples": 100,
            "gate_passed": False,
            "deployed": False,
            "reason": "INSUFFICIENT_CURRENT_STRATEGY_EXAMPLES",
        }
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 2
    train, validation, test = date_split(examples)
    baseline_features = list(config.fgv["win_probability_features"])
    features, selection_history = select_features(train, validation, baseline_features)
    incumbent_eval = fit(baseline_features, train + validation)
    candidate_eval = fit(features, train + validation)
    candidate_threshold_result = choose_threshold(fit(features, train), validation, config.fgv["max_concurrent_positions"])
    candidate_threshold = candidate_threshold_result["threshold"]
    incumbent_test = portfolio(incumbent_eval, test, config.fgv["min_win_probability"], config.fgv["max_concurrent_positions"])
    candidate_test = portfolio(candidate_eval, test, candidate_threshold, config.fgv["max_concurrent_positions"])
    incumbent_metrics = metrics(incumbent_eval, test)
    candidate_metrics = metrics(candidate_eval, test)
    gate_checks = {
        "test_net_pnl_improved": candidate_test["net_pnl"] > incumbent_test["net_pnl"],
        "test_auc_not_degraded": candidate_metrics["auc"] >= incumbent_metrics["auc"] - 0.01,
        "test_log_loss_not_degraded": candidate_metrics["log_loss"] <= incumbent_metrics["log_loss"] + 0.01,
        "test_has_at_least_10_trades": candidate_test["trades"] >= 10,
    }
    passed = all(gate_checks.values())
    final_model = fit(features, examples, 400)
    candidate_path = output / "candidate_model.json"
    final_model.save(candidate_path)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "data_period": [START.isoformat(), END.isoformat()],
        "semantics": {
            "entry_max_age_seconds": inputs["execution"]["entry_max_age_seconds"],
            "stop_exits_enabled": False,
            "slippage_bps_per_side": assumptions.slippage_bps,
            "buy_delay_seconds": assumptions.buy_delay_seconds,
            "sell_delay_seconds": assumptions.sell_delay_seconds,
            "max_concurrent_positions": config.fgv["max_concurrent_positions"],
        },
        "examples": len(examples),
        "wins": sum(example.won for example in examples),
        "rejected": rejected,
        "date_splits": {
            "train": [train[0].session_date, train[-1].session_date, len(train)],
            "validation": [validation[0].session_date, validation[-1].session_date, len(validation)],
            "test": [test[0].session_date, test[-1].session_date, len(test)],
        },
        "incumbent_features": baseline_features,
        "candidate_features": features,
        "selection_history": selection_history,
        "validation_threshold_selection": candidate_threshold_result,
        "incumbent_test_metrics": incumbent_metrics,
        "candidate_test_metrics": candidate_metrics,
        "incumbent_test_portfolio": incumbent_test,
        "candidate_test_portfolio": candidate_test,
        "gate_checks": gate_checks,
        "gate_passed": passed,
        "incumbent_sha256": sha256(ROOT / config.fgv["win_probability_model_path"]),
        "candidate_sha256": sha256(candidate_path),
        "deployed": False,
    }
    if args.deploy and passed:
        target = ROOT / config.fgv["win_probability_model_path"]
        backup = output / "incumbent_model.json"
        shutil.copy2(target, backup)
        shutil.copy2(candidate_path, target)
        report["deployed"] = True
        report["backup"] = str(backup)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
