"""Backpack external candles and reference prices (not executable quotes)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import aiohttp
import requests

from fgv_trader.models import Candle

log = logging.getLogger(__name__)


def utc_timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def parse_candles(symbol, rows, minutes, start, end):
    candles = {}
    for row in rows:
        timestamp = utc_timestamp(row['start'])
        if timestamp < start or timestamp + timedelta(minutes=minutes) > end:
            continue
        values = [float(row[key]) for key in ('open', 'high', 'low', 'close', 'volume')]
        if not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0 or values[4] < 0:
            raise ValueError(f'Invalid OHLCV for {symbol}')
        o, h, l, c, v = values
        if l > min(o, c) or h < max(o, c) or l > h:
            raise ValueError(f'Inconsistent OHLC for {symbol}')
        candles[timestamp] = Candle(symbol, timestamp, o, h, l, c, v)
    return sorted(candles.values(), key=lambda bar: bar.timestamp)


class BackpackMarketData:
    def __init__(self, settings):
        self.settings = settings
        self.base_url = settings.get('base_url', 'https://api.backpack.exchange').rstrip('/')
        self.timeout = settings.get('request_timeout_seconds', 10)
        self.quotes = {}
        self.quote_providers = {}
        self.cache = {}
        self.providers = {}
        self.last_fetch = {}
        self.fallback = None
        if settings.get('alpaca_fallback'):
            from alpaca.data.historical import StockHistoricalDataClient
            self.fallback = StockHistoricalDataClient(os.environ['ALPACA_API_KEY'], os.environ['ALPACA_API_SECRET'])

    def market_symbol(self, symbol):
        return self.settings.get('symbol_map', {}).get(symbol, f'{symbol}.US_USDC')

    def _fallback_bars(self, symbol, start, end, minutes):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        response = self.fallback.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
            start=start, end=end, feed='iex'))
        rows = [dict(start=b.timestamp.isoformat(), open=b.open, high=b.high, low=b.low,
                     close=b.close, volume=b.volume) for b in response.data.get(symbol, [])]
        return parse_candles(symbol, rows, minutes, start, end)

    def get_bars(self, symbols, start, end, minutes):
        # One task per unique symbol; no two workers mutate the same cache key.
        symbols = list(dict.fromkeys(symbols))
        with ThreadPoolExecutor(max_workers=self.settings.get('candle_workers', 8)) as pool:
            results = pool.map(lambda s: self._get_bars([s], start, end, minutes), symbols)
            return {s: bars for result in results for s, bars in result.items()}

    def _get_bars(self, symbols, start, end, minutes):
        result = {}
        for symbol in symbols:
            key = (symbol, start.date().isoformat(), minutes)
            provider_key = key[:2]
            if time.monotonic() - self.last_fetch.get(key, -1e10) < self.settings['bar_poll_interval_seconds']:
                result[symbol] = self.cache.get(key, [])
                continue
            existing = self.cache.get(key, [])
            # Completed coverage is immutable; poll again only for a new bar.
            expected = int((end - start).total_seconds() // (minutes * 60))
            stamps = {b.timestamp for b in existing}
            if expected > 0 and all(start + timedelta(minutes=minutes * i) in stamps for i in range(expected)):
                result[symbol] = [b for b in existing if start <= b.timestamp and b.timestamp + timedelta(minutes=minutes) <= end]
                continue
            fetch_start = next((start + timedelta(minutes=minutes * i) for i in range(expected)
                                if start + timedelta(minutes=minutes * i) not in stamps), start)
            self.last_fetch[key] = time.monotonic()
            try:
                if self.providers.get(provider_key) == 'alpaca':
                    bars = self._fallback_bars(symbol, fetch_start, end, minutes)
                else:
                    response = requests.get(f'{self.base_url}/api/v1/klines', params={
                        'symbol': self.market_symbol(symbol), 'interval': f'{minutes}m',
                        'startTime': int(fetch_start.timestamp()), 'endTime': int(end.timestamp()),
                        'source': 'External', 'priceType': 'Last',
                    }, timeout=self.timeout)
                    response.raise_for_status()
                    bars = parse_candles(symbol, response.json(), minutes, fetch_start, end)
                    if not bars and not existing:
                        raise ValueError(f'No completed {minutes}m candles for {symbol}')
                    self.providers[provider_key] = 'backpack'
                merged = {b.timestamp: b for b in existing + bars}
                self.cache[key] = sorted(merged.values(), key=lambda b: b.timestamp)
                self.last_fetch[key] = time.monotonic()
                result[symbol] = self.cache[key]
            except Exception:
                # A fallback can be selected only before any session bars are used.
                if self.fallback and provider_key not in self.providers:
                    try:
                        bars = self._fallback_bars(symbol, start, end, minutes)
                        if bars:
                            self.providers[provider_key] = 'alpaca'
                            self.cache[key] = bars
                            self.last_fetch[key] = time.monotonic()
                            result[symbol] = bars
                            log.warning('%s uses Alpaca for the entire session', symbol)
                            continue
                    except Exception:
                        log.exception('Alpaca fallback failed for %s', symbol)
                log.exception('Candle data unavailable for %s', symbol)
                result[symbol] = []
        return result

    def ingest_quote(self, message):
        data = message.get('data', message)
        if data.get('e') == 'externalTicker':
            symbol = self.stock_symbol(data['s'])
            price, timestamp = float(data['c']), float(data['E']) / 1_000_000
            provider = 'backpack_ws'
        elif data.get('e') == 'stockPrice':
            symbol = data['symbol']
            price, timestamp = float(data['mid']), float(data['timestamp']) / 1000
            provider = 'backpack'
        else:
            return
        if symbol and price > 0 and math.isfinite(price) and math.isfinite(timestamp):
            if timestamp >= self.quotes.get(symbol, (0, 0))[1]:
                self.quotes[symbol] = (price, timestamp)
                self.quote_providers[symbol] = provider

    def stock_symbol(self, market_symbol):
        reverse = {v: k for k, v in self.settings.get('symbol_map', {}).items()}
        if market_symbol in reverse:
            return reverse[market_symbol]
        suffix = '.US_USDC'
        return market_symbol[:-len(suffix)] if market_symbol.endswith(suffix) else None

    def get_latest_prices(self, symbols, now=None):
        return {s: quote[0] for s, quote in self.get_latest_observations(symbols, now).items()}

    def get_latest_observations(self, symbols, now=None):
        now = time.time() if now is None else now
        maximum_age = self.settings['max_price_age_seconds']
        quotes = {s: self.quotes.get(s) for s in symbols}
        return {s: q for s, q in quotes.items() if q and -2 <= now - q[1] <= maximum_age}

    async def stream(self, symbols):
        delay = 1
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.settings['websocket_url'], heartbeat=30) as ws:
                        for offset in range(0, len(symbols), 100):
                            await ws.send_json({'method': 'SUBSCRIBE', 'params': [
                                f'externalTicker.{self.market_symbol(s)}' for s in symbols[offset:offset + 100]]})
                        delay = 1
                        while True:
                            # Heartbeats alone must not keep a silent feed alive.
                            message = await asyncio.wait_for(ws.receive(), self.settings['max_price_age_seconds'])
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    payload = json.loads(message.data)
                                    if 'error' in payload or payload.get('e') == 'error':
                                        log.error('Backpack subscription error: %s', str(payload)[:300])
                                    self.ingest_quote(payload)
                                except (ValueError, KeyError, TypeError):
                                    log.warning('Invalid Backpack quote: %s', message.data[:200])
                            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                                break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                log.exception('Backpack stream disconnected')
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    def _refresh_fallback_quotes(self, symbols):
        from alpaca.data.requests import StockLatestTradeRequest
        missing = [s for s in symbols if s not in self.get_latest_prices(symbols)]
        if not missing:
            return
        response = self.fallback.get_stock_latest_trade(StockLatestTradeRequest(
            symbol_or_symbols=missing, feed='iex'))
        for symbol, quote in response.items():
            price, timestamp = float(quote.price), quote.timestamp.timestamp()
            if price > 0 and math.isfinite(price) and timestamp >= self.quotes.get(symbol, (0, 0))[1]:
                self.quotes[symbol] = (price, timestamp)
                self.quote_providers[symbol] = 'alpaca'

    async def fallback_quotes(self, symbols):
        last_warning = -1e10
        while True:
            try:
                missing = set(symbols) - self.get_latest_prices(symbols).keys()
                if missing:
                    if time.monotonic() - last_warning >= 60:
                        log.warning('Backpack feed missing/stale for %d/%d symbols; trying batch REST', len(missing), len(symbols))
                        last_warning = time.monotonic()
                    rows, timestamp = await asyncio.to_thread(self._rest_quotes)
                    for row in rows:
                        symbol = self.stock_symbol(row.get('symbol', ''))
                        if symbol not in missing or symbol in self.get_latest_prices([symbol]):
                            continue
                        try:
                            price = float(row['lastPrice'])
                            if price > 0 and math.isfinite(price):
                                self.quotes[symbol] = (price, timestamp)
                                self.quote_providers[symbol] = 'backpack_rest'
                        except (ValueError, TypeError, KeyError):
                            continue
            except Exception:
                log.exception('Backpack batch quote refresh failed')
            if self.fallback:
                try:
                    await asyncio.to_thread(self._refresh_fallback_quotes, symbols)
                except Exception:
                    log.exception('Alpaca quote refresh failed')
            await asyncio.sleep(self.settings['price_poll_interval_seconds'])

    def _rest_quotes(self):
        # REST has no source timestamp. Conservatively age from request start;
        # neither REST receipt nor WS event time proves underlying trade freshness.
        timestamp = time.time()
        response = requests.get(f'{self.base_url}/api/v1/tickers', params={'source': 'External'}, timeout=self.timeout)
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError('Invalid batch ticker response')
        return rows, timestamp

    def health(self):
        return {'fresh_count': len(self.get_latest_prices(list(self.quotes))),
                'quote_providers': dict(Counter(self.quote_providers.copy().values())),
                'candle_providers': dict(Counter(self.providers.copy().values()))}
