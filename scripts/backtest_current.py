"""Read-only market replay using production FGV decisions and frozen stop rules.

Only public data requests and dedicated backtest artifact writes are performed.
No blockchain clients, production Store, credentials, wallets or order submission.
"""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from backtest_data import HistoricalData, ROOT, snapshot
from fgv_trader.models import Candle
from fgv_trader.strategy import FGVStrategy
from fgv_trader.prediction import WinProbabilityEstimator
from fgv_trader.entry_safety import rejection
from fgv_trader.settings import STRATEGY_KEYS
from fgv_trader.stops import DEFAULTS, build_policy, stop_decision

UTC = timezone.utc
ET = ZoneInfo('America/New_York')
START = datetime(2026,7,10,13,30,tzinfo=UTC)
END = datetime(2026,9,9,20,tzinfo=UTC)


def timestamp(value):
    stamp = datetime.fromisoformat(value.replace('Z','+00:00'))
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp


def sessions():
    day = START.date()
    while day <= END.date():
        if day.weekday() < 5 and day != date(2026,9,7):
            yield day
        day += timedelta(days=1)


def bounds(day):
    return datetime.combine(day,datetime.min.time(),ET).replace(hour=9,minute=30)


def candles(symbol, rows):
    result = []
    for row in rows:
        values = [float(row[k]) for k in ('open','high','low','close','volume')]
        o,h,l,c,v = values
        if not all(math.isfinite(x) for x in values) or min(o,h,l,c)<=0 or v<0 or not l<=min(o,c)<=max(o,c)<=h:
            raise ValueError('Invalid historical OHLCV: '+symbol)
        result.append(Candle(symbol,timestamp(row['start']),o,h,l,c,v))
    if len({b.timestamp for b in result}) != len(result):
        raise ValueError('Duplicate historical candles: '+symbol)
    return sorted(result,key=lambda b:b.timestamp)


def first_range(symbol, bars, opening):
    first = bars[:3]
    if len(first)!=3 or any(b.timestamp != opening+timedelta(minutes=i*5) for i,b in enumerate(first)):
        return None
    return Candle(symbol,opening,first[0].open,max(b.high for b in first),
                  min(b.low for b in first),first[-1].close,sum(b.volume for b in first))


def completed_prefix(bars,opening,now):
    # Production uses asof=now-1s; never use the current candle's high/low/close.
    count = int((now-opening-timedelta(seconds=1)).total_seconds()//300)
    prefix = bars[:max(0,count)]
    if len(prefix)!=count or any(b.timestamp!=opening+timedelta(minutes=i*5) for i,b in enumerate(prefix)):
        return []
    return prefix


def coarse_candidates(data, inputs, scan_end_minutes=120):
    strategy=FGVStrategy(**{k:inputs['fgv'][k] for k in STRATEGY_KEYS})
    output=defaultdict(dict)
    coverage=Counter()
    allowed=set(sessions())
    for index,symbol in enumerate(inputs['symbols'],1):
        try:
            rows=data.fetch(symbol,'5m',START,END)
            history=candles(symbol,rows)
        except Exception:
            coverage['coarse_download_or_parse_failure']+=1
            continue
        grouped=defaultdict(list)
        for b in history:
            local=b.timestamp.astimezone(ET)
            if local.date() in allowed and bounds(local.date())<=local<bounds(local.date())+timedelta(minutes=390):
                grouped[local.date()].append(b)
        for day in allowed:
            bars=grouped[day]
            opening=bounds(day)
            first=first_range(symbol,bars,opening)
            if not first:
                coverage['symbol_sessions_missing_opening']+=1
                continue
            coverage['symbol_sessions_with_opening']+=1
            for minutes in range(30,scan_end_minutes,5):
                now=opening+timedelta(minutes=minutes,seconds=5)
                prefix=completed_prefix(bars,opening,now)
                if prefix and strategy.build_signal(symbol,day.isoformat(),first,prefix[3:]):
                    output[day][symbol]=bars
                    coverage['candidate_symbol_sessions']+=1
                    break
        if index%50==0:
            print(f'Candidate screen {index}/{len(inputs["symbols"])}; {coverage["candidate_symbol_sessions"]} symbol-days',flush=True)
    (data.directory/'screen_coverage.json').write_text(json.dumps(dict(coverage),indent=2))
    return output


class Tape:
    def __init__(self,rows):
        # Each 1s candle close becomes knowable only at start+1 second.
        points={int(timestamp(r['start']).timestamp())+1:float(r['close']) for r in rows}
        if any(not math.isfinite(v) or v<=0 for v in points.values()):
            raise ValueError('Invalid historical reference price')
        self.times=sorted(points)
        self.prices=[points[t] for t in self.times]

    def quote(self,stamp,max_age=30):
        i=bisect_right(self.times,stamp)-1
        if i<0 or stamp-self.times[i]>max_age:
            return None
        return self.prices[i],self.times[i]


def download_fine(data,candidates,workers=4):
    tasks=[(day,symbol) for day in sorted(candidates) for symbol in sorted(candidates[day])]
    output=defaultdict(dict)
    coverage=[]
    def task(item):
        day,symbol=item
        opening=bounds(day)
        try:
            rows=data.fetch(symbol,'1s',opening,opening+timedelta(minutes=390))
            tape=Tape(rows)
            row=dict(session=day.isoformat(),symbol=symbol,rows=len(rows),
                     first=tape.times[0] if rows else None,last=tape.times[-1] if rows else None)
            return day,symbol,tape,row
        except Exception as exc:
            return day,symbol,None,dict(session=day.isoformat(),symbol=symbol,error=type(exc).__name__)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index,(day,symbol,tape,row) in enumerate(pool.map(task,tasks),1):
            coverage.append(row)
            if tape and tape.times:
                output[day][symbol]=tape
            if index%20==0 or index==len(tasks):
                print(f'1s download {index}/{len(tasks)}; {sum("error" in r for r in coverage)} errors',flush=True)
    (data.directory/'fine_coverage.json').write_text(json.dumps(coverage,indent=2))
    return output


@dataclass
class Assumptions:
    name: str = 'base_5bps'
    stop_exits_enabled: bool = True
    initial: float = 10000
    wallet_fund: float = 50
    slippage_bps: float = 5
    fee_per_side: float = 0
    buy_delay_seconds: int = 15
    sell_delay_seconds: int = 10


class Replay:
    def __init__(self,inputs,assumptions,scan_end_minutes=120):
        self.inputs=inputs
        self.a=assumptions
        self.scan_end_minutes=scan_end_minutes
        self.strategy=FGVStrategy(**{k:inputs['fgv'][k] for k in STRATEGY_KEYS})
        model_path=Path(inputs['_directory'])/'frozen_model.json'
        if not model_path.exists():
            model_path.write_text(json.dumps(inputs['model'],indent=2))
        self.model=WinProbabilityEstimator.load(model_path,feature_names=inputs['fgv']['win_probability_features'])
        if self.model.feature_names != ['stop_risk_pct','entry_minutes','signal_minutes']:
            raise ValueError('Replay feature shortcut only supports the current three-feature model')
        self.vault=self.a.initial
        self.wallets=[]
        self.active={}
        self.trades=[]
        self.rejections=Counter()
        self.daily=[]
        self.peak=self.a.initial
        self.max_drawdown=0
        self.peak_deployed=0
        self.deployment_sum=0
        self.steps=0
        self.gaps=Counter()

    def select_wallet(self):
        idle=sorted((w for w in self.wallets if w['state']=='idle' and w['cash']>=self.inputs['fgv']['min_order_usdc']),key=lambda w:-w['cash'])
        if idle:
            return idle[0]
        if self.vault<self.a.wallet_fund+self.inputs['fgv']['reserve_usdc']:
            return None
        self.vault-=self.a.wallet_fund
        wallet=dict(id=len(self.wallets)+1,cash=self.a.wallet_fund,losses=0,state='idle')
        self.wallets.append(wallet)
        return wallet

    def select(self,symbol,signal,probability,bars,now):
        wallet=self.select_wallet()
        if wallet is None:
            self.rejections['INSUFFICIENT_CASH']+=1
            return False
        wallet['state']='busy'
        policy=build_policy(signal,bars,now,dict(DEFAULTS,**self.inputs['stop_policy']))
        stop=policy['emergency_stop']
        cutoff=bounds(now.date())+timedelta(minutes=self.scan_end_minutes)
        entry_buffer_r=self.inputs['execution'].get('max_entry_above_trigger_r',0)
        guard=dict(signal=asdict(signal),risk_stop=stop,
                   reward_risk_stop=signal.stop_loss,
                   max_entry_price=signal.trigger_low+entry_buffer_r*signal.risk,
                   max_entry_above_trigger_r=entry_buffer_r,
                   allow_entry_below_original_stop=self.inputs['execution'].get(
                       'allow_entry_below_original_stop', False),
                   expires_at=min(now.timestamp()+self.inputs['execution']['entry_max_age_seconds'],cutoff.timestamp()),
                   min_reward_risk=self.inputs['execution']['min_entry_reward_risk'])
        self.active[symbol]=dict(symbol=symbol,session=now.date().isoformat(),state='BUY_PENDING',
                                wallet=wallet,signal=asdict(signal),stop_policy=policy,entry_guard=guard,
                                selected_at=now.isoformat(),due=now.timestamp()+self.a.buy_delay_seconds,
                                probability=probability,quantity=0,entry_fee=0)
        return True

    def progress(self,now,quotes):
        stamp=now.timestamp()
        for symbol,trade in list(self.active.items()):
            wallet=trade['wallet']
            quote=quotes.get(symbol)
            if trade['state']=='BUY_PENDING':
                error=rejection(trade,now=stamp)
                if not error and stamp<trade['due']:
                    continue
                if not error and quote is None:
                    self.gaps['pending_entry_missing_quote_ticks']+=1
                    continue
                if not error:
                    price=quote[0]*(1+self.a.slippage_bps/10000)
                    error=rejection(trade,price,now=stamp)
                if error=='ENTRY_ABOVE_TRIGGER':
                    self.rejections['ABOVE_TRIGGER_RETRY_TICKS']+=1
                    continue
                if error:
                    self.rejections[error]+=1
                    wallet['state']='idle'
                    del self.active[symbol]
                    continue
                cost=math.floor(wallet['cash']*1e6)/1e6
                trade.update(state='OPEN',entry_time=now.isoformat(),entry_price=price,
                             cost=cost,quantity=cost/price,entry_fee=self.a.fee_per_side)
                wallet['cash']-=cost
                self.vault-=self.a.fee_per_side
                continue
            if trade['state']=='OPEN':
                price,quote_at=quote if quote else (None,None)
                subtype=stop_decision(trade,price,quote_at,now) if self.a.stop_exits_enabled else None
                if now>=bounds(now.date())+timedelta(minutes=375):
                    reason='FORCE_EXIT'
                elif subtype:
                    reason='STOP_LOSS'
                elif price is not None and price>=trade['signal']['take_profit']:
                    reason='TAKE_PROFIT'
                else:
                    if quote is None:
                        self.gaps['open_missing_quote_ticks']+=1
                    continue
                trade.update(state='SELL_PENDING',exit_reason=reason,stop_subtype=subtype if reason=='STOP_LOSS' else None,
                             exit_trigger_time=now.isoformat(),due=stamp+self.a.sell_delay_seconds)
                continue
            if trade['state']=='SELL_PENDING' and stamp>=trade['due']:
                if quote is None:
                    self.gaps['exit_missing_quote_ticks']+=1
                    continue
                exit_price=quote[0]*(1-self.a.slippage_bps/10000)
                proceeds=math.floor(trade['quantity']*exit_price*1e6)/1e6
                pnl=proceeds-trade['cost']
                wallet['cash']+=proceeds
                self.vault-=self.a.fee_per_side
                wallet['losses']+=int(pnl<0)
                wallet['state']='idle'
                if wallet['losses']>=self.inputs['max_loss_traders']:
                    self.vault+=wallet['cash']
                    wallet.update(cash=0,state='abandoned')
                self.trades.append(dict(symbol=symbol,session=trade['session'],wallet=wallet['id'],
                    selected_at=trade['selected_at'],entry_time=trade['entry_time'],exit_trigger_time=trade['exit_trigger_time'],
                    exit_time=now.isoformat(),entry_price=trade['entry_price'],exit_price=exit_price,
                    quantity=trade['quantity'],cost=trade['cost'],proceeds=proceeds,
                    pnl_after_slippage=pnl,modeled_fees=trade['entry_fee']+self.a.fee_per_side,
                    net_pnl=pnl-trade['entry_fee']-self.a.fee_per_side,exit_reason=trade['exit_reason'],
                    stop_subtype=trade.get('stop_subtype'),original_stop=trade['signal']['stop_loss'],
                    buffered_stop=trade['stop_policy']['buffered_stop'],emergency_stop=trade['stop_policy']['emergency_stop'],
                    atr=trade['stop_policy'].get('atr'),probability=trade['probability']))
                del self.active[symbol]

    def mark(self,quotes,last_prices):
        equity=self.vault+sum(w['cash'] for w in self.wallets)
        deployed=0
        for symbol,t in self.active.items():
            if t['quantity']:
                reference=quotes[symbol][0] if symbol in quotes else last_prices.get(symbol,t['entry_price'])
                equity+=t['quantity']*reference
                deployed+=t['cost']
        self.peak=max(self.peak,equity)
        self.max_drawdown=max(self.max_drawdown,(self.peak-equity)/self.peak*100)
        self.peak_deployed=max(self.peak_deployed,deployed)
        self.deployment_sum+=deployed
        self.steps+=1
        return equity

    def run_day(self,day,coarse,tapes):
        opening=bounds(day)
        first={s:first_range(s,b,opening) for s,b in coarse.items()}
        used=set()
        cached={}
        last_prices={}
        # Fixed five-second replay clock; production I/O timing is not reproduced.
        for seconds in range(0,390*60,5):
            now=opening+timedelta(seconds=seconds)
            quotes={s:q for s,tape in tapes.items() if (q:=tape.quote(now.timestamp(),self.inputs['market_data']['max_price_age_seconds']))}
            last_prices.update({s:q[0] for s,q in quotes.items()})
            self.progress(now,quotes)
            if 900<=seconds<self.scan_end_minutes*60:
                candidates=[]
                key=(seconds-1)//300
                for symbol,bars in coarse.items():
                    if symbol in used or symbol not in quotes or symbol not in tapes:
                        continue
                    if symbol not in cached or cached[symbol][0]!=key:
                        prefix=completed_prefix(bars,opening,now)
                        sig=self.strategy.build_signal(symbol,day.isoformat(),first[symbol],prefix[3:]) if prefix else None
                        cached[symbol]=(key,sig,prefix)
                    _,sig,prefix=cached[symbol]
                    if sig is None or not self.strategy.should_market_buy(sig,quotes[symbol][0]):
                        continue
                    minutes=seconds//60
                    if self.strategy.should_skip_entry(sig,minutes):
                        continue
                    probability=self.model.predict(dict(stop_risk_pct=sig.risk/sig.trigger_low*100,
                        entry_minutes=minutes,signal_minutes=(sig.c3_time-opening).total_seconds()/60))
                    if probability>=self.inputs['fgv']['min_win_probability']:
                        candidates.append((probability,symbol,sig,prefix))
                for probability,symbol,sig,prefix in sorted(candidates,key=lambda x:(-x[0],x[1])):
                    if len(self.active)>=self.inputs['fgv']['max_concurrent_positions']:
                        break
                    if self.select(symbol,sig,probability,prefix,now):
                        used.add(symbol)
            equity=self.mark(quotes,last_prices)
        if self.active:
            raise RuntimeError(f'Unresolved positions at session end {day}: {list(self.active)}; cannot report a fully liquidated result')
        self.daily.append(dict(session=day.isoformat(),equity=equity,
                               profit=equity-(self.daily[-1]['equity'] if self.daily else self.a.initial)))

    def summary(self):
        net=sum(t['net_pnl'] for t in self.trades)
        equity=self.vault+sum(w['cash'] for w in self.wallets)
        assert abs(equity-self.a.initial-net)<1e-5, 'Portfolio conservation failed'
        wins=sum(t['net_pnl']>0 for t in self.trades)
        return dict(assumptions=asdict(self.a),initial=self.a.initial,ending_equity=equity,net_profit=net,
                    roi_pct=net/self.a.initial*100,trades=len(self.trades),wins=wins,
                    losses=sum(t['net_pnl']<0 for t in self.trades),
                    win_rate_pct=100*wins/len(self.trades) if self.trades else None,
                    max_sampled_drawdown_pct=self.max_drawdown,peak_deployed=self.peak_deployed,
                    average_deployed=self.deployment_sum/self.steps if self.steps else 0,
                    wallets_created=len(self.wallets),wallets_retired=sum(w['state']=='abandoned' for w in self.wallets),
                    exit_reasons=dict(Counter(t['exit_reason'] for t in self.trades)),
                    stop_subtypes=dict(Counter(t['stop_subtype'] for t in self.trades if t['stop_subtype'])),
                    rejection_counts=dict(self.rejections),data_gap_ticks=dict(self.gaps),
                    after_training_period_profit=sum(t['net_pnl'] for t in self.trades if t['session']>'2026-08-28'),
                    after_training_period_trades=sum(t['session']>'2026-08-28' for t in self.trades))


def write_csv(path,rows):
    if rows:
        with path.open('w',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',required=True)
    parser.add_argument('--wallet-funds',default='50')
    parser.add_argument('--no-stop-exits',action='store_true',
                        help='Disable all stop exits, retaining entry gates and take-profit/time exits')
    args=parser.parse_args()
    data=HistoricalData(args.directory)
    inputs=snapshot(data.directory)
    inputs['_directory']=str(data.directory)
    output=data.directory/'no_stop_exits' if args.no_stop_exits else data.directory
    output.mkdir(parents=True,exist_ok=True)
    candidates=coarse_candidates(data,inputs)
    print('Candidate symbol-days:',sum(map(len,candidates.values())),flush=True)
    tapes=download_fine(data,candidates)
    assumptions=[]
    for fund in map(float,args.wallet_funds.split(',')):
        for bps,fee,delay in [(0,0,15),(5,0,15),(25,0,15),(50,0,15),(5,.05,15),(5,0,30)]:
            assumptions.append(Assumptions(name=f'wallet{fund:g}_{bps}bps_fee{fee:g}_delay{delay}',
                                          stop_exits_enabled=not args.no_stop_exits,
                                          wallet_fund=fund,slippage_bps=bps,fee_per_side=fee,buy_delay_seconds=delay))
    summaries=[]
    for assumption in assumptions:
        replay=Replay(inputs,assumption)
        for day in sessions():
            replay.run_day(day,candidates.get(day,{}),tapes.get(day,{}))
        summary=replay.summary()
        summaries.append(summary)
        write_csv(output/(assumption.name+'_trades.csv'),replay.trades)
        write_csv(output/(assumption.name+'_daily.csv'),replay.daily)
        print(json.dumps(summary),flush=True)
        (output/'results.json').write_text(json.dumps(summaries,indent=2))


if __name__=='__main__':
    main()
