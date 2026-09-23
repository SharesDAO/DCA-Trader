"""Compare preserved baseline outputs with a stop-exits-disabled simulation."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def money(value):
    return ('-' if value<0 else '')+f'${abs(value):,.2f}'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    root=args.directory
    output=root/'no_stop_exits'
    old=json.loads((root/'results.json').read_text())
    new=json.loads((output/'results.json').read_text())
    baseline={r['assumptions']['name']:r for r in old}
    name='wallet50_5bps_fee0_delay15'
    original=baseline[name]
    changed=next(r for r in new if r['assumptions']['name']==name)
    with (output/(name+'_trades.csv')).open() as handle:
        trades=list(csv.DictReader(handle))
    assert all(t['exit_reason'] in ('TAKE_PROFIT','FORCE_EXIT') for t in trades)
    assert not changed['assumptions']['stop_exits_enabled']
    assert abs(sum(float(t['net_pnl']) for t in trades)-changed['net_profit'])<1e-6
    worst=min(trades,key=lambda t:float(t['net_pnl'])/float(t['cost']))
    worst_pct=float(worst['net_pnl'])/float(worst['cost'])*100
    lines=['# Backtest comparison: no stop-loss exits','',
           'July 10–September 9, 2026; $10,000 initial cash; current $50 funding per new '
           'wallet; 10 maximum simultaneous reservations. Same frozen universe, model, '
           'historical data, entry filters, ranking, wallet reuse and retirement rules as the baseline.','',
           '**Changed only:** confirmed/buffered and emergency stop exits are disabled. '
           'Take-profit and 15:45 forced liquidation remain. Entry stop-floor and reward/risk '
           'checks are deliberately retained to isolate the exit change. Without stop exits, '
           'their reference risk distance is a selection heuristic, not a bound on realized loss.','',
           '## Baseline cost assumptions','',
           'Adverse slippage 0.05% per side, 15-second buy delay, 10-second sell delay, '
           'before unmeasured gas/exchange costs.','',
           '| Metric | Current stops | No stop exits |','|---|---:|---:|']
    for label,key,formatter in [
        ('Ending equity','ending_equity',money),('Profit/loss','net_profit',money),
        ('Portfolio ROI','roi_pct',lambda v:f'{v:.3f}%'),
        ('Closed trades','trades',str),('Wins','wins',str),('Losses','losses',str),
        ('Win rate','win_rate_pct',lambda v:f'{v:.2f}%'),
        ('Maximum sampled equity drawdown','max_sampled_drawdown_pct',lambda v:f'{v:.3f}%'),
        ('Peak position cost deployed','peak_deployed',money),
        ('Average position cost deployed','average_deployed',money),
        ('Wallets created','wallets_created',str),('Wallets retired','wallets_retired',str)]:
        lines.append(f'| {label} | {formatter(original[key])} | {formatter(changed[key])} |')
    lines+=['',f'Change in portfolio profit: **{money(changed["net_profit"]-original["net_profit"])}**.',
            f'No-stop exit counts: {json.dumps(changed["exit_reasons"],sort_keys=True)}.',
            f'Worst realized no-stop trade by percentage: **{worst["symbol"]} on {worst["session"]}, '
            f'{worst_pct:.2f}% ({money(float(worst["net_pnl"]))})**. This is an observed sample loss, '
            'not a maximum possible loss.','',
            '## Cost and delay comparisons','',
            '| Slippage / side | Fee / side | Buy delay | Current-stop P&L | No-stop P&L | No-stop ROI |',
            '|---:|---:|---:|---:|---:|---:|']
    for r in new:
        a=r['assumptions'];b=baseline[a['name']]
        lines.append(f'| {a["slippage_bps"]} bps | {money(a["fee_per_side"])} | {a["buy_delay_seconds"]} s | '
                     f'{money(b["net_profit"])} | {money(r["net_profit"])} | {r["roi_pct"]:.3f}% |')
    lines+=['','Each scenario is a full portfolio rerun. Longer holding periods change wallet '
            'availability, subsequent funding amounts and sometimes which entries fit within '
            'the position limit. This is not a fixed-size, paired-trade comparison.','',
            '## Important limitations','',
            '- Most of the $10,000 remains idle under the current $50-wallet setting. '
            'Small portfolio drawdowns do not imply individual positions have little risk.',
            '- The model was fitted using observations through August 28, 2026, overlapping '
            'most of the tested period. This is a retrospective simulation, not independent out-of-sample validation.',
            '- Historical Backpack reference bars do not reconstruct actual SharesDAO RFQs, '
            'fees, token liquidity, partial settlement or live feed delivery. Sparse 1-second '
            'bars can cause freshness gaps that differ from live ticker behavior.',
            '- No-stop exits can expose positions to substantially larger losses before the time exit. '
            'The observed result is not a return forecast or a recommendation to disable live protection.',
            '- Live trading settings and databases were not changed.','',
            '## Outputs and reproduction','',
            f'- [No-stop trade ledger]({name}_trades.csv)',
            f'- [No-stop daily equity]({name}_daily.csv)',
            '- [No-stop scenario results](results.json)',
            '- [Original report and full assumptions](../report.md)','',
            '```sh',
            'venv/bin/python scripts/backtest_current.py --directory reports/backtests/2026-07-10_2026-09-09 --no-stop-exits',
            'venv/bin/python scripts/report_no_stop.py reports/backtests/2026-07-10_2026-09-09',
            '```','']
    (output/'report.md').write_text('\n'.join(lines))
    project=Path(__file__).resolve().parents[1]
    (output/'code_hashes.json').write_text(json.dumps({p:hashlib.sha256((project/p).read_bytes()).hexdigest()
        for p in ['scripts/backtest_current.py','scripts/backtest_data.py','scripts/report_no_stop.py']},indent=2))
    print(output/'report.md')


if __name__=='__main__':
    main()
