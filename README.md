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
   VAULT_PRIVATE_KEY=0xYOUR_VAULT_PRIVATE_KEY
   DATABASE_ENCRYPTION_KEY=YOUR_GENERATED_KEY
   ALCHEMY_API_KEY=your_alchemy_api_key
   BNB_RPC_URL=https://bsc-dataseed.binance.org/
   SHARESDAO_API_URL=https://api.sharesdao.com:8443
   ```

4. **Configure trading parameters**:
   
   Edit `config/config.yaml`:
   ```yaml
   blockchain: arbitrum  # Choose: ethereum, arbitrum, base, bnb
   vault_address: "0xYOUR_VAULT_ADDRESS"
   
   trading:
     max_usd_per_wallet: 100
     min_usd_per_wallet: 10
     order_expiry_days: 7
     min_profit: 5          # Sell when profit >= 5%
     max_hold_days: 30      # Sell after 30 days regardless
     max_loss_traders: 3    # Abandon wallet after 3 losses
   
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

### Command-line Options

```bash
python -m src.main --help

Options:
  --config PATH         Path to config.yaml file
  --log-level LEVEL     Logging level (DEBUG, INFO, WARNING, ERROR)
  --check-config        Validate configuration and exit
```

## How It Works

### 1. Wallet Creation
- Bot checks vault balance
- Creates new wallet with random private key
- Transfers random amount of USDC (between MIN and MAX)
- Assigns a stock ticker to the wallet
- Saves encrypted private key to database

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
- `status`: active, abandoned, pending
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
| BNB Chain  | 56       | 0x8AC76a...  | ⚠️ Use custom RPC |

## Security Best Practices

1. **Never commit `.env` file** - It contains your private keys
2. **Use a dedicated vault wallet** - Don't use your personal wallet
3. **Start with small amounts** - Test with minimal funds first
4. **Enable dry-run mode** - Always test logic before real trading
5. **Monitor gas fees** - Ensure vault has enough native tokens
6. **Backup database** - Regularly backup `data/wallets.db`
7. **Rotate API keys** - Periodically update Alchemy API key

## Monitoring & Logs

- **Log file**: `logs/bot.log`
- **Status updates**: Printed every 10 iterations
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
- For BNB, ensure `BNB_RPC_URL` is set

### "Insufficient vault balance"
- Check vault USDC balance
- Ensure vault has native tokens for gas fees
- Lower `min_usd_per_wallet` if needed

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
