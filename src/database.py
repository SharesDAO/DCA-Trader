"""
Database management module.
Handles SQLite database operations with encrypted private key storage.
"""

import sqlite3
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any
from cryptography.fernet import Fernet
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class Database:
    """SQLite database manager with encryption support."""
    
    def __init__(self, db_path: str = None, encryption_key: str = None):
        """
        Initialize database connection.
        
        Args:
            db_path: Path to SQLite database file
            encryption_key: Fernet encryption key for private keys
        """
        if db_path is None:
            # Default to data/wallets.db in project root
            project_root = Path(__file__).parent.parent
            db_path = project_root / "data" / "wallets.db"
        
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        if not encryption_key:
            raise ValueError("encryption_key is required")
        
        self.cipher = Fernet(encryption_key.encode())
        
        # Initialize database schema
        self._init_schema()
    
    @contextmanager
    def get_connection(self):
        """
        Context manager for database connections.
        
        Yields:
            sqlite3.Connection
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Enable column access by name
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            conn.close()
    
    def _init_schema(self):
        """Create database tables if they don't exist."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Wallets table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS wallets (
                    address TEXT PRIMARY KEY,
                    private_key_encrypted TEXT NOT NULL,
                    blockchain TEXT NOT NULL,
                    assigned_stock TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    loss_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_trade_at TIMESTAMP
                )
            """)
            
            # Orders table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    wallet_address TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    stock_ticker TEXT NOT NULL,
                    amount_usdc REAL NOT NULL,
                    quantity REAL NOT NULL,
                    limit_price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    profit_loss REAL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    filled_at TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    FOREIGN KEY (wallet_address) REFERENCES wallets(address)
                )
            """)
            
            # Positions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    wallet_address TEXT PRIMARY KEY,
                    stock_ticker TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    avg_buy_price REAL NOT NULL,
                    total_cost_usdc REAL NOT NULL,
                    first_buy_date DATE NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (wallet_address) REFERENCES wallets(address)
                )
            """)
            
            # Create indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_wallets_status ON wallets(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_wallet ON orders(wallet_address)")
            
            logger.info("Database schema initialized")
    
    def encrypt_private_key(self, private_key: str) -> str:
        """
        Encrypt a private key.
        
        Args:
            private_key: Plain text private key
            
        Returns:
            Encrypted private key
        """
        return self.cipher.encrypt(private_key.encode()).decode()
    
    def decrypt_private_key(self, encrypted_key: str) -> str:
        """
        Decrypt a private key.
        
        Args:
            encrypted_key: Encrypted private key
            
        Returns:
            Plain text private key
        """
        return self.cipher.decrypt(encrypted_key.encode()).decode()
    
    # Wallet operations
    
    def create_wallet(self, address: str, private_key: str, blockchain: str, 
                     assigned_stock: str, status: str = 'active') -> bool:
        """
        Create a new wallet record.
        
        Args:
            address: Wallet address
            private_key: Private key (will be encrypted)
            blockchain: Blockchain name
            assigned_stock: Assigned stock ticker
            status: Wallet status (default: 'active')
            
        Returns:
            True if successful
        """
        try:
            encrypted_key = self.encrypt_private_key(private_key)
            
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO wallets (address, private_key_encrypted, blockchain, assigned_stock, status)
                    VALUES (?, ?, ?, ?, ?)
                """, (address, encrypted_key, blockchain, assigned_stock, status))
            
            logger.info(f"Created wallet {address} for {assigned_stock} on {blockchain}")
            return True
        except Exception as e:
            logger.error(f"Failed to create wallet {address}: {e}")
            return False
    
    def get_wallet(self, address: str) -> Optional[Dict[str, Any]]:
        """
        Get wallet by address.
        
        Args:
            address: Wallet address
            
        Returns:
            Wallet dict or None
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM wallets WHERE address = ?", (address,))
            row = cursor.fetchone()
            
            if row:
                wallet = dict(row)
                wallet['private_key'] = self.decrypt_private_key(wallet['private_key_encrypted'])
                del wallet['private_key_encrypted']
                return wallet
        return None
    
    def get_active_wallets(self, blockchain: str = None) -> List[Dict[str, Any]]:
        """
        Get all active wallets.
        
        Args:
            blockchain: Optional blockchain filter
            
        Returns:
            List of wallet dicts
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            if blockchain:
                cursor.execute(
                    "SELECT * FROM wallets WHERE status = 'active' AND blockchain = ?",
                    (blockchain,)
                )
            else:
                cursor.execute("SELECT * FROM wallets WHERE status = 'active'")
            
            wallets = []
            for row in cursor.fetchall():
                wallet = dict(row)
                wallet['private_key'] = self.decrypt_private_key(wallet['private_key_encrypted'])
                del wallet['private_key_encrypted']
                wallets.append(wallet)
            
            return wallets
    
    def get_wallets_by_status(self, blockchain: str, status: str) -> List[Dict[str, Any]]:
        """
        Get all wallets with a specific status.
        
        Args:
            blockchain: Blockchain name
            status: Wallet status (e.g., 'pending_funding', 'active', 'abandoned')
            
        Returns:
            List of wallet dicts
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM wallets WHERE status = ? AND blockchain = ?",
                (status, blockchain)
            )
            
            wallets = []
            for row in cursor.fetchall():
                wallet = dict(row)
                wallet['private_key'] = self.decrypt_private_key(wallet['private_key_encrypted'])
                del wallet['private_key_encrypted']
                wallets.append(wallet)
            
            return wallets
    
    def update_wallet_status(self, address: str, status: str) -> bool:
        """
        Update wallet status.
        
        Args:
            address: Wallet address
            status: New status (active/abandoned/pending)
            
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE wallets SET status = ? WHERE address = ?",
                    (status, address)
                )
            logger.info(f"Updated wallet {address} status to {status}")
            return True
        except Exception as e:
            logger.error(f"Failed to update wallet {address} status: {e}")
            return False
    
    def increment_loss_count(self, address: str) -> int:
        """
        Increment wallet loss count.
        
        Args:
            address: Wallet address
            
        Returns:
            New loss count
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE wallets SET loss_count = loss_count + 1 WHERE address = ?",
                (address,)
            )
            cursor.execute("SELECT loss_count FROM wallets WHERE address = ?", (address,))
            return cursor.fetchone()[0]
    
    def reset_loss_count(self, address: str) -> bool:
        """
        Reset wallet loss count to 0.
        
        Args:
            address: Wallet address
            
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE wallets SET loss_count = 0 WHERE address = ?",
                    (address,)
                )
            return True
        except Exception as e:
            logger.error(f"Failed to reset loss count for {address}: {e}")
            return False
    
    def update_wallet_stock(self, address: str, stock_ticker: str) -> bool:
        """
        Update wallet's assigned stock.
        
        Args:
            address: Wallet address
            stock_ticker: New stock ticker
            
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE wallets SET assigned_stock = ? WHERE address = ?",
                    (stock_ticker, address)
                )
            logger.info(f"Updated wallet {address} stock to {stock_ticker}")
            return True
        except Exception as e:
            logger.error(f"Failed to update wallet {address} stock: {e}")
            return False
    
    # Order operations
    
    def create_order(self, order_id: str, wallet_address: str, order_type: str,
                    stock_ticker: str, amount_usdc: float, quantity: float,
                    limit_price: float, expires_at: datetime) -> bool:
        """
        Create a new order record.
        
        Args:
            order_id: Unique order ID
            wallet_address: Wallet address
            order_type: 'buy' or 'sell'
            stock_ticker: Stock ticker
            amount_usdc: USDC amount
            quantity: Stock quantity
            limit_price: Limit price
            expires_at: Order expiration datetime
            
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO orders 
                    (order_id, wallet_address, order_type, stock_ticker, amount_usdc, 
                     quantity, limit_price, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (order_id, wallet_address, order_type, stock_ticker, amount_usdc,
                      quantity, limit_price, expires_at))
            
            logger.info(f"Created {order_type} order {order_id} for {wallet_address}")
            return True
        except Exception as e:
            logger.error(f"Failed to create order {order_id}: {e}")
            return False
    
    def update_order_status(self, order_id: str, status: str, 
                           filled_at: datetime = None, profit_loss: float = None,
                           quantity: float = None) -> bool:
        """
        Update order status.
        
        Args:
            order_id: Order ID
            status: New status
            filled_at: Optional fill datetime
            profit_loss: Optional profit/loss amount
            quantity: Optional updated quantity (actual filled amount)
            
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if quantity is not None:
                    cursor.execute("""
                        UPDATE orders 
                        SET status = ?, filled_at = ?, profit_loss = ?, quantity = ?
                        WHERE order_id = ?
                    """, (status, filled_at, profit_loss, quantity, order_id))
                else:
                    cursor.execute("""
                        UPDATE orders 
                        SET status = ?, filled_at = ?, profit_loss = ?
                        WHERE order_id = ?
                    """, (status, filled_at, profit_loss, order_id))
            
            logger.info(f"Updated order {order_id} status to {status}")
            return True
        except Exception as e:
            logger.error(f"Failed to update order {order_id}: {e}")
            return False
    
    def get_pending_orders(self) -> List[Dict[str, Any]]:
        """
        Get all pending orders.
        
        Returns:
            List of order dicts
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM orders WHERE status = 'pending'")
            return [dict(row) for row in cursor.fetchall()]
    
    def get_wallet_orders(self, wallet_address: str) -> List[Dict[str, Any]]:
        """
        Get all orders for a wallet.
        
        Args:
            wallet_address: Wallet address
            
        Returns:
            List of order dicts
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM orders WHERE wallet_address = ? ORDER BY created_at DESC",
                (wallet_address,)
            )
            return [dict(row) for row in cursor.fetchall()]
    
    # Position operations
    
    def create_or_update_position(self, wallet_address: str, stock_ticker: str,
                                  quantity: float, avg_buy_price: float,
                                  total_cost_usdc: float, first_buy_date: date = None) -> bool:
        """
        Create or update a position.
        
        Args:
            wallet_address: Wallet address
            stock_ticker: Stock ticker
            quantity: Stock quantity
            avg_buy_price: Average buy price
            total_cost_usdc: Total cost in USDC
            first_buy_date: First buy date (for new positions)
            
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Check if position exists
                cursor.execute(
                    "SELECT * FROM positions WHERE wallet_address = ?",
                    (wallet_address,)
                )
                existing = cursor.fetchone()
                
                if existing:
                    # Update existing position
                    # Note: first_buy_date is NOT updated - it should remain the original buy date
                    cursor.execute("""
                        UPDATE positions 
                        SET quantity = ?, avg_buy_price = ?, total_cost_usdc = ?, 
                            updated_at = CURRENT_TIMESTAMP
                        WHERE wallet_address = ?
                    """, (quantity, avg_buy_price, total_cost_usdc, wallet_address))
                else:
                    # Create new position
                    if first_buy_date is None:
                        first_buy_date = date.today()
                    
                    cursor.execute("""
                        INSERT INTO positions 
                        (wallet_address, stock_ticker, quantity, avg_buy_price, 
                         total_cost_usdc, first_buy_date)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (wallet_address, stock_ticker, quantity, avg_buy_price,
                          total_cost_usdc, first_buy_date))
            
            logger.info(f"Updated position for {wallet_address}: {quantity} {stock_ticker}")
            return True
        except Exception as e:
            logger.error(f"Failed to update position for {wallet_address}: {e}")
            return False
    
    def get_position(self, wallet_address: str) -> Optional[Dict[str, Any]]:
        """
        Get position for a wallet.
        
        Args:
            wallet_address: Wallet address
            
        Returns:
            Position dict or None
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM positions WHERE wallet_address = ?", (wallet_address,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_all_positions(self) -> List[Dict[str, Any]]:
        """
        Get all positions.
        
        Returns:
            List of position dicts
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM positions WHERE quantity > 0")
            return [dict(row) for row in cursor.fetchall()]
    
    def delete_wallet(self, address: str) -> bool:
        """
        Delete a wallet and its associated orders and positions from the database.
        
        Args:
            address: Wallet address
            
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM positions WHERE wallet_address = ?", (address,))
                cursor.execute("DELETE FROM orders WHERE wallet_address = ?", (address,))
                cursor.execute("DELETE FROM wallets WHERE address = ?", (address,))
            logger.info(f"Deleted wallet {address} and associated records")
            return True
        except Exception as e:
            logger.error(f"Failed to delete wallet {address}: {e}")
            return False
    
    def delete_position(self, wallet_address: str) -> bool:
        """
        Delete a position (after selling all stocks).
        
        Args:
            wallet_address: Wallet address
            
        Returns:
            True if successful
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM positions WHERE wallet_address = ?", (wallet_address,))
            logger.info(f"Deleted position for {wallet_address}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete position for {wallet_address}: {e}")
            return False


def init_database(db_path: str = None, encryption_key: str = None) -> Database:
    """
    Initialize database.
    
    Args:
        db_path: Path to database file
        encryption_key: Encryption key
        
    Returns:
        Database instance
    """
    return Database(db_path, encryption_key)
