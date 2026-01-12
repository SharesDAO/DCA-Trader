"""
Main entry point for DCA Trading Bot.
Orchestrates wallet creation, order placement, and position monitoring.
"""

import asyncio
import logging
import signal
import sys
import argparse
from pathlib import Path

# Import modules
from config import load_config
from database import init_database
from blockchain_client import create_blockchain_client
from stock_selector import create_stock_selector
from wallet_manager import create_wallet_manager
from sharesdao_client import create_sharesdao_client
from trade_manager import create_trade_manager

# Setup logging
def setup_logging(log_level: str = 'INFO'):
    """
    Setup logging configuration with daily rotation.
    
    Logs are rotated daily at midnight and kept for 7 days.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    from logging.handlers import TimedRotatingFileHandler
    
    # Create logs directory
    project_root = Path(__file__).parent.parent
    log_dir = project_root / 'logs'
    log_dir.mkdir(exist_ok=True)
    
    # Configure logging with rotation
    log_file = log_dir / 'bot.log'
    
    # Create rotating file handler
    # - when='midnight': Rotate at midnight
    # - interval=1: Rotate every 1 day
    # - backupCount=7: Keep 7 days of logs
    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when='midnight',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )
    file_handler.suffix = '%Y-%m-%d'  # Log files: bot.log.2026-01-11
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    
    # Set format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        handlers=[file_handler, console_handler]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized - Level: {log_level}")
    logger.info(f"Log file: {log_file} (rotates daily, keeps 7 days)")



logger = logging.getLogger(__name__)


class TradingBot:
    """Main trading bot orchestrator."""
    
    def __init__(self, config_path: str = None):
        """
        Initialize trading bot.
        
        Args:
            config_path: Optional path to config file
        """
        logger.info("=" * 60)
        logger.info("DCA Trading Bot Starting...")
        logger.info("=" * 60)
        
        # Load configuration
        self.config = load_config(config_path)
        logger.info(f"Blockchain: {self.config.blockchain}")
        logger.info(f"Vault: {self.config.vault_address}")
        logger.info(f"Dry-run mode: {self.config.dry_run}")
        
        # Validate configuration
        errors = self.config.validate()
        if errors:
            logger.error("Configuration validation failed:")
            for error in errors:
                logger.error(f"  - {error}")
            raise ValueError("Invalid configuration")
        
        # Initialize database
        self.db = init_database(
            encryption_key=self.config.database_encryption_key
        )
        logger.info("Database initialized")
        
        # Initialize blockchain client
        self.blockchain = create_blockchain_client(self.config)
        logger.info(f"Blockchain client connected")
        
        # Check vault balance
        vault_balance = self.blockchain.get_usdc_balance(self.config.vault_address)
        vault_native = self.blockchain.get_native_balance(self.config.vault_address)
        logger.info(f"Vault balance: {vault_balance:.2f} USDC, {vault_native:.6f} {self.blockchain.native_token}")
        
        if vault_balance < self.config.min_usd_per_wallet:
            logger.warning(f"Low vault balance: {vault_balance:.2f} USDC")
        
        # Initialize SharesDAO client (for price data and pool info)
        logger.info("Initializing SharesDAO client and loading pools...")
        self.api = create_sharesdao_client(self.config)
        
        # Load pools from API (each pool = a tradable stock ticker)
        pools = self.api.stock_pools
        if not pools:
            logger.error("Failed to load pools from SharesDAO API")
            logger.error("This could be due to:")
            logger.error("  1. Network connectivity issues")
            logger.error("  2. SharesDAO API is down")
            logger.error("  3. No pools available for the selected blockchain")
            logger.error(f"  4. API URL: {self.config.sharesdao_api_url}")
            logger.error(f"  5. Blockchain: {self.config.blockchain}")
            raise ValueError("Failed to load pools from SharesDAO API")
        
        # Update config with pools
        self.config.set_trading_stocks(pools)
        
        # Update mint/burn addresses from pools (shared across all pools)
        self.config.mint_address = self.config.get_mint_address()
        self.config.burn_address = self.config.get_burn_address()
        
        logger.info(f"Loaded {len(self.config.trading_stocks)} trading pools")
        logger.info(f"Mint address: {self.config.mint_address}")
        logger.info(f"Burn address: {self.config.burn_address}")
        
        # Initialize stock selector (selects from available pools)
        self.stock_selector = create_stock_selector(self.config)
        logger.info(f"Pool selector initialized with {len(self.config.get_stock_tickers())} pools")
        
        # Initialize wallet manager
        self.wallet_manager = create_wallet_manager(
            self.db, self.blockchain, self.stock_selector, self.config
        )
        
        # Initialize trade manager
        self.trade_manager = create_trade_manager(
            self.db, self.blockchain, self.api, self.wallet_manager, self.config
        )
        
        logger.info("All components initialized successfully")
        
        # Running flag
        self.running = True
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
    
    async def create_new_wallet_if_needed(self):
        """Create new wallet if conditions are met, or retry pending funding wallets."""
        try:
            # First, retry any pending_funding wallets
            funded_count = self.wallet_manager.retry_pending_funding_wallets(dry_run=self.config.dry_run)
            
            if funded_count > 0:
                logger.info(f"Successfully funded {funded_count} pending wallet(s)")
                
                # Place initial buy orders for newly funded wallets
                active_wallets = self.wallet_manager.get_active_wallets()
                for wallet in active_wallets:
                    # Check if wallet has no orders yet (newly funded)
                    existing_orders = self.db.get_wallet_orders(wallet['address'])
                    if not existing_orders:
                        usdc_balance = self.blockchain.get_usdc_balance(wallet['address'])
                        if usdc_balance >= 5.0:  # Minimum trading amount
                            logger.info(f"Placing initial buy order for newly funded wallet {wallet['address']}")
                            order_id = self.trade_manager.place_buy_order(
                                wallet_address=wallet['address'],
                                stock_ticker=wallet['assigned_stock'],
                                usdc_amount=usdc_balance,
                                dry_run=self.config.dry_run
                            )
                            
                            if order_id:
                                logger.info(f"Initial buy order placed: {order_id}")
                            else:
                                logger.error(f"Failed to place initial buy order for {wallet['address']}")
            
            # Then check if we can create a new wallet
            if not self.wallet_manager.can_create_new_wallet():
                logger.debug("Insufficient vault balance to create new wallet")
                return
            
            # Create wallet (will be saved with 'pending_funding' status if funding fails)
            wallet = self.wallet_manager.create_new_wallet(dry_run=self.config.dry_run)
            
            if wallet:
                logger.info(f"New wallet created and funded: {wallet['address']}")
                
                # Place initial buy order
                order_id = self.trade_manager.place_buy_order(
                    wallet_address=wallet['address'],
                    stock_ticker=wallet['assigned_stock'],
                    usdc_amount=wallet['balance'],
                    dry_run=self.config.dry_run
                )
                
                if order_id:
                    logger.info(f"Initial buy order placed: {order_id}")
                else:
                    logger.error(f"Failed to place initial buy order for {wallet['address']}")
            
        except Exception as e:
            logger.error(f"Error creating new wallet: {e}", exc_info=True)
    
    async def monitor_and_trade(self):
        """Monitor positions and execute trades."""
        try:
            # Check order confirmations and refunds (from blockchain)
            # This handles both filled orders and expired/cancelled orders (refunds)
            processed = self.trade_manager.check_order_confirmations(dry_run=self.config.dry_run)
            if processed > 0:
                logger.info(f"Processed {processed} orders (filled or refunded)")
            
            # Monitor positions (mainly for max hold time check)
            # Note: Sell orders are placed immediately after buy confirmation
            sell_orders = self.trade_manager.monitor_positions(dry_run=self.config.dry_run)
            if sell_orders > 0:
                logger.info(f"Placed {sell_orders} sell orders (max hold time reached)")
            
        except Exception as e:
            logger.error(f"Error in monitor_and_trade: {e}", exc_info=True)
    
    async def interruptible_sleep(self, seconds: int, show_countdown: bool = False):
        """
        Sleep for specified seconds, but check running flag every second for fast shutdown.
        
        Args:
            seconds: Total seconds to sleep
            show_countdown: If True, log remaining time every 60 seconds
        """
        start_time = asyncio.get_event_loop().time()
        
        for i in range(seconds):
            if not self.running:
                logger.info("Shutdown requested, stopping immediately...")
                break
            
            # Show countdown every 60 seconds (if enabled)
            if show_countdown and i > 0 and i % 60 == 0:
                remaining = seconds - i
                logger.debug(f"Next iteration in {remaining} seconds...")
            
            await asyncio.sleep(1)
    
    async def print_status(self):
        """Print current bot status."""
        try:
            # Wallet stats
            wallet_stats = self.wallet_manager.get_wallet_stats()
            logger.info("=" * 60)
            logger.info("STATUS UPDATE")
            logger.info("-" * 60)
            logger.info(f"Active wallets: {wallet_stats['total_active_wallets']}")
            logger.info(f"Total USDC in wallets: ${wallet_stats['total_usdc_in_wallets']:.2f}")
            logger.info(f"Vault balance: ${wallet_stats['vault_balance']:.2f}")
            
            if wallet_stats['stock_distribution']:
                logger.info("Stock distribution:")
                for stock, count in wallet_stats['stock_distribution'].items():
                    logger.info(f"  {stock}: {count} wallets")
            
            # Trading stats
            trade_stats = self.trade_manager.get_trading_stats()
            logger.info("-" * 60)
            logger.info(f"Total orders: {trade_stats['total_orders']} (Buy: {trade_stats['total_buys']}, Sell: {trade_stats['total_sells']})")
            logger.info(f"Filled orders: {trade_stats['filled_orders']}")
            logger.info(f"Active positions: {trade_stats['active_positions']}")
            logger.info(f"Total P&L: ${trade_stats['total_pnl']:.2f}")
            logger.info(f"Win rate: {trade_stats['win_rate']:.1f}%")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error(f"Error printing status: {e}", exc_info=True)
    
    async def main_loop(self):
        """Main bot loop."""
        logger.info("Starting main loop...")
        logger.info(f"Check interval: {self.config.check_interval_seconds} seconds")
        
        iteration = 0
        
        while self.running:
            try:
                iteration += 1
                logger.info(f"--- Iteration {iteration} ---")
                
                # Task 1: Create new wallet if needed
                await self.create_new_wallet_if_needed()
                
                # Task 2: Monitor positions and execute trades
                await self.monitor_and_trade()
                
                # Print status every 2 iterations
                if iteration % 2 == 0:
                    await self.print_status()
                
                # Wait before next iteration (interruptible)
                if self.running:
                    logger.info(f"Waiting {self.config.check_interval_seconds} seconds...")
                    await self.interruptible_sleep(self.config.check_interval_seconds)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                if self.running:
                    logger.info("Waiting 60 seconds before retry...")
                    await self.interruptible_sleep(60)
        
        logger.info("Main loop stopped")
    
    async def run(self):
        """Run the bot."""
        try:
            await self.main_loop()
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            logger.info("Trading bot stopped")


def check_config_command(args):
    """Check configuration validity."""
    setup_logging('INFO')
    
    try:
        config = load_config(args.config)
        errors = config.validate()
        
        if errors:
            print("❌ Configuration validation failed:")
            for error in errors:
                print(f"  - {error}")
            return 1
        else:
            print("✅ Configuration valid!")
            print(f"   Blockchain: {config.blockchain}")
            print(f"   Vault: {config.vault_address}")
            print(f"   Trading stocks: {', '.join(config.trading_stocks)}")
            print(f"   Dry-run mode: {config.dry_run}")
            return 0
    except Exception as e:
        print(f"❌ Error loading configuration: {e}")
        return 1


async def run_bot(args):
    """Run the bot."""
    setup_logging(args.log_level)
    
    bot = TradingBot(config_path=args.config)
    await bot.run()


async def liquidate_command(args):
    """Liquidate all positions."""
    setup_logging(args.log_level)
    
    bot = TradingBot(config_path=args.config)
    
    logger.info("Starting liquidation process...")
    
    # Liquidate all positions (place sell orders)
    result = bot.trade_manager.liquidate_all_positions(dry_run=args.dry_run)
    
    print("\n" + "=" * 60)
    print("LIQUIDATION SUMMARY")
    print("=" * 60)
    print(f"Positions found: {result['positions_found']}")
    print(f"Sell orders placed: {result['sell_orders_placed']}")
    print("=" * 60)
    
    if result['sell_orders_placed'] > 0:
        print("\n⚠️  IMPORTANT:")
        print("1. Wait for sell orders to be confirmed (monitor with bot)")
        print("2. Run 'python -m src.main --sweep' to transfer USDC to vault")
    
    return 0


async def sweep_command(args):
    """Sweep all USDC from wallets to vault."""
    setup_logging(args.log_level)
    
    bot = TradingBot(config_path=args.config)
    
    logger.info("Starting wallet sweep...")
    
    # Sweep all USDC to vault
    result = bot.trade_manager.sweep_wallets_to_vault(dry_run=args.dry_run)
    
    print("\n" + "=" * 60)
    print("SWEEP SUMMARY")
    print("=" * 60)
    print(f"Wallets checked: {result['wallets_checked']}")
    print(f"Wallets swept: {result['wallets_swept']}")
    print(f"Total USDC swept: ${result['total_usdc_swept']:.2f}")
    if result['errors']:
        print(f"Errors: {len(result['errors'])}")
        for error in result['errors']:
            print(f"  - {error}")
    print("=" * 60)
    
    return 0


async def wallets_command(args):
    """Display detailed wallet information."""
    setup_logging(args.log_level)
    
    bot = TradingBot(config_path=args.config)
    
    # Get all wallets
    all_wallets = bot.db.get_active_wallets(bot.config.blockchain)
    pending_wallets = bot.db.get_wallets_by_status(bot.config.blockchain, 'pending_funding')
    abandoned_wallets = bot.db.get_wallets_by_status(bot.config.blockchain, 'abandoned')
    
    native_token = bot.blockchain.chain_config.get('native_token', 'ETH')
    
    print("\n" + "=" * 80)
    print("WALLET INFORMATION")
    print("=" * 80)
    print(f"Blockchain: {bot.config.blockchain}")
    print(f"Total wallets: {len(all_wallets) + len(pending_wallets) + len(abandoned_wallets)}")
    print(f"  - Active: {len(all_wallets)}")
    print(f"  - Pending funding: {len(pending_wallets)}")
    print(f"  - Abandoned: {len(abandoned_wallets)}")
    print("=" * 80)
    
    # Display active wallets
    if all_wallets:
        print("\n📊 ACTIVE WALLETS")
        print("-" * 80)
        total_usdc = 0.0
        total_native = 0.0
        
        for i, wallet in enumerate(all_wallets, 1):
            address = wallet['address']
            stock = wallet['assigned_stock']
            loss_count = wallet['loss_count']
            
            # Get balances
            usdc_balance = bot.blockchain.get_usdc_balance(address)
            native_balance = bot.blockchain.get_native_balance(address)
            total_usdc += usdc_balance
            total_native += native_balance
            
            # Get active position
            position = bot.db.get_position(address)
            position_info = ""
            if position:
                stock_balance = bot.blockchain.get_token_balance(position['stock_token_address'], address)
                position_info = f" | Position: {stock_balance:.4f} {position['stock_ticker']}"
            
            # Get pending orders
            orders = bot.db.get_wallet_orders(address)
            pending_orders = [o for o in orders if o['status'] == 'pending']
            order_info = f" | Orders: {len(pending_orders)} pending" if pending_orders else ""
            
            print(f"\n{i}. {address}")
            print(f"   Stock: {stock} | Losses: {loss_count}/{bot.config.max_loss_traders}")
            print(f"   Balance: ${usdc_balance:.2f} USDC | {native_balance:.6f} {native_token}{position_info}{order_info}")
        
        print(f"\n{'─' * 80}")
        print(f"Total: ${total_usdc:.2f} USDC | {total_native:.6f} {native_token}")
    
    # Display pending funding wallets
    if pending_wallets:
        print("\n⏳ PENDING FUNDING WALLETS")
        print("-" * 80)
        
        for i, wallet in enumerate(pending_wallets, 1):
            address = wallet['address']
            stock = wallet['assigned_stock']
            
            # Get current balances
            usdc_balance = bot.blockchain.get_usdc_balance(address)
            native_balance = bot.blockchain.get_native_balance(address)
            
            print(f"\n{i}. {address}")
            print(f"   Stock: {stock}")
            print(f"   Current: ${usdc_balance:.2f} USDC | {native_balance:.6f} {native_token}")
            print(f"   Status: Waiting for funding retry")
    
    # Display abandoned wallets (summary only)
    if abandoned_wallets:
        print(f"\n🗑️  ABANDONED WALLETS: {len(abandoned_wallets)}")
        if args.show_abandoned:
            print("-" * 80)
            for i, wallet in enumerate(abandoned_wallets, 1):
                address = wallet['address']
                stock = wallet['assigned_stock']
                print(f"{i}. {address} | Stock: {stock}")
    
    # Vault information
    print("\n💰 VAULT")
    print("-" * 80)
    vault_usdc = bot.blockchain.get_usdc_balance(bot.config.vault_address)
    vault_native = bot.blockchain.get_native_balance(bot.config.vault_address)
    print(f"Address: {bot.config.vault_address}")
    print(f"Balance: ${vault_usdc:.2f} USDC | {vault_native:.6f} {native_token}")
    
    print("=" * 80)
    
    return 0


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='DCA Trading Bot')
    parser.add_argument('--config', type=str, help='Path to config.yaml file')
    parser.add_argument('--log-level', type=str, default='INFO',
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                       help='Logging level')
    parser.add_argument('--check-config', action='store_true',
                       help='Validate configuration and exit')
    parser.add_argument('--liquidate', action='store_true',
                       help='Liquidate all positions (sell all stocks)')
    parser.add_argument('--sweep', action='store_true',
                       help='Sweep all USDC from wallets to vault')
    parser.add_argument('--wallets', action='store_true',
                       help='Display detailed wallet information')
    parser.add_argument('--show-abandoned', action='store_true',
                       help='Show abandoned wallets in detail (use with --wallets)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Simulate liquidation/sweep without executing')
    
    args = parser.parse_args()
    
    if args.check_config:
        sys.exit(check_config_command(args))
    elif args.liquidate:
        try:
            sys.exit(asyncio.run(liquidate_command(args)))
        except KeyboardInterrupt:
            print("\nLiquidation cancelled by user")
            sys.exit(1)
    elif args.sweep:
        try:
            sys.exit(asyncio.run(sweep_command(args)))
        except KeyboardInterrupt:
            print("\nSweep cancelled by user")
            sys.exit(1)
    elif args.wallets:
        try:
            sys.exit(asyncio.run(wallets_command(args)))
        except KeyboardInterrupt:
            print("\nCancelled by user")
            sys.exit(1)
    else:
        # Run bot
        try:
            asyncio.run(run_bot(args))
        except KeyboardInterrupt:
            print("\nShutdown requested by user")
        except Exception as e:
            print(f"Fatal error: {e}")
            sys.exit(1)


if __name__ == '__main__':
    main()
