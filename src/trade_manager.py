"""
Trade manager module.
Handles buy/sell orders via blockchain transactions, position monitoring, and P/L calculations.
"""

import logging
import time
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class TradeManager:
    """Manages trading operations via on-chain order submission."""
    
    def __init__(self, database, blockchain_client, sharesdao_client, 
                 wallet_manager, config):
        """
        Initialize trade manager.
        
        Args:
            database: Database instance
            blockchain_client: BlockchainClient instance
            sharesdao_client: SharesDAOClient instance (for price data)
            wallet_manager: WalletManager instance
            config: Config instance
        """
        self.db = database
        self.blockchain = blockchain_client
        self.api = sharesdao_client
        self.wallet_manager = wallet_manager
        self.config = config
        
        logger.info("Trade manager initialized (on-chain order mode)")
    
    def generate_customer_id(self, wallet_address: str, order_type: str) -> str:
        """
        Generate unique customer ID for order tracking.
        
        Args:
            wallet_address: Wallet address
            order_type: 'buy' or 'sell'
            
        Returns:
            Unique customer ID string
        """
        timestamp = int(time.time() * 1000)  # milliseconds
        return f"SVIM_DCA_{order_type}_{timestamp}"
    
    def place_buy_order(self, wallet_address: str, stock_ticker: str, 
                       usdc_amount: float, dry_run: bool = False) -> Optional[str]:
        """
        Place a buy order by sending USDC to pool with memo.
        
        Args:
            wallet_address: Wallet address
            stock_ticker: Stock ticker to buy
            usdc_amount: USDC amount to spend
            dry_run: If True, simulate only
            
        Returns:
            Customer ID (order tracking ID) or None on failure
        """
        try:
            # Minimum order value check
            MIN_ORDER_VALUE = 5.0
            if usdc_amount < MIN_ORDER_VALUE:
                logger.warning(f"Order amount ${usdc_amount:.2f} is below minimum ${MIN_ORDER_VALUE}")
                return None
            
            logger.info(f"Placing buy order for {wallet_address}: {stock_ticker}")
            
            # Get wallet private key
            wallet = self.db.get_wallet(wallet_address)
            if not wallet:
                logger.error(f"Wallet not found: {wallet_address}")
                return None
            
            # Get current stock price
            current_price = self.api.get_stock_price(stock_ticker)
            if not current_price:
                logger.error(f"Failed to get price for {stock_ticker}")
                return None
            
            # Calculate stock quantity
            quantity = usdc_amount / current_price
            quantity = round(quantity, 6)
            
            # Set limit price (slightly above market for faster fill)
            limit_price = current_price * 1.005  # +0.5%
            limit_price = round(limit_price, 2)
            
            # Recalculate quantity at limit price
            quantity_at_limit = usdc_amount / limit_price
            
            logger.info(f"Buy: {quantity_at_limit:.6f} {stock_ticker} @ ${limit_price} (market: ${current_price})")
            
            # Get stock token address
            stock_token_address = self.config.get_stock_token_address(stock_ticker)
            
            # Generate customer ID for tracking
            customer_id = self.generate_customer_id(wallet_address, 'buy')
            
            # Submit buy order via blockchain
            tx_hash = self.blockchain.submit_buy_order(
                from_private_key=wallet['private_key'],
                stock_ticker=stock_ticker,
                stock_token_address=stock_token_address,
                usdc_amount=usdc_amount,
                stock_quantity=quantity_at_limit,
                customer_id=customer_id,
                mint_address=self.config.mint_address,
                expiry_days=self.config.order_expiry_days,
                dry_run=dry_run
            )
            
            if not tx_hash:
                logger.error(f"Failed to submit buy order for {wallet_address}")
                return None
            
            # Save order to database
            expires_at = datetime.now() + timedelta(days=self.config.order_expiry_days)
            
            self.db.create_order(
                order_id=customer_id,
                wallet_address=wallet_address,
                order_type='buy',
                stock_ticker=stock_ticker,
                amount_usdc=usdc_amount,
                quantity=quantity_at_limit,
                limit_price=limit_price,
                expires_at=expires_at
            )
            
            logger.info(f"Buy order placed: {customer_id}, tx: {tx_hash}")
            return customer_id
            
        except Exception as e:
            logger.error(f"Failed to place buy order: {e}", exc_info=True)
            return None
    
    def place_sell_order(self, wallet_address: str, stock_ticker: str,
                        quantity: float, dry_run: bool = False) -> Optional[str]:
        """
        Place a sell order by sending stock tokens to pool with memo.
        
        Args:
            wallet_address: Wallet address
            stock_ticker: Stock ticker to sell
            quantity: Stock quantity to sell
            dry_run: If True, simulate only
            
        Returns:
            Customer ID (order tracking ID) or None on failure
        """
        try:
            logger.info(f"Placing sell order for {wallet_address}: {quantity} {stock_ticker}")
            
            # Get wallet private key
            wallet = self.db.get_wallet(wallet_address)
            if not wallet:
                logger.error(f"Wallet not found: {wallet_address}")
                return None
            
            # Get current stock price
            current_price = self.api.get_stock_price(stock_ticker)
            if not current_price:
                logger.error(f"Failed to get price for {stock_ticker}")
                return None
            
            # Set limit price (slightly below market for faster fill)
            limit_price = current_price * 0.995  # -0.5%
            limit_price = round(limit_price, 2)
            
            # Calculate expected USDC amount
            usdc_amount = quantity * limit_price
            
            # Minimum order value check
            MIN_ORDER_VALUE = 5.0
            if usdc_amount < MIN_ORDER_VALUE:
                logger.warning(f"Sell order value ${usdc_amount:.2f} is below minimum ${MIN_ORDER_VALUE}")
                return None
            
            logger.info(f"Sell: {quantity:.6f} {stock_ticker} @ ${limit_price} (market: ${current_price})")
            
            # Get stock token address
            stock_token_address = self.config.get_stock_token_address(stock_ticker)
            
            # Generate customer ID for tracking
            customer_id = self.generate_customer_id(wallet_address, 'sell')
            
            # Submit sell order via blockchain
            tx_hash = self.blockchain.submit_sell_order(
                from_private_key=wallet['private_key'],
                stock_ticker=stock_ticker,
                stock_token_address=stock_token_address,
                stock_quantity=quantity,
                usdc_amount=usdc_amount,
                customer_id=customer_id,
                burn_address=self.config.burn_address,
                expiry_days=self.config.order_expiry_days,
                dry_run=dry_run
            )
            
            if not tx_hash:
                logger.error(f"Failed to submit sell order for {wallet_address}")
                return None
            
            # Save order to database
            expires_at = datetime.now() + timedelta(days=self.config.order_expiry_days)
            
            self.db.create_order(
                order_id=customer_id,
                wallet_address=wallet_address,
                order_type='sell',
                stock_ticker=stock_ticker,
                amount_usdc=usdc_amount,
                quantity=quantity,
                limit_price=limit_price,
                expires_at=expires_at
            )
            
            logger.info(f"Sell order placed: {customer_id}, tx: {tx_hash}")
            return customer_id
            
        except Exception as e:
            logger.error(f"Failed to place sell order: {e}", exc_info=True)
            return None
    
    def monitor_positions(self, dry_run: bool = False) -> int:
        """
        Monitor all positions and their sell orders status.
        
        Note: Most positions will have pending sell orders already placed.
        - If order expires/refunds, _handle_refunded_order() will automatically
          place a new order at market price if max_hold_days is reached.
        - This monitor mainly handles edge cases (positions without sell orders).
        
        Args:
            dry_run: If True, simulate only
            
        Returns:
            Number of sell orders placed (only for edge cases)
        """
        logger.info("Monitoring positions for max hold time...")
        
        positions = self.db.get_all_positions()
        sell_orders_placed = 0
        
        for position in positions:
            wallet_address = position['wallet_address']
            stock_ticker = position['stock_ticker']
            quantity = position['quantity']
            first_buy_date = datetime.strptime(position['first_buy_date'], '%Y-%m-%d').date()
            
            # Calculate holding days
            holding_days = (date.today() - first_buy_date).days
            
            # Check if already has pending sell order
            pending_sell_orders = [
                o for o in self.db.get_wallet_orders(wallet_address)
                if o['order_type'] == 'sell' and o['status'] == 'pending'
            ]
            
            if pending_sell_orders:
                # Has pending sell order, just monitor
                if holding_days >= self.config.max_hold_days:
                    logger.info(f"{wallet_address} - {stock_ticker}: max hold time reached ({holding_days} days), waiting for order expiry/refund")
                else:
                    logger.debug(f"{wallet_address} - {stock_ticker}: pending sell order exists, holding {holding_days} days")
                
                # Note: When the order expires, it will be refunded and _handle_refunded_order()
                # will automatically place a new sell order at market price
                continue
            
            # No pending sell order - place one (shouldn't happen normally)
            logger.warning(f"{wallet_address} - {stock_ticker}: no sell order found, placing one now")
            
            customer_id = self.place_sell_order(
                wallet_address=wallet_address,
                stock_ticker=stock_ticker,
                quantity=quantity,
                dry_run=dry_run
            )
            
            if customer_id:
                sell_orders_placed += 1
        
        if sell_orders_placed > 0:
            logger.info(f"Placed {sell_orders_placed} sell orders")
        
        return sell_orders_placed
    
    def check_order_confirmations(self, dry_run: bool = False) -> int:
        """
        Check for order confirmations and refunds by monitoring wallet balances.
        
        Logic:
        - Buy order: Check for stock token balance (filled) or USDC balance (refunded)
        - Sell order: Check for USDC balance (filled) or stock token balance (refunded)
        
        Since we use all balance when sending orders, any significant balance means
        the order was either filled or refunded.
        
        Args:
            dry_run: If True, simulate only
            
        Returns:
            Number of orders processed
        """
        logger.info("Checking order confirmations and refunds via balance monitoring...")
        
        pending_orders = self.db.get_pending_orders()
        processed = 0
        
        # Minimum balance thresholds (to account for small dust amounts)
        MIN_STOCK_BALANCE = 0.0001  # Minimum stock tokens to consider as received
        MIN_USDC_BALANCE = 0.01    # Minimum USDC to consider as received
        
        for order in pending_orders:
            order_id = order['order_id']
            order_type = order['order_type']
            wallet_address = order['wallet_address']
            stock_ticker = order['stock_ticker']
            quantity = order['quantity']
            amount_usdc = order['amount_usdc']
            
            try:
                # In dry-run mode, randomly simulate some orders
                if dry_run:
                    import random
                    rand = random.random()
                    if rand > 0.8:  # 20% filled
                        logger.info(f"[DRY RUN] Simulating order {order_id} as filled")
                        self._handle_filled_order(order, dry_run)
                        processed += 1
                    elif rand > 0.6:  # 20% refunded
                        logger.info(f"[DRY RUN] Simulating order {order_id} as refunded")
                        self._handle_refunded_order(order, dry_run)
                        processed += 1
                    continue
                
                # Get stock token address
                stock_token_address = self.config.get_stock_token_address(stock_ticker)
                
                if order_type == 'buy':
                    # Buy order: sent USDC, expecting stock tokens or USDC refund
                    # First check for stock tokens (expected outcome)
                    
                    stock_balance = self.blockchain.get_token_balance(stock_token_address, wallet_address)
                    logger.debug(f"Buy order {order_id}: checking stock balance = {stock_balance:.6f} {stock_ticker}")
                    
                    if stock_balance >= MIN_STOCK_BALANCE:
                        # Received stock tokens - order filled
                        expected_quantity = quantity * 0.95  # Allow 5% slippage
                        
                        if stock_balance >= expected_quantity:
                            logger.info(f"Buy order {order_id} FILLED: received {stock_balance:.6f} {stock_ticker}")
                            self._handle_filled_order(order, dry_run)
                            processed += 1
                        else:
                            logger.debug(f"Buy order {order_id}: stock balance too low ({stock_balance:.6f} < {expected_quantity:.6f}), waiting...")
                    else:
                        # No stock tokens yet, check for USDC refund
                        usdc_balance = self.blockchain.get_usdc_balance(wallet_address)
                        logger.debug(f"Buy order {order_id}: no stock tokens, checking USDC balance = {usdc_balance:.2f}")
                        
                        if usdc_balance >= MIN_USDC_BALANCE:
                            # Received USDC back - order refunded/expired
                            expected_refund = amount_usdc * 0.95  # Allow 5% for fees
                            
                            if usdc_balance >= expected_refund:
                                logger.info(f"Buy order {order_id} REFUNDED: received {usdc_balance:.2f} USDC back")
                                self._handle_refunded_order(order, dry_run)
                                processed += 1
                            else:
                                logger.debug(f"Buy order {order_id}: USDC balance too low ({usdc_balance:.2f} < {expected_refund:.2f}), waiting...")
                        else:
                            # No significant balance yet - order still pending
                            logger.debug(f"Buy order {order_id}: still pending (no significant balance)")
                
                elif order_type == 'sell':
                    # Sell order: sent stock tokens, expecting USDC or stock token refund
                    # First check for USDC (expected outcome)
                    
                    usdc_balance = self.blockchain.get_usdc_balance(wallet_address)
                    logger.debug(f"Sell order {order_id}: checking USDC balance = {usdc_balance:.2f}")
                    
                    if usdc_balance >= MIN_USDC_BALANCE:
                        # Received USDC - order filled
                        expected_usdc = amount_usdc * 0.95  # Allow 5% slippage
                        
                        if usdc_balance >= expected_usdc:
                            logger.info(f"Sell order {order_id} FILLED: received {usdc_balance:.2f} USDC")
                            self._handle_filled_order(order, dry_run)
                            processed += 1
                        else:
                            logger.debug(f"Sell order {order_id}: USDC balance too low ({usdc_balance:.2f} < {expected_usdc:.2f}), waiting...")
                    else:
                        # No USDC yet, check for stock token refund
                        stock_balance = self.blockchain.get_token_balance(stock_token_address, wallet_address)
                        logger.debug(f"Sell order {order_id}: no USDC, checking stock balance = {stock_balance:.6f} {stock_ticker}")
                        
                        if stock_balance >= MIN_STOCK_BALANCE:
                            # Received stock tokens back - order refunded/expired
                            expected_quantity = quantity * 0.95  # Allow 5% for fees
                            
                            if stock_balance >= expected_quantity:
                                logger.info(f"Sell order {order_id} REFUNDED: received {stock_balance:.6f} {stock_ticker} back")
                                self._handle_refunded_order(order, dry_run)
                                processed += 1
                            else:
                                logger.debug(f"Sell order {order_id}: stock balance too low ({stock_balance:.6f} < {expected_quantity:.6f}), waiting...")
                        else:
                            # No significant balance yet - order still pending
                            logger.debug(f"Sell order {order_id}: still pending (no significant balance)")
                
            except Exception as e:
                logger.error(f"Error checking order {order_id}: {e}", exc_info=True)
                continue
        
        if processed > 0:
            logger.info(f"Processed {processed} orders (filled or refunded)")
        
        return processed
    
    def _handle_refunded_order(self, order: Dict[str, Any], dry_run: bool = False):
        """
        Handle a refunded/expired order.
        
        Args:
            order: Order dict
            dry_run: If True, simulate only
        """
        order_id = order['order_id']
        order_type = order['order_type']
        wallet_address = order['wallet_address']
        stock_ticker = order['stock_ticker']
        quantity = order['quantity']
        
        logger.info(f"Processing refunded {order_type} order: {order_id}")
        
        # Update order status to expired/cancelled
        self.db.update_order_status(order_id, 'expired')
        
        if order_type == 'buy':
            # Buy order refunded - received USDC back
            # No position was created, wallet can place new order
            logger.info(f"Buy order {order_id} refunded, wallet {wallet_address} can retry")
            
            # Get current USDC balance
            usdc_balance = self.blockchain.get_usdc_balance(wallet_address)
            
            MIN_ORDER_VALUE = 5.0
            if usdc_balance >= MIN_ORDER_VALUE:
                # Automatically retry the buy order with current balance
                logger.info(f"Retrying buy order for {stock_ticker} with {usdc_balance:.2f} USDC")
                
                customer_id = self.place_buy_order(
                    wallet_address=wallet_address,
                    stock_ticker=stock_ticker,
                    usdc_amount=usdc_balance,
                    dry_run=dry_run
                )
                
                if customer_id:
                    logger.info(f"Retry buy order placed: {customer_id}")
                else:
                    logger.error(f"Failed to place retry buy order for {wallet_address}")
            else:
                logger.warning(f"Wallet {wallet_address} has insufficient balance to retry: {usdc_balance:.2f}")
                # Return funds to vault and abandon wallet
                self.wallet_manager.abandon_wallet(wallet_address, dry_run=dry_run)
        
        elif order_type == 'sell':
            # Sell order refunded - received stock tokens back
            # Position still exists, can place new sell order
            logger.info(f"Sell order {order_id} refunded, wallet {wallet_address} still holds position")
            
            # Check if position still exists
            position = self.db.get_position(wallet_address)
            if position:
                # Calculate holding time
                first_buy_date = datetime.strptime(position['first_buy_date'], '%Y-%m-%d').date()
                holding_days = (date.today() - first_buy_date).days
                
                if holding_days >= self.config.max_hold_days:
                    # Hold time exceeded, sell at market price
                    logger.info(f"Position held too long ({holding_days} days), selling at market price")
                    
                    customer_id = self.place_sell_order(
                        wallet_address=wallet_address,
                        stock_ticker=stock_ticker,
                        quantity=position['quantity'],
                        dry_run=dry_run
                    )
                    
                    if customer_id:
                        logger.info(f"New sell order placed: {customer_id}")
                else:
                    # Still within hold time, place sell order with profit target again
                    logger.info(f"Retrying sell order with profit target (held {holding_days} days)")
                    
                    customer_id = self.place_sell_order(
                        wallet_address=wallet_address,
                        stock_ticker=stock_ticker,
                        quantity=position['quantity'],
                        dry_run=dry_run
                    )
                    
                    if customer_id:
                        logger.info(f"Retry sell order placed: {customer_id}")
            else:
                logger.warning(f"Position not found for {wallet_address} after sell refund")
    
    def _handle_filled_order(self, order: Dict[str, Any], dry_run: bool = False):
        """
        Handle a filled order (buy or sell).
        
        Args:
            order: Order dict
            dry_run: If True, simulate only
        """
        order_id = order['order_id']
        order_type = order['order_type']
        wallet_address = order['wallet_address']
        stock_ticker = order['stock_ticker']
        quantity = order['quantity']
        limit_price = order['limit_price']
        
        logger.info(f"Processing filled {order_type} order: {order_id}")
        
        # Update order status
        self.db.update_order_status(order_id, 'filled', filled_at=datetime.now())
        
        if order_type == 'buy':
            # Create or update position
            total_cost = quantity * limit_price
            self.db.create_or_update_position(
                wallet_address=wallet_address,
                stock_ticker=stock_ticker,
                quantity=quantity,
                avg_buy_price=limit_price,
                total_cost_usdc=total_cost,
                first_buy_date=date.today()
            )
            logger.info(f"Position created: {wallet_address} - {quantity} {stock_ticker}")
            
            # Immediately place sell order with target profit
            logger.info(f"Placing immediate sell order with {self.config.min_profit}% target profit")
            
            try:
                # Calculate target sell price
                target_price = limit_price * (1 + self.config.min_profit / 100)
                target_price = round(target_price, 2)
                
                logger.info(f"Target sell price: ${target_price:.2f} (buy: ${limit_price:.2f}, profit: {self.config.min_profit}%)")
                
                # Place sell order immediately
                sell_customer_id = self.place_sell_order(
                    wallet_address=wallet_address,
                    stock_ticker=stock_ticker,
                    quantity=quantity,
                    dry_run=dry_run
                )
                
                if sell_customer_id:
                    logger.info(f"Immediate sell order placed: {sell_customer_id}")
                else:
                        logger.error(f"Failed to place immediate sell order for {wallet_address}")
                    
            except Exception as e:
                logger.error(f"Error placing immediate sell order: {e}", exc_info=True)
        
        elif order_type == 'sell':
            # Get position to calculate profit/loss
            position = self.db.get_position(wallet_address)
            
            if position:
                avg_buy_price = position['avg_buy_price']
                total_cost = position['total_cost_usdc']
                
                # Calculate profit/loss
                sell_amount = quantity * limit_price
                profit_loss = sell_amount - total_cost
                profit_pct = (profit_loss / total_cost) * 100
                
                logger.info(f"Sell completed: P/L=${profit_loss:.2f} ({profit_pct:.2f}%)")
                
                # Update order with profit/loss
                self.db.update_order_status(order_id, 'filled', 
                                          filled_at=datetime.now(),
                                          profit_loss=profit_loss)
                
                # Delete position
                self.db.delete_position(wallet_address)
                
                # Handle wallet based on profit/loss
                should_abandon = False
                
                if profit_loss > 0:
                    # Profitable trade - reuse wallet
                    logger.info(f"Profitable trade, reusing wallet {wallet_address}")
                    self.wallet_manager.reuse_wallet(wallet_address, dry_run=dry_run)
                else:
                    # Loss - increment loss count
                    loss_count = self.db.increment_loss_count(wallet_address)
                    logger.info(f"Loss recorded for {wallet_address}, count: {loss_count}")
                    
                    if loss_count >= self.config.max_loss_traders:
                        # Too many losses - abandon wallet
                        logger.info(f"Max losses reached for {wallet_address}, abandoning")
                        self.wallet_manager.abandon_wallet(wallet_address, dry_run=dry_run)
                        should_abandon = True
                    else:
                        # Try again with same wallet
                        logger.info(f"Reusing wallet {wallet_address} after loss (count: {loss_count})")
                        self.wallet_manager.reuse_wallet(wallet_address, dry_run=dry_run)
                if not should_abandon:
                    logger.info(f"Placing immediate buy order for reused wallet {wallet_address}")
                    
                    # Get updated wallet info
                    wallet_updated = self.db.get_wallet(wallet_address)
                    if wallet_updated:
                        new_stock = wallet_updated['assigned_stock']
                        
                        # Check USDC balance
                        usdc_balance = self.blockchain.get_usdc_balance(wallet_address)
                        
                        # Ensure minimum order value of $5
                        MIN_ORDER_VALUE = 5.0
                        if usdc_balance >= MIN_ORDER_VALUE:
                            # Place buy order with current balance
                            logger.info(f"Creating buy order for {new_stock} with {usdc_balance:.2f} USDC")
                            
                            customer_id = self.place_buy_order(
                                wallet_address=wallet_address,
                                stock_ticker=new_stock,
                                usdc_amount=usdc_balance,
                                dry_run=dry_run
                            )
                            
                            if customer_id:
                                logger.info(f"Immediate buy order placed: {customer_id}")
                            else:
                                logger.error(f"Failed to place immediate buy order for {wallet_address}")
                        else:
                            logger.warning(f"Wallet {wallet_address} has insufficient USDC for minimum order: {usdc_balance:.2f} < ${MIN_ORDER_VALUE}")
                            # Transfer wallet to vault since it can't trade anymore
                            logger.info(f"Returning insufficient funds to vault and abandoning wallet")
                            self.wallet_manager.abandon_wallet(wallet_address, dry_run=dry_run)
                    else:
                        logger.error(f"Failed to retrieve updated wallet info for {wallet_address}")
    
    
    def get_trading_stats(self) -> Dict[str, Any]:
        """
        Get trading statistics.
        
        Returns:
            Dict with trading stats
        """
        # Get all orders
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            
            # Total orders
            cursor.execute("SELECT COUNT(*) FROM orders")
            total_orders = cursor.fetchone()[0]
            
            # Buy orders
            cursor.execute("SELECT COUNT(*) FROM orders WHERE order_type = 'buy'")
            total_buys = cursor.fetchone()[0]
            
            # Sell orders
            cursor.execute("SELECT COUNT(*) FROM orders WHERE order_type = 'sell'")
            total_sells = cursor.fetchone()[0]
            
            # Filled orders
            cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'filled'")
            filled_orders = cursor.fetchone()[0]
            
            # Total profit/loss
            cursor.execute("SELECT SUM(profit_loss) FROM orders WHERE profit_loss IS NOT NULL")
            total_pnl = cursor.fetchone()[0] or 0.0
            
            # Profitable trades
            cursor.execute("SELECT COUNT(*) FROM orders WHERE profit_loss > 0")
            profitable_trades = cursor.fetchone()[0]
            
            # Active positions
            cursor.execute("SELECT COUNT(*) FROM positions WHERE quantity > 0")
            active_positions = cursor.fetchone()[0]
        
        return {
            'total_orders': total_orders,
            'total_buys': total_buys,
            'total_sells': total_sells,
            'filled_orders': filled_orders,
            'total_pnl': round(total_pnl, 2),
            'profitable_trades': profitable_trades,
            'active_positions': active_positions,
            'win_rate': round(profitable_trades / total_sells * 100, 2) if total_sells > 0 else 0.0
        }


def create_trade_manager(database, blockchain_client, sharesdao_client, 
                        wallet_manager, config) -> TradeManager:
    """
    Create trade manager.
    
    Args:
        database: Database instance
        blockchain_client: BlockchainClient instance
        sharesdao_client: SharesDAOClient instance
        wallet_manager: WalletManager instance
        config: Config instance
        
    Returns:
        TradeManager instance
    """
    return TradeManager(database, blockchain_client, sharesdao_client, 
                       wallet_manager, config)
