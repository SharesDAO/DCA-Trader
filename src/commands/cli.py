"""
CLI commands for DCA Trading Bot.
"""
import logging
from utils.logger import setup_logging
from config import load_config

logger = logging.getLogger(__name__)


def _get_trading_bot(config_path=None):
    """Get TradingBot instance (lazy import to avoid circular dependency)."""
    from main import TradingBot
    return TradingBot(config_path=config_path)


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
    
    bot = _get_trading_bot(config_path=args.config)
    await bot.run()


async def liquidate_command(args):
    """Liquidate all positions."""
    setup_logging(args.log_level)
    
    bot = _get_trading_bot(config_path=args.config)
    
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
    
    bot = _get_trading_bot(config_path=args.config)
    
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


async def collect_eth_command(args):
    """Collect ETH/BNB from wallets with almost zero USDC balance and send to vault."""
    setup_logging(args.log_level)
    
    bot = _get_trading_bot(config_path=args.config)
    
    native_token = bot.blockchain.chain_config.get('native_token', 'ETH')
    
    # Default threshold: wallets with USDC < $1.0
    min_usdc_threshold = getattr(args, 'min_usdc_threshold', 1.0)
    
    logger.info(f"Starting {native_token} collection from wallets with USDC < ${min_usdc_threshold:.2f}...")
    
    # Collect native tokens from wallets with low USDC balance
    result = bot.wallet_manager.collect_abandoned_wallets_native_token(
        dry_run=args.dry_run,
        min_usdc_threshold=min_usdc_threshold
    )
    
    print("\n" + "=" * 60)
    print(f"{native_token.upper()} COLLECTION SUMMARY")
    print("=" * 60)
    print(f"USDC threshold: < ${min_usdc_threshold:.2f}")
    print(f"Wallets checked: {result['wallets_checked']}")
    print(f"Wallets skipped (USDC >= ${min_usdc_threshold:.2f}): {result.get('wallets_skipped_usdc', 0)}")
    print(f"Wallets collected: {result['wallets_collected']}")
    print(f"Total {native_token} collected: {result['total_collected']:.6f}")
    if result['errors']:
        print(f"Errors: {len(result['errors'])}")
        for error in result['errors']:
            print(f"  - {error}")
    print("=" * 60)
    
    return 0


async def wallets_command(args):
    """Display detailed wallet information."""
    setup_logging(args.log_level)
    
    bot = _get_trading_bot(config_path=args.config)
    
    # Get all wallets
    all_wallets = bot.db.get_active_wallets(bot.config.blockchain)
    pending_wallets = bot.db.get_wallets_by_status(bot.config.blockchain, 'pending_funding')
    abandoned_wallets = bot.db.get_wallets_by_status(bot.config.blockchain, 'abandoned')
    
    native_token = bot.blockchain.chain_config.get('native_token', 'ETH')
    
    # If --abandoned-only flag is set, only show abandoned wallets
    if args.abandoned_only:
        print("\n" + "=" * 80)
        print("ABANDONED WALLETS")
        print("=" * 80)
        print(f"Blockchain: {bot.config.blockchain}")
        print(f"Total abandoned wallets: {len(abandoned_wallets)}")
        print("=" * 80)
        
        if abandoned_wallets:
            print("\n🗑️  ABANDONED WALLETS DETAILS")
            print("-" * 80)
            total_usdc = 0.0
            total_native = 0.0
            
            for i, wallet in enumerate(abandoned_wallets, 1):
                address = wallet['address']
                stock = wallet['assigned_stock']
                loss_count = wallet['loss_count']
                
                # Get current balances
                usdc_balance = bot.blockchain.get_usdc_balance(address)
                native_balance = bot.blockchain.get_native_balance(address)
                
                total_usdc += usdc_balance
                total_native += native_balance
                
                print(f"\n{i}. {address}")
                print(f"   Stock: {stock} | Losses: {loss_count}/{bot.config.max_loss_traders}")
                print(f"   USDC: ${usdc_balance:.2f} | {native_token}: {native_balance:.6f}")
            
            print(f"\n{'─' * 80}")
            print(f"Total: ${total_usdc:.2f} USDC | {total_native:.6f} {native_token}")
        else:
            print("\nNo abandoned wallets found.")
        
        print("=" * 80)
        return 0
    
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
        
        # Get all pending orders and positions upfront (more efficient)
        all_pending_orders = bot.db.get_pending_orders()
        all_positions = {pos['wallet_address']: pos for pos in bot.db.get_all_positions()}
        
        for i, wallet in enumerate(all_wallets, 1):
            address = wallet['address']
            stock = wallet['assigned_stock']
            loss_count = wallet['loss_count']
            
            # Get pending orders for this wallet
            pending_orders = [o for o in all_pending_orders if o['wallet_address'] == address]
            pending_buy_orders = [o for o in pending_orders if o['order_type'] == 'buy']
            pending_sell_orders = [o for o in pending_orders if o['order_type'] == 'sell']
            
            # Calculate USDC value from pending buy orders
            usdc_value = sum(o['amount_usdc'] for o in pending_buy_orders)
            total_usdc += usdc_value
            
            # Get native balance (still need to query for gas info)
            native_balance = bot.blockchain.get_native_balance(address)
            total_native += native_balance
            
            # Get active position from database
            position = all_positions.get(address)
            position_info = ""
            if position:
                stock_ticker = position['stock_ticker']
                # Calculate available stock quantity (subtract pending sell orders)
                pending_sell_quantity = sum(o['quantity'] for o in pending_sell_orders)
                available_quantity = max(0.0, position['quantity'] - pending_sell_quantity)
                position_info = f" | Position: {available_quantity:.4f} {stock_ticker}"
                if pending_sell_quantity > 0:
                    position_info += f" (pending sell: {pending_sell_quantity:.4f})"
            
            # Format order info
            order_info = ""
            if pending_buy_orders:
                buy_total = sum(o['amount_usdc'] for o in pending_buy_orders)
                order_info += f" | {len(pending_buy_orders)} buy order(s): ${buy_total:.2f} USDC"
            if pending_sell_orders:
                sell_total = sum(o['quantity'] for o in pending_sell_orders)
                order_info += f" | {len(pending_sell_orders)} sell order(s): {sell_total:.4f} shares"
            
            print(f"\n{i}. {address}")
            print(f"   Stock: {stock} | Losses: {loss_count}/{bot.config.max_loss_traders}")
            print(f"   USDC: ${usdc_value:.2f} (from pending orders) | {native_balance:.6f} {native_token}{position_info}{order_info}")
        
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
