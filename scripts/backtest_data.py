"""Public historical data cache. Never opens the bot's live/paper databases."""
import argparse
import concurrent.futures
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import sys
import time
import zlib
from datetime import datetime, timezone

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from config import Config
from sharesdao_client import SharesDAOClient


class HistoricalData:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'historical.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, fetched_at TEXT, data BLOB)')

    def fetch(self, symbol, interval, start, end):
        key = json.dumps([symbol, interval, int(start.timestamp()), int(end.timestamp())])
        with sqlite3.connect(self.path, timeout=60) as db:
            row = db.execute('SELECT data FROM responses WHERE key=?',(key,)).fetchone()
        if row:
            return json.loads(zlib.decompress(row[0]))
        for attempt in range(4):
            try:
                response = requests.get('https://api.backpack.exchange/api/v1/klines', params=dict(
                    symbol=symbol+'.US_USDC', source='External', priceType='Last', interval=interval,
                    startTime=int(start.timestamp()), endTime=int(end.timestamp())), timeout=40)
                if response.status_code == 429:
                    time.sleep(min(30, float(response.headers.get('Retry-After', 5))))
                    continue
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows,list):
                    raise ValueError('Expected candle array')
                compressed = zlib.compress(json.dumps(rows,separators=(',',':')).encode(), 3)
                with sqlite3.connect(self.path, timeout=60) as db:
                    db.execute('INSERT OR REPLACE INTO responses VALUES(?,?,?)',
                               (key,datetime.now(timezone.utc).isoformat(),compressed))
                return rows
            except (requests.RequestException, ValueError):
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError('Historical data rate limit retries exhausted')


def snapshot(directory):
    path = directory/'inputs.json'
    if path.exists():
        return json.loads(path.read_text())
    config = Config()
    api = SharesDAOClient(config.sharesdao_api_url,config.blockchain)
    config.set_trading_stocks(api.get_pool_list())
    model = (config.project_root/config.fgv['win_probability_model_path']).read_bytes()
    result = dict(captured_at=datetime.now(timezone.utc).isoformat(),
                  symbols=sorted(config.trading_stocks), fgv=config.fgv, market_data=config.market_data,
                  execution=config.execution, session_time=config.session_time, stop_policy=config.stop_policy,
                  max_loss_traders=config.max_loss_traders, model=json.loads(model),
                  model_sha256=hashlib.sha256(model).hexdigest(),chain=config.blockchain)
    path.write_text(json.dumps(result,indent=2))
    return result


def download_coarse(directory,start,end,workers=4):
    data = HistoricalData(directory)
    inputs = snapshot(data.directory)
    symbols = sorted(set(inputs['symbols'])|{'SPY'})
    stats = []
    def task(symbol):
        try:
            rows = data.fetch(symbol,'5m',start,end)
            return dict(symbol=symbol,rows=len(rows),first=rows[0]['start'] if rows else None,
                        last=rows[-1]['start'] if rows else None)
        except Exception as exc:
            return dict(symbol=symbol,rows=0,error=type(exc).__name__)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for index, result in enumerate(pool.map(task,symbols),1):
            stats.append(result)
            if index % 20 == 0 or index == len(symbols):
                print(f'5m download {index}/{len(symbols)}: {sum(r["rows"] for r in stats):,} bars; '
                      f'{sum("error" in r for r in stats)} errors',flush=True)
    (data.directory/'coarse_coverage.json').write_text(json.dumps(stats,indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',required=True)
    parser.add_argument('--start',default='2026-07-10T13:30:00+00:00')
    parser.add_argument('--end',default='2026-09-09T20:00:00+00:00')
    args=parser.parse_args()
    download_coarse(args.directory,datetime.fromisoformat(args.start),datetime.fromisoformat(args.end))
