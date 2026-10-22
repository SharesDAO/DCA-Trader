# Stock tokens, underlying shares, and P&L

Live FGV trade quantities, settlement quantities, and on-chain balances remain in
wallet token units. Historical records are not rewritten. Actual shares are:

    actual_shares = token_quantity / token_per_share
    market_value = actual_shares * current_share_price
    unrealized_pnl = market_value - remaining_cash_cost

The dashboard obtains current pool ratios from the configured SharesDAO API for
the selected network, separately from Backpack's per-share reference prices. It
exposes `actual_shares`, the original `token_quantity`, and `token_per_share`.
The Shares column shows actual shares; hover for the wallet-token conversion.
Portfolio value and daily performance use the same corrected market value.
Missing or invalid ratios make valuation and totals unavailable instead of
assuming one token is one share. An explicitly returned legacy pool without a
ratio field retains the API's historical default of 1. Invalid explicit values
are never treated as 1. Paper positions are already shares and are not converted.

Live order preparation refreshes the ratio with the execution price. Buy memo
requests convert actual shares into tokens; sell transfer amounts stay in exact
wallet base units, and estimated proceeds use underlying shares. Existing signed
operations keep their original bytes and are not resized or resubmitted. The
FGV losing-position time-exit comparison also values actual shares.

Legacy DCA quantities/average costs remain per token. Its order prices and
portfolio valuation convert current per-share quotes to per-token prices before
using those quantities. Actual cash cost basis and recorded realized proceeds
are not divided by the ratio. Existing historical accounting/settlement errors
require a separate transaction audit; this change does not infer compensation.

Restart the dashboard to apply valuation changes and restart the bot for updated
order estimates. No database migration or token deployment is required.
