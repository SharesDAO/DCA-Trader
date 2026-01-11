"""
Blockchain client module.
Handles Web3 connections and blockchain interactions via Alchemy API.
"""

import json
import logging
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
    
    def __init__(self, rpc_url: str, chain_config: dict):
        """
        Initialize blockchain client.
        
        Args:
            rpc_url: RPC endpoint URL (Alchemy)
            chain_config: Chain configuration dict
        """
        self.rpc_url = rpc_url
        self.chain_config = chain_config
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
    
    def get_native_balance(self, address: str) -> float:
        """
        Get native token balance (ETH/BNB).
        
        Args:
            address: Wallet address
            
        Returns:
            Balance in native token
        """
        try:
            checksum_address = Web3.to_checksum_address(address)
            balance_wei = self.w3.eth.get_balance(checksum_address)
            balance = self.w3.from_wei(balance_wei, 'ether')
            return float(balance)
        except Exception as e:
            logger.error(f"Failed to get native balance for {address}: {e}")
            return 0.0
    
    def get_usdc_balance(self, address: str) -> float:
        """
        Get USDC balance.
        
        Args:
            address: Wallet address
            
        Returns:
            USDC balance
        """
        try:
            checksum_address = Web3.to_checksum_address(address)
            balance_raw = self.usdc_contract.functions.balanceOf(checksum_address).call()
            balance = balance_raw / (10 ** self.usdc_decimals)
            return float(balance)
        except Exception as e:
            logger.error(f"Failed to get USDC balance for {address}: {e}")
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
    
    def get_token_balance(self, token_address: str, wallet_address: str) -> float:
        """
        Get ERC-20 token balance.
        
        Args:
            token_address: Token contract address
            wallet_address: Wallet address
            
        Returns:
            Token balance
        """
        try:
            contract = self.get_token_contract(token_address)
            checksum_wallet = Web3.to_checksum_address(wallet_address)
            
            balance_raw = contract.functions.balanceOf(checksum_wallet).call()
            decimals = contract.functions.decimals().call()
            balance = balance_raw / (10 ** decimals)
            
            return float(balance)
        except Exception as e:
            logger.error(f"Failed to get token balance for {wallet_address}: {e}")
            return 0.0
    
    def submit_buy_order(self, from_private_key: str, stock_ticker: str,
                        stock_token_address: str, usdc_amount: float,
                        stock_quantity: float, customer_id: str,
                        mint_address: str, expiry_days: int,
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
                "type": "LIMIT",
                "offer": offer_wei,
                "request": request_wei,
                "token_address": Web3.to_checksum_address(stock_token_address),
                "expiry_days": expiry_days
            }
            
            logger.info(f"Buy order: {usdc_amount} USDC for {stock_quantity} {stock_ticker}")
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
            nonce = self.w3.eth.get_transaction_count(from_address)
            
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
            
            gas_price = self.w3.eth.gas_price
            
            transaction.update({
                'gas': gas_limit,
                'gasPrice': gas_price
            })
            
            # Sign and send
            signed_txn = self.w3.eth.account.sign_transaction(transaction, from_private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.rawTransaction)
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
            logger.error(f"Buy order error: {e}", exc_info=True)
            return None
    
    def submit_sell_order(self, from_private_key: str, stock_ticker: str,
                         stock_token_address: str, stock_quantity: float,
                         usdc_amount: float, customer_id: str,
                         burn_address: str, expiry_days: int,
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
            dry_run: If True, simulate only
            
        Returns:
            Transaction hash or None on failure
        """
        try:
            sender_account = self.get_account(from_private_key)
            from_address = sender_account.address
            
            # Stock tokens use 18 decimals
            # USDC uses self.usdc_decimals
            offer_wei = int(stock_quantity * (10 ** 18))
            request_wei = int(usdc_amount * (10 ** self.usdc_decimals))
            
            # Build memo
            memo = {
                "customer_id": customer_id,
                "type": "LIMIT",
                "offer": offer_wei,
                "request": request_wei,
                "token_address": Web3.to_checksum_address(stock_token_address),
                "expiry_days": expiry_days
            }
            
            logger.info(f"Sell order: {stock_quantity} {stock_ticker} for {usdc_amount} USDC")
            logger.info(f"Memo: {json.dumps(memo)}")
            
            if dry_run:
                logger.info("[DRY RUN] Would submit sell order")
                return f"0xdry_sell_{customer_id}"
            
            # Check stock token balance
            stock_balance = self.get_token_balance(stock_token_address, from_address)
            if stock_balance < stock_quantity:
                logger.error(f"Insufficient stock tokens: {stock_balance} < {stock_quantity}")
                return None
            
            # Get stock token contract
            stock_contract = self.get_token_contract(stock_token_address)
            
            # Encode memo
            memo_json = json.dumps(memo, separators=(',', ':'))
            memo_bytes = memo_json.encode('utf-8')
            
            # Build stock token transfer with memo
            to_checksum = Web3.to_checksum_address(burn_address)
            nonce = self.w3.eth.get_transaction_count(from_address)
            
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
            
            gas_price = self.w3.eth.gas_price
            
            transaction.update({
                'gas': gas_limit,
                'gasPrice': gas_price
            })
            
            # Sign and send
            signed_txn = self.w3.eth.account.sign_transaction(transaction, from_private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.rawTransaction)
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
            logger.error(f"Sell order error: {e}", exc_info=True)
            return None
    
    def transfer_usdc(self, from_private_key: str, to_address: str, 
                     amount: float, dry_run: bool = False) -> Optional[str]:
        """
        Transfer USDC from one address to another.
        
        Args:
            from_private_key: Sender's private key
            to_address: Recipient address
            amount: USDC amount to transfer
            dry_run: If True, simulate only
            
        Returns:
            Transaction hash or None on failure
        """
        try:
            # Get sender account
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
            
            # Check balance
            sender_balance = self.get_usdc_balance(from_address)
            if sender_balance < amount:
                logger.error(f"Insufficient USDC balance: {sender_balance} < {amount}")
                return None
            
            # Build transaction
            nonce = self.w3.eth.get_transaction_count(from_address)
            
            # Estimate gas
            gas_estimate = self.usdc_contract.functions.transfer(
                to_checksum, amount_raw
            ).estimate_gas({'from': from_address})
            
            # Get gas price
            gas_price = self.w3.eth.gas_price
            
            # Build transaction
            transaction = self.usdc_contract.functions.transfer(
                to_checksum, amount_raw
            ).build_transaction({
                'from': from_address,
                'gas': int(gas_estimate * 1.2),  # Add 20% buffer
                'gasPrice': gas_price,
                'nonce': nonce,
                'chainId': self.chain_id
            })
            
            # Sign transaction
            signed_txn = self.w3.eth.account.sign_transaction(
                transaction, from_private_key
            )
            
            # Send transaction
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.rawTransaction)
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
            logger.error(f"USDC transfer error: {e}")
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
    
    return BlockchainClient(rpc_url, chain_config)
