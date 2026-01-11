"""
Stock selector module.
Handles random stock assignment to wallets.
"""

import random
import logging
from typing import List

logger = logging.getLogger(__name__)


class StockSelector:
    """Stock selector with random assignment."""
    
    def __init__(self, available_stocks: List[str] or dict):
        """
        Initialize stock selector.
        
        Args:
            available_stocks: List of available stock tickers or dict with stock info
        """
        if isinstance(available_stocks, dict):
            self.available_stocks = list(available_stocks.keys())
        else:
            self.available_stocks = available_stocks
        
        if not self.available_stocks:
            raise ValueError("available_stocks cannot be empty")
        
        logger.info(f"Stock selector initialized with {len(self.available_stocks)} stocks")
    
    def assign_random_stock(self) -> str:
        """
        Randomly select a stock from available stocks.
        
        Returns:
            Stock ticker
        """
        stock = random.choice(self.available_stocks)
        logger.debug(f"Assigned stock: {stock}")
        return stock
    
    def assign_weighted_stock(self, weights: dict = None) -> str:
        """
        Assign stock with optional weighting.
        
        Args:
            weights: Optional dict of {ticker: weight}
            
        Returns:
            Stock ticker
        """
        if weights:
            # Use weighted random selection
            tickers = list(weights.keys())
            weight_values = list(weights.values())
            stock = random.choices(tickers, weights=weight_values)[0]
        else:
            # Fallback to uniform random
            stock = self.assign_random_stock()
        
        logger.debug(f"Assigned weighted stock: {stock}")
        return stock
    
    def get_stock_distribution(self, wallets: List[dict]) -> dict:
        """
        Get current distribution of stocks across wallets.
        
        Args:
            wallets: List of wallet dicts
            
        Returns:
            Dict of {ticker: count}
        """
        distribution = {}
        for wallet in wallets:
            stock = wallet.get('assigned_stock')
            if stock:
                distribution[stock] = distribution.get(stock, 0) + 1
        
        return distribution
    
    def is_stock_over_allocated(self, stock: str, wallets: List[dict], 
                                max_percentage: float = 0.5) -> bool:
        """
        Check if a stock is over-allocated.
        
        Args:
            stock: Stock ticker to check
            wallets: List of active wallets
            max_percentage: Maximum percentage of wallets for one stock
            
        Returns:
            True if over-allocated
        """
        if not wallets:
            return False
        
        distribution = self.get_stock_distribution(wallets)
        stock_count = distribution.get(stock, 0)
        percentage = stock_count / len(wallets)
        
        return percentage > max_percentage
    
    def assign_balanced_stock(self, wallets: List[dict]) -> str:
        """
        Assign stock while maintaining balance across portfolio.
        Avoids over-allocation to any single stock.
        
        Args:
            wallets: List of existing active wallets
            
        Returns:
            Stock ticker
        """
        if not wallets:
            return self.assign_random_stock()
        
        # Get current distribution
        distribution = self.get_stock_distribution(wallets)
        
        # Calculate weights (inverse of current allocation)
        total_wallets = len(wallets)
        weights = {}
        
        for stock in self.available_stocks:
            current_count = distribution.get(stock, 0)
            # Give higher weight to under-allocated stocks
            weight = max(1, total_wallets - current_count * 2)
            weights[stock] = weight
        
        return self.assign_weighted_stock(weights)


def create_stock_selector(config) -> StockSelector:
    """
    Create stock selector from config.
    
    Args:
        config: Config instance
        
    Returns:
        StockSelector instance
    """
    return StockSelector(config.trading_stocks)
