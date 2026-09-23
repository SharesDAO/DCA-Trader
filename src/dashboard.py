"""Read-only dashboard, local by default. Never instantiates a trading broker or DB writer."""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import math
from collections import Counter
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

if __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import Config

ALLOW_REMOTE = web.AppKey('allow_remote', bool)


def read_database(path, chain):
    result = dict(trades=[], orders=[], wallets=[], paper_balances=[], execution=[], database_available=False)
    if not path.exists():
        return result
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'wallets' in tables:
            result['wallets'] = [dict(r) for r in db.execute(
                'SELECT address,assigned_stock,status,loss_count FROM wallets WHERE blockchain=?', (chain,))]
        if 'orders' in tables:
            result['orders'] = [dict(r) for r in db.execute('''SELECT o.order_id,o.order_type,o.stock_ticker,
                o.amount_usdc,o.quantity,o.status,o.profit_loss,o.created_at,o.filled_at
                FROM orders o JOIN wallets w ON w.address=o.wallet_address
                WHERE w.blockchain=? ORDER BY o.created_at DESC''', (chain,))]
        if 'fgv_trades' in tables:
            trade_query = '''SELECT t.payload,t.state,t.updated_at,
                    (SELECT COALESCE(json_extract(o.payload,'$.settlement_at'),
                                     json_extract(o.payload,'$.recognized_at'),o.updated_at)
                     FROM fgv_operations o WHERE o.trade_id=t.id AND o.state='SETTLED'
                       AND json_extract(o.payload,'$.kind')='sell'
                     ORDER BY o.rowid DESC LIMIT 1) AS closed_at
                    FROM fgv_trades t WHERE t.chain=? ORDER BY t.rowid DESC'''
            if 'fgv_operations' not in tables:
                trade_query = '''SELECT payload,state,NULL AS updated_at,NULL AS closed_at
                                 FROM fgv_trades WHERE chain=? ORDER BY rowid DESC'''
            for row in db.execute(trade_query, (chain,)):
                payload = json.loads(row['payload'])
                if payload.get('maintenance'):
                    continue
                trade = {k: payload.get(k) for k in (
                    'symbol', 'session', 'wallet', 'amount', 'quantity', 'cost', 'realized_pnl', 'exit_reason',
                    'buy_reconciling', 'cost_provisional')}
                trade['state'] = row['state']
                trade['closed_at'] = row['closed_at'] or (row['updated_at'] if row['state'] == 'CLOSED' else None)
                signal = payload.get('signal', {})
                trade.update(stop_loss=signal.get('stop_loss'), take_profit=signal.get('take_profit'))
                policy = payload.get('stop_policy', {})
                if policy.get('enabled'):
                    trade.update(stop_loss=policy['buffered_stop'], emergency_stop=policy['emergency_stop'],
                                 original_stop=policy['original_stop'],
                                 stop_observations=policy['confirmation_observations'])
                result['trades'].append(trade)
        if 'fgv_paper_balances' in tables:
            result['paper_balances'] = [dict(r) for r in db.execute('SELECT address,asset,amount FROM fgv_paper_balances')]
        if 'fgv_operations' in tables and 'fgv_trades' in tables:
            for row in db.execute('''SELECT o.payload,o.state FROM fgv_operations o JOIN fgv_trades t
                    ON t.id=o.trade_id WHERE t.chain=? ORDER BY o.rowid DESC''',(chain,)):
                op=json.loads(row['payload'])
                if op.get('kind') not in ('buy','sell'):
                    continue
                public={k:op.get(k) for k in ('id','symbol','kind','created_at','decision_at','decision_price',
                    'execution_quote','quote_at','signed_at','submission_at','settlement_at','recognized_at',
                    'recognition_delay_seconds','last_error_at','settlement_detection',
                    'entry_above_trigger','entry_buffer_r_used',
                    'delivered_units','delivery_decimals','delivery_at','delivery_recognized_at',
                    'delivery_recognition_delay_seconds')}
                if public['delivered_units'] is not None:
                    public['delivered_units'] = str(public['delivered_units'])  # Exact beyond JavaScript's integer range.
                public['state']=row['state']
                # Only our fixed entry-safety codes; never forward RPC errors.
                error=op.get('error','')
                public['rejection']=error if error.startswith(('ENTRY_', 'EXECUTION_QUOTE_', 'INVALID_EXECUTION_')) else None
                result['execution'].append(public)
    result['database_available'] = True
    return result


def read_health(root):
    path = root / 'logs/fgv-live.log'
    if not path.exists():
        return None
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 128_000))
        lines = stream.read().decode(errors='replace').splitlines()
    for line in reversed(lines):
        if 'FGV active=' not in line or ' scan=' not in line:
            continue
        try:
            feed, scan = line.split(' feed=', 1)[1].split(' scan=', 1)
            parsed_feed, parsed_scan = ast.literal_eval(feed), ast.literal_eval(scan)
            # Explicitly whitelist public diagnostics; never return raw log lines.
            return {'logged_at': line[:23], 'feed': {k: parsed_feed.get(k) for k in (
                'fresh_count', 'quote_providers', 'candle_providers')},
                'scan': {k: v for k, v in parsed_scan.items() if isinstance(v, (str, int, float))}}
        except (ValueError, SyntaxError, IndexError):
            continue
    return None


class Dashboard:
    def __init__(self, config):
        self.config = config
        self.cache = {}
        self.lock = asyncio.Lock()

    async def reference_prices(self, symbols):
        if not symbols:
            return {}, None
        settings = self.config.market_data
        started = time.time()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as client:
            async with client.get(settings.get('base_url', 'https://api.backpack.exchange').rstrip('/') +
                    '/api/v1/tickers', params={'source': 'External'}) as response:
                response.raise_for_status()
                rows = await response.json()
        if time.time() - started > settings.get('max_price_age_seconds', 30):
            raise ValueError('Reference price response is stale')
        mapping = {settings.get('symbol_map', {}).get(s, f'{s}.US_USDC'): s for s in symbols}
        prices = {}
        for row in rows:
            if row.get('symbol') not in mapping:
                continue
            try:
                price = float(row['lastPrice'])
                if price > 0 and math.isfinite(price):
                    prices[mapping[row['symbol']]] = price
            except (KeyError, ValueError, TypeError):
                continue
        return prices, datetime.fromtimestamp(started, timezone.utc).isoformat()

    async def snapshot(self, mode):
        async with self.lock:
            cached = self.cache.get(mode)
            if cached and time.monotonic() - cached[0] < 10:
                return cached[1]
            cfg = self.config
            path = cfg.paper_database_path if mode == 'paper' else cfg.database_path
            data = await asyncio.to_thread(read_database, path, cfg.blockchain)
            data.update(mode=mode, chain=cfg.blockchain,
                        generated_at=datetime.now(timezone.utc).isoformat(),
                        configured_mode='paper' if cfg.dry_run else 'live',
                        schedule=cfg.session_time, service='unknown', health=None,
                        balances=[], warnings=[])
            data['stop_exits_enabled'] = getattr(cfg, 'stop_policy', {}).get('exits_enabled', True)
            if not data['stop_exits_enabled']:
                data['warnings'].append('Stop-loss exits are disabled, including emergency stops. Take-profit and time exits remain enabled.')
            if mode == 'live':
                data['health'] = await asyncio.to_thread(read_health, cfg.project_root)
                if data['health']:
                    stamp = datetime.fromisoformat(data['health']['logged_at'].replace(',', '.')).timestamp()
                    if time.time() - stamp > 180:
                        data['warnings'].append('Bot health log is over 3 minutes old.')
                try:
                    process = await asyncio.create_subprocess_exec('systemctl', '--user', 'is-active',
                        'dca-fgv-live.service', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    try:
                        output, _ = await asyncio.wait_for(process.communicate(), 3)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.communicate()
                        raise
                    data['service'] = output.decode().strip() or 'unknown'
                except (OSError, asyncio.TimeoutError):
                    pass
                addresses = {w['address']: ('Ready' if w['assigned_stock'] == '__FGV_READY__' else w['assigned_stock'])
                             for w in data['wallets']}
                if cfg.vault_address:
                    addresses[cfg.vault_address] = 'Vault'
                try:
                    rpc = cfg.get_rpc_url()
                    chain = cfg.get_chain_config()
                    token = chain['usdc_address']
                    semaphore = asyncio.Semaphore(6)
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as client:
                        async with client.post(rpc, json=dict(jsonrpc='2.0', id=1, method='eth_call',
                                params=[{'to': token, 'data': '0x313ce567'}, 'latest'])) as response:
                            response.raise_for_status()
                            decimals = int((await response.json())['result'], 16)
                        async def balance(address, label):
                            async with semaphore:
                                try:
                                    async def call(method, params):
                                        async with client.post(rpc, json=dict(jsonrpc='2.0', id=1, method=method, params=params)) as response:
                                            response.raise_for_status()
                                            return int((await response.json())['result'], 16)
                                    usdc, native = await asyncio.gather(
                                        call('eth_call', [{'to': token, 'data': '0x70a08231' + address[2:].lower().zfill(64)}, 'latest']),
                                        call('eth_getBalance', [address, 'latest']))
                                    return dict(address=address, label=label, usdc=usdc / 10 ** decimals,
                                                native=native / 10 ** 18)
                                except Exception:
                                    return dict(address=address, label=label, usdc=None, native=None)
                        data['balances'] = await asyncio.gather(*(balance(a, l) for a, l in addresses.items()))
                except Exception:
                    data['warnings'].append('Live balance lookup unavailable.')
                if any(b['usdc'] is None for b in data['balances']):
                    data['warnings'].append('Some wallet balances are unavailable; totals are incomplete.')
            else:
                labels = {w['address']: ('Ready' if w['assigned_stock'] == '__FGV_READY__' else w['assigned_stock'])
                          for w in data['wallets']}
                for row in data['paper_balances']:
                    if row['asset'] == 'USDC':
                        data['balances'].append(dict(address=row['address'], label=labels.get(row['address'], 'Paper vault'),
                                                     usdc=row['amount'], native=None))
            del data['paper_balances']
            wallet_info = {w['address']: w for w in data['wallets']}
            for balance in data['balances']:
                info = wallet_info.get(balance['address'], {})
                balance.update(status=info.get('status', 'vault'), loss_count=info.get('loss_count', 0))
            data['wallet_summary'] = {'total': len(data['wallets']),
                'by_status': dict(Counter(w['status'] for w in data['wallets']))}
            data['trade_summary'] = summarize_trades(data['trades'])
            for trade in data['trades']:
                if trade.get('buy_reconciling'):
                    data['warnings'].append(f"{trade['symbol']}: on-chain delivery is monitored; final buy completion/cost still reconciling.")
            for op in data.get('execution', []):
                if op['state'] not in ('SETTLED','FAILED') and time.time()-datetime.fromisoformat(op['created_at']).timestamp() > getattr(cfg,'execution',{}).get('stuck_order_alert_seconds',60):
                    data['warnings'].append(f"Stuck {op['symbol']} {op['kind']} ({op['state']}); check execution diagnostics.")
            symbols = {t['symbol'] for t in data['trades'] if t['state'] in ('OPEN', 'SELL_PENDING') and (t['quantity'] or 0) > 0}
            try:
                prices, fetched_at = await self.reference_prices(symbols)
            except Exception:
                prices, fetched_at = {}, None
            data['valuation'] = value_trades(data['trades'], prices)
            data['valuation'].update(source='Backpack external last price', fetched_at=fetched_at)
            if data['valuation']['missing_symbols']:
                data['warnings'].append('Unrealized P&L unavailable for: ' + ', '.join(data['valuation']['missing_symbols']))
            complete_cash = bool(data['balances']) and all(b['usdc'] is not None for b in data['balances'])
            complete_value = not data['valuation']['missing_symbols']
            current_total = None
            if complete_cash and complete_value:
                cash = sum(b['usdc'] for b in data['balances'])
                market_value = sum((t.get('market_value') or 0) for t in data['trades'])
                wallet_cash = {b['address']: b['usdc'] for b in data['balances']}
                # Submitted buys temporarily disappear from wallet balances before
                # shares/refunds arrive. Preserve that reserved value in the total.
                in_transit = sum(max(0, (t.get('amount') or 0) - wallet_cash.get(t.get('wallet'), 0))
                                 for t in data['trades'] if t['state'] == 'BUY_PENDING')
                current_total = cash + market_value + in_transit
            data['daily_performance'] = daily_performance(
                data['trades'], current_total, data['valuation']['unrealized_pnl'],
                cfg.session_time.get('exchange_timezone', 'America/New_York'))
            self.cache[mode] = (time.monotonic(), data)
            return data


def summarize_trades(trades):
    closed = [t for t in trades if t['state'] == 'CLOSED' and not t.get('maintenance')]
    wins = sum((t.get('realized_pnl') or 0) > 0 for t in closed)
    losses = sum((t.get('realized_pnl') or 0) < 0 for t in closed)
    reasons = Counter(t.get('exit_reason') for t in closed)
    return dict(closed=len(closed), wins=wins, losses=losses,
                breakeven=len(closed) - wins - losses,
                win_rate_pct=100 * wins / len(closed) if closed else None,
                take_profit=reasons['TAKE_PROFIT'], stop_loss=reasons['STOP_LOSS'],
                other_exits=len(closed) - reasons['TAKE_PROFIT'] - reasons['STOP_LOSS'])


def value_trades(trades, prices):
    total, missing = 0.0, set()
    for trade in trades:
        trade.update(reference_price=None, market_value=None, unrealized_pnl=None, unrealized_pct=None)
        if trade['state'] not in ('OPEN', 'SELL_PENDING') or (trade['quantity'] or 0) <= 0:
            continue
        price = prices.get(trade['symbol'])
        if price is None or trade['cost'] is None:
            missing.add(trade['symbol'])
            continue
        value = trade['quantity'] * price
        pnl = value - trade['cost']
        trade.update(reference_price=price, market_value=value, unrealized_pnl=pnl,
                     unrealized_pct=100 * pnl / trade['cost'] if trade['cost'] > 0 else None)
        total += pnl
    return {'unrealized_pnl': None if missing else total, 'missing_symbols': sorted(missing)}


def daily_performance(trades, current_total, unrealized_pnl, exchange_timezone, today=None):
    """Build a realized daily equity curve anchored to the current portfolio."""
    zone = ZoneInfo(exchange_timezone)
    profits = Counter()
    closed = [t for t in trades if t['state'] == 'CLOSED' and not t.get('maintenance')]
    for trade in closed:
        day = None
        if trade.get('closed_at'):
            try:
                stamp = datetime.fromisoformat(str(trade['closed_at']).replace('Z', '+00:00'))
                stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
                day = stamp.astimezone(zone).date()
            except (TypeError, ValueError):
                pass
        if day is None:
            try:
                day = date.fromisoformat(trade['session'])
            except (TypeError, ValueError):
                continue
        profits[day] += float(trade.get('realized_pnl') or 0)
    if not profits:
        return dict(points=[], current_total=current_total, baseline=None,
                    methodology='No closed trades yet')
    end = today or datetime.now(zone).date()
    start = min(profits)
    closed_profit = sum(profits.values())
    baseline = (current_total - closed_profit - (unrealized_pnl or 0)
                if current_total is not None and unrealized_pnl is not None else None)
    points, cumulative = [], 0.0
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            daily = profits[cursor]
            cumulative += daily
            points.append(dict(date=cursor.isoformat(), daily_profit=daily,
                               cumulative_profit=cumulative,
                               total_value=baseline + cumulative if baseline is not None else None))
        cursor += timedelta(days=1)
    return dict(points=points, current_total=current_total, baseline=baseline,
                methodology='Estimated realized equity anchored to current on-chain value; excludes historical unrealized P&L, native gas and external cash-flow timing')


@web.middleware
async def local_only(request, handler):
    if not request.app[ALLOW_REMOTE] and request.host.split(':')[0] not in ('127.0.0.1', 'localhost'):
        raise web.HTTPForbidden()
    response = await handler(request)
    response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"})
    return response


def create_app(config, allow_remote=False):
    app = web.Application(middlewares=[local_only])
    app[ALLOW_REMOTE] = allow_remote
    dashboard = Dashboard(config)
    assets = Path(__file__).parent / 'dashboard_assets'
    async def state(request):
        mode = request.query.get('mode', 'paper' if config.dry_run else 'live')
        if mode not in ('live', 'paper'):
            raise web.HTTPBadRequest(text='Invalid mode')
        try:
            return web.json_response(await dashboard.snapshot(mode))
        except Exception:
            return web.json_response({'error': 'Dashboard data temporarily unavailable'}, status=503)
    async def asset(request):
        name = request.match_info.get('name', 'index.html')
        if name not in ('index.html', 'app.js', 'style.css'):
            raise web.HTTPNotFound()
        return web.FileResponse(assets / name)
    app.router.add_get('/api/state', state)
    app.router.add_get('/', asset)
    app.router.add_get('/{name}', asset)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--host', choices=['127.0.0.1', '0.0.0.0'], default='127.0.0.1',
                        help='0.0.0.0 exposes read-only account data without authentication')
    args = parser.parse_args()
    web.run_app(create_app(Config(), allow_remote=args.host == '0.0.0.0'),
                host=args.host, port=args.port, access_log=None)
