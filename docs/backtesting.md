# Current-strategy historical replay

The isolated replay for July 10–September 9, 2026 lives in
`scripts/backtest_current.py`; public data caching lives in
`scripts/backtest_data.py`. Neither imports a broker nor opens a production wallet
database. The live service does not need to be stopped or restarted.

```sh
venv/bin/python scripts/backtest_data.py --directory reports/backtests/2026-07-10_2026-09-09
venv/bin/python scripts/backtest_current.py --directory reports/backtests/2026-07-10_2026-09-09
venv/bin/python scripts/report_backtest.py reports/backtests/2026-07-10_2026-09-09
```

The downloader snapshots the current public configuration, model and eligible
pool symbols into `inputs.json` on its first run. Reusing a directory reuses that
snapshot and the historical cache; it does not silently adopt later live changes.
The historical cache is ignored by git. The scripts are scoped to these dates;
the replay's session calendar explicitly excludes September 7, 2026.

To compare removal of stop exits only, add `--no-stop-exits` to the replay.
Results go to the separate `no_stop_exits/` subdirectory; the original results are
preserved. Both confirmed/buffered and emergency exits are disabled. Take-profit,
15:45 forced liquidation, configured entry-price floor and entry reward/risk gates are left
unchanged to isolate the exit-policy change. The latter still use the original
frozen stop levels as entry filters; without an actual stop exit, that ratio is
only a selection heuristic, not a limit on realized loss. This switch has no
effect on live trading.

Default capital is $10,000, with $50 funding per new wallet. To run a separate
position-size sensitivity using the same frozen input data, pass
`--wallet-funds 50,1000` to the replay. This does **not** alter live funding.

The model, signals, execution entry gates and stop decision functions are reused
from production. Five-minute candles are made available only after completion;
one-second closes only after that second ends. Prices are sampled every five
seconds with the configured freshness limit. Entries and exits have explicit
simulated delays and slippage. Requests that fail and unavailable observations
are reported in coverage files, never silently fabricated. If any fine requests
fail, retry the replay with its cache before interpreting results.

The output directory contains scenario summaries, trade CSVs, daily equity CSVs,
coverage files, a model hash, code hashes and a generated `report.md`. Cash
conservation is asserted for every scenario. Unit tests cover time availability,
entry expiry, wallet reuse/retirement, production stop confirmation, and fees.

## Gated model retraining

`scripts/retrain_current.py` derives independent symbol/session examples from the
same isolated public cache using the live entry guards, configured entry deadline,
no stop exits, take-profit/time exits, and modeled execution costs. It uses
chronological 60/20/20 train, validation, and untouched test partitions. Candidate
features and the probability threshold are selected without using the test dates.

```sh
venv/bin/python scripts/retrain_current.py --deploy
```

`--deploy` is fail-closed: the configured model is replaced only if there are at
least 100 executable examples and the candidate passes the out-of-sample profit,
AUC, log-loss, and trade-count gates. The incumbent is backed up beside the report
before replacement. A failed gate still writes `examples.csv` and `report.json`
for audit and does not restart or modify the live service.

Important: reference bars cannot recreate historical executable SharesDAO quotes,
websocket delivery timing, token liquidity or actual blockchain settlement. Sparse
trade bars can produce freshness gaps even where live external ticker messages
would have continued; the report is a market-data simulation, not an exact exchange
replay. Current pool membership is not point-in-time membership. The current model
was fitted using data through August 28, overlapping most of the tested period.
This is a retrospective test, not unbiased out-of-sample evidence or a profit
forecast. See the generated report for all assumptions and coverage limitations.
