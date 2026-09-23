# Trading dashboard

Run independently of the trading bot:

```bash
venv/bin/python -m src.dashboard --port 8080
```

Open http://127.0.0.1:8080. For a remote server, use an SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 your-user@your-server
```

The dashboard binds to loopback by default. To expose it on all IPv4 interfaces:

```bash
venv/bin/python -m src.dashboard --host 0.0.0.0 --port 8080
```

It has no authentication: anyone able to reach port 8080 can view balances and
trading activity. Restrict access to trusted networks using your firewall or VPN;
do not publish it on the public internet without authentication. All routes are read-only; SQLite is
opened with `mode=ro` and never initialized or migrated. It runs separately and
cannot submit orders, transfer funds, decrypt wallet keys, or restart the bot.
The existing configuration loader reads environment settings for RPC access;
private keys, API credentials, raw signed transactions, and raw logs are not
included in API responses. No additional dependencies are required.

Live and paper views use separate databases. Snapshots refresh every 10 seconds
and use a shared per-mode cache. Live balances are read directly through RPC;
paper balances come from the simulation ledger. Balance lookup failure is not
displayed as zero. Available USDC is cash, not total equity. Realized P&L covers
all recorded FGV sessions and excludes native-token gas. No unrealized valuation
is inferred from missing prices. Every data table has independent pagination with
10, 25, 50, or 100 rows per page. Order and execution history include all stored
records rather than truncating the API response to the latest 200.

Unrealized P&L uses remaining position quantity × Backpack external last price
minus remaining cost basis. It includes open positions and pending sells until
settlement, but excludes unfilled buys. The dashboard fetches public batch ticker
prices for each uncached snapshot. This is an indicative valuation, excluding
fees, gas, and execution slippage; the displayed fetch time is not the underlying
trade timestamp. Missing prices show unavailable values, and the aggregate is
unavailable if any position cannot be valued.

Wallet count includes all recorded trading wallets in the selected mode and chain,
including retiring and abandoned wallets, with a status breakdown. It excludes
the vault and deleted wallet records, so it is not an audit of deleted history.
The visible wallet table hides abandoned wallets and labels unassigned prefunded
wallets as `Ready`; totals still retain every recorded status.

Performance cards count fully closed FGV trades across all sessions in the selected
mode/chain, not individual buy/sell orders. Wins and losses use positive/negative
realized USDC P&L, excluding native gas; zero P&L is breakeven. Win rate is wins
divided by all closed trades, including breakeven trades. Take-profit and stop-loss
counts use recorded exit reasons, which need not match profitability because fills
can differ from trigger prices. Other exits include time/forced exits. Open trades,
partial exits still awaiting final closure, failed entries, and maintenance are
excluded. An empty history shows no win rate, rather than a misleading percentage.

The daily performance chart groups realized profit by the sell settlement date
in the configured exchange timezone. Its total-value line is a reconstructed
realized-equity series anchored to the current on-chain USDC plus current open
market value. Current unrealized P&L is removed from the historical anchor, since
historical wallet/position snapshots were not recorded. Consequently the line is
useful for strategy changes over time but is not an accounting ledger of deposits,
withdrawals, native gas, or historical intraday/unrealized value.

Health reflects `dca-fgv-live.service` and its latest periodic diagnostic log,
not the paper process. The health timestamp uses server local time; order times
are converted from UTC to the browser timezone. Health older than 3 minutes
raises a warning. Failed HTTP refreshes retain the last view with a stale-data
warning. Strategy schedule uses the configured exchange timezone.

Execution diagnostics show decision price, execution quote, whether an accepted
buy used the above-trigger buffer and its fraction of R, on-chain submission,
settlement, recognition lag, and entry-rejection codes. Enabled buffered-stop
positions display the confirmation threshold plus the immediate emergency level;
legacy positions retain a single original stop. When `stop_policy.exits_enabled`
is false, the stop column displays "Disabled" and a warning explains that both
normal and emergency stop exits are off. Historical missing fields
are left blank, not reconstructed. Operations pending beyond the configured
stuck-order threshold produce dashboard warnings. Raw transactions, credentials,
and provider exception messages are never returned by this view.

On-chain stock delivery can appear as an open position while its buy order is
still pending final reconciliation. The dashboard warns that cost/P&L is provisional
until completion and refunds are resolved. The wallet remains reserved during
this period, including when currently received shares have already been sold.
