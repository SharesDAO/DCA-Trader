"""
Main entry point for DCA Trading Bot.
Orchestrates wallet creation, order placement, and position monitoring.
"""

import asyncio
import logging
import signal
import sys
import argparse

# Import modules
from config import load_config
from database import init_database
from blockchain_client import create_blockchain_client
from stock_selector import create_stock_selector
from wallet_manager import create_wallet_manager
from sharesdao_client import create_sharesdao_client
from trade_manager import create_trade_manager

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
        
        # Portfolio value cache to reduce API calls
        self._portfolio_cache = None
        self._portfolio_cache_iteration = 0
        self._portfolio_cache_interval = self.config.portfolio_cache_refresh  # Refresh every N iterations
        
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
            # Skip wallet creation and funding in liquidation mode
            if self.config.liquid_mode:
                logger.debug("Liquidation mode enabled - skipping wallet creation and funding")
                return
            
            # First, retry any pending_funding wallets
            funded_count = self.wallet_manager.retry_pending_funding_wallets(dry_run=self.config.dry_run)
            
            if funded_count > 0:
                logger.info(f"Successfully funded {funded_count} pending wallet(s)")
                # Invalidate cache after funding wallets
                self.invalidate_portfolio_cache()
                
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
            
            # Check if there are still unfunded wallets — skip creating new ones until all are funded
            remaining_pending = self.db.get_wallets_by_status(self.config.blockchain, 'pending_funding')
            if remaining_pending:
                logger.info(f"Skipping new wallet creation — {len(remaining_pending)} unfunded wallet(s) still pending")
                return
            
            # Then check if we can create a new wallet
            if not self.wallet_manager.can_create_new_wallet():
                logger.debug("Insufficient vault balance to create new wallet")
                return
            
            # Create wallet (will be saved with 'pending_funding' status if funding fails)
            wallet = self.wallet_manager.create_new_wallet(dry_run=self.config.dry_run)
            
            if wallet:
                logger.info(f"New wallet created and funded: {wallet['address']}")
                # Invalidate cache after creating new wallet
                self.invalidate_portfolio_cache()
                
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
            
            # Then check if we can create a new wallet
            if not self.wallet_manager.can_create_new_wallet():
                logger.debug("Insufficient vault balance to create new wallet")
                return
            
            # Create wallet (will be saved with 'pending_funding' status if funding fails)
            wallet = self.wallet_manager.create_new_wallet(dry_run=self.config.dry_run)
            
            if wallet:
                logger.info(f"New wallet created and funded: {wallet['address']}")
                # Invalidate cache after creating new wallet
                self.invalidate_portfolio_cache()
                
                # Place initial buy order (skip in liquidation mode)
                if not self.config.liquid_mode:
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
                else:
                    logger.debug("Liquidation mode enabled - skipping buy order placement")
            
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
                # Invalidate cache after order confirmations (balance changed)
                self.invalidate_portfolio_cache()
            
            # In liquidation mode, check for empty wallets and collect funds
            if self.config.liquid_mode:
                cleaned = self.trade_manager.cleanup_empty_wallets(dry_run=self.config.dry_run)
                if cleaned > 0:
                    logger.info(f"Cleaned up {cleaned} empty wallet(s) in liquidation mode")
                    self.invalidate_portfolio_cache()
            
            # Monitor positions (mainly for max hold time check)
            # Note: Sell orders are placed immediately after buy confirmation
            sell_orders = self.trade_manager.monitor_positions(dry_run=self.config.dry_run)
            if sell_orders > 0:
                logger.info(f"Placed {sell_orders} sell orders (max hold time reached)")
                # Invalidate cache after placing sell orders (tokens will be transferred)
                self.invalidate_portfolio_cache()
            
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
    
    def invalidate_portfolio_cache(self):
        """Invalidate portfolio cache after important events (wallet creation, order confirmation, etc.)."""
        self._portfolio_cache = None
        self._portfolio_cache_iteration = 0
        logger.debug("Portfolio cache invalidated")
    
    async def calculate_total_usd_value(self, force_refresh: bool = False):
        """
        Calculate total USD value across all wallets and vault.
        
        Uses caching to reduce blockchain API calls. Cache is refreshed:
        - Every N iterations (configurable)
        - When force_refresh=True (after wallet creation, order confirmation, etc.)
        - When cache is empty
        
        Args:
            force_refresh: Force cache refresh regardless of iteration count
        
        Returns:
            Dict with total_value, vault_balance, wallet_count, wallet_details
        """
        try:
            # Return cached value if available and fresh enough
            if not force_refresh and self._portfolio_cache is not None:
                if self._portfolio_cache_iteration < self._portfolio_cache_interval:
                    self._portfolio_cache_iteration += 1
                    logger.debug(f"Using cached portfolio value (iteration {self._portfolio_cache_iteration}/{self._portfolio_cache_interval})")
                    return self._portfolio_cache
            
            # Refresh cache
            logger.debug("Refreshing portfolio value from database...")
            
            total_value = 0.0
            wallet_details = []
            
            # Get all active wallets
            active_wallets = self.wallet_manager.get_active_wallets()
            
            # Get all pending orders grouped by wallet
            pending_orders_by_wallet = {}
            all_pending_orders = self.db.get_pending_orders()
            for order in all_pending_orders:
                wallet_addr = order['wallet_address']
                if wallet_addr not in pending_orders_by_wallet:
                    pending_orders_by_wallet[wallet_addr] = {'buy': [], 'sell': []}
                pending_orders_by_wallet[wallet_addr][order['order_type']].append(order)
            
            # Get all positions
            all_positions = {pos['wallet_address']: pos for pos in self.db.get_all_positions()}
            
            for wallet in active_wallets:
                wallet_address = wallet['address']
                stock_ticker = wallet['assigned_stock']
                wallet_value = 0.0
                usdc_value = 0.0
                stock_value = 0.0
                stock_balance = 0.0
                
                # Get pending orders for this wallet
                pending_orders = pending_orders_by_wallet.get(wallet_address, {'buy': [], 'sell': []})
                pending_buy_orders = pending_orders['buy']
                pending_sell_orders = pending_orders['sell']
                
                # 1. Get actual token balances in wallet (current holdings)
                actual_usdc_balance = self.blockchain.get_usdc_balance(wallet_address)
                pool_info = self.config.get_pool_by_ticker(stock_ticker)
                actual_stock_balance = 0.0
                if pool_info:
                    token_address = pool_info.get('asset_id')
                    if token_address:
                        actual_stock_balance = self.blockchain.get_token_balance(token_address, wallet_address)
                
                # 2. Calculate value from actual balances
                usdc_value = actual_usdc_balance
                if actual_stock_balance > 0:
                    stock_price = self.api.get_stock_price(stock_ticker)
                    if stock_price and stock_price > 0:
                        stock_value = actual_stock_balance * stock_price
                    else:
                        logger.warning(f"Failed to get price for {stock_ticker}, stock value not included")
                
                # 3. Add pending buy orders value (USDC sent but order not confirmed)
                # Pending buy orders: USDC has been sent but order not confirmed yet
                # Only count USDC value, NOT stock value (order not confirmed)
                pending_buy_usdc = sum(buy_order['amount_usdc'] for buy_order in pending_buy_orders)
                usdc_value += pending_buy_usdc
                
                # 4. Add pending sell orders value (stocks sent but order not confirmed)
                # Pending sell orders: Stocks have been sent but order not confirmed yet
                # Only count stock value, NOT USDC value (order not confirmed)
                pending_sell_quantity = sum(sell_order['quantity'] for sell_order in pending_sell_orders)
                if pending_sell_quantity > 0:
                    stock_price = self.api.get_stock_price(stock_ticker)
                    if stock_price and stock_price > 0:
                        pending_sell_value = pending_sell_quantity * stock_price
                        stock_value += pending_sell_value
                    else:
                        logger.warning(f"Failed to get price for {stock_ticker}, pending sell value not included")
                
                wallet_value = usdc_value + stock_value
                stock_balance = actual_stock_balance
                
                position = all_positions.get(wallet_address)
                wallet_details.append({
                    'address': wallet_address,
                    'stock': stock_ticker,
                    'value': wallet_value,
                    'usdc_value': usdc_value,
                    'stock_value': stock_value,
                    'actual_usdc_balance': actual_usdc_balance,
                    'actual_stock_balance': actual_stock_balance,
                    'pending_buy_usdc': pending_buy_usdc,
                    'pending_sell_quantity': pending_sell_quantity,
                    'position_quantity': position['quantity'] if position else 0.0,
                    'pending_buy_orders': len(pending_buy_orders),
                    'pending_sell_orders': len(pending_sell_orders)
                })
                total_value += wallet_value
            
            # Add vault USDC balance (vault doesn't have pending orders, so query balance)
            vault_balance = self.blockchain.get_usdc_balance(self.config.vault_address)
            total_value += vault_balance
            
            result = {
                'total_value': total_value,
                'vault_balance': vault_balance,
                'wallet_count': len(active_wallets),
                'wallet_details': wallet_details
            }
            
            # Update cache
            self._portfolio_cache = result
            self._portfolio_cache_iteration = 1
            logger.debug(f"Portfolio cache refreshed: ${result['total_value']:.2f}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error calculating total USD value: {e}", exc_info=True)
            return {
                'total_value': 0.0,
                'vault_balance': 0.0,
                'wallet_count': 0,
                'wallet_details': []
            }
    
    async def print_status(self):
        """Print current bot status."""
        try:
            # Calculate total portfolio value (force refresh for accurate status report)
            value_info = await self.calculate_total_usd_value(force_refresh=True)
            
            # Calculate breakdown
            total_usdc = value_info['vault_balance']
            total_stock_value = 0.0
            for wallet_detail in value_info['wallet_details']:
                total_usdc += wallet_detail.get('usdc_value', 0.0)
                total_stock_value += wallet_detail.get('stock_value', 0.0)
            
            # Wallet stats
            wallet_stats = self.wallet_manager.get_wallet_stats()
            logger.info("=" * 60)
            logger.info("STATUS UPDATE")
            logger.info("-" * 60)
            logger.info(f"💰 TOTAL PORTFOLIO VALUE: ${value_info['total_value']:.2f} USD")
            logger.info(f"   USDC Value: ${total_usdc:.2f} | Stock Value: ${total_stock_value:.2f}")
            logger.info("-" * 60)
            logger.info(f"Active wallets: {wallet_stats['total_active_wallets']}")
            logger.info(f"Total USDC in wallets: ${wallet_stats['total_usdc_in_wallets']:.2f}")
            logger.info(f"Vault balance: ${wallet_stats['vault_balance']:.2f}")
            
            if wallet_stats['stock_distribution']:
                logger.info("Stock distribution:")
                for stock, count in wallet_stats['stock_distribution'].items():
                    logger.info(f"  {stock}: {count} wallets")
            
            # Wallet values breakdown (top 5 by value)
            if value_info['wallet_details']:
                sorted_wallets = sorted(value_info['wallet_details'], key=lambda x: x['value'], reverse=True)
                top_wallets = sorted_wallets[:5]
                if top_wallets:
                    logger.info("Top wallets by value:")
                    for w in top_wallets:
                        usdc_val = w.get('usdc_value', 0.0)
                        stock_val = w.get('stock_value', 0.0)
                        logger.info(f"  {w['address'][:8]}...{w['address'][-4:]} ({w['stock']}): ${w['value']:.2f} (USDC: ${usdc_val:.2f}, Stock: ${stock_val:.2f})")
            
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
                
                # Log total USD value
                value_info = await self.calculate_total_usd_value()
                
                # Calculate breakdown
                total_usdc = value_info['vault_balance']
                total_stock_value = 0.0
                for wallet_detail in value_info['wallet_details']:
                    total_usdc += wallet_detail.get('usdc_value', 0.0)
                    total_stock_value += wallet_detail.get('stock_value', 0.0)
                
                logger.info(f"💰 Total Portfolio Value: ${value_info['total_value']:.2f} USD")
                logger.info(f"   USDC: ${total_usdc:.2f} | Stocks: ${total_stock_value:.2f}")
                logger.info(f"   Vault: ${value_info['vault_balance']:.2f} | Active Wallets: {value_info['wallet_count']}")
                
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


def main():
    """Main entry point."""
    from commands.cli import (
        check_config_command,
        run_bot,
        liquidate_command,
        sweep_command,
        collect_eth_command,
        wallets_command,
        delete_unfunded_command
    )
    
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
    parser.add_argument('--collect-eth', action='store_true',
                       help='Collect ETH/BNB from wallets with almost zero USDC balance and send to vault')
    parser.add_argument('--min-usdc-threshold', type=float, default=1.0,
                       help='Maximum USDC balance to consider wallet for ETH collection (default: 1.0)')
    parser.add_argument('--wallets', action='store_true',
                       help='Display detailed wallet information')
    parser.add_argument('--show-abandoned', action='store_true',
                       help='Show abandoned wallets in detail (use with --wallets)')
    parser.add_argument('--abandoned-only', action='store_true',
                       help='Show only abandoned wallets with their USDC and ETH balances')
    parser.add_argument('--delete-unfunded', action='store_true',
                       help='Delete all unfunded (pending_funding) wallets from the database')
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
    elif args.collect_eth:
        try:
            sys.exit(asyncio.run(collect_eth_command(args)))
        except KeyboardInterrupt:
            print("\nCollection cancelled by user")
            sys.exit(1)
    elif args.wallets or args.abandoned_only:
        try:
            sys.exit(asyncio.run(wallets_command(args)))
        except KeyboardInterrupt:
            print("\nCancelled by user")
            sys.exit(1)
    elif args.delete_unfunded:
        try:
            sys.exit(asyncio.run(delete_unfunded_command(args)))
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
