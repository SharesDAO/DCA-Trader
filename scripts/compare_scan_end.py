"""Compare entry cutoffs without changing the live configuration or database."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from backtest_current import (
    Assumptions,
    Replay,
    coarse_candidates,
    download_fine,
    sessions,
    write_csv,
)
from backtest_data import HistoricalData

ROOT = Path(__file__).resolve().parents[1]


def minutes_after_open(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    result = hour * 60 + minute - (9 * 60 + 30)
    if not 15 < result < 375:
        raise argparse.ArgumentTypeError("cutoff must be after 09:45 and before 15:45 ET")
    return result


def current_inputs(directory: Path) -> dict:
    # Preserve the historical symbol universe and data snapshot while applying
    # the strategy/model settings that the live bot currently uses.
    from config import Config

    inputs = json.loads((directory / "inputs.json").read_text())
    config = Config()
    inputs["fgv"] = dict(config.fgv)
    inputs["execution"] = dict(config.execution)
    inputs["stop_policy"] = dict(config.stop_policy)
    inputs["max_loss_traders"] = config.max_loss_traders
    inputs["model"] = json.loads(
        (config.project_root / config.fgv["win_probability_model_path"]).read_text()
    )
    inputs["_directory"] = str(directory)
    return inputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory", default="reports/backtests/2026-07-10_2026-09-09"
    )
    parser.add_argument("--cutoffs", default="11:30,12:30")
    args = parser.parse_args()

    directory = Path(args.directory)
    cutoffs = [value.strip() for value in args.cutoffs.split(",")]
    cutoff_minutes = {value: minutes_after_open(value) for value in cutoffs}
    inputs = current_inputs(directory)
    data = HistoricalData(directory)

    # Screen and download once through the latest requested cutoff so every
    # scenario sees an identical data universe.
    coarse = coarse_candidates(data, inputs, max(cutoff_minutes.values()))
    tapes = download_fine(data, coarse)
    assumption = Assumptions(
        name="wallet50_5bps_fee0_delay15",
        stop_exits_enabled=False,
        wallet_fund=50,
        slippage_bps=5,
        fee_per_side=0,
        buy_delay_seconds=15,
        sell_delay_seconds=10,
    )
    output = directory / "scan_end_comparison"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for cutoff in cutoffs:
        replay = Replay(inputs, assumption, cutoff_minutes[cutoff])
        for day in sessions():
            replay.run_day(day, coarse.get(day, {}), tapes.get(day, {}))
        summary = replay.summary()
        summary["scan_end_et"] = cutoff
        results.append(summary)
        write_csv(output / f"scan_end_{cutoff.replace(':', '')}_trades.csv", replay.trades)
        write_csv(output / f"scan_end_{cutoff.replace(':', '')}_daily.csv", replay.daily)
        print(json.dumps(summary), flush=True)

    artifact = {
        "comparison_only": True,
        "live_configuration_changed": False,
        "data_period": ["2026-07-10", "2026-09-09"],
        "assumptions": asdict(assumption),
        "results": results,
    }
    (output / "results.json").write_text(json.dumps(artifact, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
