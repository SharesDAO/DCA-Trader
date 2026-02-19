"""
Blockchain client module.
Handles Web3 connections and blockchain interactions via Alchemy API.
"""

import json
import logging
import threading
import time
from typing import Optional, Dict, Any
from decimal import Decimal
from web3 import Web3
from web3.contract import Contract
from eth_account import Account
from eth_account.signers.local import LocalAccount

logger = logging.getLogger(__name__)

# ERC-20 ABI (minimal, for token operations)
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function"
    }
]


class BlockchainClient:
    """Web3 blockchain client using Alchemy API."""
    
    def __init__(self, rpc_url: str, chain_config: dict, config=None):
        """
        Initialize blockchain client.
        
        Args:
            rpc_url: RPC endpoint URL (Alchemy)
            chain_config: Chain configuration dict
            config: Optional Config instance for accessing gas cost estimates
        """
        self.rpc_url = rpc_url
        self.chain_config = chain_config
        self.config = config  # Store config for gas cost estimates
        self.chain_id = chain_config['chain_id']
        self.usdc_address = Web3.to_checksum_address(chain_config['usdc_address'])
        self.native_token = chain_config['native_token']
        
        # Initialize Web3
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        
        if not self.w3.is_connected():
            raise ConnectionError(f"Failed to connect to {rpc_url}")
        
        logger.info(f"Connected to {chain_config['name']} (Chain ID: {self.chain_id})")
        
        # Initialize USDC contract
        self.usdc_contract = self.w3.eth.contract(
            address=self.usdc_address,
            abi=ERC20_ABI
        )
        
        # Get USDC decimals
        self.usdc_decimals = self.usdc_contract.functions.decimals().call()
        logger.info(f"USDC contract loaded: {self.usdc_address} (decimals: {self.usdc_decimals})")
        
        # Nonce management for preventing conflicts
        self._nonce_lock = threading.Lock()
        self._nonce_cache: Dict[str, int] = {}  # address -> next nonce
    
    def create_account(self) -> tuple[str, str]:
        """
        Create a new Ethereum account.
        
        Returns:
            Tuple of (address, private_key)
        """
        account: LocalAccount = Account.create()
        return account.address, account.key.hex()
    
    def get_account(self, private_key: str) -> LocalAccount:
        """
        Get account from private key.
        
        Args:
            private_key: Private key hex string
            
        Returns:
            LocalAccount instance
        """
        return Account.from_key(private_key)
    
    def get_nonce(self, address: str, pending: bool = True) -> int:
        """
        Get transaction nonce for an address with caching to prevent conflicts.
        Thread-safe for concurrent transactions from same address.
        
        Args:
            address: Wallet address
            pending: If True, include pending transactions (recommended)
            
        Returns:
            Next nonce to use
        """
        checksum_address = Web3.to_checksum_address(address)
        
        with self._nonce_lock:
            try:
                # Get current nonce from blockchain
                if pending:
                    chain_nonce = self.w3.eth.get_transaction_count(checksum_address, 'pending')
                else:
                    chain_nonce = self.w3.eth.get_transaction_count(checksum_address)
                
                # Get cached nonce (if exists)
                cached_nonce = self._nonce_cache.get(checksum_address, 0)
                
                # Use the higher of chain nonce or cached nonce
                nonce = max(chain_nonce, cached_nonce)
                
                # Update cache for next transaction
                self._nonce_cache[checksum_address] = nonce + 1
                
                logger.debug(f"Nonce for {address}: {nonce} (chain={chain_nonce}, cached={cached_nonce}, pending={pending})")
                return nonce
                
            except Exception as e:
                logger.error(f"Failed to get nonce for {address}: {e}")
                raise
    
    def reset_nonce_cache(self, address: str = None):
        """
        Reset nonce cache for an address (or all addresses).
        Useful when transactions fail and need to resync with blockchain.
        
        Args:
            address: Specific address to reset, or None to reset all
        """
        with self._nonce_lock:
            if address:
                checksum_address = Web3.to_checksum_address(address)
                if checksum_address in self._nonce_cache:
                    del self._nonce_cache[checksum_address]
                    logger.debug(f"Reset nonce cache for {address}")
            else:
                self._nonce_cache.clear()
                logger.debug("Reset all nonce caches")
    
    def _is_nonce_error(self, error: Exception) -> bool:
        """
        Check if an error is related to nonce issues.
        
        Args:
            error: Exception to check
            
        Returns:
            True if error is nonce-related
        """
        error_msg = str(error).lower()
        nonce_keywords = ['nonce', 'transaction underpriced', 'replacement transaction underpriced']
        return any(keyword in error_msg for keyword in nonce_keywords)
    
    def get_native_balance(self, address: str, max_retries: int = 3) -> float:
        """
        Get native token balance (ETH/BNB) with retry on network errors.
        
        Args:
            address: Wallet address
            max_retries: Maximum retry attempts for network errors
            
        Returns:
            Balance in native token
        """
        checksum_address = Web3.to_checksum_address(address)
        
        for attempt in range(1, max_retries + 1):
            try:
                balance_wei = self.w3.eth.get_balance(checksum_address)
                balance = self.w3.from_wei(balance_wei, 'ether')
                return float(balance)
            except Exception as e:
                error_msg = str(e).lower()
                is_network_error = any(keyword in error_msg for keyword in [
                    'connection', 'timeout', 'reset', 'refused', 'unreachable'
                ])
                
                if is_network_error and attempt < max_retries:
                    logger.warning(f"[Attempt {attempt}/{max_retries}] Network error getting balance for {address}, retrying...")
                    time.sleep(1)
                    continue
                
                logger.error(f"Failed to get native balance for {address}: {e}")
                return 0.0
        
        return 0.0
    
    def get_usdc_balance(self, address: str, max_retries: int = 3) -> float:
        """
        Get USDC balance with retry on network errors.
        
        Args:
            address: Wallet address
            max_retries: Maximum retry attempts for network errors
            
        Returns:
            USDC balance
        """
        checksum_address = Web3.to_checksum_address(address)
        
        for attempt in range(1, max_retries + 1):
            try:
                balance_raw = self.usdc_contract.functions.balanceOf(checksum_address).call()
                balance = balance_raw / (10 ** self.usdc_decimals)
                return float(balance)
            except Exception as e:
                error_msg = str(e).lower()
                is_network_error = any(keyword in error_msg for keyword in [
                    'connection', 'timeout', 'reset', 'refused', 'unreachable'
                ])
                
                if is_network_error and attempt < max_retries:
                    logger.warning(f"[Attempt {attempt}/{max_retries}] Network error getting USDC balance for {address}, retrying...")
                    time.sleep(1)
                    continue
                
                logger.error(f"Failed to get USDC balance for {address}: {e}")
                return 0.0
        
        return 0.0
    
    def get_token_contract(self, token_address: str) -> Contract:
        """
        Get ERC-20 token contract instance.
        
        Args:
            token_address: Token contract address
            
        Returns:
            Contract instance
        """
        checksum_address = Web3.to_checksum_address(token_address)
        return self.w3.eth.contract(address=checksum_address, abi=ERC20_ABI)
    
    def get_token_balance(self, token_address: str, wallet_address: str, max_retries: int = 3) -> float:
        """
        Get ERC-20 token balance with retry on network errors.
        
        Args:
            token_address: Token contract address
            wallet_address: Wallet address
            max_retries: Maximum retry attempts for network errors
            
        Returns:
            Token balance
        """
        for attempt in range(1, max_retries + 1):
            try:
                contract = self.get_token_contract(token_address)
                checksum_wallet = Web3.to_checksum_address(wallet_address)
                
                balance_raw = contract.functions.balanceOf(checksum_wallet).call()
                decimals = contract.functions.decimals().call()
                balance = balance_raw / (10 ** decimals)
                
                return float(balance)
            except Exception as e:
                error_msg = str(e).lower()
                is_network_error = any(keyword in error_msg for keyword in [
                    'connection', 'timeout', 'reset', 'refused', 'unreachable'
                ])
                
                if is_network_error and attempt < max_retries:
                    logger.warning(f"[Attempt {attempt}/{max_retries}] Network error getting token balance for {wallet_address}, retrying...")
                    time.sleep(1)
                    continue
                
                logger.error(f"Failed to get token balance for {wallet_address}: {e}")
                return 0.0
        
        return 0.0
    
    def submit_buy_order(self, from_private_key: str, stock_ticker: str,
                        stock_token_address: str, usdc_amount: float,
                        stock_quantity: float, customer_id: str,
                        mint_address: str, expiry_days: int,
                        order_type: str = 'LIMIT',
                        dry_run: bool = False) -> Optional[str]:
        """
        Submit a buy order by sending USDC to mint address with memo.
        
        Args:
            from_private_key: Buyer's private key
            stock_ticker: Stock ticker symbol
            stock_token_address: Stock token contract address
            usdc_amount: USDC amount to offer (in USDC, not wei)
            stock_quantity: Stock quantity to request (in tokens, not wei)
            customer_id: Order tracking ID
            mint_address: Pool mint address
            expiry_days: Order expiry in days
            order_type: Order type ('LIMIT' or 'MARKET')
            dry_run: If True, simulate only
            
        Returns:
            Transaction hash or None on failure
        """
        try:
            sender_account = self.get_account(from_private_key)
            from_address = sender_account.address
            
            # USDC uses self.usdc_decimals (usually 6 or 18 depending on chain)
            # Stock tokens always use 18 decimals
            offer_wei = int(usdc_amount * (10 ** self.usdc_decimals))
            request_wei = int(stock_quantity * (10 ** 18))
            
            # Build memo
            memo = {
                "customer_id": customer_id,
                "type": order_type,  # 'LIMIT' or 'MARKET'
                "offer": offer_wei,
                "request": request_wei,
                "token_address": Web3.to_checksum_address(stock_token_address),
                "expiry_days": expiry_days,
                "did_id": from_address
            }
            
            logger.info(f"Buy order: {usdc_amount} USDC for {stock_quantity} {stock_ticker} (type: {order_type})")
            logger.info(f"Memo: {json.dumps(memo)}")
            
            if dry_run:
                logger.info("[DRY RUN] Would submit buy order")
                return f"0xdry_buy_{customer_id}"
            
            # Check USDC balance
            usdc_balance = self.get_usdc_balance(from_address)
            if usdc_balance < usdc_amount:
                logger.error(f"Insufficient USDC: {usdc_balance} < {usdc_amount}")
                return None
            
            # Encode memo as JSON string, then to hex
            memo_json = json.dumps(memo, separators=(',', ':'))
            memo_bytes = memo_json.encode('utf-8')
            
            # Build USDC transfer with memo appended to data
            to_checksum = Web3.to_checksum_address(mint_address)
            # Get nonce (includes pending transactions)
            nonce = self.get_nonce(from_address)
            
            # Standard ERC20 transfer data
            transfer_data = self.usdc_contract.functions.transfer(
                to_checksum, offer_wei
            ).build_transaction({
                'from': from_address,
                'nonce': nonce,
                'chainId': self.chain_id
            })['data']
            
            # Append memo to transaction data
            if isinstance(transfer_data, bytes):
                transfer_data_hex = transfer_data.hex()
            else:
                transfer_data_hex = transfer_data[2:] if transfer_data.startswith('0x') else transfer_data
            
            transaction_data = '0x' + transfer_data_hex + memo_bytes.hex()
            
            # Estimate gas
            transaction = {
                'from': from_address,
                'to': self.usdc_address,
                'data': transaction_data,
                'nonce': nonce,
                'chainId': self.chain_id
            }
            
            try:
                gas_estimate = self.w3.eth.estimate_gas(transaction)
                gas_limit = int(gas_estimate * 1.3)  # 30% buffer
            except:
                gas_limit = 150000  # Safe fallback
            
            transaction['gas'] = gas_limit
            
            # Add EIP-1559 gas parameters
            transaction = self.build_eip1559_transaction(transaction)
            
            # Sign and send
            # Use Account directly (web3.py 6.0+ compatible)
            signed_txn = Account.sign_transaction(transaction, from_private_key)
            # Support both old (rawTransaction) and new (raw_transaction) web3.py versions
            raw_tx = getattr(signed_txn, 'raw_transaction', None) or getattr(signed_txn, 'rawTransaction', None)
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            tx_hash_hex = tx_hash.hex()
            
            logger.info(f"Buy order submitted: {tx_hash_hex}")
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            
            if receipt['status'] == 1:
                logger.info(f"Buy order confirmed: {tx_hash_hex}")
                return tx_hash_hex
            else:
                logger.error(f"Buy order failed: {tx_hash_hex}")
                return None
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Buy order error: {e}", exc_info=True)
            
            # Special handling for nonce errors
            if 'nonce' in error_msg.lower():
                try:
                    # Reset nonce cache for this address
                    self.reset_nonce_cache(from_address)
                    
                    # Get fresh nonce from blockchain for debugging
                    with self._nonce_lock:
                        current_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address))
                        pending_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address), 'pending')
                    logger.error(f"Nonce debug - Address: {from_address}, Current: {current_nonce}, Pending: {pending_nonce}")
                    logger.info(f"Nonce cache reset for {from_address}, will resync on next transaction")
                except:
                    pass
            
            return None
    
    def submit_sell_order(self, from_private_key: str, stock_ticker: str,
                         stock_token_address: str, stock_quantity: float,
                         usdc_amount: float, customer_id: str,
                         burn_address: str, expiry_days: int,
                         order_type: str = 'LIMIT',
                         dry_run: bool = False) -> Optional[str]:
        """
        Submit a sell order by sending stock tokens to burn address with memo.
        
        Args:
            from_private_key: Seller's private key
            stock_ticker: Stock ticker symbol
            stock_token_address: Stock token contract address
            stock_quantity: Stock quantity to offer (in tokens, not wei)
            usdc_amount: USDC amount to request (in USDC, not wei)
            customer_id: Order tracking ID
            burn_address: Pool burn address
            expiry_days: Order expiry in days
            order_type: Order type ('LIMIT' or 'MARKET')
            dry_run: If True, simulate only
            
        Returns:
            Transaction hash or None on failure
        """
        try:
            sender_account = self.get_account(from_private_key)
            from_address = sender_account.address
            
            # Stock tokens use 18 decimals, truncate last 6 digits to avoid
            # rounding dust that could exceed the wallet's actual balance
            # USDC uses self.usdc_decimals
            offer_wei = int(stock_quantity * (10 ** 18)) // (10 ** 6) * (10 ** 6)
            request_wei = int(usdc_amount * (10 ** self.usdc_decimals))
            
            # Build memo
            memo = {
                "customer_id": customer_id,
                "type": order_type,  # 'LIMIT' or 'MARKET'
                "offer": offer_wei,
                "request": request_wei,
                "token_address": Web3.to_checksum_address(stock_token_address),
                "expiry_days": expiry_days,
                "did_id": from_address
            }
            
            logger.info(f"Sell order: {stock_quantity} {stock_ticker} for {usdc_amount} USDC (type: {order_type})")
            logger.info(f"Memo: {json.dumps(memo)}")
            
            if dry_run:
                logger.info("[DRY RUN] Would submit sell order")
                return f"0xdry_sell_{customer_id}"
            
            # Check stock token balance and adjust to sell all available if less than requested
            stock_balance = self.get_token_balance(stock_token_address, from_address)
            
            if stock_balance <= 0:
                logger.error(f"No stock tokens to sell: balance is {stock_balance} {stock_ticker}")
                return None
            
            if stock_balance < stock_quantity:
                difference_pct = ((stock_quantity - stock_balance) / stock_quantity) * 100
                logger.warning(
                    f"Stock balance less than requested: {stock_balance} < {stock_quantity} "
                    f"(diff: {difference_pct:.2f}%), selling entire balance instead"
                )
                
                original_quantity = stock_quantity
                stock_quantity = stock_balance
                offer_wei = int(stock_quantity * (10 ** 18))
                
                adjusted_usdc_amount = (usdc_amount / original_quantity) * stock_quantity
                request_wei = int(adjusted_usdc_amount * (10 ** self.usdc_decimals))
                
                memo["offer"] = offer_wei
                memo["request"] = request_wei
                
                logger.info(f"Adjusted sell order: {stock_quantity:.10f} {stock_ticker} for ${adjusted_usdc_amount:.2f} USDC")
            
            # Get stock token contract
            stock_contract = self.get_token_contract(stock_token_address)
            
            # Encode memo
            memo_json = json.dumps(memo, separators=(',', ':'))
            memo_bytes = memo_json.encode('utf-8')
            
            # Build stock token transfer with memo
            to_checksum = Web3.to_checksum_address(burn_address)
            # Get nonce (includes pending transactions)
            nonce = self.get_nonce(from_address)
            
            # Standard ERC20 transfer data
            transfer_data = stock_contract.functions.transfer(
                to_checksum, offer_wei
            ).build_transaction({
                'from': from_address,
                'nonce': nonce,
                'chainId': self.chain_id
            })['data']
            
            # Append memo to transaction data
            if isinstance(transfer_data, bytes):
                transfer_data_hex = transfer_data.hex()
            else:
                transfer_data_hex = transfer_data[2:] if transfer_data.startswith('0x') else transfer_data
            
            transaction_data = '0x' + transfer_data_hex + memo_bytes.hex()
            
            # Estimate gas
            transaction = {
                'from': from_address,
                'to': Web3.to_checksum_address(stock_token_address),
                'data': transaction_data,
                'nonce': nonce,
                'chainId': self.chain_id
            }
            
            try:
                gas_estimate = self.w3.eth.estimate_gas(transaction)
                gas_limit = int(gas_estimate * 1.3)  # 30% buffer
            except:
                gas_limit = 150000  # Safe fallback
            
            transaction['gas'] = gas_limit
            
            # Add EIP-1559 gas parameters
            transaction = self.build_eip1559_transaction(transaction)
            
            # Sign and send
            # Use Account directly (web3.py 6.0+ compatible)
            signed_txn = Account.sign_transaction(transaction, from_private_key)
            # Support both old (rawTransaction) and new (raw_transaction) web3.py versions
            raw_tx = getattr(signed_txn, 'raw_transaction', None) or getattr(signed_txn, 'rawTransaction', None)
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            tx_hash_hex = tx_hash.hex()
            
            logger.info(f"Sell order submitted: {tx_hash_hex}")
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
            
            if receipt['status'] == 1:
                logger.info(f"Sell order confirmed: {tx_hash_hex}")
                return tx_hash_hex
            else:
                logger.error(f"Sell order failed: {tx_hash_hex}")
                return None
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Sell order error: {e}", exc_info=True)
            
            # Special handling for nonce errors
            if 'nonce' in error_msg.lower():
                try:
                    # Reset nonce cache for this address
                    self.reset_nonce_cache(from_address)
                    
                    # Get fresh nonce from blockchain for debugging
                    with self._nonce_lock:
                        current_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address))
                        pending_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address), 'pending')
                    logger.error(f"Nonce debug - Address: {from_address}, Current: {current_nonce}, Pending: {pending_nonce}")
                    logger.info(f"Nonce cache reset for {from_address}, will resync on next transaction")
                except:
                    pass
            
            return None
    
    def transfer_usdc(self, from_private_key: str, to_address: str, 
                     amount: float, dry_run: bool = False, max_retries: int = 3) -> Optional[str]:
        """
        Transfer USDC from one address to another with automatic retry on nonce errors.
        
        Args:
            from_private_key: Sender's private key
            to_address: Recipient address
            amount: USDC amount to transfer
            dry_run: If True, simulate only
            max_retries: Maximum number of retry attempts for nonce errors
            
        Returns:
            Transaction hash or None on failure
        """
        # Get sender account (outside retry loop)
        sender_account = self.get_account(from_private_key)
        from_address = sender_account.address
        
        # Convert addresses to checksum format
        to_checksum = Web3.to_checksum_address(to_address)
        
        # Convert amount to wei
        amount_raw = int(amount * (10 ** self.usdc_decimals))
        
        logger.info(f"Transferring {amount} USDC from {from_address} to {to_address}")
        
        if dry_run:
            logger.info("[DRY RUN] Would transfer USDC")
            return "0x" + "0" * 64  # Fake tx hash
        
        # Check balance (outside retry loop)
        sender_balance = self.get_usdc_balance(from_address)
        if sender_balance < amount:
            logger.error(f"Insufficient USDC balance: {sender_balance} < {amount}")
            return None
        
        # Retry loop for nonce errors
        for attempt in range(1, max_retries + 1):
            try:
                # Build transaction
                # Get nonce (includes pending transactions)
                nonce = self.get_nonce(from_address)
                
                # Estimate gas
                gas_estimate = self.usdc_contract.functions.transfer(
                    to_checksum, amount_raw
                ).estimate_gas({'from': from_address})
                
                # Build base transaction
                transaction = self.usdc_contract.functions.transfer(
                    to_checksum, amount_raw
                ).build_transaction({
                    'from': from_address,
                    'gas': int(gas_estimate * 1.2),  # Add 20% buffer
                    'nonce': nonce,
                    'chainId': self.chain_id
                })
                
                # Add EIP-1559 gas parameters
                transaction = self.build_eip1559_transaction(transaction)
                
                # Sign transaction
                # Use Account directly (web3.py 6.0+ compatible)
                signed_txn = Account.sign_transaction(transaction, from_private_key)
                
                # Send transaction
                # Support both old (rawTransaction) and new (raw_transaction) web3.py versions
                raw_tx = getattr(signed_txn, 'raw_transaction', None) or getattr(signed_txn, 'rawTransaction', None)
                tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
                tx_hash_hex = tx_hash.hex()
                
                logger.info(f"USDC transfer submitted: {tx_hash_hex}")
                
                # Wait for confirmation
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
                
                if receipt['status'] == 1:
                    logger.info(f"USDC transfer confirmed: {tx_hash_hex}")
                    return tx_hash_hex
                else:
                    logger.error(f"USDC transfer failed: {tx_hash_hex}")
                    return None
                    
            except Exception as e:
                error_msg = str(e)
                is_nonce_error = self._is_nonce_error(e)
                
                if is_nonce_error and attempt < max_retries:
                    # Reset nonce cache and retry
                    self.reset_nonce_cache(from_address)
                    
                    # Get fresh nonce from blockchain for debugging
                    try:
                        with self._nonce_lock:
                            current_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address))
                            pending_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address), 'pending')
                        logger.warning(f"[Attempt {attempt}/{max_retries}] Nonce error - Address: {from_address}, Current: {current_nonce}, Pending: {pending_nonce}")
                        logger.info(f"Nonce cache reset, retrying in 2 seconds...")
                        time.sleep(2)  # Brief delay before retry
                        continue
                    except:
                        pass
                
                # Log error and exit
                logger.error(f"USDC transfer error: {e}")
                if is_nonce_error:
                    try:
                        self.reset_nonce_cache(from_address)
                        with self._nonce_lock:
                            current_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address))
                            pending_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address), 'pending')
                        logger.error(f"Nonce debug - Address: {from_address}, Current: {current_nonce}, Pending: {pending_nonce}")
                    except:
                        pass
                
                return None
        
        logger.error(f"USDC transfer failed after {max_retries} attempts")
        return None
    
    def transfer_native_token(self, from_private_key: str, to_address: str,
                             amount: float, dry_run: bool = False, max_retries: int = 3) -> Optional[str]:
        """
        Transfer native token (ETH/BNB) from one address to another with automatic retry on nonce errors.
        
        Args:
            from_private_key: Sender's private key
            to_address: Recipient address
            amount: Amount in native token (e.g., 0.001 ETH)
            dry_run: If True, simulate only
            max_retries: Maximum number of retry attempts for nonce errors
            
        Returns:
            Transaction hash or None on failure
        """
        # Get sender account (outside retry loop)
        sender_account = self.get_account(from_private_key)
        from_address = sender_account.address
        
        # Convert addresses to checksum format
        to_checksum = Web3.to_checksum_address(to_address)
        
        # Convert amount to wei (18 decimals for all native tokens)
        amount_wei = int(amount * (10 ** 18))
        
        native_token = self.chain_config.get('native_token', 'ETH')
        logger.info(f"Transferring {amount} {native_token} from {from_address} to {to_address}")
        
        if dry_run:
            logger.info("[DRY RUN] Would transfer native token")
            return "0x" + "0" * 64  # Fake tx hash
        
        # Check balance (outside retry loop)
        sender_balance_wei = self.w3.eth.get_balance(from_address)
        sender_balance = sender_balance_wei / (10 ** 18)
        
        # Need to reserve some for gas (use config if available, otherwise fallback)
        if self.config:
            gas_reserve = self.config.get_gas_cost_estimate()
        else:
            # Fallback to chain config or default
            gas_reserve = self.chain_config.get('gas_cost_estimate', 0.0002)
        
        if sender_balance < (amount + gas_reserve):
            logger.error(f"Insufficient {native_token} balance: {sender_balance} < {amount + gas_reserve} (including gas reserve)")
            return None
        
        # Retry loop for nonce errors
        for attempt in range(1, max_retries + 1):
            try:
                # Build transaction
                # Get nonce (includes pending transactions)
                nonce = self.get_nonce(from_address)
                
                # Build base transaction
                transaction = {
                    'from': from_address,
                    'to': to_checksum,
                    'value': amount_wei,
                    'nonce': nonce,
                    'chainId': self.chain_id
                }
                
                # Estimate gas for simple transfer
                try:
                    gas_estimate = self.w3.eth.estimate_gas(transaction)
                    gas_limit = int(gas_estimate * 1.2)  # Add 20% buffer
                except Exception as e:
                    logger.warning(f"Failed to estimate gas for native transfer: {e}")
                    gas_limit = 21000  # Standard ETH transfer gas
                
                transaction['gas'] = gas_limit
                
                # Add EIP-1559 gas parameters
                transaction = self.build_eip1559_transaction(transaction)
                
                # Sign transaction
                signed_txn = Account.sign_transaction(transaction, from_private_key)
                
                # Send transaction
                raw_tx = getattr(signed_txn, 'raw_transaction', None) or getattr(signed_txn, 'rawTransaction', None)
                tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
                tx_hash_hex = tx_hash.hex()
                
                logger.info(f"{native_token} transfer submitted: {tx_hash_hex}")
                
                # Wait for confirmation
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
                
                if receipt['status'] == 1:
                    logger.info(f"{native_token} transfer confirmed: {tx_hash_hex}")
                    return tx_hash_hex
                else:
                    logger.error(f"{native_token} transfer failed: {tx_hash_hex}")
                    return None
                    
            except Exception as e:
                error_msg = str(e)
                is_nonce_error = self._is_nonce_error(e)
                
                if is_nonce_error and attempt < max_retries:
                    # Reset nonce cache and retry
                    self.reset_nonce_cache(from_address)
                    
                    # Get fresh nonce from blockchain for debugging
                    try:
                        with self._nonce_lock:
                            current_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address))
                            pending_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address), 'pending')
                        logger.warning(f"[Attempt {attempt}/{max_retries}] Nonce error - Address: {from_address}, Current: {current_nonce}, Pending: {pending_nonce}")
                        logger.info(f"Nonce cache reset, retrying in 2 seconds...")
                        time.sleep(2)  # Brief delay before retry
                        continue
                    except:
                        pass
                
                # Log error and exit
                logger.error(f"Native token transfer error: {e}")
                if is_nonce_error:
                    try:
                        self.reset_nonce_cache(from_address)
                        with self._nonce_lock:
                            current_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address))
                            pending_nonce = self.w3.eth.get_transaction_count(Web3.to_checksum_address(from_address), 'pending')
                        logger.error(f"Nonce debug - Address: {from_address}, Current: {current_nonce}, Pending: {pending_nonce}")
                    except:
                        pass
                
                return None
        
        logger.error(f"{native_token} transfer failed after {max_retries} attempts")
        return None
    
    def get_transaction_receipt(self, tx_hash: str) -> Optional[dict]:
        """
        Get transaction receipt.
        
        Args:
            tx_hash: Transaction hash
            
        Returns:
            Receipt dict or None
        """
        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
            return dict(receipt)
        except Exception as e:
            logger.error(f"Failed to get receipt for {tx_hash}: {e}")
            return None
    
    def estimate_gas_cost(self, gas_units: int) -> float:
        """
        Estimate gas cost in native token.
        
        Args:
            gas_units: Estimated gas units
            
        Returns:
            Cost in native token
        """
        try:
            gas_price = self.w3.eth.gas_price
            cost_wei = gas_units * gas_price
            cost = self.w3.from_wei(cost_wei, 'ether')
            return float(cost)
        except Exception as e:
            logger.error(f"Failed to estimate gas cost: {e}")
            return 0.0
    
    def get_current_gas_price(self) -> float:
        """
        Get current gas price in Gwei.
        
        Returns:
            Gas price in Gwei
        """
        try:
            gas_price_wei = self.w3.eth.gas_price
            gas_price_gwei = self.w3.from_wei(gas_price_wei, 'gwei')
            return float(gas_price_gwei)
        except Exception as e:
            logger.error(f"Failed to get gas price: {e}")
            return 0.0
    
    def check_native_balance_for_gas(self, address: str, min_balance: float = 0.01) -> bool:
        """
        Check if address has enough native token for gas fees.
        
        Args:
            address: Wallet address
            min_balance: Minimum required balance
            
        Returns:
            True if balance is sufficient
        """
        balance = self.get_native_balance(address)
        return balance >= min_balance
    
    def build_eip1559_transaction(self, base_transaction: dict) -> dict:
        """
        Build transaction with EIP-1559 gas parameters.
        
        Automatically uses EIP-1559 (maxFeePerGas/maxPriorityFeePerGas) if supported,
        otherwise falls back to legacy gasPrice.
        
        Args:
            base_transaction: Base transaction dict without gas parameters
            
        Returns:
            Transaction dict with appropriate gas parameters
        """
        try:
            # Try to get latest block to check if EIP-1559 is supported
            latest_block = self.w3.eth.get_block('latest')
            
            # Check if baseFeePerGas exists (EIP-1559 support)
            if 'baseFeePerGas' in latest_block:
                # EIP-1559 transaction
                base_fee = latest_block['baseFeePerGas']
                
                # Get max priority fee (tip to miner)
                try:
                    max_priority_fee = self.w3.eth.max_priority_fee
                except:
                    # Fallback priority fee (1 Gwei)
                    max_priority_fee = self.w3.to_wei(1, 'gwei')
                
                # Calculate maxFeePerGas: base fee * 2 + priority fee
                # Multiplying by 2 gives buffer for base fee increases
                max_fee_per_gas = (base_fee * 2) + max_priority_fee
                
                base_transaction['maxFeePerGas'] = max_fee_per_gas
                base_transaction['maxPriorityFeePerGas'] = max_priority_fee
                
                logger.debug(f"Using EIP-1559: maxFee={max_fee_per_gas}, priorityFee={max_priority_fee}, baseFee={base_fee}")
            else:
                # Legacy transaction
                gas_price = self.w3.eth.gas_price
                base_transaction['gasPrice'] = gas_price
                logger.debug(f"Using legacy gas: gasPrice={gas_price}")
            
            return base_transaction
            
        except Exception as e:
            logger.warning(f"Error building EIP-1559 transaction, falling back to legacy: {e}")
            # Fallback to legacy gas price
            base_transaction['gasPrice'] = self.w3.eth.gas_price
            return base_transaction


def create_blockchain_client(config) -> BlockchainClient:
    """
    Create blockchain client from config.
    
    Args:
        config: Config instance
        
    Returns:
        BlockchainClient instance
    """
    rpc_url = config.get_rpc_url()
    chain_config = config.get_chain_config()
    
    return BlockchainClient(rpc_url, chain_config, config=config)
