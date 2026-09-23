# FGV trading

The default strategy is now `fgv`. Backpack supplies external stock candles and
provider-derived live quotes. Orders still execute through SharesDAO on the
configured blockchain, using the existing vault and encrypted trading wallets.
Every FGV buy and sell is a **MARKET** order.

## Run

```bash
python -m venv venv
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m src.main --check-config
venv/bin/python -m src.main --dry-run
```

The checked-in configuration has `dry_run: true`. Paper trading needs Internet
access to Backpack and SharesDAO's pool directory, but no private key, Alchemy
key, or Alpaca credentials. It uses `data/fgv-paper.db`, starts with $1,000, and
persists simulated balances across restarts. Fills use fresh reference prices
with the configured 5 bps simulated slippage. It does not model settlement
latency, gas costs, or market liquidity.

`--once --dry-run` runs a single cycle; an operation that needs another cycle
remains journaled for the next run. Use the continuous command for a full paper
session. `--status` or `--wallets` displays persisted state without contacting
market-data or blockchain services. `--show-abandoned` and `--abandoned-only`
include retired wallets.

Live execution requires the existing `VAULT_PRIVATE_KEY`,
`DATABASE_ENCRYPTION_KEY`, and `ALCHEMY_API_KEY` environment variables, funded
vault balances, and `dry_run: false`. `--dry-run` always selects the separate
paper ledger, including for maintenance. Do not run the old DCA process against
the same wallet database while FGV is running.

## Strategy parity

The following source modules were copied from `../fgv-trader/src/fgv_trader`
without logic changes: `models.py`, `strategy/fgv.py`, `features.py`,
`prediction.py`, `portfolio/risk_manager.py`, and `time_utils.py`.
The original fitted `config/win_probability_model.json` is included unchanged.
There is no dependency on the sibling checkout at runtime.

The `fgv` configuration reproduces the source configuration: 1.5R target,
1% minimum C2 range, the original first-hour 1–2% risk-band exclusion,
45% probability threshold and the same three model features,
$5 reserve/minimum, the configured concurrent-position limit, and one
symbol trade per session. Candidates are sorted by descending probability,
then symbol. Stops and targets retain the original signal anchors after fills;
they are not recalculated from execution prices.

Sizing intentionally differs from the source: `fgv.initial_usdc_per_wallet`
(currently 50) is the base order size. Confidence sizing linearly maps the
configured minimum qualified probability (currently 45%) to the configured
minimum multiplier (0.50x) and `confidence_sizing.max_probability` (currently
the model's 95% hard ceiling) to the configured maximum multiplier (1.50x), then
rounds to the nearest whole USDC. No dollar allocation is hardcoded. With
`fgv.prefund_wallets: true`, the bot
prepares capacity before selecting stocks, up to `max_concurrent_positions`.
New ready wallets hold the maximum configured allocation so confidence sizing
does not add an entry-time funding transfer. A buy spends its exact allocation
and keeps unused USDC isolated in the busy wallet until the position closes.
Reused wallets are capped by their available balance and never receive USDC
top-ups. The retained
`allocation_pct_per_trade` and `risk_pct_per_trade` settings no longer determine
funding. New funding preserves the vault's configured USDC reserve. Idle wallets
below `min_order_usdc` are skipped. If the vault cannot fund the complete
maximum-allocation pool, the smaller ready capacity is used and automatically expanded
when funds permit.

Prefunding uses durable maintenance trades and `fund`/`gas` operations. New
wallets remain `pending_funding` and cannot be selected until both confirmed;
terminal preparation failure changes them to `prefund_failed` for inspection.
Restarts resume the same signed operations and reserve their outstanding USDC,
so a crash cannot create a duplicate top-up. A wallet is labeled `Ready` before
selection, assigned a ticker only when selected, and its buy operation is created
immediately without per-entry fund/gas operations. After a trade becomes terminal,
the idle wallet is prepared for reuse by topping up gas only; its remaining USDC
is never replenished. A qualified candidate is deferred rather than assigned an
unready wallet. Set `prefund_wallets: false` to restore on-demand funding.

Execution safety intentionally extends the original strategy: unsigned entries
expire 300 seconds after selection or at the session cutoff, whichever comes
first. With `execution.allow_entry_below_original_stop: true`, buy quotes may be
below the original stop but must remain strictly above the frozen emergency risk
floor and meet `execution.min_entry_reward_risk` (currently 1.4). If stop-policy
history is unusable, the emergency floor falls back to the original stop, so no
deeper entry is allowed. Quotes older than
`execution.max_execution_quote_age_seconds` (default 5) are rejected. These checks
also run after funding. `ENTRY_ABOVE_TRIGGER` keeps the same unsigned buy pending
and retries a fresh quote on the normal reconciliation cycle (configured 5 seconds,
plus request/processing time). It keeps the wallet and position slot reserved and
preserves the original deadline across restarts. Other safety rejections are
terminal; expiry or session cutoff stops these retries. No new trade, wallet, or
nonce is created for a rejected quote, and historical failed entries are not reopened.
Market fills can still differ from the checked quote. A terminally rejected symbol
remains reserved for that session under the existing one-attempt rule.

`execution.max_entry_above_trigger_r` permits a small execution buffer above the
signal trigger, expressed as a fraction of original R. The configured `0.05`
means a hard ceiling of `trigger + 0.05R`. The separate reward/risk gate remains
authoritative and is measured to the original stop for quotes above that stop;
with a 1.5R target and the configured 1.4 minimum, its practical ceiling is about
`trigger + 0.0417R`. Quotes beyond either ceiling remain `ENTRY_ABOVE_TRIGGER` or
`ENTRY_REWARD_RISK_TOO_LOW`. Each buy operation records `entry_above_trigger` and
`entry_buffer_r_used` for audit.
The source's fixed New York schedule is retained: 09:30 opening,
09:45 first range completion, 11:30 entry cutoff, and 15:45 forced exit.
Named time zones handle DST. Automatic holiday/early-close scheduling was
deliberately not introduced because it would change the source schedule.

Normal positions that are losing at the entry session's 15:45 cutoff use the
configured `execution.losing_time_exit_max_sessions` extension (currently three
additional weekday sessions). Their original take-profit remains active. At each
intermediate 15:45 cutoff, a recovered position is sold; a still-losing position
continues until the final cutoff. The third additional session forces liquidation
regardless of profit. The extension is not granted to buys settled after their
entry-session cutoff, positions without a fresh reference price at the initial
cutoff, explicit liquidation, or positions already exiting for a stop or target.
The weekday count follows the existing fixed schedule and does not add a holiday
calendar.

### Confirmed, volatility-buffered live stops

**Current configuration: `stop_policy.exits_enabled: false`.** This global switch
disables original, confirmed/buffered and emergency stop-loss exits for both
existing and new positions. Take-profit, liquidation/time exits, entry-price
floor and entry reward/risk filters remain unchanged. The stored stop levels
remain entry-selection references; with exits disabled they do not limit losses.
Analytics continue and record whether stop exits were enabled. Pending exits are
not cancelled, resized or reopened by this switch, and signed transactions still
reconcile. Restart the bot after changing the setting.

`stop_policy.enabled` is a separate setting controlling creation of buffered
levels. Setting it to false alone would restore original immediate stops, **not**
disable stop-loss exits. The description below applies when `exits_enabled` is true.

`stop_policy` now enables an explicit exit-policy deviation from the copied FGV
strategy for **new entries**, in live and paper mode. Existing trades without an
enabled frozen policy retain their original immediate stop. Changing configuration
does not move the stops of an existing trade, cancel a pending exit, or rewrite a
signed transaction. The probability model remains the original model and has not
been recalibrated for these exits.

At selection, R is the original trigger minus the original c1-low stop. The bot
averages the true ranges of up to 14 completed, contiguous, same-session 5-minute
bars (at least 3 true ranges / 4 candles). This is a simple mean, not Wilder's ATR.
True range includes gaps from the preceding close. The buffer is
`min(0.25 * ATR, 0.25 * R)`. The confirmed stop is original stop minus buffer;
the emergency stop is confirmed stop minus another `0.25 * R`. Configuration
validation caps the combined extension at `0.5 * R`. These levels and the ATR
inputs are persisted at entry and never widened later. Unusable/insufficient
history, a gap, or a last completed bar ending over 420 seconds ago means fallback
to the original immediate stop, without a buffer or confirmation delay.

A confirmed exit requires two distinct fresh feed observations below the buffered
stop, counted at least 5 seconds apart. A price at/above that stop, missing fresh
prices, or a gap over 15 seconds between evaluated fresh observations resets the
sequence. The same cached timestamp never counts twice; the confirmation state
survives restart subject to the same gap limit. A price at/below the emergency
stop initiates an immediate market exit. Take-profit and the 15:45 force-exit
remain immediate. Emergency is a software trigger, **not a guaranteed execution
price or maximum realized loss**, and depends on feed and chain availability.
Backpack event timestamps identify fresh messages, not necessarily new underlying
exchange trades. Moves between samples can be missed.

When `execution.allow_entry_below_original_stop` is enabled, the frozen emergency
stop replaces the original stop as the entry-price floor. The execution
reward-to-risk gate also measures risk to that emergency floor. This can admit a
deeper pullback that the original setup considered broken. Because stop exits may
be disabled, the floor is an entry check, not a post-entry loss bound. Wallet
spending is unchanged, but deeper entries can increase realized losses. The
parameters are starting settings, not demonstrated improvements.

### Stop comparison data

With `stop_policy.record_observations: true`, the live DB's
`fgv_stop_observations` table records fresh reference-price samples on each normal
bot cycle, deduplicated by trade and feed timestamp. Paper uses its separate DB.
Recording continues for today's entered trades **after a sale** through the
15:45 cutoff, with one minute of grace to capture a nearby cutoff sample. There
is no historical backfill. If quotes are unavailable or the process is stopped,
the missing intervals remain gaps, not invented prices. Recording errors are
logged and do not block execution/reconciliation.

Each row has trade ID, observed time, quote timestamp, price and JSON payload:
position state, quantity/cost, provisional-cost flag, realized PnL, original stop,
target, frozen policy/ATR, confirmation state, source and actual stop subtype.
`STOP_LOSS` remains the accounting reason for both confirmed and emergency exits;
`stop_exit_detail.subtype` distinguishes them. Join on `fgv_trades.id` and
`fgv_operations.trade_id` to get actual quantities, proceeds, gas, and settlements.
This allows subsequent comparison of immediate-original, confirmation-only,
buffer-only and combined stops on the same sampled path, including recovery,
maximum adverse excursion and reference-value outcomes near 15:45. Hypothetical
exits are not executable fills or measured net profits; actual PnL and gas must
be analyzed separately, with missing coverage disclosed. No automatic strategy
optimization or stop widening is performed from these records.

Read-only inspection example (does not expose wallet keys or signed payloads):

```sql
SELECT t.symbol, t.session, COUNT(*) AS samples,
       MIN(o.observed_at) AS first_sample, MAX(o.observed_at) AS last_sample,
       MIN(o.price) AS min_reference, MAX(o.price) AS max_reference
FROM fgv_stop_observations o JOIN fgv_trades t ON t.id = o.trade_id
WHERE t.chain = 'arbitrum'
GROUP BY t.id;
```

Execution/data differences are explicit:

- Backpack external OHLCV and last-price references replace Alpaca's bars/latest
  trades. Identical rules can therefore produce different signals and fills.
- Only completed, contiguous candles and fresh quotes may initiate entries.
  Missing/stale data is treated as unavailable rather than as a price signal.
- The existing stock allowlist limits the entry universe. SPY is also fetched
  for feature calculation. Existing holdings remain monitored if removed from
  the allowlist.
- New entries require pool `mint_mode` equal to `3`. Other modes and missing
  values are excluded, even with `stocks: []` or an explicit ticker allowlist.
  Pool metadata for existing holdings/orders remains available for settlement
  and exits, including when no pools qualify for new entries.
- Capital sizing uses currently available custody cash, excluding wallets with
  active trades/operations and vault funding reservations. Pending entries
  consume the configured position limit. A symbol/session is reserved when a
  ready wallet is assigned, preventing duplicate orders after restart.
- Prefunded wallets remove funding from the entry-critical path. A confirmed buy opens a position; order
  submission alone does not. A sell closes only after verified settlement.
- Paper balances are actual simulated balances, not the source application's
  constant $1,000 return value. The sizing formula itself is unchanged.

## Market data

Backpack REST candles use `/api/v1/klines` with `source=External`,
`priceType=Last`, and `5m` intervals. The opening 15m range is aggregated from
the first three completed 5m candles. The default mapping is
`TSLA` → `TSLA.US_USDC`; overrides live in `market_data.symbol_map`. RFQ and
perpetual market symbols must not be substituted for the external stock feed.
Responses are normalized to UTC and cached incrementally, with up to
`candle_workers` concurrent requests (default 8). Completed coverage is reused.
Scans check continuity against the snapshot's captured time, not the later
scan time, and reject snapshots older than `max_candle_snapshot_age_seconds`
(default 120). Strategy thresholds and session hours are unchanged.

Live reference prices use `externalTicker.TSLA.US_USDC` WebSocket subscriptions,
with reconnect/backoff and a silent-stream timeout. Missing/stale prices use
one `/api/v1/tickers?source=External` batch REST request, checked every
`price_poll_interval_seconds`. WS age uses event time; REST age uses request
start time. Neither proves underlying trade freshness or executable price.
`max_price_age_seconds` limits feed age, not underlying trade age.
Minute status logs summarize feed coverage and entry rejection reasons.

Optional fallback: install `alpaca-py`, set `ALPACA_API_KEY` and
`ALPACA_API_SECRET`, and enable `market_data.alpaca_fallback`. Candle fallback
is selected only before any bars for that symbol/session have been accepted;
an established session never silently switches candle providers. Stale live
quotes may use timestamped Alpaca trades. Provider attribution is logged.

If both price sources are unavailable, new entries and price-triggered exits
wait for usable data. Time-triggered/live liquidation exits do not require a
Backpack quote; the broker obtains a SharesDAO execution quote. Paper fills
always require a fresh simulated reference price.

API reference: <https://docs.backpack.exchange/>

## Execution and recovery

`trading.max_loss_traders` also applies to FGV (default 2). Losses are cumulative
per wallet, matching the legacy implementation, and count only fully closed
trades with negative realized USDC P&L, excluding native gas. Wins do not reset
the count; partial exits and unfilled/cancelled buys do not count. Historical
closed FGV trades are counted once on upgrade, with a transactional marker to
prevent recounting on restart.

At the limit, a wallet becomes `retiring` and cannot accept new entries. Existing
positions and pending operations finish first. Journaled cleanup then tops up
gas if needed, sweeps all USDC, and collects recoverable native currency in that
order, waiting for confirmation between steps. Gas costs can leave native dust.
Only completed cleanup changes the status to `abandoned`. Failed cleanup leaves
the wallet quarantined in `retiring` for inspection. Paper mode follows the same
lifecycle in its isolated ledger. With prefunding enabled, replacement capacity
is created before selection rather than on demand after a signal.

Each new buy spends the assigned trading wallet's entire USDC balance, allowing
fractional shares. New wallets receive the configured initial funding; existing
wallets receive no USDC top-up. Existing excess USDC is also spent, so actual
allocation can exceed the new-wallet amount. The actual budget is fixed
and journaled before signing; submitted orders are never resized. Paper trading
uses the same whole-wallet rule. Native ETH for gas is kept separate.

New sell orders offer the wallet's entire on-chain stock-token balance using the
exact integer returned by `balanceOf`, including token dust. The transfer and
MARKET memo use the same integer, persisted as `amount_units` before broadcast.
Floating-point share counts are for reporting, never used to reconstruct the
sell transfer. Signed/submitted orders keep their original payload on retry.
Zero token balance does not imply a fill; it remains unresolved for inspection.

```text
FUNDING → BUY_PENDING → OPEN → SELL_PENDING → CLOSED
```

Failed/refunded buys become `FAILED`; they do not create positions. Partial
terminal sells reduce the position and allocate cost proportionally. The exit
intent remains active until the remainder settles. Funding/gas transfers,
orders, and sweeps have their own operation journal.

The live broker signs a transaction, saves its exact bytes/hash, then
broadcasts. A timeout/restart rebroadcasts the same signed bytes, never a new
order nonce. One writer per database is enforced with a process lock. A
transaction's successful receipt confirms submission, not a stock trade fill.

Settlement queries SharesDAO `/transaction/pool` for terminal records matching
both the customer ID and submission hash. It then verifies the settlement
receipt and incoming ERC-20 transfers to the assigned wallet after the
configured confirmation depth. Unrelated cash, missing records, incomplete
refunds, or ambiguous partial settlement remain pending; they never release a
wallet for another trade. `execution.transaction_history_limit` controls how
many recent pool records are queried (default 1,000). Older/unresolved records
require investigation rather than automatically declaring a fill.

With `execution.chain_settlement_detection: true`, sells first scan confirmed
USDC transfer logs in bounded 2,000-block ranges. A payout must originate from
the configured pool burn address and carry a matching symbol, customer ID,
SELL side, and COMPLETED status in its transfer memo. The receipt and actual
incoming amounts are still verified. This bypasses cached history for validated
sell payouts. Buy delivery monitoring also scans confirmed token mint logs. It
requires the expected token contract, wallet recipient, configured pool mint
authority, a successful `mint(address,uint256)` call after submission, and exact
agreement between integer calldata and receipt amounts. It runs only when that
wallet has exactly one pending buy. Transaction hashes deduplicate deliveries
across restarts; unrelated deposits and unconfirmed transactions do not count.

Verified delivered shares immediately become monitored positions, even while
the buy remains `SUBMITTED` awaiting final API reconciliation. Stops, targets,
and forced exits can sell those shares without waiting for the API cache. Until
completion/refunds are resolved, cost basis is provisional (the entire offered
USDC is conservatively assigned to delivered shares), and the wallet remains
reserved even if all currently delivered shares have been sold. Late partial
deliveries reopen protection; cumulative quantities and realized cost are
recomputed rather than counting deliveries twice. The dashboard labels this
reconciliation state. If observed delivery exceeds the matched completion,
the wallet stays reserved for investigation.

Buy mints lack customer-ID memos, so this proves asset delivery, not full order
completion. SharesDAO remains a fallback for final/ambiguous completion and
refunds. Its separate 300-second history cache may delay final accounting and
wallet reuse, but no longer delays protection of verified delivered stock.

New operations record decision price/time, execution quote/time, signing and
broadcast attempts, on-chain submission and settlement times, and recognition
time/lag. Pending operations older than `execution.stuck_order_alert_seconds`
(default 60) emit a warning at most once per minute per operation and appear in
dashboard warnings. Alerts are local logs/dashboard, not external notifications.
Old unsigned entries inherit their original age on restart, never a fresh
deadline. Signed/submitted orders always continue receipt reconciliation.

Realized P/L uses actual verified USDC proceeds and cost. Native gas usage is
journaled separately and is not converted into USDC P/L. The `limit_price`
column in the legacy compatibility table contains an average executed price;
it does not imply a LIMIT order. Signed payloads are excluded from CLI output.

## Existing wallets and maintenance

Existing database tables and encrypted keys are preserved. FGV creates
additional `fgv_*` tables and mirrors settled holdings into the original
positions/orders tables. It reuses eligible idle wallets without USDC top-ups.
Legacy random $80–$100 allocation is unused; FGV uses fixed new-wallet funding
and the cumulative loss retirement policy described above.

Existing DCA holdings/orders continue through the legacy reconciler, which is
isolated from FGV records and cannot initiate new DCA buys. No FGV stops are
invented for those holdings. Their original balance-based settlement/P&L
limitations remain until they wind down.

```bash
# Selects paper or live DB according to dry_run in the configuration:
venv/bin/python -m src.main --status
venv/bin/python -m src.main --liquidate
venv/bin/python -m src.main --sweep
venv/bin/python -m src.main --collect-eth
venv/bin/python -m src.main --delete-unfunded
venv/bin/python -m src.main --resume-entries
```

Stop the running process before running a maintenance writer. `--liquidate`
persists an entry pause, cancels unsubmitted entry work, monitors existing
submissions, and requests market exits until all positions settle. Pending
orders are never "cancelled" merely by editing a local status. `--sweep` moves
idle USDC to the vault and journals the transfer. `--collect-eth` collects
native tokens from idle wallets below the USDC threshold, leaving maximum gas
cost for the transfer. `--delete-unfunded` removes only pending-funding wallet
records with zero checked balances and no positions/pending operations.
`--resume-entries` clears the persistent liquidation flag only after settlement;
`liquid_mode` must also be false.

The `dca` strategy remains available for legacy operation, but switching back
while FGV positions or journaled operations exist is not supported.

## Verification

```bash
venv/bin/python -m pytest -q
venv/bin/python -m src.main --check-config
venv/bin/python -m src.main --once --dry-run
```

Tests include the source strategy/feature/model tests, Backpack normalization
and freshness, candidate ranking/sizing, paper round trips, restart recovery,
market-order memos, signed-transaction replay, refunds, and partial exits.
Historical five-minute Backpack data was read successfully for all eight
configured symbols plus SPY. The SharesDAO terminal-record endpoint was also
checked read-only. No live orders were submitted during development. A regular
trading-session quote/settlement trial and feed-specific model/backtest
comparison are still needed before drawing conclusions about live performance.
