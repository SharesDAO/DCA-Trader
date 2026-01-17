"""
Portfolio value calculation utilities.
"""
import logging

logger = logging.getLogger(__name__)


class PortfolioCalculator:
    """Calculate total portfolio value from database data."""
    
    def __init__(self, db, config, api):
        """
        Initialize portfolio calculator.
        
        Args:
            db: Database instance
            config: Config instance
            api: SharesDAO API client
        """
        self.db = db
        self.config = config
        self.api = api
        
        # Portfolio value cache to reduce API calls
        self._portfolio_cache = None
        self._portfolio_cache_iteration = 0
        self._portfolio_cache_interval = config.portfolio_cache_refresh
    
    def invalidate_cache(self):
        """Invalidate portfolio cache after important events."""
        self._portfolio_cache = None
        self._portfolio_cache_iteration = 0
        logger.debug("Portfolio cache invalidated")
    
    async def calculate_total_usd_value(self, wallet_manager, blockchain_client, force_refresh: bool = False):
        """
        Calculate total USD value across all wallets and vault.
        
        Uses caching to reduce blockchain API calls. Cache is refreshed:
        - Every N iterations (configurable)
        - When force_refresh=True (after wallet creation, order confirmation, etc.)
        - When cache is empty
        
        Args:
            wallet_manager: WalletManager instance
            blockchain_client: BlockchainClient instance (for vault balance)
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
                    # Still need to update vault balance from cache
                    cached_result = self._portfolio_cache.copy()
                    cached_result['vault_balance'] = blockchain_client.get_usdc_balance(self.config.vault_address)
                    cached_result['total_value'] = cached_result['total_value'] - (self._portfolio_cache.get('vault_balance', 0) or 0) + cached_result['vault_balance']
                    return cached_result
            
            # Refresh cache
            logger.debug("Refreshing portfolio value from database...")
            
            total_value = 0.0
            wallet_details = []
            
            # Get all active wallets
            active_wallets = wallet_manager.get_active_wallets()
            
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
                
                # Calculate USDC value from pending buy orders
                # Pending buy orders mean USDC has been sent but order not confirmed yet
                for buy_order in pending_buy_orders:
                    usdc_value += buy_order['amount_usdc']
                
                # Get position (if exists)
                position = all_positions.get(wallet_address)
                if position:
                    # Calculate available stock quantity
                    # Subtract stocks that are in pending sell orders
                    pending_sell_quantity = sum(sell_order['quantity'] for sell_order in pending_sell_orders)
                    available_stock_quantity = max(0.0, position['quantity'] - pending_sell_quantity)
                    
                    if available_stock_quantity > 0:
                        stock_balance = available_stock_quantity
                        # Get current stock price
                        stock_price = self.api.get_stock_price(stock_ticker)
                        if stock_price and stock_price > 0:
                            stock_value = stock_balance * stock_price
                        else:
                            logger.warning(f"Failed to get price for {stock_ticker}, stock value not included")
                
                wallet_value = usdc_value + stock_value
                
                wallet_details.append({
                    'address': wallet_address,
                    'stock': stock_ticker,
                    'value': wallet_value,
                    'usdc_value': usdc_value,
                    'stock_value': stock_value,
                    'stock_balance': stock_balance,
                    'pending_buy_orders': len(pending_buy_orders),
                    'pending_sell_orders': len(pending_sell_orders)
                })
                total_value += wallet_value
            
            # Add vault USDC balance (vault doesn't have pending orders, so query balance)
            vault_balance = blockchain_client.get_usdc_balance(self.config.vault_address)
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
