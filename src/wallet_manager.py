"""
Wallet manager module.
Handles wallet creation, funding, and lifecycle management.
"""

import logging
import random
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
        
        Args:
            dry_run: If True, simulate only
            
        Returns:
            Wallet dict or None on failure
        """
        try:
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
            
            logger.info(f"Funding {address} with {funding_amount} USDC for {assigned_stock}")
            
            # Transfer USDC from vault
            tx_hash = self.blockchain.transfer_usdc(
                self.config.vault_private_key,
                address,
                funding_amount,
                dry_run=dry_run
            )
            
            if not tx_hash:
                logger.error(f"Failed to fund wallet {address}")
                return None
            
            # Save to database
            success = self.db.create_wallet(
                address=address,
                private_key=private_key,
                blockchain=self.config.blockchain,
                assigned_stock=assigned_stock
            )
            
            if not success:
                logger.error(f"Failed to save wallet {address} to database")
                return None
            
            wallet = {
                'address': address,
                'private_key': private_key,
                'blockchain': self.config.blockchain,
                'assigned_stock': assigned_stock,
                'balance': funding_amount,
                'tx_hash': tx_hash
            }
            
            logger.info(f"Successfully created and funded wallet {address}")
            return wallet
            
        except Exception as e:
            logger.error(f"Failed to create wallet: {e}")
            return None
    
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
                    logger.warning(f"Failed to return funds from {address} to vault")
            
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
            
            # Reset loss count
            self.db.reset_loss_count(address)
            
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
