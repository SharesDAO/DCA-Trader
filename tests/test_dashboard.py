import asyncio
from datetime import date
import json
import sqlite3
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from dashboard import (Dashboard, create_app, daily_performance, read_database,
                       read_health, value_trades, summarize_trades)


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'missing.db'
    assert not read_database(path, 'arbitrum')['database_available']
    assert not path.exists()


def test_payload_secrets_are_not_exposed(tmp_path):
    path = tmp_path / 'test.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE fgv_trades(chain TEXT, state TEXT, payload TEXT)')
        db.execute('INSERT INTO fgv_trades VALUES(?,?,?)', ('arbitrum','OPEN',json.dumps({
            'symbol':'TSLA','private_key':'SECRET','raw_tx':'SECRET','signal':{'stop_loss':98,'secret':'SECRET'}})))
    before = path.read_bytes()
    data = read_database(path,'arbitrum')
    assert data['trades'][0]['stop_loss'] == 98
    assert 'SECRET' not in json.dumps(data)
    assert path.read_bytes() == before


def test_dashboard_history_is_not_truncated_to_200_rows(tmp_path):
    path = tmp_path / 'history.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE wallets(address TEXT, assigned_stock TEXT, status TEXT, loss_count INTEGER, blockchain TEXT)')
        db.execute('''CREATE TABLE orders(order_id TEXT, wallet_address TEXT, order_type TEXT,
                    stock_ticker TEXT, amount_usdc REAL, quantity REAL, status TEXT,
                    profit_loss REAL, created_at TEXT, filled_at TEXT)''')
        db.execute('CREATE TABLE fgv_trades(id TEXT, chain TEXT, state TEXT, payload TEXT, updated_at TEXT)')
        db.execute('CREATE TABLE fgv_operations(trade_id TEXT, state TEXT, payload TEXT, updated_at TEXT)')
        db.execute('INSERT INTO wallets VALUES(?,?,?,?,?)', ('wallet', 'AAA', 'active', 0, 'arbitrum'))
        db.execute('INSERT INTO fgv_trades VALUES(?,?,?,?,?)',
                   ('trade', 'arbitrum', 'OPEN', json.dumps({'symbol': 'AAA'}), '2026-09-01'))
        db.executemany('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)', [
            (f'order-{i}', 'wallet', 'buy', 'AAA', 1, 1, 'filled', 0,
             f'2026-09-01T00:{i % 60:02d}:00+00:00', None) for i in range(205)
        ])
        db.executemany('INSERT INTO fgv_operations VALUES(?,?,?,?)', [
            ('trade', 'SETTLED', json.dumps({'id': f'op-{i}', 'symbol': 'AAA', 'kind': 'buy'}),
             '2026-09-01') for i in range(205)
        ])
    data = read_database(path, 'arbitrum')
    assert len(data['orders']) == 205
    assert len(data['execution']) == 205


def test_health_does_not_return_raw_logs(tmp_path):
    (tmp_path/'logs').mkdir()
    (tmp_path/'logs/fgv-live.log').write_text("ERROR SECRET\n2026-09-09 10:00:00,000 INFO FGV active=1 feed={'fresh_count': 588, 'secret': 'SECRET'} scan={'status': 'scanned'}\n")
    data=read_health(tmp_path)
    assert data['feed']['fresh_count']==588
    assert 'SECRET' not in json.dumps(data)


def test_dashboard_reports_buffered_and_emergency_levels(tmp_path):
    path = tmp_path/'stops.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE fgv_trades(chain TEXT,state TEXT,payload TEXT)')
        db.execute('INSERT INTO fgv_trades VALUES(?,?,?)',('arbitrum','OPEN',json.dumps(dict(
            symbol='TSLA',signal=dict(stop_loss=98,take_profit=103),
            stop_policy=dict(enabled=True,original_stop=98,buffered_stop=97.5,
                             emergency_stop=97,confirmation_observations=2,secret='SECRET')))))
    t = read_database(path,'arbitrum')['trades'][0]
    assert t['stop_loss'] == 97.5
    assert t['emergency_stop'] == 97
    assert t['original_stop'] == 98
    assert t['stop_observations'] == 2
    assert 'SECRET' not in json.dumps(t)


def test_http_read_only_routes_and_mode_validation(tmp_path):
    config=SimpleNamespace(dry_run=True,paper_database_path=tmp_path/'paper.db',database_path=tmp_path/'live.db',
        blockchain='arbitrum',session_time={},project_root=tmp_path)
    async def run():
        async with TestClient(TestServer(create_app(config))) as client:
            response=await client.get('/api/state?mode=paper')
            assert response.status==200
            assert (await response.json())['mode']=='paper'
            assert response.headers['Cache-Control']=='no-store'
            assert (await client.post('/api/state')).status==405
            assert (await client.get('/api/state?mode=oops')).status==400
            assert (await client.get('/.env')).status==404
            assert (await client.get('/api/state',headers={'Host':'evil.example'})).status==403
            assert (await client.get('/')).status==200
            assert (await client.get('/app.js')).status==200
    asyncio.run(run())
    assert not (tmp_path/'paper.db').exists()


def test_live_health_timestamp_and_rpc_failure_are_safe(tmp_path, monkeypatch):
    config=SimpleNamespace(dry_run=False,database_path=tmp_path/'live.db',blockchain='arbitrum',
                           session_time={},project_root=tmp_path,vault_address=None)
    monkeypatch.setattr('dashboard.read_health', lambda root: {
        'logged_at':'2026-01-01 10:00:00,123','feed':{},'scan':{}})
    async def unavailable(*args, **kwargs):
        raise OSError('systemd unavailable')
    monkeypatch.setattr('dashboard.asyncio.create_subprocess_exec', unavailable)
    result=asyncio.run(Dashboard(config).snapshot('live'))
    assert result['service']=='unknown'
    assert 'Bot health log is over 3 minutes old.' in result['warnings']
    assert 'Live balance lookup unavailable.' in result['warnings']


def test_explicit_remote_mode_allows_lan_host_but_not_writes(tmp_path):
    config=SimpleNamespace(dry_run=True,paper_database_path=tmp_path/'paper.db',
        blockchain='arbitrum',session_time={},project_root=tmp_path)
    async def run():
        async with TestClient(TestServer(create_app(config, allow_remote=True))) as client:
            headers={'Host':'192.168.1.7:8080'}
            assert (await client.get('/',headers=headers)).status==200
            assert (await client.get('/api/state?mode=paper',headers=headers)).status==200
            assert (await client.post('/api/state',headers=headers)).status==405
    asyncio.run(run())


def test_unrealized_pnl_uses_remaining_shares_and_cost():
    trades=[dict(symbol='AAA',state='OPEN',quantity=.5,cost=40),
            dict(symbol='BBB',state='SELL_PENDING',quantity=2,cost=100),
            dict(symbol='CCC',state='CLOSED',quantity=0,cost=0),
            dict(symbol='DDD',state='BUY_PENDING',quantity=0,cost=0)]
    result=value_trades(trades,{'AAA':100,'BBB':45})
    assert result['unrealized_pnl']==0
    assert trades[0]['unrealized_pnl']==10
    assert trades[0]['unrealized_pct']==25
    assert trades[1]['unrealized_pnl']==-10
    assert trades[2]['unrealized_pnl'] is None
    assert result['missing_symbols']==[]


def test_missing_price_does_not_report_partial_total_as_full_pnl():
    trades=[dict(symbol='AAA',state='OPEN',quantity=1,cost=100),
            dict(symbol='BBB',state='OPEN',quantity=1,cost=100)]
    result=value_trades(trades,{'AAA':110})
    assert result['unrealized_pnl'] is None
    assert result['missing_symbols']==['BBB']
    assert trades[0]['unrealized_pnl']==10
    assert trades[1]['unrealized_pnl'] is None
    assert value_trades([], {})['unrealized_pnl']==0


def test_wallet_summary_includes_retired_but_excludes_vault(tmp_path,monkeypatch):
    config=SimpleNamespace(dry_run=True,paper_database_path=tmp_path/'paper.db',blockchain='arbitrum',session_time={})
    monkeypatch.setattr('dashboard.read_database',lambda *args:dict(database_available=True,trades=[],orders=[],
        wallets=[dict(address='one',assigned_stock='AAA',status='active',loss_count=0),
                 dict(address='two',assigned_stock='BBB',status='abandoned',loss_count=2)],
        paper_balances=[dict(address='paper-vault',asset='USDC',amount=1000)]))
    result=asyncio.run(Dashboard(config).snapshot('paper'))
    assert result['wallet_summary']=={'total':2,'by_status':{'active':1,'abandoned':1}}


def test_dashboard_discloses_disabled_stop_exits(tmp_path,monkeypatch):
    config=SimpleNamespace(dry_run=True,paper_database_path=tmp_path/'paper.db',blockchain='arbitrum',
                           session_time={},stop_policy={'exits_enabled':False})
    monkeypatch.setattr('dashboard.read_database',lambda *args:dict(database_available=True,trades=[],orders=[],
        wallets=[],paper_balances=[]))
    result=asyncio.run(Dashboard(config).snapshot('paper'))
    assert result['stop_exits_enabled'] is False
    assert any('Stop-loss exits are disabled' in w for w in result['warnings'])


def test_trade_statistics_count_closed_trades_not_orders_or_partial_exits():
    def trade(state,pnl,reason=None,**extra):
        return dict(state=state,realized_pnl=pnl,exit_reason=reason,**extra)
    result=summarize_trades([
        trade('CLOSED',5,'TAKE_PROFIT'),trade('CLOSED',-2,'STOP_LOSS'),
        trade('CLOSED',1,'STOP_LOSS'),trade('CLOSED',0,'FORCE_EXIT_15M_BEFORE_CLOSE'),
        trade('OPEN',-1,'STOP_LOSS'),trade('SELL_PENDING',10,'TAKE_PROFIT'),
        trade('FAILED',0),trade('CLOSED',-10,maintenance=True)])
    assert result==dict(closed=4,wins=2,losses=1,breakeven=1,win_rate_pct=50,
                        take_profit=1,stop_loss=2,other_exits=1)


def test_empty_trade_statistics_have_no_win_rate():
    result=summarize_trades([])
    assert result['closed']==0
    assert result['wins']==result['losses']==result['take_profit']==result['stop_loss']==0
    assert result['win_rate_pct'] is None


def test_daily_performance_uses_sell_date_and_reconciles_current_value():
    trades = [
        dict(state='CLOSED', session='2026-09-01', closed_at='2026-09-02T01:00:00+00:00', realized_pnl=5),
        dict(state='CLOSED', session='2026-09-03', closed_at=None, realized_pnl=-2),
        dict(state='OPEN', session='2026-09-03', realized_pnl=0),
    ]
    result = daily_performance(trades, current_total=150, unrealized_pnl=10,
                               exchange_timezone='America/New_York', today=date(2026, 9, 3))
    # 01:00 UTC is still the prior New York trading date.
    assert [p['date'] for p in result['points']] == ['2026-09-01', '2026-09-02', '2026-09-03']
    assert [p['daily_profit'] for p in result['points']] == [5, 0, -2]
    assert result['baseline'] == 137
    assert result['points'][-1]['total_value'] == 140


def test_daily_performance_without_balance_still_reports_profit_bars():
    result = daily_performance([
        dict(state='CLOSED', session='2026-09-04', closed_at=None, realized_pnl=1.25),
    ], None, None, 'America/New_York', today=date(2026, 9, 4))
    assert result['points'][0]['daily_profit'] == 1.25
    assert result['points'][0]['total_value'] is None
