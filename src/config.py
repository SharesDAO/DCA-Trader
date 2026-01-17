"""
Configuration management module.
Loads settings from config.yaml and environment variables.
"""

import os
import yaml
import logging
from pathlib import Path
from typing import Dict, List, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logger
logger = logging.getLogger(__name__)


class Config:
    """Main configuration class."""
    
    def __init__(self, config_path: str = None):
        """
        Initialize configuration.
        
        Args:
            config_path: Path to config.yaml file
        """
        if config_path is None:
            # Default to config/config.yaml in project root
            project_root = Path(__file__).parent.parent
            config_path = project_root / "config" / "config.yaml"
        
        self.config_path = Path(config_path)
        self.chains_path = self.config_path.parent / "chains.yaml"
        
        # Load configurations
        self._load_config()
        self._load_chains()
        self._load_env()
        
    def _load_config(self):
        """Load main configuration from config.yaml."""
        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        # Blockchain
        self.blockchain = config.get('blockchain', 'arbitrum')
        
        # Pool addresses (will be populated from API)
        pool = config.get('pool', {})
        self.mint_address = pool.get('mint_address', '')
        self.burn_address = pool.get('burn_address', '')
        
        # Trading parameters
        trading = config.get('trading', {})
        self.max_usd_per_wallet = trading.get('max_usd_per_wallet', 100)
        self.min_usd_per_wallet = trading.get('min_usd_per_wallet', 10)
        self.gas_per_wallet = trading.get('gas_per_wallet', 0.002)
        self.order_expiry_days = trading.get('order_expiry_days', 7)
        self.min_profit = trading.get('min_profit', 5)
        self.max_hold_days = trading.get('max_hold_days', 30)
        self.max_loss_traders = trading.get('max_loss_traders', 3)
        self.sell_slippage = trading.get('sell_slippage', 0.005)  # Default 0.5%
        
        # Pools filter (optional list of pool tickers to trade)
        # Can be a list of tickers or empty list to use all available pools
        stocks_config = config.get('stocks', [])
        if isinstance(stocks_config, list):
            self.stock_filter = stocks_config  # List of pool tickers to filter, or empty for all
        elif isinstance(stocks_config, dict):
            # Legacy format: use keys as filter
            self.stock_filter = list(stocks_config.keys())
        else:
            self.stock_filter = []
        
        # Actual trading pools will be populated from SharesDAO API
        self.trading_stocks = {}  # Maps ticker to pool info
        
        # Monitoring
        monitoring = config.get('monitoring', {})
        self.check_interval_seconds = monitoring.get('check_interval_seconds', 300)
        self.portfolio_cache_refresh = monitoring.get('portfolio_cache_refresh', 3)
        
        # Dry-run mode
        self.dry_run = config.get('dry_run', False)
        
    def _load_chains(self):
        """Load chain configurations from chains.yaml."""
        with open(self.chains_path, 'r') as f:
            self.chains = yaml.safe_load(f)
    
    def _load_env(self):
        """Load sensitive data from environment variables."""
        self.vault_private_key = os.getenv('VAULT_PRIVATE_KEY')
        self.database_encryption_key = os.getenv('DATABASE_ENCRYPTION_KEY')
        self.alchemy_api_key = os.getenv('ALCHEMY_API_KEY')
        
        # SharesDAO API
        self.sharesdao_api_url = os.getenv('SHARESDAO_API_URL', 'https://api.sharesdao.com:8443')
        
        # Derive vault address from private key
        self.vault_address = self._derive_address_from_private_key(self.vault_private_key)
    
    def _derive_address_from_private_key(self, private_key: str) -> str:
        """
        Derive Ethereum address from private key.
        
        Args:
            private_key: Private key (with or without 0x prefix)
            
        Returns:
            Ethereum address (checksummed)
        """
        if not private_key:
            return None
        
        try:
            from eth_account import Account
            
            # Ensure private key has 0x prefix
            if not private_key.startswith('0x'):
                private_key = '0x' + private_key
            
            # Derive account from private key
            account = Account.from_key(private_key)
            return account.address
        except Exception as e:
            logger.error(f"Failed to derive address from private key: {e}")
            return None
    
    def get_chain_config(self, blockchain: str = None) -> Dict[str, Any]:
        """
        Get configuration for a specific blockchain.
        
        Args:
            blockchain: Blockchain name (default: current blockchain from config)
            
        Returns:
            Chain configuration dictionary
        """
        chain = blockchain or self.blockchain
        if chain not in self.chains:
            raise ValueError(f"Unknown blockchain: {chain}")
        return self.chains[chain]
    
    def get_rpc_url(self, blockchain: str = None) -> str:
        """
        Get RPC URL for a specific blockchain.
        
        Args:
            blockchain: Blockchain name (default: current blockchain from config)
            
        Returns:
            RPC URL string
        """
        chain = blockchain or self.blockchain
        chain_config = self.get_chain_config(chain)
        
        # Use Alchemy for all supported chains
        alchemy_network = chain_config.get('alchemy_network')
        if not alchemy_network:
            raise ValueError(f"No alchemy_network configured for {chain}")
        
        if not self.alchemy_api_key:
            raise ValueError("ALCHEMY_API_KEY not set in environment variables")
        
        return f"https://{alchemy_network}.g.alchemy.com/v2/{self.alchemy_api_key}"
    
    def validate(self) -> List[str]:
        """
        Validate configuration.
        
        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        
        # Check required fields
        if not self.vault_private_key:
            errors.append("VAULT_PRIVATE_KEY not set in .env")
        elif not self.vault_address:
            errors.append("Failed to derive vault address from VAULT_PRIVATE_KEY")
        
        # Pool addresses will be loaded from API, so we don't validate them here
        
        if not self.database_encryption_key:
            errors.append("DATABASE_ENCRYPTION_KEY not set in .env")
        
        if not self.alchemy_api_key:
            errors.append("ALCHEMY_API_KEY not set in .env")
        
        # Note: trading_stocks (pools) will be populated from API at runtime
        # Validation happens after API call
        
        # Check parameter ranges
        if self.max_usd_per_wallet <= self.min_usd_per_wallet:
            errors.append("max_usd_per_wallet must be greater than min_usd_per_wallet")
        
        if self.min_profit <= 0:
            errors.append("min_profit must be positive")
        
        if self.max_hold_days <= 0:
            errors.append("max_hold_days must be positive")
        
        return errors
    
    def set_trading_stocks(self, stocks: Dict[str, Any]):
        """
        Set trading pools from API data.
        
        Args:
            stocks: Dict of pool info from SharesDAO API (ticker -> pool_info)
        """
        # Apply filter if specified
        if self.stock_filter:
            filtered_stocks = {k: v for k, v in stocks.items() if k in self.stock_filter}
            self.trading_stocks = filtered_stocks
            logger.info(f"Filtered pools: {len(filtered_stocks)} out of {len(stocks)}")
        else:
            self.trading_stocks = stocks
            logger.info(f"Using all available pools: {len(stocks)}")
    
    def get_pool_by_ticker(self, ticker: str) -> Dict[str, Any]:
        """
        Get pool information by ticker.
        
        Args:
            ticker: Pool ticker symbol
            
        Returns:
            Pool info dictionary, or None if not found
        """
        return self.trading_stocks.get(ticker)
    
    def get_stock_token_address(self, ticker: str) -> str:
        """
        Get token address for a pool ticker.
        
        Args:
            ticker: Pool ticker symbol
            
        Returns:
            Token address
            
        Raises:
            ValueError: If ticker not found or no token address
        """
        if ticker not in self.trading_stocks:
            raise ValueError(f"Unknown pool ticker: {ticker}")
        
        token_address = self.trading_stocks[ticker].get('asset_id')
        if not token_address:
            raise ValueError(f"No token address for pool {ticker}")
        
        return token_address
    
    def get_stock_tickers(self) -> list:
        """
        Get list of available pool tickers.
        
        Returns:
            List of pool ticker symbols
        """
        return list(self.trading_stocks.keys())
    
    def get_mint_address(self) -> str:
        """
        Get pool mint address (for buy orders).
        All pools share the same mint address.
        
        Returns:
            Mint address from first pool (same for all pools)
        """
        if self.trading_stocks:
            first_pool = next(iter(self.trading_stocks.values()))
            return first_pool.get('mint_address', self.mint_address)
        return self.mint_address
    
    def get_burn_address(self) -> str:
        """
        Get pool burn address (for sell orders).
        All pools share the same burn address.
        
        Returns:
            Burn address from first pool (same for all pools)
        """
        if self.trading_stocks:
            first_pool = next(iter(self.trading_stocks.values()))
            return first_pool.get('burn_address', self.burn_address)
        return self.burn_address
    
    def __repr__(self):
        """String representation of config."""
        return f"<Config blockchain={self.blockchain} dry_run={self.dry_run}>"


# Global config instance (can be imported by other modules)
config = None


def load_config(config_path: str = None) -> Config:
    """
    Load and return global config instance.
    
    Args:
        config_path: Optional path to config.yaml
        
    Returns:
        Config instance
    """
    global config
    config = Config(config_path)
    return config


def get_config() -> Config:
    """
    Get global config instance.
    
    Returns:
        Config instance
        
    Raises:
        RuntimeError: If config not loaded yet
    """
    if config is None:
        raise RuntimeError("Config not loaded. Call load_config() first.")
    return config
