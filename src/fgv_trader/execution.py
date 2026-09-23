"""Market-only execution with persisted signed transactions and verified settlements."""
from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from datetime import datetime, timezone

import requests
from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionNotFound
from fgv_trader.entry_safety import rejection, now_timestamp

log = logging.getLogger(__name__)


def units(amount, decimals):
    return int(Decimal(str(amount)) * 10 ** decimals)


def hex_id(value):
    return str(value).lower().removeprefix('0x')


def entry_rejection_result(error):
    # Keep the original unsigned operation reserved for the next reconcile tick.
    # Never extend its guard deadline or retry a signed order with new bytes.
    return dict(state='PREPARED' if error == 'ENTRY_ABOVE_TRIGGER' else 'FAILED', error=error)


class LiveBroker:
    def __init__(self, store, blockchain, config, api):
        self.store, self.blockchain, self.config, self.api = store, blockchain, config, api
        self.w3 = blockchain.w3
        self.vault = config.vault_address

    def balance(self, address, asset='USDC'):
        address = Web3.to_checksum_address(address)
        if asset == 'NATIVE':
            return float(self.w3.from_wei(self.w3.eth.get_balance(address), 'ether'))
        contract = self.contract(asset)
        return int(contract.functions.balanceOf(address).call()) / 10 ** contract.functions.decimals().call()

    def contract(self, asset):
        return (self.blockchain.usdc_contract if asset == 'USDC' else
                self.blockchain.get_token_contract(self.config.get_stock_token_address(asset)))

    def create_wallet(self, symbol, status='active'):
        account = Account.create()
        if not self.store.db.create_wallet(account.address, account.key.hex(), self.config.blockchain, symbol, status=status):
            raise RuntimeError('Could not persist trading wallet')
        return account.address

    def snapshot(self):
        wallets = self.store.db.get_active_wallets(self.config.blockchain)
        wallets += self.store.db.get_wallets_by_status(self.config.blockchain, 'pending_funding')
        return {address: self.balance(address) for address in [self.vault] + [w['address'] for w in wallets]}

    def balance_units(self, address, asset):
        address = Web3.to_checksum_address(address)
        if asset == 'NATIVE':
            return int(self.w3.eth.get_balance(address))
        return int(self.contract(asset).functions.balanceOf(address).call())

    def _prepare(self, op):
        error = rejection(op)
        if error:
            return dict(state='FAILED', error=error)
        kind = op['kind']
        funding = kind in ('fund', 'gas')
        source = self.vault if funding else op['wallet']
        private_key = (self.config.vault_private_key if funding else
                       self.store.db.get_wallet(source)['private_key'])
        destination = op['wallet'] if funding else self.vault
        asset = 'NATIVE' if kind in ('gas', 'collect') else 'USDC'
        amount = op['amount']
        decimals = 18 if asset == 'NATIVE' else self.contract(asset).functions.decimals().call()
        amount_units = units(amount, decimals)
        if op.get('retirement') and kind == 'gas' and self.balance_units(destination, 'USDC') == 0:
            return dict(state='SETTLED', transferred=0)
        if op.get('sweep_all') and kind in ('sweep', 'collect'):
            amount_units = self.balance_units(source, asset)
            if amount_units == 0:
                return dict(state='SETTLED', transferred=0)
            amount = float(Decimal(amount_units) / 10 ** decimals)
        if kind == 'buy':
            # Fix the spend at preparation time. Signed retries replay identical
            # bytes and must never resize an already submitted order.
            balance_units = self.balance_units(source, 'USDC')
            amount_units = min(amount_units, balance_units) if op.get('spend_exact') else balance_units
            if amount_units < units(self.config.fgv['min_order_usdc'], decimals):
                raise ValueError('Trading wallet USDC is below the minimum buy amount')
            amount = float(Decimal(amount_units) / 10 ** decimals)
            op.setdefault('planned_amount', op['amount'])
            op['amount'] = amount
        if funding:
            amount_units = max(0, amount_units - self.balance_units(destination, asset))
            if amount_units == 0:
                return dict(state='SETTLED', transferred=0)
            amount = float(Decimal(amount_units) / 10 ** decimals)
        memo = None
        if kind in ('buy', 'sell'):
            pool = self.config.trading_stocks[op['symbol']]
            destination = pool.get('mint_address' if kind == 'buy' else 'burn_address')
            destination = destination or (self.config.mint_address if kind == 'buy' else self.config.burn_address)
            quote_at = now_timestamp()
            price = (self.api.get_stock_price(op['symbol'], slippage=0) if kind == 'buy' else
                     self.api.get_stock_sell_price(op['symbol'], slippage=0))
            if not price or price <= 0:
                raise ValueError(f'No execution quote for {op["symbol"]}')
            op.update(execution_quote=price, quote_at=datetime.fromtimestamp(quote_at, timezone.utc).isoformat())
            if kind == 'buy':
                signal = op.get('entry_guard', {}).get('signal', {})
                trigger = signal.get('trigger_low')
                risk = signal.get('risk')
                op['entry_above_trigger'] = bool(trigger is not None and price > trigger)
                op['entry_buffer_r_used'] = ((price - trigger) / risk
                                             if op['entry_above_trigger'] and risk and risk > 0 else 0)
                error = rejection(op, price)
                if error:
                    return entry_rejection_result(error)
            asset = 'USDC' if kind == 'buy' else op['symbol']
            decimals = self.contract(asset).functions.decimals().call()
            if kind == 'sell':
                # The chain balance is authoritative. Never reconstruct an
                # 18-decimal token transfer from the float used for reporting.
                amount_units = self.balance_units(source, asset)
                if amount_units <= 0:
                    raise ValueError('No on-chain shares available for sell; reconciliation required')
                op.setdefault('planned_amount', op['amount'])
                amount = float(Decimal(amount_units) / 10 ** decimals)
                op.update(amount=amount, amount_units=amount_units, token_decimals=decimals)
            stock_decimals = self.contract(op['symbol']).functions.decimals().call()
            quantity = amount / price if kind == 'buy' else amount
            usdc = amount if kind == 'buy' else Decimal(amount_units) * Decimal(str(price)) / 10 ** decimals
            memo = dict(customer_id=op['id'], type='MARKET',
                        offer=amount_units,
                        request=units(quantity, stock_decimals) if kind == 'buy' else units(usdc, self.blockchain.usdc_decimals),
                        token_address=self.config.get_stock_token_address(op['symbol']),
                        expiry_days=op['expiry_days'], did_id=source)
            op.update(estimated_price=price, estimated_quantity=quantity, order_type='MARKET')
        available_units = self.balance_units(source, asset)
        if available_units < amount_units:
            raise ValueError(f'Insufficient {asset} for {kind}: available={available_units}, required={amount_units} base units')
        # Do not allocate a nonce until all fallible read-only preparation is complete.
        tx = {'from': Web3.to_checksum_address(source), 'chainId': self.blockchain.chain_id,
              'to': Web3.to_checksum_address(destination), 'value': amount_units if asset == 'NATIVE' else 0}
        if kind == 'collect':
            tx['value'] = 0  # Estimate before subtracting the maximum gas charge.
        if asset != 'NATIVE':
            contract = self.contract(asset)
            decimals = contract.functions.decimals().call()
            data = contract.functions.transfer(Web3.to_checksum_address(destination), amount_units)._encode_transaction_data()
            tx.update(to=contract.address, data=data + (json.dumps(memo, separators=(',', ':')).encode().hex() if memo else ''))
        tx['gas'] = int(self.w3.eth.estimate_gas(tx) * 1.3)
        tx = self.blockchain.build_eip1559_transaction(tx)
        if kind == 'collect':
            maximum_fee = tx['gas'] * tx.get('maxFeePerGas', tx.get('gasPrice', 0)) / 1e18
            amount = max(0, min(amount, self.balance(source, 'NATIVE')) - maximum_fee)
            if amount <= 0:
                return dict(state='SETTLED', transferred=0)
            tx['value'] = units(amount, 18)
        error = rejection(op, price if kind == 'buy' else None)
        if kind == 'buy' and now_timestamp() - quote_at > self.config.execution.get('max_execution_quote_age_seconds', 5):
            error = 'EXECUTION_QUOTE_STALE'
        if error:
            return dict(state='FAILED', error=error)
        tx['nonce'] = self.blockchain.get_nonce(source)
        if kind == 'buy':
            error = rejection(op, price)
            if now_timestamp() - quote_at > self.config.execution.get('max_execution_quote_age_seconds', 5):
                error = 'EXECUTION_QUOTE_STALE'
            if error:
                self.blockchain.reset_nonce_cache(source)
                return dict(state='FAILED', error=error)
        signed = Account.sign_transaction(tx, private_key)
        raw = getattr(signed, 'raw_transaction', None) or getattr(signed, 'rawTransaction', None)
        op.update(state='SIGNED', tx_hash=Web3.to_hex(signed.hash), raw_tx=Web3.to_hex(raw),
                  transferred=amount, source=source, memo=memo, signed_at=datetime.now(timezone.utc).isoformat())
        # Commit the exact signed transaction BEFORE broadcasting. Recovery rebroadcasts
        # identical bytes; a timeout cannot create a second order with a new nonce.
        self.store.save_operation(op)
        return None

    def _confirmed_receipt(self, tx_hash):
        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None
        if self.w3.eth.block_number - receipt['blockNumber'] + 1 < self.config.execution['confirmations']:
            return None
        return receipt

    def _incoming(self, receipt, token, wallet):
        topic = Web3.keccak(text='Transfer(address,address,uint256)')
        amount = 0
        for event in receipt['logs']:
            topics = event['topics']
            if (event['address'].lower() == token.address.lower() and len(topics) == 3
                    and bytes(topics[0]) == bytes(topic)
                    and bytes(topics[2])[-20:].hex() == wallet.lower().removeprefix('0x')):
                data = event['data']
                amount += int.from_bytes(data, 'big') if isinstance(data, bytes) else int(data, 16)
        return amount / 10 ** token.functions.decimals().call()

    def settlement(self, op):
        pool = self.config.trading_stocks[op['symbol']]
        for status in (4, 3):  # SharesDAO COMPLETED / CANCELLED; never infer from cash balances.
            response = requests.post(f'{self.config.sharesdao_api_url.rstrip("/")}/transaction/pool', json={
                'pool_id': pool['pool_id'], 'status': status, 'start_index': 0,
                'num_of_transactions': self.config.execution.get('transaction_history_limit', 1000),
                'sort_by_ascending': False,
            }, timeout=10)
            response.raise_for_status()
            records = response.json()
            if not isinstance(records, list):
                raise ValueError('SharesDAO transaction response is not a list')
            for record in records:
                if (record.get('customer_id') != op['id'] or
                        hex_id(record.get('crypto_tx_id', '')) != hex_id(op['tx_hash'])):
                    continue
                if int(record.get('status', 0)) != status or not record.get('closed_tx_id'):
                    continue
                receipt = self._confirmed_receipt(record['closed_tx_id'])
                if not receipt or receipt['status'] != 1:
                    continue
                usdc = self._incoming(receipt, self.contract('USDC'), op['wallet'])
                stock = self._incoming(receipt, self.contract(op['symbol']), op['wallet'])
                if op['kind'] == 'buy':
                    if status == 4 and stock > 0:
                        return dict(state='SETTLED', quantity=stock, cost=op['amount'] - usdc,
                                    refund=usdc, settlement_tx=record['closed_tx_id'])
                    if status == 3 and usdc >= op['amount'] - 1e-6:
                        return dict(state='SETTLED', quantity=0, cost=0, refund=usdc,
                                    settlement_tx=record['closed_tx_id'])
                else:
                    if status == 4 and usdc > 0:
                        return dict(state='SETTLED', quantity=max(0, op['amount'] - stock), proceeds=usdc,
                                    remaining=stock, settlement_tx=record['closed_tx_id'])
                    if status == 3 and stock >= op['amount'] - 1e-9:
                        return dict(state='SETTLED', quantity=0, proceeds=0, remaining=stock,
                                    settlement_tx=record['closed_tx_id'])
                # Partial/ambiguous terminal records remain reserved for reconciliation.
                log.error('Unresolved settlement %s: status=%s USDC=%s stock=%s', op['id'], status, usdc, stock)
        return dict(state='SUBMITTED')

    def step(self, op):
        if op['state'] == 'PREPARED':
            if op.get('error') == 'ENTRY_ABOVE_TRIGGER':
                op.pop('error')
            result = self._prepare(op)
            if result:
                log.warning('Operation %s preparation %s: %s', op['id'], result['state'], result.get('error', result['state']))
                return result
        receipt = self._confirmed_receipt(op['tx_hash'])
        if receipt is None:
            # Resubmitting the same signed bytes is idempotent, including after restart.
            try:
                op.setdefault('broadcast_attempt_at', datetime.now(timezone.utc).isoformat())
                self.w3.eth.send_raw_transaction(bytes.fromhex(op['raw_tx'].removeprefix('0x')))
                op.setdefault('broadcast_acknowledged_at', datetime.now(timezone.utc).isoformat())
            except Exception:
                log.warning('Broadcast uncertain for %s; retaining signed transaction', op['id'])
            return dict(state='SIGNED', **{k: op[k] for k in ('tx_hash', 'raw_tx', 'transferred')})
        fee_native = receipt['gasUsed'] * receipt.get('effectiveGasPrice', 0) / 1e18
        if not op.get('submission_at'):
            op['submission_at'] = self.block_time(receipt)
        if receipt['status'] != 1:
            return dict(state='FAILED', gas_native=fee_native, error='Transaction reverted')
        if op['kind'] not in ('buy', 'sell'):
            return dict(state='SETTLED', transferred=op['transferred'], gas_native=fee_native)
        fast = None
        if op['kind'] == 'buy' and self.config.execution.get('chain_settlement_detection', True):
            try:
                delivery = self.chain_buy_delivery(op, receipt)
                if delivery:
                    # Publish verified holdings before potentially slow API I/O.
                    return dict(delivery, gas_native=fee_native)
            except Exception:
                log.warning('On-chain buy delivery lookup unavailable for %s; retaining API fallback', op['id'])
        if op['kind'] == 'sell' and self.config.execution.get('chain_settlement_detection', True):
            try:
                fast = self.chain_sell_settlement(op, receipt)
            except Exception:
                log.warning('On-chain settlement lookup unavailable for %s; using verified API history', op['id'])
        result = dict(fast or self.settlement(op), gas_native=fee_native)
        if op['kind'] == 'buy' and op.get('delivered_units') and result['state'] == 'SETTLED':
            # All independently verified delivery receipts belong to the
            # quarantined wallet; a last receipt must not erase earlier fills.
            observed = op['delivered_units'] / 10 ** op['delivery_decimals']
            if result.get('quantity', 0) < observed:
                result.update(state='SUBMITTED', reconciliation_issue='Observed delivery exceeds matched completion; retaining reservation')
            result['quantity'] = max(result.get('quantity', 0), observed)
            if result.get('cost', 0) == 0 and result['quantity'] > 0:
                result['state'] = 'SUBMITTED'  # Full-refund + stock is ambiguous.
        if result['state'] == 'SETTLED':
            result['recognized_at'] = datetime.now(timezone.utc).isoformat()
            settled_receipt = self._confirmed_receipt(result['settlement_tx'])
            result['settlement_at'] = self.block_time(settled_receipt) if settled_receipt else None
            if result['settlement_at']:
                result['recognition_delay_seconds'] = max(0, time.time() - datetime.fromisoformat(result['settlement_at']).timestamp())
            log.info('FGV execution %s symbol=%s side=%s decision=%s submission=%s settlement=%s recognized=%s',
                     op['id'], op['symbol'], op['kind'], op.get('decision_at'), op.get('submission_at'),
                     result.get('settlement_at'), result['recognized_at'])
        return result

    def block_time(self, receipt):
        try:
            stamp = self.w3.eth.get_block(receipt['blockNumber'])['timestamp']
            return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if isinstance(stamp, int) else None
        except Exception:
            return None  # Telemetry failure must not prevent receipt reconciliation.

    def chain_sell_settlement(self, op, submission):
        """Recognize authenticated payouts with the exact customer ID, not balances.

        Buy mints have no order ID in calldata and deliberately stay on the
        matched API-record path until an authenticated correlation is available.
        """
        token = self.contract('USDC')
        start = max(submission['blockNumber'], op.get('settlement_scan_next', submission['blockNumber']))
        end = min(start + 1999, self.w3.eth.block_number - self.config.execution['confirmations'] + 1)
        if end < start:
            return None
        events = self.w3.eth.get_logs(dict(address=token.address, fromBlock=start, toBlock=end,
            topics=[Web3.to_hex(Web3.keccak(text='Transfer(address,address,uint256)')), None,
                    '0x' + op['wallet'][2:].lower().zfill(64)]))
        pool = self.config.trading_stocks[op['symbol']]
        sender = pool.get('burn_address') or self.config.burn_address
        for event in events:
            tx = self.w3.eth.get_transaction(event['transactionHash'])
            data = bytes(tx['input'])
            if (tx['from'].lower() != sender.lower() or (tx['to'] or '').lower() != token.address.lower()
                    or len(data) <= 68 or data[:4].hex() != 'a9059cbb'
                    or data[16:36].hex() != op['wallet'][2:].lower()):
                continue
            try:
                memo = json.loads(data[68:].decode())
            except (ValueError, UnicodeDecodeError):
                continue
            if (memo.get('customer_id') != op['id'] or memo.get('symbol') != op['symbol']
                    or memo.get('side') != 'SELL' or memo.get('status') != 'COMPLETED'):
                continue
            receipt = self._confirmed_receipt(event['transactionHash'])
            if not receipt or receipt['status'] != 1:
                return None  # Retry this range rather than skipping an unconfirmed payout.
            usdc = self._incoming(receipt, token, op['wallet'])
            stock = self._incoming(receipt, self.contract(op['symbol']), op['wallet'])
            if usdc > 0:
                return dict(state='SETTLED', quantity=max(0, op['amount'] - stock), proceeds=usdc,
                    remaining=stock, settlement_tx=Web3.to_hex(event['transactionHash']),
                    settlement_detection='authenticated_chain_memo')
        op['settlement_scan_next'] = end + 1
        return None

    def chain_buy_delivery(self, op, submission):
        """Observe authenticated mint receipts; do not guess final order status.

        The mint has no customer ID, so only exclusive pending-buy wallets can
        be credited. Delivery starts risk monitoring; API reconciliation retains
        responsibility for final completion/refund and releasing the wallet.
        """
        pending = [o for o in self.store.operations() if o['wallet'] == op['wallet']
                   and o['kind'] == 'buy' and o['state'] not in ('SETTLED', 'FAILED')]
        if len(pending) != 1 or pending[0]['id'] != op['id']:
            return None
        token = self.contract(op['symbol'])
        decimals = token.functions.decimals().call()
        start = max(submission['blockNumber'], op.get('delivery_scan_next', submission['blockNumber']))
        end = min(start + 1999, self.w3.eth.block_number - self.config.execution['confirmations'] + 1)
        if end < start:
            return None
        events = self.w3.eth.get_logs(dict(address=token.address, fromBlock=start, toBlock=end,
            topics=[Web3.to_hex(Web3.keccak(text='Transfer(address,address,uint256)')),
                    '0x' + '00' * 32, '0x' + op['wallet'][2:].lower().zfill(64)]))
        pool = self.config.trading_stocks[op['symbol']]
        authority = pool.get('mint_address') or self.config.mint_address
        deliveries = dict(op.get('delivery_transactions', {}))
        total = int(op.get('delivered_units', 0))
        latest_settlement = op.get('delivery_at')
        for event in events:
            txhash = Web3.to_hex(event['transactionHash'])
            if (event.get('blockNumber') == submission['blockNumber'] and
                    event.get('transactionIndex', -1) <= submission.get('transactionIndex', -1)):
                continue
            if txhash in deliveries:
                continue
            tx = self.w3.eth.get_transaction(event['transactionHash'])
            data = bytes(tx['input'])
            if (tx['from'].lower() != authority.lower() or (tx['to'] or '').lower() != token.address.lower()
                    or len(data) != 68 or data[:4].hex() != '40c10f19'
                    or data[16:36].hex() != op['wallet'][2:].lower()):
                continue
            receipt = self._confirmed_receipt(event['transactionHash'])
            if not receipt or receipt['status'] != 1:
                return None  # Do not advance past a not-yet-confirmed candidate.
            raw = 0
            for item in receipt['logs']:
                topics = item['topics']
                if (item['address'].lower() == token.address.lower() and len(topics) == 3
                    and bytes(topics[0]) == bytes(Web3.keccak(text='Transfer(address,address,uint256)'))
                    and int.from_bytes(bytes(topics[1]), 'big') == 0
                    and bytes(topics[2])[-20:].hex() == op['wallet'][2:].lower()):
                    raw += int.from_bytes(bytes(item['data']), 'big')
            if raw <= 0 or raw != int.from_bytes(data[36:68], 'big'):
                continue
            deliveries[txhash] = raw
            total += raw
            latest_settlement = self.block_time(receipt)
        op['delivery_scan_next'] = end + 1
        if total == op.get('delivered_units', 0):
            return None
        log.info('FGV on-chain stock delivery %s: %s integer units; monitoring while completion reconciles', op['id'], total)
        return dict(state='SUBMITTED', delivered_units=total, delivery_decimals=decimals,
                    delivery_transactions=deliveries, delivery_at=latest_settlement,
                    delivery_recognized_at=datetime.now(timezone.utc).isoformat(),
                    delivery_recognition_delay_seconds=max(0, time.time()-datetime.fromisoformat(latest_settlement).timestamp()) if latest_settlement else None,
                    settlement_detection='authenticated_mint_pending_reconciliation')


class PaperBroker:
    """Deterministic ledger isolated from both live keys and live RPC."""
    def __init__(self, store, config, prices):
        self.store, self.config, self.prices = store, config, prices
        self.vault = 'paper-vault'
        store.initialize_paper(self.vault, config.paper.get('initial_usdc', 1000))

    def balance(self, address, asset='USDC'):
        return self.store.balance(address, asset)

    def create_wallet(self, symbol, status='active'):
        from uuid import uuid4
        address = 'paper-' + uuid4().hex
        if not self.store.db.create_wallet(address, 'paper-no-private-key', self.config.blockchain, symbol, status=status):
            raise RuntimeError('Could not persist paper wallet')
        return address

    def snapshot(self):
        wallets = self.store.db.get_active_wallets(self.config.blockchain)
        wallets += self.store.db.get_wallets_by_status(self.config.blockchain, 'pending_funding')
        return {a: self.balance(a) for a in [self.vault] + [w['address'] for w in wallets]}

    def step(self, op):
        # Ledger mutation and result commit are atomic, so replay cannot spend twice.
        with self.store.db.get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            saved = conn.execute('SELECT payload FROM fgv_operations WHERE id=?', (op['id'],)).fetchone()
            existing = json.loads(saved['payload'])
            if existing['state'] == 'SETTLED':
                return existing
            if op.get('error') == 'ENTRY_ABOVE_TRIGGER':
                op.pop('error')
            error = rejection(op)
            if error:
                return dict(state='FAILED', error=error)
            changes = []
            kind, wallet, amount = op['kind'], op['wallet'], op['amount']
            result = dict(state='SETTLED', tx_hash='paper:' + op['id'], gas_native=0)
            if kind in ('fund', 'gas', 'sweep', 'collect'):
                asset = 'NATIVE' if kind in ('gas', 'collect') else 'USDC'
                source, destination = (wallet, self.vault) if kind in ('sweep', 'collect') else (self.vault, wallet)
                if op.get('sweep_all') and kind in ('sweep', 'collect'):
                    amount = self.balance(source, asset)
                if kind not in ('sweep', 'collect'):
                    amount = max(0, amount - self.balance(destination, asset))
                changes = [(source, asset, -amount), (destination, asset, amount)]
                result['transferred'] = amount
            else:
                price = self.prices.get_latest_prices([op['symbol']]).get(op['symbol'])
                if not price:
                    raise ValueError('Paper execution needs a fresh price')
                slippage = self.config.paper.get('slippage_bps', 5) / 10000
                price *= 1 + slippage if kind == 'buy' else 1 - slippage
                if kind == 'buy':
                    error = rejection(op, price)
                    if error:
                        return entry_rejection_result(error)
                if kind == 'buy':
                    balance = self.balance(wallet, 'USDC')
                    amount = min(amount, balance) if op.get('spend_exact') else balance
                    if amount < self.config.fgv['min_order_usdc']:
                        raise ValueError('Trading wallet USDC is below the minimum buy amount')
                    result.update(amount=amount, planned_amount=op.get('planned_amount', op['amount']))
                    quantity = amount / price
                    changes = [(wallet, 'USDC', -amount), (wallet, op['symbol'], quantity)]
                    signal = op.get('entry_guard', {}).get('signal', {})
                    trigger = signal.get('trigger_low')
                    risk = signal.get('risk')
                    above = bool(trigger is not None and price > trigger)
                    result.update(quantity=quantity, cost=amount, refund=0,
                                  entry_above_trigger=above,
                                  entry_buffer_r_used=((price-trigger)/risk if above and risk and risk > 0 else 0))
                else:
                    amount = self.balance(wallet, op['symbol'])
                    if amount <= 0:
                        raise ValueError('No paper shares available for sell; reconciliation required')
                    result.update(amount=amount, planned_amount=op.get('planned_amount', op['amount']))
                    changes = [(wallet, op['symbol'], -amount), (wallet, 'USDC', amount * price)]
                    result.update(quantity=amount, proceeds=amount * price, remaining=0)
                result['order_type'] = 'MARKET'
            for address, asset, delta in changes:
                row = conn.execute('SELECT amount FROM fgv_paper_balances WHERE address=? AND asset=?',
                                   (address, asset)).fetchone()
                balance = (row['amount'] if row else 0) + delta
                if balance < -1e-8:
                    raise ValueError('Insufficient paper balance')
                conn.execute('INSERT OR REPLACE INTO fgv_paper_balances VALUES(?,?,?)', (address, asset, max(0, balance)))
            op.update(result)
            conn.execute('UPDATE fgv_operations SET state=?,payload=? WHERE id=?', ('SETTLED', json.dumps(op), op['id']))
        return result
