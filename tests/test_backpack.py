from datetime import datetime, timezone

import pytest

from fgv_trader.market_data import BackpackMarketData, parse_candles


def row(start='2026-09-04 13:30:00'):
    return dict(start=start, open='100', high='102', low='99', close='101', volume='20')


def test_normalizes_deduplicates_and_excludes_incomplete_bars():
    start = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 9, 4, 13, 37, tzinfo=timezone.utc)
    bars = parse_candles('TSLA', [row(), row(), row('2026-09-04T13:35:00Z')], 5, start, end)
    assert len(bars) == 1
    assert bars[0].timestamp == start


def test_rejects_invalid_candles():
    start = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_candles('TSLA', [dict(row(), close='nan')], 5, start, end)


def test_quote_source_timestamp_and_out_of_order_messages():
    market = BackpackMarketData({'max_price_age_seconds': 30})
    market.ingest_quote({'data': dict(e='stockPrice', symbol='TSLA', mid='100', timestamp=100000)})
    market.ingest_quote(dict(e='stockPrice', symbol='TSLA', mid='90', timestamp=90000))
    assert market.get_latest_prices(['TSLA'], now=110) == {'TSLA': 100}
    assert market.get_latest_prices(['TSLA'], now=131) == {}
    assert market.get_latest_prices(['TSLA'], now=90) == {}


def test_external_source_symbol_and_incremental_cache(monkeypatch):
    calls = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return [row()]
    def get(url, **kwargs):
        calls.append(kwargs['params'])
        return Response()
    monkeypatch.setattr('fgv_trader.market_data.requests.get', get)
    market = BackpackMarketData({'bar_poll_interval_seconds': 30})
    start = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 9, 4, 13, 35, tzinfo=timezone.utc)
    for _ in range(2):
        assert len(market.get_bars(['TSLA'], start, end, 5)['TSLA']) == 1
    assert len(calls) == 1
    assert calls[0]['symbol'] == 'TSLA.US_USDC'
    assert calls[0]['source'] == 'External'
    assert calls[0]['priceType'] == 'Last'


def test_external_ticker_mapping_age_and_validation():
    market = BackpackMarketData({'max_price_age_seconds': 30, 'symbol_map': {'BRK.B': 'CUSTOM.US_USDC'}})
    def ingest(price, stamp, symbol='CUSTOM.US_USDC'):
        market.ingest_quote({'data': dict(e='externalTicker', s=symbol, c=price, E=stamp * 1_000_000)})
    ingest('100', 100)
    ingest('90', 99)
    ingest('nan', 101)
    ingest('55', 101, 'SOL_USDC_PERP')
    assert market.get_latest_prices(['BRK.B'], now=110) == {'BRK.B': 100}
    assert market.get_latest_prices(['BRK.B'], now=131) == {}
    assert market.quote_providers == {'BRK.B': 'backpack_ws'}


def test_completed_coverage_survives_poll_expiry(monkeypatch):
    market = BackpackMarketData({'bar_poll_interval_seconds': 30})
    start = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 9, 4, 13, 35, tzinfo=timezone.utc)
    key = ('TSLA', '2026-09-04', 5)
    market.cache[key] = parse_candles('TSLA', [row()], 5, start, end)
    monkeypatch.setattr('fgv_trader.market_data.requests.get', lambda *a, **kw: pytest.fail('Unnecessary refetch'))
    assert len(market.get_bars(['TSLA'], start, end, 5)['TSLA']) == 1


def test_batch_rest_fallback_does_not_override_fresh_ws(monkeypatch):
    import asyncio
    import time
    market = BackpackMarketData({'max_price_age_seconds': 30, 'price_poll_interval_seconds': .01})
    market.ingest_quote(dict(e='externalTicker', s='TSLA.US_USDC', c='100', E=time.time()*1_000_000))
    monkeypatch.setattr(market, '_rest_quotes', lambda: ([
        {'symbol': 'TSLA.US_USDC', 'lastPrice': '90'},
        {'symbol': 'NVDA.US_USDC', 'lastPrice': '200'}], time.time()))
    async def run():
        task = asyncio.create_task(market.fallback_quotes(['TSLA', 'NVDA']))
        try:
            for _ in range(100):
                if 'NVDA' in market.quotes:
                    break
                await asyncio.sleep(.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    assert market.get_latest_prices(['TSLA', 'NVDA']) == {'TSLA': 100, 'NVDA': 200}
    assert market.quote_providers['NVDA'] == 'backpack_rest'
