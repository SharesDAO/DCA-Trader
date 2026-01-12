"""
SharesDAO client module.
Handles stock price fetching and order status monitoring via blockchain transactions.
"""

import json
import logging
import requests
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)


# Blockchain type mapping (from SharesDAO API)
BLOCKCHAIN_TYPES = {
    'chia': 1,
    'solana': 5,
    'evm': 6  # All EVM chains (Ethereum, Arbitrum, Base, BNB)
}


class SharesDAOClient:
    """Client for SharesDAO price API and on-chain order monitoring."""
    
    def __init__(self, api_url: str = "https://api.sharesdao.com:8443", blockchain: str = "arbitrum"):
        """
        Initialize SharesDAO client.
        
        Args:
            api_url: SharesDAO API base URL
            blockchain: Blockchain name (ethereum, arbitrum, base, bnb)
        """
        self.api_url = api_url.rstrip('/')
        self.blockchain = blockchain.lower()
        self.session = requests.Session()
        self.session.headers.update({'Content-Type': 'application/json'})
        
        # Cache for stock pools
        self.stock_pools = {}
        
        logger.info(f"SharesDAO client initialized: {api_url}, blockchain: {blockchain}")
    
    def get_pool_list(self, slippage: float = 0.005) -> Dict[str, Dict[str, Any]]:
        """
        Get list of available pools from SharesDAO API.
        Each pool represents a tradable stock ticker.
        
        Args:
            slippage: Slippage tolerance (default 0.5%)
            
        Returns:
            Dict of {ticker: pool_info}
        """
        logger.info(f"Fetching pool list from {self.api_url}/pool/list for blockchain={self.blockchain}")
        
        try:
            url = f"{self.api_url}/pool/list"
            logger.debug(f"POST {url} with payload: {{'type': 2}}")
            
            response = self.session.post(url, json={"type": 2}, timeout=10)
            
            logger.debug(f"Response status: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"Failed to get pool list: {response.status_code}, Response: {response.text[:200]}")
                return {}
            
            pools_data = response.json()
            logger.info(f"Received {len(pools_data)} pools from API")
            
            pools = {}
            
            # Determine blockchain type
            blockchain_type = BLOCKCHAIN_TYPES.get('evm') if self.blockchain in ['ethereum', 'arbitrum', 'base', 'bnb'] else None
            logger.debug(f"Blockchain type for {self.blockchain}: {blockchain_type}")
            
            filtered_count = 0
            for pool in pools_data:
                # Filter by blockchain type
                pool_blockchain = pool.get("blockchain")
                if blockchain_type and pool_blockchain != blockchain_type:
                    filtered_count += 1
                    continue
                
                symbol = pool.get("symbol")
                if not symbol:
                    logger.debug(f"Skipping pool without symbol: {pool.get('pool_id')}")
                    continue
                
                # Parse token_id for EVM chains
                token_id = pool.get("token_id")
                asset_id = token_id
                
                if blockchain_type == BLOCKCHAIN_TYPES['evm'] and isinstance(token_id, str):
                    try:
                        token_id_dict = json.loads(token_id)
                        
                        # Map chain name to find the correct address
                        chain_mapping = {
                            'bnb': ['bnb', 'bsc'],
                            'ethereum': ['ethereum', 'eth'],
                            'arbitrum': ['arbitrum', 'arb'],
                            'base': ['base']
                        }
                        
                        # Try to find address for current chain
                        for chain_key in chain_mapping.get(self.blockchain, [self.blockchain]):
                            if chain_key in token_id_dict:
                                asset_id = token_id_dict[chain_key]
                                break
                        else:
                            # Fallback: use first available address
                            if token_id_dict:
                                asset_id = list(token_id_dict.values())[0]
                                logger.warning(f"Using fallback address for {symbol}: {asset_id}")
                    
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"Failed to parse token_id for {symbol}: {e}")
                        asset_id = token_id
                
                pools[symbol] = {
                    "blockchain": pool.get("blockchain"),
                    "asset_id": asset_id,
                    "mint_address": pool.get("mint_address"),
                    "burn_address": pool.get("burn_address"),
                    "pool_id": pool.get("pool_id")
                }
                logger.debug(f"Added pool: {symbol} (pool_id={pool.get('pool_id')}, asset_id={asset_id})")
            
            logger.info(f"Loaded {len(pools)} pools for {self.blockchain} (filtered out {filtered_count} pools from other blockchains)")
            
            if len(pools) == 0:
                logger.warning(f"No pools found for blockchain={self.blockchain}, blockchain_type={blockchain_type}")
                logger.warning(f"Total pools from API: {len(pools_data)}")
            
            self.stock_pools = pools
            return pools
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error fetching pool list: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error fetching pool list: {e}", exc_info=True)
            return {}
    
    def get_stock_price(self, ticker: str, slippage: float = 0.005) -> Optional[float]:
        """
        Get current price for a pool/stock from SharesDAO API.
        
        Args:
            ticker: Pool ticker symbol
            slippage: Slippage tolerance (default 0.5%)
            
        Returns:
            Buy price (with slippage) in USDC or None on error
        """
        try:
            # Get pool info
            if ticker not in self.stock_pools:
                logger.error(f"Pool {ticker} not found in pool list")
                return None
            
            pool_id = self.stock_pools[ticker].get("pool_id")
            if not pool_id:
                logger.error(f"No pool_id for {ticker}")
                return None
            
            # Get pool details
            url = f"{self.api_url}/pool/{pool_id}"
            response = self.session.get(url, timeout=10)
            
            if response.status_code == 200:
                pool_data = response.json()
                
                # Get buy price (price to buy the stock) with slippage
                buy_price = float(pool_data.get("buy_price", 0))
                buy_price_with_slippage = buy_price * (1 - slippage)
                
                logger.debug(f"{ticker} buy price: ${buy_price_with_slippage:.2f} (base: ${buy_price:.2f})")
                return buy_price_with_slippage
            
            logger.error(f"Failed to get price for {ticker}: {response.status_code}")
            return None
            
        except Exception as e:
            logger.error(f"Error fetching price for {ticker}: {e}")
            return None
    
    def get_stock_sell_price(self, ticker: str, slippage: float = 0.005) -> Optional[float]:
        """
        Get current sell price for a pool/stock.
        
        Args:
            ticker: Pool ticker symbol
            slippage: Slippage tolerance (default 0.5%)
            
        Returns:
            Sell price (with slippage) in USDC or None on error
        """
        try:
            if ticker not in self.stock_pools:
                logger.error(f"Pool {ticker} not found in pool list")
                return None
            
            pool_id = self.stock_pools[ticker].get("pool_id")
            if not pool_id:
                logger.error(f"No pool_id for {ticker}")
                return None
            
            url = f"{self.api_url}/pool/{pool_id}"
            response = self.session.get(url, timeout=10)
            
            if response.status_code == 200:
                pool_data = response.json()
                
                # Get sell price (price to sell the stock) with slippage
                sell_price = float(pool_data.get("sell_price", 0))
                sell_price_with_slippage = sell_price * (1 + slippage)
                
                logger.debug(f"{ticker} sell price: ${sell_price_with_slippage:.2f} (base: ${sell_price:.2f})")
                return sell_price_with_slippage
            
            logger.error(f"Failed to get sell price for {ticker}: {response.status_code}")
            return None
            
        except Exception as e:
            logger.error(f"Error fetching sell price for {ticker}: {e}")
            return None
    
    def get_stock_token_address(self, ticker: str) -> Optional[str]:
        """
        Get token address for a pool/stock ticker.
        
        Args:
            ticker: Pool ticker symbol
            
        Returns:
            Token address or None
        """
        if ticker in self.stock_pools:
            return self.stock_pools[ticker].get("asset_id")
        return None
    
    def get_available_stocks(self) -> List[str]:
        """
        Get list of available pool/stock tickers.
        
        Returns:
            List of ticker symbols
        """
        return list(self.stock_pools.keys())
    
    def get_multiple_prices(self, tickers: List[str]) -> Dict[str, float]:
        """
        Get prices for multiple stocks.
        
        Args:
            tickers: List of stock tickers
            
        Returns:
            Dict of {ticker: price}
        """
        prices = {}
        for ticker in tickers:
            price = self.get_stock_price(ticker)
            if price:
                prices[ticker] = price
        return prices
    
    def decode_transaction_memo(self, tx_data: str) -> Optional[Dict[str, Any]]:
        """
        Decode memo from transaction data.
        
        For ERC20 transfers, memo is appended after standard transfer data.
        Standard ERC20 transfer: 4 bytes selector + 32 bytes address + 32 bytes amount = 68 bytes
        
        Args:
            tx_data: Transaction data hex string
            
        Returns:
            Decoded memo dict or None
        """
        try:
            if not tx_data or len(tx_data) < 138:  # 0x + 136 hex chars
                return None
            
            # Remove 0x prefix
            if tx_data.startswith('0x'):
                tx_data = tx_data[2:]
            
            # Skip standard ERC20 transfer data (136 hex chars = 68 bytes)
            if len(tx_data) > 136:
                memo_hex = tx_data[136:]
                
                try:
                    memo_bytes = bytes.fromhex(memo_hex)
                    memo_text = memo_bytes.decode('utf-8')
                    memo_dict = json.loads(memo_text)
                    
                    logger.debug(f"Decoded memo: {memo_dict}")
                    return memo_dict
                    
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.debug(f"Failed to decode memo: {e}")
                    return None
            
            return None
            
        except Exception as e:
            logger.error(f"Error decoding memo: {e}")
            return None
    
    def check_api_health(self) -> bool:
        """
        Check if SharesDAO API is accessible.
        
        Returns:
            True if API is healthy
        """
        try:
            # Try to get a common stock price as health check
            price = self.get_stock_price("AAPL")
            return price is not None
        except Exception as e:
            logger.error(f"API health check failed: {e}")
            return False


def create_sharesdao_client(config) -> SharesDAOClient:
    """
    Create SharesDAO client from config.
    
    Args:
        config: Config instance
        
    Returns:
        SharesDAOClient instance
    """
    api_url = config.sharesdao_api_url if hasattr(config, 'sharesdao_api_url') else "https://api.sharesdao.com:8443"
    
    logger.info(f"Creating SharesDAO client: URL={api_url}, blockchain={config.blockchain}")
    
    client = SharesDAOClient(api_url, blockchain=config.blockchain)
    
    # Load stock pools on initialization
    logger.info("Loading pools from SharesDAO API...")
    pools = client.get_pool_list()
    
    if not pools:
        logger.error("Failed to load any pools from SharesDAO API")
    else:
        logger.info(f"Successfully loaded {len(pools)} pools")
    
    return client
