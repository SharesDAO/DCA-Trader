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
    Setup logging configuration.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    # Create logs directory
    project_root = Path(__file__).parent.parent
    log_dir = project_root / 'logs'
    log_dir.mkdir(exist_ok=True)
    
    # Configure logging
    log_file = log_dir / 'bot.log'
    
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized - Level: {log_level}, File: {log_file}")


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
        
        # Initialize stock selector (selects from available pools)
        self.stock_selector = create_stock_selector(self.config)
        logger.info(f"Pool selector initialized with {len(self.config.get_stock_tickers())} pools")
        
        # Initialize SharesDAO client (for price data and pool info)
        self.api = create_sharesdao_client(self.config)
        
        # Load pools from API (each pool = a tradable stock ticker)
        pools = self.api.stock_pools
        if not pools:
            raise ValueError("Failed to load pools from SharesDAO API")
        
        # Update config with pools
        self.config.set_trading_stocks(pools)
        
        # Update mint/burn addresses from pools (shared across all pools)
        self.config.mint_address = self.config.get_mint_address()
        self.config.burn_address = self.config.get_burn_address()
        
        logger.info(f"Loaded {len(self.config.trading_stocks)} trading pools")
        logger.info(f"Mint address: {self.config.mint_address}")
        logger.info(f"Burn address: {self.config.burn_address}")
        
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
        """Create new wallet if conditions are met."""
        try:
            # Check if we can create a new wallet
            if not self.wallet_manager.can_create_new_wallet():
                logger.debug("Insufficient vault balance to create new wallet")
                return
            
            # Create wallet
            wallet = self.wallet_manager.create_new_wallet(dry_run=self.config.dry_run)
            
            if wallet:
                logger.info(f"New wallet created: {wallet['address']}")
                
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
                
                # Print status every 10 iterations
                if iteration % 2 == 0:
                    await self.print_status()
                
                # Wait before next iteration
                logger.info(f"Waiting {self.config.check_interval_seconds} seconds...")
                await asyncio.sleep(self.config.check_interval_seconds)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                logger.info("Waiting 60 seconds before retry...")
                await asyncio.sleep(60)
        
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


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='DCA Trading Bot')
    parser.add_argument('--config', type=str, help='Path to config.yaml file')
    parser.add_argument('--log-level', type=str, default='INFO',
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                       help='Logging level')
    parser.add_argument('--check-config', action='store_true',
                       help='Validate configuration and exit')
    
    args = parser.parse_args()
    
    if args.check_config:
        sys.exit(check_config_command(args))
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
