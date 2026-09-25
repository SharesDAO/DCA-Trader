"""Durable FGV reservations and operation journal alongside existing wallet tables."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db, chain):
        self.db, self.chain = db, chain
        with db.get_connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS fgv_trades (
                    id TEXT PRIMARY KEY, chain TEXT NOT NULL, symbol TEXT NOT NULL,
                    session TEXT NOT NULL, wallet TEXT NOT NULL, state TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(chain, symbol, session)
                );
                CREATE TABLE IF NOT EXISTS fgv_operations (
                    id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, state TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fgv_paper_balances (
                    address TEXT NOT NULL, asset TEXT NOT NULL, amount REAL NOT NULL,
                    PRIMARY KEY(address, asset)
                );
                CREATE TABLE IF NOT EXISTS fgv_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fgv_stop_observations (
                    trade_id TEXT NOT NULL, observed_at TEXT NOT NULL, quote_at REAL NOT NULL,
                    price REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(trade_id, quote_at)
                );
                CREATE TABLE IF NOT EXISTS fgv_candidate_evaluations (
                    chain TEXT NOT NULL, session TEXT NOT NULL, symbol TEXT NOT NULL,
                    entry_minute INTEGER NOT NULL, observed_at TEXT NOT NULL,
                    probability REAL NOT NULL, qualified INTEGER NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0, model_version INTEGER NOT NULL,
                    features TEXT NOT NULL,
                    PRIMARY KEY(chain, session, symbol, entry_minute)
                );
                CREATE INDEX IF NOT EXISTS idx_fgv_candidates_session
                    ON fgv_candidate_evaluations(chain, session, probability DESC);
            ''')

    def trades(self, active=False):
        with self.db.get_connection() as conn:
            rows = conn.execute('SELECT * FROM fgv_trades WHERE chain=? ORDER BY rowid', (self.chain,))
            result = [dict(json.loads(r['payload']), id=r['id'], state=r['state']) for r in rows]
        return [r for r in result if r['state'] not in ('CLOSED', 'FAILED')] if active else result

    def has_traded(self, symbol, session):
        return any(t['symbol'] == symbol and t['session'] == session for t in self.trades())

    def record_stop_observations(self, trades, observations, now, providers):
        with self.db.get_connection() as conn:
            for trade in trades:
                quote = observations.get(trade['symbol'])
                if quote is None:
                    continue
                price, quote_at = quote
                payload = dict(state=trade['state'], quantity=trade.get('quantity'), cost=trade.get('cost'),
                               realized_pnl=trade.get('realized_pnl'), exit_reason=trade.get('exit_reason'),
                               stop_exit_detail=trade.get('stop_exit_detail'),
                               stop_exits_enabled=trade.get('stop_exits_enabled', True),
                               confirmation=trade.get('stop_confirmation'),
                               policy=trade.get('stop_policy'), original_stop=trade['signal']['stop_loss'],
                               take_profit=trade['signal']['take_profit'], provider=providers.get(trade['symbol']),
                               provisional=trade.get('cost_provisional', False))
                conn.execute('INSERT OR IGNORE INTO fgv_stop_observations VALUES(?,?,?,?,?)',
                             (trade['id'], now.isoformat(), quote_at, price, json.dumps(payload)))

    def record_candidate_evaluations(self, records):
        """Persist one model-stage observation per symbol and entry minute."""
        if not records:
            return
        with self.db.get_connection() as conn:
            conn.executemany('''INSERT INTO fgv_candidate_evaluations
                (chain,session,symbol,entry_minute,observed_at,probability,qualified,selected,model_version,features)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chain,session,symbol,entry_minute) DO UPDATE SET
                    observed_at=excluded.observed_at,
                    probability=excluded.probability,
                    qualified=excluded.qualified,
                    selected=MAX(fgv_candidate_evaluations.selected,excluded.selected),
                    model_version=excluded.model_version,
                    features=excluded.features''', [
                (self.chain, record['session'], record['symbol'], record['entry_minute'],
                 record['observed_at'], record['probability'], int(record['qualified']),
                 int(record.get('selected', False)), record['model_version'],
                 json.dumps(record['features'], sort_keys=True))
                for record in records
            ])

    def mark_candidate_selected(self, session, symbol, entry_minute):
        with self.db.get_connection() as conn:
            conn.execute('''UPDATE fgv_candidate_evaluations SET selected=1
                            WHERE chain=? AND session=? AND symbol=? AND entry_minute=?''',
                         (self.chain, session, symbol, entry_minute))

    def candidate_evaluations(self, session=None):
        query = '''SELECT session,symbol,entry_minute,observed_at,probability,qualified,
                          selected,model_version,features
                   FROM fgv_candidate_evaluations WHERE chain=?'''
        params = [self.chain]
        if session is not None:
            query += ' AND session=?'
            params.append(session)
        query += ' ORDER BY session,entry_minute,symbol'
        with self.db.get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(dict(row), features=json.loads(row['features']),
                     qualified=bool(row['qualified']), selected=bool(row['selected'])) for row in rows]

    def record_closed_losses(self, limit):
        """Count each closed FGV trade once, including pre-upgrade history."""
        with self.db.get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute("SELECT id,payload FROM fgv_trades WHERE chain=? AND state='CLOSED' ORDER BY rowid",
                                (self.chain,)).fetchall()
            for row in rows:
                trade = json.loads(row['payload'])
                if trade.get('maintenance') or trade.get('wallet_loss_counted'):
                    continue
                if trade.get('realized_pnl', 0) < 0:
                    conn.execute('UPDATE wallets SET loss_count=loss_count+1 WHERE address=? AND blockchain=?',
                                 (trade['wallet'], self.chain))
                trade['wallet_loss_counted'] = True
                conn.execute('UPDATE fgv_trades SET payload=?,updated_at=? WHERE id=?',
                             (json.dumps(trade), now_iso(), row['id']))
            conn.execute("UPDATE wallets SET status='retiring' WHERE blockchain=? AND status='active' AND loss_count>=?",
                         (self.chain, limit))

    def excluded_wallets(self):
        with self.db.get_connection() as conn:
            return {r['address'] for r in conn.execute(
                "SELECT address FROM wallets WHERE blockchain=? AND status IN ('retiring','abandoned')", (self.chain,))}

    def retirement_trade(self, wallet):
        for trade in self.trades():
            if trade.get('retirement') and trade['wallet'] == wallet['address']:
                return trade
        trade = self.maintenance_trade(wallet)
        trade['retirement'] = True
        self.save_trade(trade)
        return trade

    def maintenance_trade(self, wallet, **extra):
        """Attach sweeps to a durable journal without claiming a trading session."""
        trade = dict(id=uuid4().hex, symbol=wallet['assigned_stock'],
                     session='maintenance:' + uuid4().hex, wallet=wallet['address'],
                     state='CLOSED', amount=0, quantity=0, cost=0, realized_pnl=0,
                     maintenance=True, **extra)
        self.save_trade(trade)
        return trade

    def save_trade(self, trade):
        with self.db.get_connection() as conn:
            conn.execute('''INSERT INTO fgv_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET state=excluded.state, payload=excluded.payload,
                updated_at=excluded.updated_at''', (
                    trade['id'], self.chain, trade['symbol'], trade['session'], trade['wallet'],
                    trade['state'], json.dumps(trade), now_iso()))
            if not trade.get('maintenance'):
                self._sync_position(conn, trade)

    def operations(self, trade_id=None):
        with self.db.get_connection() as conn:
            rows = conn.execute('''SELECT o.* FROM fgv_operations o JOIN fgv_trades t
                ON o.trade_id=t.id WHERE t.chain=? ORDER BY o.rowid''', (self.chain,))
            result = [dict(json.loads(r['payload']), id=r['id'], state=r['state']) for r in rows]
        return [r for r in result if r['trade_id'] == trade_id] if trade_id else result

    def busy_wallets(self):
        return {t['wallet'] for t in self.trades(active=True)} | {
            o['wallet'] for o in self.operations() if o['state'] not in ('SETTLED', 'FAILED')}

    def save_operation(self, op):
        with self.db.get_connection() as conn:
            conn.execute('''INSERT INTO fgv_operations VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET state=excluded.state, payload=excluded.payload,
                updated_at=excluded.updated_at''', (
                    op['id'], op['trade_id'], op['state'], json.dumps(op), now_iso()))
            if op['kind'] == 'buy':
                conn.execute('UPDATE orders SET amount_usdc=? WHERE order_id=?', (op['amount'], op['id']))
            elif op['kind'] == 'sell' and op['state'] in ('SIGNED', 'SUBMITTED'):
                conn.execute('UPDATE orders SET quantity=? WHERE order_id=?', (op['amount'], op['id']))

    def new_operation(self, trade, kind, amount, **extra):
        op = dict(id=f'FGV_{kind}_{uuid4().hex}', trade_id=trade['id'], kind=kind,
                  wallet=trade['wallet'], symbol=trade['symbol'], amount=amount,
                  state='PREPARED', created_at=now_iso(), **extra)
        with self.db.get_connection() as conn:
            conn.execute('INSERT INTO fgv_operations VALUES (?, ?, ?, ?, ?)',
                         (op['id'], op['trade_id'], op['state'], json.dumps(op), now_iso()))
            if kind in ('buy', 'sell'):
                conn.execute('''INSERT INTO orders(order_id,wallet_address,order_type,stock_ticker,
                    amount_usdc,quantity,limit_price,status,expires_at) VALUES(?,?,?,?,?,?,?,'pending',?)''',
                    (op['id'], op['wallet'], kind, op['symbol'],
                     amount if kind == 'buy' else 0, amount if kind == 'sell' else 0, 0,
                     (datetime.now(timezone.utc) + timedelta(days=extra['expiry_days'])).isoformat()))
        return op

    def balance(self, address, asset='USDC'):
        with self.db.get_connection() as conn:
            row = conn.execute('SELECT amount FROM fgv_paper_balances WHERE address=? AND asset=?',
                               (address, asset)).fetchone()
            return row['amount'] if row else 0.0

    def initialize_paper(self, vault, amount):
        with self.db.get_connection() as conn:
            conn.execute('INSERT OR IGNORE INTO fgv_paper_balances VALUES (?, ?, ?)', (vault, 'USDC', amount))
            conn.execute('INSERT OR IGNORE INTO fgv_paper_balances VALUES (?, ?, ?)', (vault, 'NATIVE', 1000))

    def liquidating(self):
        with self.db.get_connection() as conn:
            return conn.execute('SELECT 1 FROM fgv_settings WHERE key=?', (f'liquidating:{self.chain}',)).fetchone() is not None

    def set_liquidating(self):
        with self.db.get_connection() as conn:
            conn.execute('INSERT OR REPLACE INTO fgv_settings VALUES (?, ?)', (f'liquidating:{self.chain}', 'true'))

    def resume_entries(self):
        if self.busy_wallets() or self.legacy_positions() or self.db.get_pending_orders():
            raise ValueError('Positions and pending operations must settle before resuming entries')
        with self.db.get_connection() as conn:
            conn.execute('DELETE FROM fgv_settings WHERE key=?', (f'liquidating:{self.chain}',))

    def finish_operation(self, op, result):
        op.update(result)
        self.save_operation(op)
        if op['kind'] in ('buy', 'sell') and op['state'] in ('SETTLED', 'FAILED'):
            with self.db.get_connection() as conn:
                qty = op.get('quantity', 0)
                cost = op.get('cost', 0) if op['kind'] == 'buy' else op.get('proceeds', 0)
                conn.execute('''UPDATE orders SET status=?,quantity=?,amount_usdc=?,limit_price=?,filled_at=?
                    WHERE order_id=?''', ('failed' if op['state'] == 'FAILED' else 'filled' if qty else 'refunded', qty, cost,
                                         cost / qty if qty else 0, now_iso(), op['id']))

    def sync_position(self, trade):
        with self.db.get_connection() as conn:
            self._sync_position(conn, trade)

    @staticmethod
    def _sync_position(conn, trade):
        if trade['state'] in ('OPEN', 'SELL_PENDING'):
            conn.execute('''INSERT INTO positions(wallet_address,stock_ticker,quantity,avg_buy_price,
                total_cost_usdc,first_buy_date) VALUES(?,?,?,?,?,?) ON CONFLICT(wallet_address)
                DO UPDATE SET quantity=excluded.quantity,avg_buy_price=excluded.avg_buy_price,
                total_cost_usdc=excluded.total_cost_usdc''', (
                    trade['wallet'], trade['symbol'], trade['quantity'],
                    trade['cost'] / trade['quantity'], trade['cost'], trade['session']))
        elif trade['state'] in ('CLOSED', 'FAILED'):
            conn.execute('DELETE FROM positions WHERE wallet_address=?', (trade['wallet'],))
            if trade.get('exit_order'):
                conn.execute('UPDATE orders SET profit_loss=? WHERE order_id=?',
                             (trade.get('realized_pnl'), trade['exit_order']))

    def legacy_positions(self):
        wallets = {t['wallet'] for t in self.trades(active=True)}
        chain_wallets = {w['address'] for w in self.db.get_active_wallets(self.chain)}
        return [p for p in self.db.get_all_positions()
                if p['wallet_address'] not in wallets and p['wallet_address'] in chain_wallets]
