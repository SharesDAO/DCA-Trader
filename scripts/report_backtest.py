"""Generate a concise, auditable report from an isolated historical replay."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    directory=args.directory
    results=json.loads((directory/'results.json').read_text())
    inputs=json.loads((directory/'inputs.json').read_text())
    coarse=json.loads((directory/'coarse_coverage.json').read_text())
    screen=json.loads((directory/'screen_coverage.json').read_text())
    fine=json.loads((directory/'fine_coverage.json').read_text())
    baseline=next(r for r in results if r['assumptions']['wallet_fund']==50
                  and r['assumptions']['slippage_bps']==5 and r['assumptions']['fee_per_side']==0
                  and r['assumptions']['buy_delay_seconds']==15)
    lines=['# Current-strategy retrospective backtest', '',
        'Period: **July 10–September 9, 2026**, inclusive; 43 U.S. trading sessions. '
        'September 10 is excluded because its session was unfinished. September 7 was Labor Day.', '',
        '## $10,000 portfolio with current $50 wallet funding', '',
        f'- Ending equity: **${baseline["ending_equity"]:,.2f}**',
        f'- Simulated profit: **${baseline["net_profit"]:,.2f}**',
        f'- Total-portfolio ROI: **{baseline["roi_pct"]:.3f}%** (not annualized)',
        f'- Closed trades: **{baseline["trades"]}**; win rate **{baseline["win_rate_pct"]:.2f}%**',
        f'- Maximum sampled equity drawdown: **{baseline["max_sampled_drawdown_pct"]:.3f}%**',
        f'- Peak position cost deployed: **${baseline["peak_deployed"]:,.2f}**',
        f'- Average position cost deployed across market hours: **${baseline["average_deployed"]:,.2f}**',
        f'- Trading wallets created / retired: **{baseline["wallets_created"]} / {baseline["wallets_retired"]}**', '',
        'These are hypothetical results after assumed 5-basis-point adverse slippage per side, '
        '**before unmeasured gas, SharesDAO execution costs, taxes and cash yield**. '
        'They are not the net profit of actual blockchain trades or a return forecast.', '',
        '## Cost and execution-delay sensitivity', '',
        '| New-wallet funding | Slippage per side | Assumed fee per side | Buy delay | Trades | Profit | Portfolio ROI |',
        '|---:|---:|---:|---:|---:|---:|---:|']
    for r in results:
        a=r['assumptions']
        lines.append(f'| ${a["wallet_fund"]:,.0f} | {a["slippage_bps"]} bps | ${a["fee_per_side"]:.2f} | '
                     f'{a["buy_delay_seconds"]} s | {r["trades"]} | ${r["net_profit"]:,.2f} | {r["roi_pct"]:.3f}% |')
    lines+=['', 'Each row is a fresh portfolio replay: worse buy quotes can reject different entries, '
            'not merely subtract costs from the same trades. Fee assumptions are illustrative, not measured exchange fees.', '',
        '## Rules and accounting', '',
        '- Uses the current production FGV signal builder, frozen probability model, entry safety gates, '
        'and confirmed/buffered stop functions. Signals only use completed 5-minute candles; '
        'a one-second candle close is only available after that second ends.',
        '- Current 593-symbol mint-mode-3 universe, one selected attempt per symbol/session, '
        'probability ranking with ticker tie-breaks, 10 concurrent reservations including pending entries.',
        '- Starts with $10,000 cash and no trading wallets. New wallets receive $50. Idle eligible wallets '
        'reuse their entire remaining balance, highest balance first, with no top-ups. '
        'After two cumulative losing trades a wallet is retired and remaining USDC returned to the vault. '
        'The vault retains a $5 funding reserve. Most of $10,000 can remain idle under these settings.',
        '- Fixed 5-second polling clock. Default entry preparation/settlement delay is 15 seconds. '
        'Above-trigger quotes are retried until the original 60-second deadline or 11:30 cutoff; '
        'other execution rejections consume the symbol’s daily attempt. Sell fills occur 10 seconds '
        'after a trigger, at the then-available historical reference price adjusted for scenario slippage.',
        '- ATR buffer = min(0.25 × recent 5-minute mean true range, 0.25 × original R). '
        'Two distinct below-buffer observations at least 5 seconds apart; emergency stop another 0.25 R lower. '
        'ATR and levels freeze at selection. Reward/risk is checked against the emergency level. '
        'Original stop fallback applies if ATR history is unusable. Take-profit and 15:45 forced exit remain immediate triggers.',
        '- Fractional shares, USDC payout precision of 6 decimals, balance reuse, '
        'mark-to-reference equity each polling step. Drawdown can miss moves between samples. '
        'USDC is valued at $1; native-token balances and yield on idle cash are not modeled. '
        'No fabricated fills are used when a quote is unavailable; unresolved end-of-session positions abort the report.', '',
        '## Data coverage and limitations', '',
        f'- Downloaded **{sum(r["rows"] for r in coarse):,}** five-minute bars; '
        f'**{sum(r.get("rows",0) for r in fine):,}** one-second bars for **{len(fine)}** potential-signal symbol-days.',
        f'- Opening history available for **{screen["symbol_sessions_with_opening"]:,}** stock-sessions; '
        f'**{screen["symbol_sessions_missing_opening"]:,}** lack a complete opening range. '
        'These cannot initiate an FGV trade; no missing candles are fabricated.',
        f'- Fine-history request/parse errors: **{sum("error" in r for r in fine)}**; '
        f'empty fine histories: **{sum(r.get("rows")==0 for r in fine)}**. See the coverage JSON files for individual symbols/dates.',
        '- Current pool membership and mint mode are applied retrospectively; point-in-time membership, '
        'delisted pools and historical token availability were not available. This introduces universe/survivorship bias.',
        '- Backpack external-market bars are reference data, not historical SharesDAO RFQs. '
        'Five-second polling is aligned to historical one-second bar closes, not an exact replay of live websocket delivery times. '
        'Sparse trade bars can cause freshness gaps even when live ticker messages might have continued to refresh the same last price. '
        'Order-book liquidity, actual spreads, slippage, pool capacity, partial delivery, transaction failures, '
        'gas funding and retirement-sweep latency cannot be reconstructed from this data.',
        '- **Training overlap:** the current model has 1,646 training examples and weights matching the sibling '
        '`technical_12m_feature_report.json`; its source dataset spans September 2, 2025–August 28, 2026. '
        'The training script fits the final model on all examples. Thus most of this two-month test overlaps '
        'model training. The model is frozen, not refit during replay, but the overall result is not an unbiased out-of-sample test.',
        f'- Within this run, trades dated August 31–September 9 generated **${baseline["after_training_period_profit"]:,.2f}** '
        f'across **{baseline["after_training_period_trades"]}** trades. This is only a short post-training subset '
        'of the same continuing portfolio, not a separate $10,000 test or independent validation of the newly designed stops.', '',
        '## Reproduction and outputs', '',
        '```sh',
        'venv/bin/python scripts/backtest_data.py --directory reports/backtests/2026-07-10_2026-09-09',
        'venv/bin/python scripts/backtest_current.py --directory reports/backtests/2026-07-10_2026-09-09',
        'venv/bin/python scripts/report_backtest.py reports/backtests/2026-07-10_2026-09-09',
        '```', '',
        '- [Scenario results](results.json)',
        f'- [Baseline trade ledger]({baseline["assumptions"]["name"]}_trades.csv)',
        f'- [Baseline daily equity]({baseline["assumptions"]["name"]}_daily.csv)',
        '- [Frozen public configuration and model](inputs.json)',
        '- [Five-minute coverage](coarse_coverage.json), [signal-screen coverage](screen_coverage.json), [one-second coverage](fine_coverage.json)',
        f'- Model SHA-256: `{inputs["model_sha256"]}`', '',
        'The dedicated historical SQLite cache contains only public market data. Neither production database '
        'was opened for writing, and no live orders or configuration changes were made by this backtest.', '',
        '## References', '',
        '- [Backpack API: K-lines](https://docs.backpack.exchange/): external OHLCV source and one-second interval.',
        '- [NYSE 2026–2028 calendar announcement](https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/): September 7, 2026 closure.',
        '- [SEC investor bulletin: performance claims](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-47): backtested results are hypothetical, not actual performance.', '']
    (directory/'report.md').write_text('\n'.join(lines))
    root=Path(__file__).resolve().parents[1]
    paths=['scripts/backtest_data.py','scripts/backtest_current.py','src/fgv_trader/strategy/fgv.py',
           'src/fgv_trader/stops.py','src/fgv_trader/entry_safety.py','src/fgv_trader/prediction.py']
    (directory/'code_hashes.json').write_text(json.dumps({p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths},indent=2))
    print(directory/'report.md')


if __name__=='__main__':
    main()
