from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from database import Database
from fgv_trader.runtime import PAPER_KEY
from fgv_trader.store import Store
from trade_manager import TradeManager


def test_migration_preserves_existing_wallets_and_positions(tmp_path):
    db = Database(str(tmp_path / 'old.db'), PAPER_KEY)
    db.create_wallet('existing', 'existing-key', 'arbitrum', 'TSLA')
    db.create_or_update_position('existing', 'TSLA', 1, 100, 100, datetime.now().date())
    store = Store(db, 'arbitrum')
    assert db.get_wallet('existing')['private_key'] == 'existing-key'
    assert len(store.legacy_positions()) == 1
    assert db.get_position('existing')['quantity'] == 1


def test_legacy_callbacks_ignore_fgv_positions_and_orders(tmp_path):
    db = Database(str(tmp_path / 'old.db'), PAPER_KEY)
    db.create_wallet('fgv-wallet', 'paper-key', 'arbitrum', 'TSLA')
    store = Store(db, 'arbitrum')
    trade = dict(id='fgv', symbol='TSLA', session='2026-09-04', wallet='fgv-wallet',
                 state='OPEN', amount=100, quantity=1, cost=100)
    store.save_trade(trade)
    store.new_operation(trade, 'sell', 1, expiry_days=1)
    blockchain = MagicMock()
    config = SimpleNamespace(strategy_name='fgv', blockchain='arbitrum')
    manager = TradeManager(db, blockchain, MagicMock(), MagicMock(), config)
    assert manager.check_order_confirmations() == 0
    assert manager.monitor_positions() == 0
    assert not blockchain.mock_calls


def test_failed_or_refunded_legacy_buy_does_not_restart_dca(tmp_path):
    db = Database(str(tmp_path / 'old.db'), PAPER_KEY)
    db.create_wallet('old-wallet', 'paper-key', 'arbitrum', 'TSLA')
    db.create_order('legacy-buy', 'old-wallet', 'buy', 'TSLA', 100, 1, 100, datetime.now())
    manager = TradeManager(db, MagicMock(), MagicMock(), MagicMock(), SimpleNamespace(strategy_name='fgv'))
    manager.place_buy_order = MagicMock()
    manager._handle_refunded_order(db.get_wallet_orders('old-wallet')[0])
    manager.place_buy_order.assert_not_called()
    assert db.get_wallet_orders('old-wallet')[0]['status'] == 'refunded'
