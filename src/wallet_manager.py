"""
Wallet manager module.
Handles wallet creation, funding, and lifecycle management.
"""

import logging
import random
import time
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class WalletManager:
    """Manages wallet creation and lifecycle."""
    
    def __init__(self, database, blockchain_client, stock_selector, config):
        """
        Initialize wallet manager.
        
        Args:
            database: Database instance
            blockchain_client: BlockchainClient instance
            stock_selector: StockSelector instance
            config: Config instance
        """
        self.db = database
        self.blockchain = blockchain_client
        self.stock_selector = stock_selector
        self.config = config
        
        logger.info("Wallet manager initialized")
    
    def create_new_wallet(self, dry_run: bool = False) -> Optional[Dict[str, Any]]:
        """
        Create a new wallet with funding and stock assignment.
        If funding fails, wallet is saved with 'pending_funding' status for retry in next iteration.
        
        Args:
            dry_run: If True, simulate only
            
        Returns:
            Wallet dict or None on failure
        """
        # Generate new account
        address, private_key = self.blockchain.create_account()
        logger.info(f"Generated new wallet: {address}")
        
        # Get active wallets for balanced stock selection
        active_wallets = self.db.get_active_wallets(self.config.blockchain)
        
        # Assign stock (balanced allocation)
        assigned_stock = self.stock_selector.assign_balanced_stock(active_wallets)
        
        # Random funding amount (ensure minimum $5 for trading)
        MIN_TRADING_AMOUNT = 5.0
        min_amount = max(self.config.min_usd_per_wallet, MIN_TRADING_AMOUNT)
        
        funding_amount = random.uniform(
            min_amount,
            self.config.max_usd_per_wallet
        )
        funding_amount = round(funding_amount, 2)
        
        # Save wallet to database with 'pending_funding' status
        try:
            success = self.db.create_wallet(
                address=address,
                private_key=private_key,
                blockchain=self.config.blockchain,
                assigned_stock=assigned_stock,
                status='pending_funding'
            )
            
            if not success:
                logger.error(f"Failed to save wallet {address} to database")
                return None
                
            logger.info(f"Wallet {address} saved to database with 'pending_funding' status")
        except Exception as e:
            logger.error(f"Failed to save wallet to database: {e}")
            return None
        
        # Try to fund the wallet
        result = self.fund_wallet(address, private_key, assigned_stock, funding_amount, dry_run)
        
        if result:
            # Update status to active
            self.db.update_wallet_status(address, 'active')
            logger.info(f"Successfully created and funded wallet {address}")
            return result
        else:
            logger.warning(f"Wallet {address} created but funding failed, will retry in next iteration")
            return None
    
    def fund_wallet(self, address: str, private_key: str, assigned_stock: str, 
                    funding_amount: float, dry_run: bool = False) -> Optional[Dict[str, Any]]:
        """
        Fund a wallet with ETH/BNB and USDC.
        
        Args:
            address: Wallet address
            private_key: Wallet private key
            assigned_stock: Assigned stock ticker
            funding_amount: USDC amount to transfer
            dry_run: If True, simulate only
            
        Returns:
            Wallet dict if successful, None otherwise
        """
        try:
            native_token = self.blockchain.chain_config.get('native_token', 'ETH')
            logger.info(f"Funding {address} with {funding_amount} USDC and {self.config.gas_per_wallet} {native_token} for {assigned_stock}")
            
            # Check vault has enough native token (ETH/BNB) for gas
            vault_native_balance = self.blockchain.get_native_balance(self.config.vault_address)
            gas_reserve = 0.001  # Reserve some for vault's own transactions
            required_native = self.config.gas_per_wallet + gas_reserve
            
            if vault_native_balance < required_native:
                logger.error(f"Insufficient {native_token} in vault: {vault_native_balance:.6f} < {required_native:.6f}")
                logger.error(f"Please add more {native_token} to vault address: {self.config.vault_address}")
                return None
            
            logger.debug(f"Vault {native_token} balance: {vault_native_balance:.6f} (sufficient)")
            
            # Step 1: Transfer USDC first (only transfer native token after USDC succeeds)
            # Check if wallet already has USDC (from previous failed attempt)
            current_usdc_balance = self.blockchain.get_usdc_balance(address)
            if current_usdc_balance >= (funding_amount * 0.5):
                logger.info(f"Wallet already has {current_usdc_balance:.2f} USDC, skipping USDC transfer")
            else:
                # Transfer USDC from vault
                tx_hash = self.blockchain.transfer_usdc(
                    self.config.vault_private_key,
                    address,
                    funding_amount,
                    dry_run=dry_run
                )
                
                if not tx_hash:
                    logger.error(f"Failed to fund wallet {address} with USDC, aborting funding")
                    return None
                
                logger.info(f"USDC transfer confirmed: {tx_hash}")
            
            # Step 2: Transfer native token (ETH/BNB) for gas only after USDC transfer succeeds
            # Check if wallet already has gas (from previous failed attempt)
            current_gas_balance = self.blockchain.get_native_balance(address)
            if current_gas_balance >= (self.config.gas_per_wallet * 0.5):
                logger.info(f"Wallet already has {current_gas_balance:.6f} {native_token}, skipping gas transfer")
            else:
                # Transfer native token (ETH/BNB) for gas
                gas_tx_hash = self.blockchain.transfer_native_token(
                    self.config.vault_private_key,
                    address,
                    self.config.gas_per_wallet,
                    dry_run=dry_run
                )
                
                if not gas_tx_hash:
                    logger.error(f"Failed to transfer gas to wallet {address}")
                    # Note: USDC already transferred, but gas transfer failed
                    # Wallet can still use USDC if it has some gas from elsewhere
                    return None
                
                logger.info(f"Gas transfer confirmed: {gas_tx_hash}")
            
            wallet = {
                'address': address,
                'private_key': private_key,
                'blockchain': self.config.blockchain,
                'assigned_stock': assigned_stock,
                'balance': funding_amount,
            }
            
            return wallet
            
        except Exception as e:
            logger.error(f"Error funding wallet {address}: {e}")
            return None
    
    def retry_pending_funding_wallets(self, dry_run: bool = False) -> int:
        """
        Retry funding for all pending_funding wallets.
        
        Args:
            dry_run: If True, simulate only
            
        Returns:
            Number of wallets successfully funded
        """
        # Get all pending_funding wallets
        pending_wallets = self.db.get_wallets_by_status(self.config.blockchain, 'pending_funding')
        
        if not pending_wallets:
            return 0
        
        logger.info(f"Found {len(pending_wallets)} wallet(s) with pending funding, retrying...")
        
        success_count = 0
        for wallet in pending_wallets:
            address = wallet['address']
            private_key = wallet['private_key']
            assigned_stock = wallet['assigned_stock']
            
            # Use a default funding amount (could also store this in DB)
            funding_amount = (self.config.min_usd_per_wallet + self.config.max_usd_per_wallet) / 2
            
            logger.info(f"Retrying funding for wallet {address}")
            result = self.fund_wallet(address, private_key, assigned_stock, funding_amount, dry_run)
            
            if result:
                # Update status to active
                self.db.update_wallet_status(address, 'active')
                logger.info(f"Successfully funded wallet {address}, status updated to 'active'")
                success_count += 1
            else:
                logger.warning(f"Funding still failed for {address}, will retry in next iteration")
        
        return success_count
    
    def get_active_wallets(self) -> List[Dict[str, Any]]:
        """
        Get all active wallets for current blockchain.
        
        Returns:
            List of wallet dicts
        """
        return self.db.get_active_wallets(self.config.blockchain)
    
    def get_wallet(self, address: str) -> Optional[Dict[str, Any]]:
        """
        Get wallet by address.
        
        Args:
            address: Wallet address
            
        Returns:
            Wallet dict or None
        """
        return self.db.get_wallet(address)
    
    def abandon_wallet(self, address: str, dry_run: bool = False) -> bool:
        """
        Abandon a wallet and return funds to vault.
        
        Args:
            address: Wallet address
            dry_run: If True, simulate only
            
        Returns:
            True if successful
        """
        try:
            wallet = self.db.get_wallet(address)
            if not wallet:
                logger.error(f"Wallet not found: {address}")
                return False
            
            logger.info(f"Abandoning wallet {address}")
            
            native_token = self.blockchain.chain_config.get('native_token', 'ETH')
            
            # Check USDC balance
            usdc_balance = self.blockchain.get_usdc_balance(address)
            
            if usdc_balance > 0.01:  # Only transfer if significant balance
                logger.info(f"Returning {usdc_balance} USDC to vault")
                
                tx_hash = self.blockchain.transfer_usdc(
                    wallet['private_key'],
                    self.config.vault_address,
                    usdc_balance,
                    dry_run=dry_run
                )
                
                if not tx_hash:
                    logger.warning(f"Failed to return USDC from {address} to vault")
            
            # Check native token balance and return to vault
            native_balance = self.blockchain.get_native_balance(address)
            min_native_to_return = 0.0005  # Minimum to make it worth the gas cost
            gas_cost_estimate = self.config.get_gas_cost_estimate()  # Get from chain config
            
            if native_balance > (min_native_to_return + gas_cost_estimate):
                # Calculate amount to send (keep enough for gas)
                amount_to_return = native_balance - gas_cost_estimate
                logger.info(f"Returning {amount_to_return:.6f} {native_token} to vault (keeping {gas_cost_estimate:.6f} for gas)")
                
                native_tx_hash = self.blockchain.transfer_native_token(
                    wallet['private_key'],
                    self.config.vault_address,
                    amount_to_return,
                    dry_run=dry_run
                )
                
                if native_tx_hash:
                    logger.info(f"Recovered {amount_to_return:.6f} {native_token} from {address}")
                else:
                    logger.warning(f"Failed to return {native_token} from {address} to vault")
            else:
                logger.debug(f"Skipping {native_token} recovery from {address} - balance too low: {native_balance:.6f}")
            
            # Update wallet status
            self.db.update_wallet_status(address, 'abandoned')
            
            logger.info(f"Wallet {address} abandoned successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to abandon wallet {address}: {e}")
            return False
    
    def reuse_wallet(self, address: str, dry_run: bool = False) -> bool:
        """
        Reuse a wallet after a profitable trade.
        Resets loss count and assigns new stock.
        
        Args:
            address: Wallet address
            dry_run: If True, simulate only
            
        Returns:
            True if successful
        """
        try:
            wallet = self.db.get_wallet(address)
            if not wallet:
                logger.error(f"Wallet not found: {address}")
                return False
            
            logger.info(f"Reusing wallet {address}")
            
            # Get active wallets for balanced selection
            active_wallets = self.db.get_active_wallets(self.config.blockchain)
            
            # Assign new stock
            new_stock = self.stock_selector.assign_balanced_stock(active_wallets)
            self.db.update_wallet_stock(address, new_stock)
            
            # Check USDC balance
            usdc_balance = self.blockchain.get_usdc_balance(address)
            
            # Ensure minimum tradable amount
            MIN_TRADING_AMOUNT = 5.0
            min_required = max(self.config.min_usd_per_wallet, MIN_TRADING_AMOUNT)
            
            # If balance too low, add funds from vault
            if usdc_balance < min_required:
                needed = min_required - usdc_balance
                logger.info(f"Wallet balance low ({usdc_balance:.2f}), adding {needed:.2f} USDC")
                
                tx_hash = self.blockchain.transfer_usdc(
                    self.config.vault_private_key,
                    address,
                    needed,
                    dry_run=dry_run
                )
                
                if not tx_hash:
                    logger.warning(f"Failed to add funds to {address}")
                    return False
            
            logger.info(f"Wallet {address} ready for reuse with {new_stock}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to reuse wallet {address}: {e}")
            return False
    
    def check_vault_balance(self) -> float:
        """
        Check vault USDC balance.
        
        Returns:
            USDC balance
        """
        return self.blockchain.get_usdc_balance(self.config.vault_address)
    
    def can_create_new_wallet(self) -> bool:
        """
        Check if vault has enough balance to create new wallet.
        
        Returns:
            True if sufficient balance
        """
        vault_balance = self.check_vault_balance()
        
        # Need at least 2x min amount (buffer for multiple wallets)
        min_required = self.config.min_usd_per_wallet * 2
        
        return vault_balance >= min_required
    
    def ensure_wallet_has_gas(self, wallet_address: str, dry_run: bool = False) -> bool:
        """
        Check if wallet has enough gas, and refill if needed.
        
        Args:
            wallet_address: Wallet address to check
            dry_run: If True, simulate only
            
        Returns:
            True if wallet has sufficient gas or was successfully refilled
        """
        try:
            native_token = self.blockchain.chain_config.get('native_token', 'ETH')
            
            # Check wallet's current gas balance
            current_balance = self.blockchain.get_native_balance(wallet_address)
            min_required_gas = self.config.gas_per_wallet * 0.3  # Alert if below 30% of original allocation
            
            if current_balance >= min_required_gas:
                logger.debug(f"{wallet_address} has sufficient gas: {current_balance:.6f} {native_token}")
                return True
            
            # Wallet needs refill
            refill_amount = self.config.gas_per_wallet  # Refill to full amount
            logger.warning(f"{wallet_address} low on gas: {current_balance:.6f} {native_token} (< {min_required_gas:.6f})")
            logger.info(f"Refilling {wallet_address} with {refill_amount:.6f} {native_token}")
            
            # Check vault has enough native token
            vault_native_balance = self.blockchain.get_native_balance(self.config.vault_address)
            gas_reserve = 0.005  # Reserve for vault's own transactions
            required_native = refill_amount + gas_reserve
            
            if vault_native_balance < required_native:
                logger.error(f"Cannot refill - insufficient {native_token} in vault: {vault_native_balance:.6f} < {required_native:.6f}")
                logger.error(f"Please add more {native_token} to vault address: {self.config.vault_address}")
                return False
            
            # Transfer gas from vault to wallet
            tx_hash = self.blockchain.transfer_native_token(
                self.config.vault_private_key,
                wallet_address,
                refill_amount,
                dry_run=dry_run
            )
            
            if tx_hash:
                logger.info(f"Successfully refilled {wallet_address} with {refill_amount:.6f} {native_token} - TX: {tx_hash}")
                return True
            else:
                logger.error(f"Failed to refill gas for {wallet_address}")
                return False
                
        except Exception as e:
            logger.error(f"Error checking/refilling gas for {wallet_address}: {e}")
            return False
    
    def check_all_wallets_gas(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Check and refill gas for all active wallets.
        
        Args:
            dry_run: If True, simulate only
            
        Returns:
            Dict with summary of checks and refills
        """
        logger.info("Checking gas levels for all active wallets...")
        
        active_wallets = self.db.get_active_wallets(self.config.blockchain)
        
        if not active_wallets:
            logger.info("No active wallets to check")
            return {
                'wallets_checked': 0,
                'wallets_refilled': 0,
                'wallets_sufficient': 0,
                'wallets_failed': 0
            }
        
        native_token = self.blockchain.chain_config.get('native_token', 'ETH')
        wallets_refilled = 0
        wallets_sufficient = 0
        wallets_failed = 0
        
        for wallet in active_wallets:
            wallet_address = wallet['address']
            current_balance = self.blockchain.get_native_balance(wallet_address)
            min_required = self.config.gas_per_wallet * 0.3
            
            if current_balance >= min_required:
                wallets_sufficient += 1
            else:
                # Try to refill
                success = self.ensure_wallet_has_gas(wallet_address, dry_run=dry_run)
                if success:
                    wallets_refilled += 1
                else:
                    wallets_failed += 1
        
        summary = {
            'wallets_checked': len(active_wallets),
            'wallets_refilled': wallets_refilled,
            'wallets_sufficient': wallets_sufficient,
            'wallets_failed': wallets_failed
        }
        
        logger.info(f"Gas check complete: {wallets_sufficient} sufficient, {wallets_refilled} refilled, {wallets_failed} failed")
        return summary
    
    def collect_abandoned_wallets_native_token(self, dry_run: bool = False, 
                                               min_usdc_threshold: float = 1.0) -> Dict[str, Any]:
        """
        Collect native tokens (ETH/BNB) from wallets with almost zero USDC balance.
        Scans all wallets (active, pending_funding, abandoned) and collects ETH/BNB
        from those with USDC balance below threshold.
        
        Args:
            dry_run: If True, simulate only
            min_usdc_threshold: Maximum USDC balance to consider wallet as "almost zero" (default: 1.0)
            
        Returns:
            Dict with summary of collection
        """
        logger.info(f"Collecting native tokens from wallets with USDC balance < ${min_usdc_threshold:.2f}...")
        
        # Get all wallets regardless of status (active, pending_funding, abandoned)
        active_wallets = self.db.get_active_wallets(self.config.blockchain)
        pending_wallets = self.db.get_wallets_by_status(self.config.blockchain, 'pending_funding')
        abandoned_wallets = self.db.get_wallets_by_status(self.config.blockchain, 'abandoned')
        
        # Combine all wallets
        all_wallets = active_wallets + pending_wallets + abandoned_wallets
        
        if not all_wallets:
            logger.info("No wallets found")
            return {
                'wallets_checked': 0,
                'wallets_collected': 0,
                'total_collected': 0.0,
                'errors': [],
                'wallets_skipped_usdc': 0
            }
        
        native_token = self.blockchain.chain_config.get('native_token', 'ETH')
        wallets_collected = 0
        wallets_skipped_usdc = 0
        total_collected = 0.0
        errors = []
        
        # Gas cost estimate for native token transfer (get from chain config)
        gas_cost_estimate = self.config.get_gas_cost_estimate()
        
        for wallet in all_wallets:
            wallet_address = wallet['address']
            wallet_status = wallet.get('status', 'active')
            
            try:
                # Check USDC balance first
                usdc_balance = self.blockchain.get_usdc_balance(wallet_address)
                
                if usdc_balance >= min_usdc_threshold:
                    logger.debug(f"Skipping {wallet_address} ({wallet_status}) - USDC balance ${usdc_balance:.2f} >= ${min_usdc_threshold:.2f}")
                    wallets_skipped_usdc += 1
                    continue
                
                # Get current native token balance
                native_balance = self.blockchain.get_native_balance(wallet_address)
                
                if native_balance <= gas_cost_estimate:
                    logger.debug(f"Skipping {wallet_address} ({wallet_status}) - native balance too low: {native_balance:.6f} {native_token} (need > {gas_cost_estimate:.6f} for gas)")
                    continue
                
                # Calculate amount to send (keep enough for gas)
                amount_to_return = native_balance - gas_cost_estimate
                
                logger.info(
                    f"Collecting {amount_to_return:.6f} {native_token} from {wallet_address} "
                    f"({wallet_status}, USDC: ${usdc_balance:.2f}, {native_token}: {native_balance:.6f})"
                )
                
                # Transfer native token to vault
                tx_hash = self.blockchain.transfer_native_token(
                    wallet['private_key'],
                    self.config.vault_address,
                    amount_to_return,
                    dry_run=dry_run
                )
                
                if tx_hash:
                    wallets_collected += 1
                    total_collected += amount_to_return
                    logger.info(f"Collected {amount_to_return:.6f} {native_token} from {wallet_address} - TX: {tx_hash}")
                else:
                    error_msg = f"Failed to collect {native_token} from {wallet_address}"
                    logger.error(error_msg)
                    errors.append(error_msg)
                    
            except Exception as e:
                error_msg = f"Error collecting from {wallet_address}: {e}"
                logger.error(error_msg, exc_info=True)
                errors.append(error_msg)
        
        summary = {
            'wallets_checked': len(all_wallets),
            'wallets_collected': wallets_collected,
            'wallets_skipped_usdc': wallets_skipped_usdc,
            'total_collected': total_collected,
            'errors': errors
        }
        
        logger.info(
            f"Collection complete: {wallets_collected}/{len(all_wallets)} wallets collected "
            f"({wallets_skipped_usdc} skipped due to USDC balance >= ${min_usdc_threshold:.2f}), "
            f"total: {total_collected:.6f} {native_token}"
        )
        
        return summary
    
    def get_wallet_stats(self) -> Dict[str, Any]:
        """
        Get wallet statistics.
        
        Returns:
            Dict with wallet stats
        """
        active_wallets = self.get_active_wallets()
        
        # Stock distribution
        stock_dist = self.stock_selector.get_stock_distribution(active_wallets)
        
        # Total balance in wallets
        total_balance = 0.0
        for wallet in active_wallets:
            balance = self.blockchain.get_usdc_balance(wallet['address'])
            total_balance += balance
        
        return {
            'total_active_wallets': len(active_wallets),
            'total_usdc_in_wallets': round(total_balance, 2),
            'vault_balance': round(self.check_vault_balance(), 2),
            'stock_distribution': stock_dist
        }


def create_wallet_manager(database, blockchain_client, stock_selector, config) -> WalletManager:
    """
    Create wallet manager.
    
    Args:
        database: Database instance
        blockchain_client: BlockchainClient instance
        stock_selector: StockSelector instance
        config: Config instance
        
    Returns:
        WalletManager instance
    """
    return WalletManager(database, blockchain_client, stock_selector, config)
