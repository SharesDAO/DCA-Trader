# DCA Trading Bot

An automated trading bot that creates multiple wallets, distributes funds, and executes buy/sell orders for stocks across different blockchains.

## Features

- 🔗 **Multi-blockchain Support**: Ethereum, Arbitrum, Base, BNB Chain
- 🌐 **Alchemy Integration**: Reliable blockchain connectivity via Alchemy API
- 💼 **Automated Wallet Management**: Creates and manages multiple trading wallets
- 📊 **Smart Stock Assignment**: Balanced distribution of stocks across wallets
- 💰 **Profit-based Reuse**: Reuses profitable wallets, abandons consistent losers
- 🔒 **Secure Storage**: Encrypted private key storage in SQLite database
- 📈 **Position Monitoring**: Automatic selling based on profit targets or holding period
- 🧪 **Dry-run Mode**: Test your strategy without real transactions

## Architecture

```
DCA-Trader/
├── src/
│   ├── main.py              # Main entry point
│   ├── config.py            # Configuration management
│   ├── database.py          # SQLite database operations
│   ├── blockchain_client.py # Web3 blockchain interactions
│   ├── wallet_manager.py    # Wallet lifecycle management
│   ├── stock_selector.py    # Stock assignment logic
│   ├── sharesdao_api.py     # Trading API client
│   └── trade_manager.py     # Order execution and monitoring
├── config/
│   ├── config.yaml          # Main configuration
│   └── chains.yaml          # Blockchain configurations
├── data/
│   └── wallets.db           # SQLite database (auto-created)
├── logs/
│   └── bot.log              # Application logs
└── requirements.txt         # Python dependencies
```

## Installation

### Prerequisites

- Python 3.10 or higher
- An Alchemy account and API key ([Sign up here](https://www.alchemy.com/))
- A vault wallet with USDC and native token for gas fees

### Setup

1. **Clone the repository** (or you're already here!)

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**:
   
   Create a `.env` file in the project root:
   ```bash
   # Generate encryption key
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   
   # Use the output as DATABASE_ENCRYPTION_KEY
   ```
   
   Then create `.env`:
   ```env
   VAULT_PRIVATE_KEY=0xYOUR_VAULT_PRIVATE_KEY  # Vault address is derived from this
   DATABASE_ENCRYPTION_KEY=YOUR_GENERATED_KEY
   ALCHEMY_API_KEY=your_alchemy_api_key
   SHARESDAO_API_URL=https://api.sharesdao.com:8443
   ```
   
   **Note**: The vault wallet address is automatically derived from `VAULT_PRIVATE_KEY`, no need to configure it separately.

4. **Configure trading parameters**:
   
   Edit `config/config.yaml`:
   ```yaml
   blockchain: arbitrum  # Choose: ethereum, arbitrum, base, bnb
   
   trading:
     max_usd_per_wallet: 100
     min_usd_per_wallet: 10
     gas_per_wallet: 0.001  # ETH/BNB for gas fees
     order_expiry_days: 7
     min_profit: 5          # Sell when profit >= 5%
     max_hold_days: 30      # Sell after 30 days regardless
     max_loss_traders: 3    # Abandon wallet after 3 losses
     sell_slippage: 0.005   # Sell price slippage (0.5% below market)
   
   stocks:
     - AAPL
     - GOOGL
     - MSFT
     - TSLA
     - NVDA
   
   dry_run: true  # Set to false for real trading
   ```

5. **Validate configuration**:
   ```bash
   python -m src.main --check-config
   ```

## Usage

### Dry-run Mode (Testing)

Test the bot logic without executing real transactions:

```bash
# Make sure dry_run: true in config.yaml
python -m src.main --log-level INFO
```

### Production Mode

Run the bot with real transactions:

```bash
# Set dry_run: false in config.yaml
python -m src.main --log-level INFO
```

### Stop the Bot

To gracefully stop the bot, press `Ctrl+C`:

```bash
# Press Ctrl+C (sends SIGINT signal)
^C
# Output: Received signal 2, shutting down...
# Bot will stop within 1 second (fast shutdown)
```

**How it works**:
- The bot checks for shutdown signals every second
- Maximum shutdown delay: **1 second** (even during long wait periods)
- All transactions in progress will complete before shutdown
- No data loss or incomplete operations

**Alternative methods**:
```bash
# Send SIGTERM signal (same as Ctrl+C)
kill -TERM <pid>

# Or use pkill
pkill -f "python -m src.main"
```

**Note**: The bot will **NOT** wait for the full iteration cycle (10 minutes) to complete. It responds to shutdown signals immediately during sleep periods.

### Command-line Options

```bash
python -m src.main --help

Options:
  --config PATH         Path to config.yaml file
  --log-level LEVEL     Logging level (DEBUG, INFO, WARNING, ERROR)
  --check-config        Validate configuration and exit
  --wallets             Display detailed wallet information
  --show-abandoned      Show abandoned wallets in detail (use with --wallets)
  --liquidate           Liquidate all positions (sell all stocks)
  --sweep               Sweep all USDC from wallets to vault
  --dry-run             Simulate liquidation/sweep without executing
```

### View Wallet Information

Display detailed information about all wallets:

```bash
# Show all active and pending wallets
python -m src.main --wallets

# Include abandoned wallets in detail
python -m src.main --wallets --show-abandoned
```

**Output includes**:
- ✅ Active wallets with balances (USDC and ETH/BNB)
- ✅ Stock assignments and loss counts
- ✅ Current positions and pending orders
- ✅ Pending funding wallets
- ✅ Abandoned wallet count
- ✅ Vault balance

**Example output**:
```
================================================================================
WALLET INFORMATION
================================================================================
Blockchain: arbitrum
Total wallets: 5
  - Active: 3
  - Pending funding: 1
  - Abandoned: 1
================================================================================

📊 ACTIVE WALLETS
--------------------------------------------------------------------------------

1. 0x2f0d04eC4CF05589359C4617768c8355e58567c4
   Stock: TQQQ | Losses: 0/3
   Balance: $25.00 USDC | 0.001000 ETH | Orders: 1 pending

2. 0x5A8B9C2D1E3F4A5B6C7D8E9F0A1B2C3D4E5F6A7B
   Stock: NVDA | Losses: 1/3
   Balance: $0.50 USDC | 0.000800 ETH | Position: 0.5234 NVDA

3. 0x8C9D0E1F2A3B4C5D6E7F8A9B0C1D2E3F4A5B6C7D
   Stock: COIN | Losses: 0/3
   Balance: $30.00 USDC | 0.000900 ETH

────────────────────────────────────────────────────────────────────────────────
Total: $55.50 USDC | 0.002700 ETH

⏳ PENDING FUNDING WALLETS
--------------------------------------------------------------------------------

1. 0x1A2B3C4D5E6F7A8B9C0D1E2F3A4B5C6D7E8F9A0B
   Stock: GOOGL
   Current: $0.00 USDC | 0.001000 ETH
   Status: Waiting for funding retry

🗑️  ABANDONED WALLETS: 1

💰 VAULT
--------------------------------------------------------------------------------
Address: 0xfb3e9D6c54aDa8BC118A9fDC35C7dC32ef40d853
Balance: $1,234.56 USDC | 0.050000 ETH
================================================================================
```

### Liquidation & Shutdown

When you want to stop the bot and withdraw all funds:

**Step 1: Liquidate all positions**
```bash
# Sell all stock tokens at market price
python -m src.main --liquidate

# Or test first with dry-run
python -m src.main --liquidate --dry-run
```

**Step 2: Wait for sell orders to confirm**
- Run the bot normally or monitor order confirmations
- Check that all positions are closed

**Step 3: Sweep all USDC to vault**
```bash
# Transfer all USDC from wallets back to vault
python -m src.main --sweep

# Or test first with dry-run
python -m src.main --sweep --dry-run
```

This process ensures:
- ✅ All stock positions are sold at market price
- ✅ All USDC is returned to the vault wallet
- ✅ All trading wallets are marked as abandoned

**Quick Liquidation Script:**
```bash
# Use the provided script for automated liquidation
./scripts/liquidate_and_withdraw.sh
```

The script will guide you through the liquidation process step-by-step.

## How It Works

### 1. Wallet Creation & Funding
- Bot checks vault balance
- Creates new wallet with random private key and saves to database (status: `pending_funding`)
- Attempts to transfer ETH/BNB for gas fees
- Attempts to transfer USDC (between MIN and MAX)
- If funding succeeds: Updates status to `active`
- If funding fails: Wallet remains in `pending_funding` status for retry in next iteration
- Assigns a stock ticker to the wallet
- Saves encrypted private key to database

**Retry Mechanism**:
- Each iteration, bot first checks for wallets with `pending_funding` status
- Retries funding these wallets before creating new ones
- Uses the same wallet address (no new address generated)
- Skips transfers if wallet already has sufficient balance from previous attempts

### 2. Initial Buy Order
- Places limit buy order for assigned stock
- Uses all available USDC in the wallet
- Order expires after configured days

### 3. Position Monitoring
Bot continuously monitors all positions and sells when:
- **Profit target reached**: Current profit >= `min_profit` %
- **Max holding period**: Held for >= `max_hold_days` days

### 4. Wallet Lifecycle
After a sell order completes:
- **Profitable**: Reset loss count, assign new stock, reuse wallet
- **Loss**: Increment loss count
  - If loss count < `max_loss_traders`: Reuse wallet
  - If loss count >= `max_loss_traders`: Abandon wallet, return funds to vault

### 5. Continuous Operation
The bot runs in a loop:
1. Create new wallets (if vault has sufficient balance)
2. Monitor positions for sell opportunities
3. Process filled orders
4. Cancel expired orders
5. Wait for next iteration

## Database Schema

### Wallets Table
- `address`: Wallet address (primary key)
- `private_key_encrypted`: Encrypted private key
- `blockchain`: Blockchain name
- `assigned_stock`: Stock ticker
- `status`: `active`, `pending_funding`, `abandoned`
  - `pending_funding`: Wallet created but funding not yet complete (will retry next iteration)
  - `active`: Wallet fully funded and ready for trading
  - `abandoned`: Wallet retired after max consecutive losses
- `loss_count`: Number of consecutive losses
- `created_at`, `last_trade_at`: Timestamps

### Orders Table
- `order_id`: Unique order ID (primary key)
- `wallet_address`: Associated wallet
- `order_type`: buy or sell
- `stock_ticker`: Stock symbol
- `amount_usdc`: USDC amount
- `quantity`: Stock quantity
- `limit_price`: Limit price
- `status`: pending, filled, cancelled, expired
- `profit_loss`: P&L for sell orders
- `created_at`, `filled_at`, `expires_at`: Timestamps

### Positions Table
- `wallet_address`: Wallet address (primary key)
- `stock_ticker`: Stock symbol
- `quantity`: Number of shares
- `avg_buy_price`: Average buy price
- `total_cost_usdc`: Total cost
- `first_buy_date`: First purchase date
- `updated_at`: Last update timestamp

## Supported Blockchains

| Blockchain | Chain ID | USDC Address | Alchemy Support |
|------------|----------|--------------|-----------------|
| Ethereum   | 1        | 0xA0b869...  | ✅ Yes          |
| Arbitrum   | 42161    | 0xaf88d0...  | ✅ Yes          |
| Base       | 8453     | 0x833589... | ✅ Yes          |
| BNB Chain  | 56       | 0x8AC76a...  | ✅ Yes          |

## Configuration Parameters

### Trading Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_usd_per_wallet` | 100 | Maximum USDC allocated per wallet |
| `min_usd_per_wallet` | 10 | Minimum USDC allocated per wallet (≥ $5) |
| `gas_per_wallet` | 0.002 | Native token (ETH/BNB) allocated per wallet for gas fees |
| `order_expiry_days` | 7 | Days until order expires |
| `min_profit` | 5 | Minimum profit % to trigger sell |
| `max_hold_days` | 30 | Maximum days to hold position |
| `max_loss_traders` | 3 | Consecutive losses before abandoning wallet |
| `sell_slippage` | 0.005 | Slippage for LIMIT sell orders (0.005 = 0.5%) |

**Note**: `sell_slippage` only applies to LIMIT orders. MARKET orders (used in liquidation) use exact market price.

### Examples

```yaml
# Conservative: Lower slippage, wider spread
trading:
  sell_slippage: 0.001  # 0.1% below market

# Aggressive: Higher slippage, faster fills
trading:
  sell_slippage: 0.01   # 1% below market

# Default: Balanced
trading:
  sell_slippage: 0.005  # 0.5% below market
```

### Gas Per Wallet Configuration

Each new wallet receives native tokens (ETH/BNB) for gas fees. Configure `gas_per_wallet` based on your chain:

| Chain | Recommended | Min Required | Notes |
|-------|-------------|--------------|-------|
| **Arbitrum** | 0.001 ETH | 0.0005 ETH | Lowest gas costs (~$0.03/tx) |
| **Base** | 0.001 ETH | 0.0005 ETH | Low gas costs (~$0.065/tx) |
| **BNB Chain** | 0.005 BNB | 0.002 BNB | Moderate gas costs (~$0.10/tx) |
| **Ethereum** | 0.005 ETH | 0.003 ETH | Highest gas costs (~$3.90/tx) |

**Default**: `0.002` (works for all chains except Ethereum mainnet)

**Gas Management Features**:

1. **Vault Balance Check**: Before creating a new wallet, the bot checks if the vault has sufficient native tokens
2. **Auto-Refill**: If an active wallet's gas drops below 30% of the allocated amount, it's automatically refilled
3. **Gas Recovery**: When a wallet is abandoned, remaining gas (above dust threshold) is returned to the vault
4. **Pre-Transaction Check**: Before submitting any order, the bot ensures the wallet has enough gas

**Important**: 
- Each wallet needs gas to submit buy and sell orders
- Insufficient gas will cause "insufficient funds for gas" errors
- Vault must have enough native tokens to fund new wallets
- Gas is automatically managed (refilled when low, recovered when abandoned)

### Gas Management Behavior

#### 1. Wallet Creation
```
✓ Check vault has sufficient ETH/BNB (gas_per_wallet + 0.005 reserve)
✓ Transfer gas_per_wallet amount to new wallet
✓ Then transfer USDC for trading
✗ Abort if vault has insufficient gas
```

#### 2. Active Wallet Monitoring
```
✓ Before each buy/sell order, check wallet gas balance
✓ If gas < 30% of gas_per_wallet, automatically refill to full amount
✓ Refill from vault (checks vault balance first)
✗ Skip order if wallet cannot be refilled
```

#### 3. Wallet Abandonment
```
✓ Transfer remaining USDC to vault (if > $0.01)
✓ Transfer remaining gas to vault (if > 0.0005 + gas_cost)
✓ Reserve estimated gas cost (0.0001) for the transfer itself
✓ Mark wallet as abandoned
```

**Thresholds**:
- **Refill trigger**: 30% of `gas_per_wallet` (e.g., 0.0006 ETH if configured as 0.002)
- **Recovery minimum**: 0.0005 ETH + 0.0001 gas cost = 0.0006 ETH
- **Vault reserve**: 0.005 ETH (kept for vault's own transactions)

**Example Flow** (Arbitrum, gas_per_wallet: 0.002):
```
Day 1: Wallet created → receives 0.002 ETH
Day 2: Places buy order → uses ~0.0003 ETH → balance: 0.0017 ETH (OK)
Day 3: Places sell order → uses ~0.0003 ETH → balance: 0.0014 ETH (OK)
Day 5: Needs to place order → balance: 0.0005 ETH (< 30% threshold)
       → Bot auto-refills to 0.002 ETH from vault
Day 10: Wallet abandoned → returns ~0.0015 ETH to vault
```

## Security Best Practices

1. **Never commit `.env` file** - It contains your private keys
2. **Use a dedicated vault wallet** - Don't use your personal wallet
3. **Start with small amounts** - Test with minimal funds first
4. **Enable dry-run mode** - Always test logic before real trading
5. **Monitor gas fees** - Ensure vault has enough native tokens
6. **Backup database** - Regularly backup `data/wallets.db`
7. **Rotate API keys** - Periodically update Alchemy API key

## Monitoring & Logs

### Log Files

- **Current log**: `logs/bot.log`
- **Rotation**: Daily at midnight
- **Retention**: 7 days (automatically deletes older logs)
- **Archived logs**: `logs/bot.log.2026-01-11`, `logs/bot.log.2026-01-10`, etc.

### Log Rotation Details

The bot automatically manages log files:
- ✅ Creates a new log file every day at midnight
- ✅ Keeps the last 7 days of logs
- ✅ Automatically deletes logs older than 7 days
- ✅ Current logs written to `bot.log`
- ✅ Previous days archived with date suffix (e.g., `bot.log.2026-01-11`)

**Example log directory structure:**
```
logs/
├── bot.log              # Current day
├── bot.log.2026-01-11   # Yesterday
├── bot.log.2026-01-10   # 2 days ago
├── bot.log.2026-01-09
├── bot.log.2026-01-08
├── bot.log.2026-01-07
└── bot.log.2026-01-06   # 7 days ago (oldest kept)
```

**View logs:**
```bash
# Current log
tail -f logs/bot.log

# Yesterday's log
cat logs/bot.log.2026-01-11

# All logs from last 7 days
cat logs/bot.log.*
```

### Status Updates

- **Frequency**: Printed every 2 iterations
- **Metrics tracked**:
  - Active wallets
  - Total USDC deployed
  - Vault balance
  - Stock distribution
  - Total orders (buy/sell)
  - P&L
  - Win rate

## Troubleshooting

### "Failed to connect to RPC"
- Check your `ALCHEMY_API_KEY` in `.env`
- Verify network selection in `config.yaml`
- Ensure Alchemy supports your selected blockchain

### "Insufficient vault balance"
- Check vault USDC balance
- Ensure vault has native tokens (ETH/BNB) for gas fees
- Lower `min_usd_per_wallet` if needed

### "insufficient funds for gas * price + value"

**Error**: `address 0x... have 0 want 2529012876000`

**Cause**: Wallet doesn't have native tokens (ETH/BNB) to pay for gas.

**Solutions**:
1. **Check vault balance**: Ensure vault has sufficient native tokens
   ```bash
   # Check on block explorer or use web3
   # Vault needs: (number_of_wallets × gas_per_wallet) + 0.005 reserve
   ```

2. **Increase `gas_per_wallet`** in `config.yaml` if using Ethereum mainnet:
   ```yaml
   trading:
     gas_per_wallet: 0.005  # Increase for Ethereum
   ```

3. **Check logs** for gas-related warnings:
   ```bash
   tail -f logs/bot.log | grep -i "gas\|insufficient"
   ```

4. **Manual refill** if needed:
   - Bot automatically refills wallets when gas drops below 30%
   - Check logs for "low on gas" or "Refilling" messages

**Required vault balance** (examples):

| Scenario | Calculation | Total Needed |
|----------|-------------|--------------|
| 10 wallets on Arbitrum | 10 × 0.002 + 0.005 | ~0.025 ETH |
| 20 wallets on Base | 20 × 0.002 + 0.005 | ~0.045 ETH |
| 10 wallets on Ethereum | 10 × 0.005 + 0.005 | ~0.055 ETH |

**Note**: The bot will refuse to create wallets or refill gas if vault balance is insufficient.

### "nonce too low" or "nonce too high"

**Error**: `nonce too low: address 0x..., tx: 1 state: 2`

**Cause**: Transaction nonce conflicts, usually when multiple transactions are sent rapidly.

**Solutions**:
1. **Automatic fix**: The bot now uses `pending` nonce, which includes pending transactions
2. **Wait for pending transactions**: Let current transactions confirm before retrying
3. **Check transaction status** on block explorer (Etherscan/Arbiscan/etc.)
4. **Review logs** for nonce debug information:
   ```bash
   tail -f logs/bot.log | grep -i "nonce"
   ```

**How the bot handles it**:
- Uses `get_transaction_count(address, 'pending')` to include pending transactions
- Logs detailed nonce information when errors occur
- Shows both current and pending nonce counts for debugging

**Prevention**:
- ✅ Bot automatically uses correct nonce calculation
- ✅ Includes pending transactions in nonce count
- ✅ Detailed error logging for troubleshooting

### "Failed to create buy/sell order"
- Verify SharesDAO API credentials
- Check if stock ticker is tradable
- Review API rate limits

### "Database error"
- Ensure `DATABASE_ENCRYPTION_KEY` is set correctly
- Check file permissions on `data/` directory
- Backup and reinitialize database if corrupted

## Development

### Running Tests

```bash
# TODO: Add test suite
python -m pytest tests/
```

### Code Structure

Each module is independent and can be tested separately:
- `config.py`: Pure configuration loading
- `database.py`: SQLite operations with encryption
- `blockchain_client.py`: Web3 interactions
- `wallet_manager.py`: Wallet lifecycle
- `trade_manager.py`: Trading logic
- `main.py`: Orchestration layer

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## Disclaimer

⚠️ **IMPORTANT**: This bot is provided as-is for educational purposes. Cryptocurrency and stock trading involves substantial risk. You are responsible for:
- Understanding the code before running it
- Managing your own private keys securely
- Complying with local regulations
- Any financial losses incurred

The authors assume no liability for any financial losses or damages.

## License

MIT License - See LICENSE file for details

## Support

For issues, questions, or contributions:
- Open an issue on GitHub
- Review the logs in `logs/bot.log`
- Check configuration with `--check-config`

---

**Happy Trading! 🚀**
