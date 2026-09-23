# Backtest comparison: no stop-loss exits

July 10–September 9, 2026; $10,000 initial cash; current $50 funding per new wallet; 10 maximum simultaneous reservations. Same frozen universe, model, historical data, entry filters, ranking, wallet reuse and retirement rules as the baseline.

**Changed only:** confirmed/buffered and emergency stop exits are disabled. Take-profit and 15:45 forced liquidation remain. Entry stop-floor and reward/risk checks are deliberately retained to isolate the exit change. Without stop exits, their reference risk distance is a selection heuristic, not a bound on realized loss.

## Baseline cost assumptions

Adverse slippage 0.05% per side, 15-second buy delay, 10-second sell delay, before unmeasured gas/exchange costs.

| Metric | Current stops | No stop exits |
|---|---:|---:|
| Ending equity | $9,996.16 | $10,008.59 |
| Profit/loss | -$3.84 | $8.59 |
| Portfolio ROI | -0.038% | 0.086% |
| Closed trades | 60 | 60 |
| Wins | 16 | 26 |
| Losses | 44 | 34 |
| Win rate | 26.67% | 43.33% |
| Maximum sampled equity drawdown | 0.103% | 0.093% |
| Peak position cost deployed | $252.62 | $303.79 |
| Average position cost deployed | $22.64 | $50.25 |
| Wallets created | 24 | 19 |
| Wallets retired | 21 | 16 |

Change in portfolio profit: **$12.43**.
No-stop exit counts: {"FORCE_EXIT": 49, "TAKE_PROFIT": 11}.
Worst realized no-stop trade by percentage: **FSLY on 2026-08-07, -4.32% (-$2.22)**. This is an observed sample loss, not a maximum possible loss.

## Cost and delay comparisons

| Slippage / side | Fee / side | Buy delay | Current-stop P&L | No-stop P&L | No-stop ROI |
|---:|---:|---:|---:|---:|---:|
| 0 bps | $0.00 | 15 s | -$0.53 | $10.90 | 0.109% |
| 5 bps | $0.00 | 15 s | -$3.84 | $8.59 | 0.086% |
| 25 bps | $0.00 | 15 s | -$10.03 | -$6.76 | -0.068% |
| 50 bps | $0.00 | 15 s | -$14.95 | -$13.16 | -0.132% |
| 5 bps | $0.05 | 15 s | -$9.84 | $2.59 | 0.026% |
| 5 bps | $0.00 | 30 s | -$2.53 | $0.56 | 0.006% |

Each scenario is a full portfolio rerun. Longer holding periods change wallet availability, subsequent funding amounts and sometimes which entries fit within the position limit. This is not a fixed-size, paired-trade comparison.

## Important limitations

- Most of the $10,000 remains idle under the current $50-wallet setting. Small portfolio drawdowns do not imply individual positions have little risk.
- The model was fitted using observations through August 28, 2026, overlapping most of the tested period. This is a retrospective simulation, not independent out-of-sample validation.
- Historical Backpack reference bars do not reconstruct actual SharesDAO RFQs, fees, token liquidity, partial settlement or live feed delivery. Sparse 1-second bars can cause freshness gaps that differ from live ticker behavior.
- No-stop exits can expose positions to substantially larger losses before the time exit. The observed result is not a return forecast or a recommendation to disable live protection.
- Live trading settings and databases were not changed.

## Outputs and reproduction

- [No-stop trade ledger](wallet50_5bps_fee0_delay15_trades.csv)
- [No-stop daily equity](wallet50_5bps_fee0_delay15_daily.csv)
- [No-stop scenario results](results.json)
- [Original report and full assumptions](../report.md)

```sh
venv/bin/python scripts/backtest_current.py --directory reports/backtests/2026-07-10_2026-09-09 --no-stop-exits
venv/bin/python scripts/report_no_stop.py reports/backtests/2026-07-10_2026-09-09
```
