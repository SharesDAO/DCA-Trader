"""FGV orchestration: unchanged decision core, wallet-backed asynchronous settlement."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import fcntl
import json
import logging
import signal as os_signal
import time
from dataclasses import asdict
from collections import Counter
from datetime import datetime, timedelta
from uuid import uuid4

from database import Database
from fgv_trader.execution import LiveBroker, PaperBroker
from fgv_trader.entry_safety import rejection
from fgv_trader.features import TechnicalFeatureCalculator
from fgv_trader.market_data import BackpackMarketData
from fgv_trader.models import Candle
from fgv_trader.portfolio import RiskManager
from fgv_trader.prediction import WinProbabilityEstimator
from fgv_trader.settings import STRATEGY_KEYS
from fgv_trader.store import Store
from fgv_trader.stops import settings as stop_settings, build_policy, stop_decision
from fgv_trader.strategy import FGVStrategy
from fgv_trader.time_utils import TimeService

log = logging.getLogger(__name__)
READY_SYMBOL = '__FGV_READY__'
# Public simulation key: used ONLY for a separate paper DB containing no real keys.
PAPER_KEY = base64.urlsafe_b64encode(hashlib.sha256(b'FGV paper simulation only').digest()).decode()


class Engine:
    def __init__(self, config, store, broker, market):
        self.config, self.store, self.broker, self.market = config, store, broker, market
        self.clock = TimeService(config.session_time['exchange_timezone'],
                                 config.session_time['local_timezone'], config.session_time)
        self.strategy = FGVStrategy(**{k: config.fgv[k] for k in STRATEGY_KEYS})
        self.risk = RiskManager(**{k: config.fgv[k] for k in (
            'allocation_pct_per_trade', 'risk_pct_per_trade', 'max_concurrent_positions',
            'min_order_usdc', 'reserve_usdc')})
        self.features = TechnicalFeatureCalculator()
        self.model = WinProbabilityEstimator.load(
            config.project_root / config.fgv['win_probability_model_path'],
            feature_names=config.fgv['win_probability_features'])
        self.symbols = sorted(config.trading_stocks)
        self.first = {}
        self.bars = {}
        self.balances = {}
        self.data_session = None
        self.jobs = {}
        self.refresh_job = None
        self.data_asof = None
        self.scan_diagnostics = {}
        self.last_order_alert = {}
        self.prefund_enabled = config.fgv.get('prefund_wallets', False)
        self.stop_settings = stop_settings(config)
        log.warning('FGV stop-loss exits enabled=%s (includes emergency and legacy stops)',
                    self.stop_settings['exits_enabled'])
        self.decision_observations = {}
        log.info('FGV new-entry stop policy: enabled=%s observations=%s interval=%ss ATR multiplier=%s buffer cap=%sR emergency extra=%sR analytics=%s',
                 self.stop_settings['enabled'], self.stop_settings['confirmation_observations'],
                 self.stop_settings['confirmation_seconds'], self.stop_settings['atr_multiplier'],
                 self.stop_settings['max_buffer_r'], self.stop_settings['emergency_extra_r'],
                 self.stop_settings['record_observations'])
        log.info('FGV prefunded wallet pool enabled=%s target=%s initial_usdc=%.6f',
                 self.prefund_enabled, self.config.fgv['max_concurrent_positions'],
                 self.prefund_amount())

    def confidence_allocation(self, probability):
        """Linearly size qualified entries and round to the nearest whole USDC."""
        base = float(self.config.fgv.get('initial_usdc_per_wallet', 100))
        settings = self.config.fgv.get('confidence_sizing', {})
        if not settings.get('enabled', False):
            return int(base * 1e6) / 1e6, 1.0
        floor = float(self.config.fgv['min_win_probability'])
        ceiling = float(settings['max_probability'])
        normalized = max(0.0, min(1.0, (float(probability) - floor) / (ceiling - floor)))
        minimum = float(settings['min_multiplier'])
        multiplier = minimum + normalized * (float(settings['max_multiplier']) - minimum)
        return int(base * multiplier + 0.5), multiplier

    def prefund_amount(self):
        """Fund ready wallets for the largest possible confidence allocation."""
        base = float(self.config.fgv.get('initial_usdc_per_wallet', 100))
        settings = self.config.fgv.get('confidence_sizing', {})
        multiplier = float(settings['max_multiplier']) if settings.get('enabled', False) else 1.0
        return int(base * multiplier * 1e6) / 1e6

    def apply_funding_reservations(self, snapshot):
        """Reserve unmined funding against a fresh vault snapshot after restart."""
        balances = dict(snapshot)
        for trade in self.store.trades(active=True):
            if trade['state'] == 'FUNDING' and not trade.get('reused_wallet'):
                shortfall = max(0, trade['amount'] - snapshot.get(trade['wallet'], 0))
                balances[self.broker.vault] = balances.get(self.broker.vault, 0) - shortfall
        for op in self.store.operations():
            if op.get('prefund') and op['kind'] == 'fund' and op['state'] not in ('SETTLED', 'FAILED'):
                shortfall = max(0, op['amount'] - snapshot.get(op['wallet'], 0))
                balances[self.broker.vault] = balances.get(self.broker.vault, 0) - shortfall
        return balances

    def _prefund_trade(self, wallet):
        candidates = [t for t in self.store.trades() if t.get('prefund') and not t.get('consumed_at')
                      and t['wallet'] == wallet['address']]
        if candidates:
            return candidates[-1]
        return self.store.maintenance_trade(wallet, prefund=True)

    def _consume_prefunded_wallet(self, wallet):
        candidates = [t for t in self.store.trades() if t.get('prefund') and not t.get('consumed_at')
                      and t['wallet'] == wallet]
        if not candidates:
            raise RuntimeError('Ready wallet has no durable prefunding record')
        trade = candidates[-1]
        trade['consumed_at'] = datetime.now().astimezone().isoformat()
        self.store.save_trade(trade)

    def ensure_prefunded_wallets(self):
        """Recover and fill the ready pool without ever assigning a stock early."""
        if not self.prefund_enabled or self.config.liquid_mode or self.store.liquidating() or not self.balances:
            return
        initial = self.prefund_amount()
        pending = [w for w in self.store.db.get_wallets_by_status(self.config.blockchain, 'pending_funding')
                   if w['assigned_stock'] == READY_SYMBOL]
        # A crash between wallet persistence and operation creation resumes here.
        for wallet in pending:
            trade = self._prefund_trade(wallet)
            ops = self.store.operations(trade['id'])
            for kind, amount in (('fund', initial), ('gas', self.config.gas_per_wallet)):
                if not any(o['kind'] == kind for o in ops):
                    if kind == 'fund':
                        shortfall = max(0, initial - self.balances.get(wallet['address'], 0))
                        if self.balances.get(self.broker.vault, 0) < shortfall + self.risk.reserve_usdc:
                            log.warning('FGV cannot resume prefunding %s: vault needs %.6f USDC plus %.6f reserve',
                                        wallet['address'], shortfall, self.risk.reserve_usdc)
                            break
                        self.balances[self.broker.vault] -= shortfall
                    self.store.new_operation(trade, kind, amount, prefund=True)
                    ops = self.store.operations(trade['id'])
            ops = self.store.operations(trade['id'])
            funding = [o for o in ops if o['kind'] in ('fund', 'gas')]
            if len(funding) == 2 and all(o['state'] == 'SETTLED' for o in funding):
                trade['ready_at'] = datetime.now().astimezone().isoformat()
                self.store.save_trade(trade)
                self.store.db.update_wallet_status(wallet['address'], 'active')
                # A settled fund operation targets this exact balance and an
                # existing orphan balance only reduces the transferred shortfall.
                self.balances[wallet['address']] = initial
                log.info('FGV wallet ready before stock selection: %s USDC=%.6f',
                         wallet['address'], self.balances[wallet['address']])
            elif len(funding) == 2 and all(o['state'] in ('SETTLED', 'FAILED') for o in funding) and any(
                    o['state'] == 'FAILED' for o in funding):
                self.store.db.update_wallet_status(wallet['address'], 'prefund_failed')
                log.error('FGV prefunding quarantined after terminal failure: %s', wallet['address'])

        active = self.store.db.get_active_wallets(self.config.blockchain)
        busy = self.store.busy_wallets()
        busy.update(p['wallet_address'] for p in self.store.db.get_all_positions())
        busy.update(o['wallet_address'] for o in self.store.db.get_pending_orders())
        # Re-arm reusable wallets with gas while idle; never top up their USDC.
        for wallet in active:
            address = wallet['address']
            if (address in busy or self.balances.get(address, 0) < self.risk.min_order_usdc
                    or wallet['assigned_stock'] == READY_SYMBOL):
                continue
            trade = self._prefund_trade(wallet)
            ops = self.store.operations(trade['id'])
            gas = next((o for o in ops if o['kind'] == 'gas'), None)
            if gas is None:
                self.store.new_operation(trade, 'gas', self.config.gas_per_wallet, prefund=True, rearm=True)
            elif gas['state'] == 'SETTLED':
                trade['ready_at'] = datetime.now().astimezone().isoformat()
                self.store.save_trade(trade)
                self.store.db.update_wallet_stock(address, READY_SYMBOL)
                log.info('FGV reused wallet ready before stock selection: %s USDC=%.6f',
                         address, self.balances.get(address, 0))
            elif gas['state'] == 'FAILED':
                self.store.db.update_wallet_status(address, 'prefund_failed')
                log.error('FGV wallet gas preparation quarantined: %s', address)

        # Count invested/reserved wallets plus ready or in-flight capacity. Do not
        # count empty historical wallets, which cannot accept a new allocation.
        active = self.store.db.get_active_wallets(self.config.blockchain)
        pending = [w for w in self.store.db.get_wallets_by_status(self.config.blockchain, 'pending_funding')
                   if w['assigned_stock'] == READY_SYMBOL]
        occupied = {t['wallet'] for t in self.store.trades(active=True)}
        occupied.update(p['wallet_address'] for p in self.store.db.get_all_positions())
        occupied.update(o['wallet_address'] for o in self.store.db.get_pending_orders())
        capable = occupied | {w['address'] for w in active
                              if w['assigned_stock'] == READY_SYMBOL and self.balances.get(w['address'], 0) >= self.risk.min_order_usdc}
        for wallet in pending:
            trades = [t for t in self.store.trades() if t.get('prefund') and not t.get('consumed_at')
                      and t['wallet'] == wallet['address']]
            ops = self.store.operations(trades[-1]['id']) if trades else []
            if {o['kind'] for o in ops if o['state'] != 'FAILED'} >= {'fund', 'gas'}:
                capable.add(wallet['address'])
        # Active wallets undergoing a gas-only rearm already contain reusable cash.
        capable.update(w['address'] for w in active if self.balances.get(w['address'], 0) >= self.risk.min_order_usdc
                       and any(o.get('prefund') and o['wallet'] == w['address'] and o['state'] not in ('SETTLED', 'FAILED')
                               for o in self.store.operations()))
        needed = max(0, int(self.config.fgv['max_concurrent_positions']) - len(capable))
        created = 0
        for _ in range(needed):
            if self.balances.get(self.broker.vault, 0) < initial + self.risk.reserve_usdc:
                break
            address = self.broker.create_wallet(READY_SYMBOL, status='pending_funding')
            wallet = self.store.db.get_wallet(address)
            trade = self.store.maintenance_trade(wallet, prefund=True)
            self.store.new_operation(trade, 'fund', initial, prefund=True)
            self.store.new_operation(trade, 'gas', self.config.gas_per_wallet, prefund=True)
            self.balances[self.broker.vault] -= initial
            self.balances[address] = 0
            created += 1
        if created:
            log.info('FGV created %s prefunding wallets; target=%s reserved_capacity=%s',
                     created, self.config.fgv['max_concurrent_positions'], len(capable) + created)
        if len(capable) + created < int(self.config.fgv['max_concurrent_positions']):
            log.warning('FGV prefunded capacity %s/%s; vault needs %.6f USDC plus %.6f reserve for next wallet',
                        len(capable) + created, self.config.fgv['max_concurrent_positions'], initial, self.risk.reserve_usdc)

    def refresh(self, now):
        session = self.clock.session_for(now.date())
        symbols = sorted(set(self.symbols) | {'SPY'})
        asof = now - timedelta(seconds=1)
        bars = self.market.get_bars(symbols, session.market_open, asof, 5)
        # Same opening 15m OHLCV, aggregated from the three completed 5m bars.
        # They remain in the incremental session cache; no separate 15m sweep.
        first = {}
        for symbol, history in bars.items():
            opening = [b for b in history if session.market_open <= b.timestamp < session.first_15m_complete]
            if len(opening) == 3 and self.continuous(opening, session.market_open, session.first_15m_complete, 5):
                first[symbol] = [Candle(symbol, session.market_open, opening[0].open,
                    max(b.high for b in opening), min(b.low for b in opening),
                    opening[-1].close, sum(b.volume for b in opening))]
        return session.session_date.isoformat(), first, bars, self.broker.snapshot(), asof

    def available_usdc(self):
        busy = self.store.busy_wallets()
        busy.update(self.store.excluded_wallets())
        busy.update(p['wallet_address'] for p in self.store.legacy_positions())
        busy.update(o['wallet_address'] for o in self.store.db.get_pending_orders())
        available = sum(amount for address, amount in self.balances.items() if address not in busy)
        # New claims immediately debit virtual cash; refreshing also subtracts
        # outstanding funding reservations, preventing repeated allocation.
        return max(0, available)

    @staticmethod
    def continuous(bars, start, end, minutes):
        expected = int((end - start).total_seconds() // (minutes * 60))
        timestamps = {b.timestamp for b in bars}
        return all(start + timedelta(minutes=minutes * i) in timestamps for i in range(expected))

    def scan(self, now):
        session = self.clock.session_for(now.date())
        session_date = session.session_date.isoformat()
        if (self.config.liquid_mode or self.store.liquidating() or now < session.first_15m_complete
                or now >= session.scan_end or self.data_session != session_date):
            self.scan_diagnostics = {'status': 'paused' if self.config.liquid_mode or self.store.liquidating()
                                     else 'outside_entry_window' if now < session.first_15m_complete or now >= session.scan_end
                                     else 'awaiting_candles'}
            return
        asof = self.data_asof or now - timedelta(seconds=1)
        if not 0 <= (now - asof).total_seconds() <= self.config.market_data.get('max_candle_snapshot_age_seconds', 120):
            self.scan_diagnostics = {'status': 'stale_candle_snapshot'}
            return
        reasons = Counter()
        prices = self.market.get_latest_prices(self.symbols)
        entry_minutes = int((now - session.market_open).total_seconds() // 60)
        actionable = []
        evaluations = []
        model_version = self.model.to_dict()['model_version']
        for symbol in self.symbols:
            if self.store.has_traded(symbol, session_date):
                reasons['already_traded'] += 1
                continue
            if symbol not in prices:
                reasons['missing_price'] += 1
                continue
            first = (self.first.get(symbol) or [None])[0]
            history = self.bars.get(symbol, [])
            if not first or first.timestamp != session.market_open:
                reasons['missing_opening_range'] += 1
                continue
            # Bars are completed by the adapter. Missing bars are a data failure,
            # not a new price/signal filter.
            if not self.continuous(history, session.market_open, asof, 5):
                reasons['gapped_candles'] += 1
                continue
            signal = self.strategy.build_signal(symbol, session_date, first, [
                b for b in history if session.first_15m_complete <= b.timestamp < session.scan_end])
            price = prices.get(symbol)
            if not signal:
                reasons['no_fgv_setup'] += 1
                continue
            if not self.strategy.should_market_buy(signal, price):
                reasons['awaiting_pullback'] += 1
                continue
            if self.strategy.should_skip_entry(signal, entry_minutes):
                reasons['early_entry_filter'] += 1
                continue
            features = self.features.calculate(signal, first, history, self.bars.get('SPY', []),
                                               entry_minutes, session.market_open)
            probability = self.model.predict(features)
            qualified = probability >= self.config.fgv['min_win_probability']
            evaluations.append(dict(session=session_date, symbol=symbol, entry_minute=entry_minutes,
                                    observed_at=now.isoformat(), probability=probability,
                                    qualified=qualified, selected=False, model_version=model_version,
                                    features=features.values))
            if qualified:
                actionable.append((probability, symbol, signal))
            else:
                reasons['low_probability'] += 1
        evaluations_recorded = False
        if evaluations:
            try:
                self.store.record_candidate_evaluations(evaluations)
                evaluations_recorded = True
            except Exception:
                log.exception('FGV candidate telemetry persistence failed; trading continues')
        reasons['qualified'] = len(actionable)
        actionable.sort(key=lambda item: (-item[0], item[1]))
        for probability, symbol, signal in actionable:
            # Legacy holdings/pending orders consume slots during migration too.
            legacy_wallets = {p['wallet_address'] for p in self.store.legacy_positions()}
            legacy_wallets.update(o['wallet_address'] for o in self.store.db.get_pending_orders()
                                  if not o['order_id'].startswith('FGV_'))
            if not self.risk.can_enter(len(self.store.trades(active=True)) + len(legacy_wallets)):
                reasons['position_limit'] += 1
                break
            amount, _ = self.confidence_allocation(probability)
            if amount < self.risk.min_order_usdc:
                reasons['insufficient_cash'] += 1
                continue
            if not self.enter(signal, amount, probability, now=now):
                reasons['prefunded_wallet_unavailable'] += 1
            elif evaluations_recorded:
                try:
                    self.store.mark_candidate_selected(session_date, symbol, entry_minutes)
                except Exception:
                    log.exception('FGV candidate selection telemetry update failed; trading continues')
        self.scan_diagnostics = {'status': 'scanned', 'snapshot_age_seconds': round((now - asof).total_seconds(), 1), **reasons}

    def enter(self, signal, amount, probability, now=None):
        now = now or self.clock.now_exchange()
        desired_amount, allocation_multiplier = self.confidence_allocation(probability)
        busy = self.store.busy_wallets()
        busy.update(p['wallet_address'] for p in self.store.db.get_all_positions())
        busy.update(o['wallet_address'] for o in self.store.db.get_pending_orders())
        idle = [w for w in self.store.db.get_active_wallets(self.config.blockchain)
                if w['address'] not in busy and self.balances.get(w['address'], 0) >= self.risk.min_order_usdc
                and (not self.prefund_enabled or w['assigned_stock'] == READY_SYMBOL)]
        # Prefer the smallest wallet that fully covers the allocation. If none does,
        # use the richest remaining wallet and cap the allocation to its balance.
        idle.sort(key=lambda w: (self.balances.get(w['address'], 0) < desired_amount,
                                self.balances.get(w['address'], 0) if self.balances.get(w['address'], 0) >= desired_amount
                                else -self.balances.get(w['address'], 0)))
        wallet = idle[0]['address'] if idle else None
        wallet_cash = self.balances.get(wallet, 0)
        reused = wallet is not None
        if self.prefund_enabled and not reused:
            log.warning('FGV qualified %s but no prefunded wallet is ready; selection deferred', signal.symbol)
            return False
        if reused:
            amount = (min(desired_amount, wallet_cash)
                      if self.config.fgv.get('confidence_sizing', {}).get('enabled', False) else wallet_cash)
        else:
            amount = desired_amount
        amount = int(amount * 1e6) / 1e6
        if amount < self.risk.min_order_usdc:
            log.warning('FGV %s confidence allocation %.6f is below the minimum order', signal.symbol, amount)
            return False
        shortfall = 0 if reused else amount
        if not reused and shortfall + self.risk.reserve_usdc > self.balances.get(self.broker.vault, 0):
            log.warning('FGV new wallet needs %.6f USDC plus vault reserve', amount)
            return
        if wallet is None:
            wallet = self.broker.create_wallet(signal.symbol)
        if self.prefund_enabled:
            self._consume_prefunded_wallet(wallet)
        self.store.db.update_wallet_stock(wallet, signal.symbol)
        payload = asdict(signal)
        for key in ('c1_time', 'c2_time', 'c3_time'):
            payload[key] = payload[key].isoformat()
        session = self.clock.session_for(now.date())
        stop_policy = build_policy(signal, self.bars.get(signal.symbol, []), now, self.stop_settings)
        entry_buffer_r = self.config.execution.get('max_entry_above_trigger_r', 0)
        guard = dict(expires_at=min(now.timestamp() + self.config.execution.get('entry_max_age_seconds', 60),
                                    session.scan_end.timestamp()), signal=payload,
                     risk_stop=stop_policy['emergency_stop'],
                     reward_risk_stop=signal.stop_loss,
                     max_entry_price=signal.trigger_low + entry_buffer_r * signal.risk,
                     max_entry_above_trigger_r=entry_buffer_r,
                     allow_entry_below_original_stop=self.config.execution.get(
                         'allow_entry_below_original_stop', False),
                     allow_entry_at_or_below_risk_stop=self.config.execution.get(
                         'allow_entry_at_or_below_risk_stop', False),
                     min_reward_risk=self.config.execution.get('min_entry_reward_risk', 1.5))
        state = 'BUY_PENDING' if self.prefund_enabled else 'FUNDING'
        trade = dict(id=uuid4().hex, symbol=signal.symbol, session=signal.session_date,
                     wallet=wallet, amount=amount, state=state, signal=payload,
                     probability=probability, cost=0, quantity=0, realized_pnl=0,
                     allocation_multiplier=allocation_multiplier, desired_amount=desired_amount,
                     reused_wallet=reused,
                     stop_policy=stop_policy,
                     entry_guard=guard, decision_at=now.isoformat(),
                     decision_price=self.market.get_latest_prices([signal.symbol]).get(signal.symbol),
                     data_provider=getattr(self.market, 'providers', {}).get((signal.symbol, signal.session_date), 'backpack'),
                     model_version=self.model.to_dict()['model_version'])
        self.store.save_trade(trade)
        if self.prefund_enabled:
            self.store.new_operation(trade, 'buy', amount, expiry_days=self.config.order_expiry_days,
                entry_guard=guard, decision_at=trade.get('decision_at'), decision_price=trade.get('decision_price'),
                spend_exact=self.config.fgv.get('confidence_sizing', {}).get('enabled', False))
        self.balances[self.broker.vault] = self.balances.get(self.broker.vault, 0) - shortfall
        self.balances[wallet] = max(0, wallet_cash - amount)
        log.info('FGV selected %s p=%.4f multiplier=%.4f desired=%.6f amount=%.6f wallet=%s',
                 signal.symbol, probability, allocation_multiplier, desired_amount, amount, wallet)
        return True

    def advance(self, now):
        session = self.clock.session_for(now.date())
        symbols = [t['symbol'] for t in self.store.trades(active=True)]
        observations = self.observations(symbols, now)
        self.decision_observations = {s: observations.get(s) for s in symbols}
        prices = {s: q[0] for s, q in observations.items()}
        for trade in self.store.trades(active=True):
            trade['stop_exits_enabled'] = self.stop_settings['exits_enabled']
            ops = self.store.operations(trade['id'])
            delivered_buy = next((o for o in ops if o['kind'] == 'buy' and o.get('delivered_units')), None)
            if delivered_buy:
                self.sync_delivered_position(trade, ops, delivered_buy)
            if trade['state'] in ('FUNDING', 'BUY_PENDING') and not trade.get('entry_guard'):
                # Never give old unsigned entries a fresh deadline on restart.
                session_for_trade = self.clock.session_for(datetime.fromisoformat(trade['session']).date())
                created = min((datetime.fromisoformat(o['created_at']).timestamp() for o in ops),
                              default=datetime.fromisoformat(trade['signal']['c3_time']).timestamp())
                trade['entry_guard'] = dict(expires_at=min(created + self.config.execution.get('entry_max_age_seconds', 60),
                    session_for_trade.scan_end.timestamp()), signal=trade['signal'],
                    min_reward_risk=self.config.execution.get('min_entry_reward_risk', 1.5))
                self.store.save_trade(trade)
            if trade['state'] in ('FUNDING', 'BUY_PENDING'):
                for op in ops:
                    if op['state'] == 'PREPARED' and op['id'] not in self.jobs and not op.get('entry_guard'):
                        op['entry_guard'] = trade['entry_guard']
                        self.store.save_operation(op)
            liquidating = self.config.liquid_mode or self.store.liquidating()
            expired = rejection(trade, now=now.timestamp()) == 'ENTRY_EXPIRED'
            if (liquidating or expired) and trade['state'] in ('FUNDING', 'BUY_PENDING'):
                for op in ops:
                    if op['state'] == 'PREPARED' and op['id'] not in self.jobs:
                        self.store.finish_operation(op, dict(state='FAILED', error='ENTRY_EXPIRED' if expired else 'Cancelled before submission for liquidation'))
                if not delivered_buy and all(op['state'] in ('SETTLED', 'FAILED') for op in ops) and not any(
                        op['kind'] == 'buy' and op.get('quantity', 0) > 0 for op in ops):
                    trade['state'] = 'FAILED'
                    self.store.save_trade(trade)
                    continue
            if trade['state'] == 'FUNDING':
                if liquidating or expired:
                    continue  # Only reconcile already signed funding; never initiate a buy.
                for kind, amount in (('fund', trade['amount']), ('gas', self.config.gas_per_wallet)):
                    if not any(o['kind'] == kind for o in ops):
                        if kind == 'fund' and trade.get('reused_wallet'):
                            amount = 0  # No USDC top-ups, even if the balance changes.
                        self.store.new_operation(trade, kind, amount, entry_guard=trade['entry_guard'])
                funding = [o for o in ops if o['kind'] in ('fund', 'gas')]
                if any(o['state'] == 'FAILED' for o in funding):
                    # Other funding may still be in flight; don't release the wallet.
                    if all(o['state'] in ('SETTLED', 'FAILED') for o in funding) and len(funding) == 2:
                        trade['state'] = 'FAILED'
                elif len(funding) == 2 and all(o['state'] == 'SETTLED' for o in funding):
                    if not any(o['kind'] == 'buy' for o in ops):
                        self.store.new_operation(trade, 'buy', trade['amount'], expiry_days=self.config.order_expiry_days,
                            entry_guard=trade['entry_guard'], decision_at=trade.get('decision_at'),
                            decision_price=trade.get('decision_price'),
                            spend_exact=self.config.fgv.get('confidence_sizing', {}).get('enabled', False))
                    trade['state'] = 'BUY_PENDING'
            elif trade['state'] == 'BUY_PENDING':
                buy = next(o for o in ops if o['kind'] == 'buy')
                if not delivered_buy and buy['state'] in ('SETTLED', 'FAILED'):
                    self.store.finish_operation(buy, {})
                    trade.update(quantity=buy.get('quantity', 0), cost=buy.get('cost', 0))
                    trade['state'] = 'OPEN' if trade['quantity'] else 'FAILED'
                    if trade['state'] == 'OPEN':
                        trade.setdefault('opened_at', buy.get('settlement_at') or
                                         buy.get('recognized_at') or now.isoformat())
            elif trade['state'] == 'OPEN':
                price = prices.get(trade['symbol'])
                reason = trade.get('exit_reason')
                if not reason:
                    if self.stop_settings['exits_enabled']:
                        stop = stop_decision(trade, price, observations.get(trade['symbol'], (None, None))[1], now)
                    else:
                        stop = None
                        trade.pop('stop_confirmation', None)
                    if liquidating:
                        reason = 'FORCE_EXIT_15M_BEFORE_CLOSE'
                    elif stop:
                        reason = 'STOP_LOSS'
                        trade['stop_exit_detail'] = dict(subtype=stop, price=price, observed_at=now.isoformat(),
                                                        quote_at=observations.get(trade['symbol'], (None, None))[1])
                    elif price is not None and price >= trade['signal']['take_profit']:
                        reason = 'TAKE_PROFIT'
                    else:
                        reason = self.time_exit_reason(trade, ops, price, now)
                if reason:
                    # An operation persisted before a crash must be reused.
                    pending = [o for o in ops if o['kind'] == 'sell' and o['state'] not in ('SETTLED', 'FAILED')]
                    sell = pending[-1] if pending else self.store.new_operation(
                        trade, 'sell', trade['quantity'], expiry_days=self.config.order_expiry_days, reason=reason)
                    if not any(o.get('for_order') == sell['id'] for o in ops):
                        self.store.new_operation(trade, 'gas', self.config.gas_per_wallet, for_order=sell['id'])
                    trade.update(state='SELL_PENDING', exit_order=sell['id'], exit_reason=reason)
            elif trade['state'] == 'SELL_PENDING':
                sell = next(o for o in ops if o['id'] == trade['exit_order'])
                if sell['state'] in ('SETTLED', 'FAILED'):
                    self.store.finish_operation(sell, {})
                    if delivered_buy:
                        self.sync_delivered_position(trade, ops, delivered_buy)
                        trade['state'] = ('OPEN' if trade['quantity'] > 0 else
                                          'BUY_PENDING' if trade['buy_reconciling'] else 'CLOSED')
                        self.store.save_trade(trade)
                        continue
                    sold = sell.get('quantity', 0)
                    fraction = min(1, sold / trade['quantity']) if trade['quantity'] else 0
                    realized_cost = trade['cost'] * fraction
                    trade['realized_pnl'] += sell.get('proceeds', 0) - realized_cost
                    trade['cost'] -= realized_cost
                    trade['quantity'] = max(0, trade['quantity'] - sold)
                    trade['state'] = 'OPEN' if trade['quantity'] > 1e-12 else 'CLOSED'
            self.store.save_trade(trade)

    @staticmethod
    def weekday_sessions_after(start, end):
        """Count weekday sessions after start through end; source scheduling has no holiday calendar."""
        sessions = 0
        cursor = start + timedelta(days=1)
        while cursor <= end:
            sessions += cursor.weekday() < 5
            cursor += timedelta(days=1)
        return sessions

    def time_exit_reason(self, trade, ops, price, now):
        """Carry only normal losing cutoff exits, then close recovery or max-age positions."""
        session = self.clock.session_for(now.date())
        if now.weekday() >= 5 or now < session.force_exit:
            return None
        maximum = int(self.config.execution.get('losing_time_exit_max_sessions', 0))
        if maximum <= 0:
            return 'FORCE_EXIT_15M_BEFORE_CLOSE'
        entry_date = datetime.fromisoformat(trade['session']).date()
        buy = next((op for op in ops if op['kind'] == 'buy'), None)
        opened_at = trade.get('opened_at') or (buy or {}).get('settlement_at') or (buy or {}).get('recognized_at')
        try:
            opened = self.clock.to_exchange(datetime.fromisoformat(opened_at))
        except (TypeError, ValueError):
            opened = None
        entry_cutoff = self.clock.session_for(entry_date).force_exit
        normal_entry = bool(buy and buy['state'] == 'SETTLED' and opened
                            and opened.date() == entry_date and opened <= entry_cutoff)
        carry = trade.get('time_exit_carry')
        losing = bool(price is not None and trade.get('quantity', 0) > 0
                      and price * trade['quantity'] < trade.get('cost', 0))
        elapsed = self.weekday_sessions_after(entry_date, now.date())
        if now.date() == entry_date:
            if normal_entry and losing:
                trade['time_exit_carry'] = dict(
                    started_at=now.isoformat(), max_additional_sessions=maximum)
                return None
            return 'FORCE_EXIT_15M_BEFORE_CLOSE'
        if not carry:
            return 'FORCE_EXIT_15M_BEFORE_CLOSE'
        if elapsed >= maximum:
            return f'FORCE_EXIT_MAX_HOLD_{maximum}_SESSIONS'
        if price is not None and not losing:
            return 'CARRIED_POSITION_RECOVERY'
        return None

    def observations(self, symbols, now):
        if hasattr(self.market, 'get_latest_observations'):
            return self.market.get_latest_observations(symbols)
        # Simulation adapters without source timestamps observe at the supplied clock.
        return {s: (p, now.timestamp()) for s, p in self.market.get_latest_prices(symbols).items()}

    def record_stop_observations(self, now):
        if not self.stop_settings['record_observations']:
            return
        session = self.clock.session_for(now.date())
        # One grace minute records the first available cutoff sample (not an exact close).
        if not session.market_open <= now <= session.force_exit + timedelta(minutes=1):
            return
        trades = [t for t in self.store.trades() if t['session'] == now.date().isoformat()
                  and not t.get('maintenance') and t['state'] != 'FAILED']
        observations = self.observations([t['symbol'] for t in trades], now)
        # For active positions, record the exact snapshot used by advance(), not
        # a newer websocket message arriving during DB writes. Missing is a gap.
        for symbol, quote in self.decision_observations.items():
            if quote is None:
                observations.pop(symbol, None)
            else:
                observations[symbol] = quote
        self.store.record_stop_observations(trades, observations, now,
                                          getattr(self.market, 'quote_providers', {}))

    @staticmethod
    def sync_delivered_position(trade, ops, buy):
        """Recompute from cumulative deliveries/sells, including late partial fills."""
        acquired = buy['delivered_units'] / 10 ** buy['delivery_decimals']
        if buy['state'] == 'SETTLED':
            acquired = max(acquired, buy.get('quantity', 0))
        sells = [o for o in ops if o['kind'] == 'sell' and o['state'] == 'SETTLED']
        sold = sum(o.get('quantity', 0) for o in sells)
        proceeds = sum(o.get('proceeds', 0) for o in sells)
        reconciling = buy['state'] != 'SETTLED'
        total_cost = buy['amount'] if reconciling else buy.get('cost', buy['amount'])
        fraction = min(1, sold / acquired) if acquired else 0
        trade.update(quantity=max(0, acquired - sold), cost=total_cost * (1 - fraction),
                     realized_pnl=proceeds - total_cost * fraction,
                     buy_reconciling=reconciling, cost_provisional=reconciling)
        if trade['state'] in ('BUY_PENDING', 'OPEN', 'CLOSED'):
            trade['state'] = ('OPEN' if trade['quantity'] > 0 else
                              'BUY_PENDING' if reconciling else 'CLOSED')

    async def tick(self, now=None, scan=True):
        now = now or self.clock.now_exchange()
        for op_id, (op, task) in list(self.jobs.items()):
            if task.done():
                try:
                    self.store.finish_operation(op, task.result())
                except Exception:
                    log.exception('Operation %s remains reserved; retrying reconciliation', op_id)
                    self.store.finish_operation(op, dict(last_error_at=datetime.now().astimezone().isoformat()))
                del self.jobs[op_id]
        if self.refresh_job and self.refresh_job.done():
            try:
                self.data_session, self.first, self.bars, snapshot, self.data_asof = self.refresh_job.result()
                self.balances = self.apply_funding_reservations(snapshot)
            except Exception:
                log.exception('FGV data refresh failed')
                self.data_session = None
            self.refresh_job = None
        self.advance(now)
        try:
            self.record_stop_observations(now)
        except Exception:
            # Analytics must never prevent execution or reconciliation.
            log.exception('Stop observation recording failed; execution continues')
        self.retire_wallets()
        self.ensure_prefunded_wallets()
        if scan:
            self.scan(now)
        for op in self.store.operations():
            if op['state'] not in ('SETTLED', 'FAILED'):
                age = time.time() - datetime.fromisoformat(op['created_at']).timestamp()
                if age > self.config.execution.get('stuck_order_alert_seconds', 60) and time.monotonic() - self.last_order_alert.get(op['id'], -1e10) > 60:
                    log.warning('FGV STUCK OPERATION %s symbol=%s kind=%s state=%s age=%.0fs',
                                op['id'], op['symbol'], op['kind'], op['state'], age)
                    self.last_order_alert[op['id']] = time.monotonic()
            if op['state'] not in ('SETTLED', 'FAILED') and op['id'] not in self.jobs:
                if op['kind'] == 'sell':
                    gas = [o for o in self.store.operations(op['trade_id']) if o.get('for_order') == op['id']]
                    if gas and all(o['state'] == 'FAILED' for o in gas):
                        trade = next(t for t in self.store.trades(active=True) if t['id'] == op['trade_id'])
                        self.store.new_operation(trade, 'gas', self.config.gas_per_wallet, for_order=op['id'])
                        continue
                    if any(o['state'] != 'SETTLED' for o in gas):
                        if not any(o['state'] == 'SETTLED' for o in gas):
                            continue
                self.jobs[op['id']] = (op, asyncio.create_task(asyncio.to_thread(self.broker.step, op)))
        session = self.clock.session_for(now.date())
        if (scan and session.first_15m_complete <= now < session.scan_end and self.refresh_job is None):
            self.refresh_job = asyncio.create_task(asyncio.to_thread(self.refresh, now))

    def retire_wallets(self):
        self.store.record_closed_losses(self.config.max_loss_traders)
        active = {t['wallet'] for t in self.store.trades(active=True)}
        active.update(p['wallet_address'] for p in self.store.db.get_all_positions())
        active.update(o['wallet_address'] for o in self.store.db.get_pending_orders())
        for wallet in self.store.db.get_wallets_by_status(self.config.blockchain, 'retiring'):
            address = wallet['address']
            if address in active:
                continue
            trade = self.store.retirement_trade(wallet)
            if any(o['wallet'] == address and o['trade_id'] != trade['id']
                   and o['state'] not in ('SETTLED', 'FAILED') for o in self.store.operations()):
                continue
            ops = self.store.operations(trade['id'])
            # Confirm USDC recovery before collecting the gas needed to send it.
            for kind, amount in (('gas', self.config.gas_per_wallet), ('sweep', 0), ('collect', 0)):
                op = next((o for o in ops if o['kind'] == kind), None)
                if op is None:
                    self.store.new_operation(trade, kind, amount, retirement=True, sweep_all=kind in ('sweep', 'collect'))
                    break
                if op['state'] != 'SETTLED':
                    # Signed work is reconciled normally; failed cleanup stays
                    # quarantined for inspection, never silently reused.
                    break
            else:
                self.store.db.update_wallet_status(address, 'abandoned')
                log.info('FGV wallet retired after %s losses; cleanup settled: %s', wallet['loss_count'], address)

    async def drain(self):
        if self.refresh_job:
            await asyncio.gather(self.refresh_job, return_exceptions=True)
        if self.jobs:
            await asyncio.gather(*(task for _, task in self.jobs.values()), return_exceptions=True)
        # Persist completed work without creating new operations during shutdown.
        for op, task in self.jobs.values():
            if not task.cancelled() and task.exception() is None:
                self.store.finish_operation(op, task.result())


async def run_cli(args, config):
    if args.dry_run:
        config.dry_run = True
    if args.check_config or args.status or args.wallets:
        return await _run_cli(args, config)
    path = config.paper_database_path if config.dry_run else config.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # One writer per DB, including maintenance commands; OS releases on crash.
    with open(str(path) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another FGV process is using this database. Stop it before running maintenance.')
            return 2
        return await _run_cli(args, config)


async def _run_cli(args, config):
    logging.basicConfig(level=args.log_level, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    if args.dry_run:
        config.dry_run = True
    errors = config.validate()
    if errors:
        for error in errors:
            log.error(error)
        return 2
    if args.check_config:
        print('FGV configuration valid; Backpack data, MARKET orders, ' + ('paper mode' if config.dry_run else 'live mode'))
        return 0
    db = Database(str(config.paper_database_path if config.dry_run else config.database_path),
                  PAPER_KEY if config.dry_run else config.database_encryption_key)
    store = Store(db, config.blockchain)
    if args.resume_entries:
        if config.liquid_mode:
            log.error('Set liquid_mode: false before resuming entries')
            return 2
        try:
            store.resume_entries()
        except ValueError as exc:
            log.error('%s', exc)
            return 2
        print('Liquidation flag cleared. The next bot run may accept FGV signals.')
        return 0
    if args.status or args.wallets or args.abandoned_only:
        status_wallets = db.get_wallets_by_status(config.blockchain, 'abandoned') if args.abandoned_only else db.get_active_wallets(config.blockchain)
        if args.show_abandoned and not args.abandoned_only:
            status_wallets += db.get_wallets_by_status(config.blockchain, 'abandoned')
        print(json.dumps({'mode': 'paper' if config.dry_run else 'live', 'liquidating': store.liquidating(),
                          'wallets': [{k: v for k, v in w.items() if 'private_key' not in k}
                                      for w in status_wallets],
                          'legacy_positions': store.legacy_positions(),
                          'trades': store.trades(), 'operations': [
                              {k: v for k, v in o.items() if k != 'raw_tx'} for o in store.operations()]}, indent=2))
        return 0
    from sharesdao_client import create_sharesdao_client
    api = await asyncio.to_thread(create_sharesdao_client, config)
    config.set_trading_stocks(api.stock_pools)
    # Keep metadata for existing holdings even if they were removed from the allowlist.
    entry_symbols = set(config.trading_stocks)
    existing_symbols = {t['symbol'] for t in store.trades(active=True)}
    existing_symbols.update(p['stock_ticker'] for p in store.legacy_positions())
    existing_symbols.update(o['stock_ticker'] for o in db.get_pending_orders())
    for symbol in existing_symbols:
        if symbol not in config.trading_stocks:
            if symbol not in api.stock_pools:
                log.error('Missing pool metadata for existing holding %s', symbol)
                return 2
            config.trading_stocks[symbol] = api.stock_pools[symbol]
    if not config.trading_stocks:
        log.error('No mint mode 3 pools match the stock universe, and no existing holdings need monitoring')
        return 2
    if not entry_symbols:
        log.warning('No eligible entry pools; continuing settlement and existing-position monitoring only')
    config.mint_address = config.get_mint_address()
    config.burn_address = config.get_burn_address()
    market = BackpackMarketData(config.market_data)
    if config.dry_run:
        broker = PaperBroker(store, config, market)
    else:
        from blockchain_client import create_blockchain_client
        blockchain = await asyncio.to_thread(create_blockchain_client, config)
        broker = LiveBroker(store, blockchain, config, api)
    engine = Engine(config, store, broker, market)
    engine.symbols = sorted(entry_symbols)
    engine.balances = engine.apply_funding_reservations(await asyncio.to_thread(broker.snapshot))
    if args.liquidate:
        store.set_liquidating()
    if args.sweep or args.collect_eth or args.delete_unfunded:
        active = store.busy_wallets()
        active.update(o['wallet_address'] for o in db.get_pending_orders())
        active.update(p['wallet_address'] for p in db.get_all_positions())
        wallets = (db.get_wallets_by_status(config.blockchain, 'pending_funding') if args.delete_unfunded else
                   db.get_active_wallets(config.blockchain))
        if args.collect_eth:
            wallets += db.get_wallets_by_status(config.blockchain, 'abandoned')
        for wallet in wallets:
            address = wallet['address']
            if address in active:
                continue
            amount = await asyncio.to_thread(broker.balance, address)
            if args.delete_unfunded:
                native = await asyncio.to_thread(broker.balance, address, 'NATIVE')
                if amount == 0 and native == 0:
                    stock = (0 if wallet['assigned_stock'] == READY_SYMBOL else
                             await asyncio.to_thread(broker.balance, address, wallet['assigned_stock']))
                    if stock == 0:
                        db.delete_wallet(address)
                        log.info('Deleted unfunded wallet %s (no balances or pending operations)', address)
                continue
            kind = 'collect' if args.collect_eth else 'sweep'
            if args.collect_eth:
                if amount > args.min_usdc_threshold:
                    continue
                amount = await asyncio.to_thread(broker.balance, address, 'NATIVE')
            minimum = .0001 if args.collect_eth else .01
            if amount >= minimum and not any(o['wallet'] == address and o['state'] not in ('SETTLED', 'FAILED')
                                            for o in store.operations()):
                trade = store.maintenance_trade(wallet)
                store.new_operation(trade, kind, amount)
        await engine.tick(scan=False)
        await engine.drain()
        print('Maintenance complete or journaled. Run the bot to reconcile pending transfers.')
        return 0
    legacy = store.legacy_positions()
    legacy_pending = [o for o in db.get_pending_orders() if not o['order_id'].startswith('FGV_')]
    if legacy or legacy_pending:
        log.warning('Legacy positions/orders detected; managed separately without new DCA entries')
    legacy_manager = None
    if not config.dry_run and (legacy or legacy_pending):
        from wallet_manager import create_wallet_manager
        from stock_selector import create_stock_selector
        from trade_manager import create_trade_manager
        wallets = create_wallet_manager(db, blockchain, create_stock_selector(config), config)
        legacy_manager = create_trade_manager(db, blockchain, api, wallets, config)
        if args.liquidate or config.liquid_mode or store.liquidating():
            config.liquid_mode = True
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (os_signal.SIGINT, os_signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    stream = asyncio.create_task(market.stream(sorted(set(config.trading_stocks) | {'SPY'})))
    fallback = asyncio.create_task(market.fallback_quotes(sorted(config.trading_stocks)))
    legacy_job = None
    last_status = 0
    try:
        if args.once:
            # One cycle waits briefly for the first stream snapshots, then persists
            # any resulting work; subsequent invocations resume the journal.
            await asyncio.sleep(min(5, config.market_data['price_poll_interval_seconds']))
            now = engine.clock.now_exchange()
            session = engine.clock.session_for(now.date())
            if session.first_15m_complete <= now < session.scan_end:
                engine.data_session, engine.first, engine.bars, engine.balances, engine.data_asof = await asyncio.to_thread(engine.refresh, now)
            await engine.tick(scan=not args.liquidate)
            if legacy_manager:
                await asyncio.to_thread(legacy_manager.check_order_confirmations)
                await asyncio.to_thread(legacy_manager.monitor_positions)
        else:
            while not stop.is_set():
                try:
                    await engine.tick(scan=not args.liquidate)
                    if time.monotonic() - last_status >= 60:
                        log.info('FGV active=%s feed=%s scan=%s', len(store.trades(active=True)), market.health(), engine.scan_diagnostics)
                        last_status = time.monotonic()
                    if legacy_manager and (legacy_job is None or legacy_job.done()):
                        if legacy_job and legacy_job.exception():
                            log.error('Legacy reconciliation failed: %s', legacy_job.exception())
                        def reconcile_legacy():
                            legacy_manager.check_order_confirmations()
                            legacy_manager.monitor_positions()
                        legacy_job = asyncio.create_task(asyncio.to_thread(reconcile_legacy))
                    if (args.liquidate and not store.trades(active=True) and not db.get_all_positions()
                            and not db.get_pending_orders() and not store.busy_wallets()):
                        log.info('Liquidation settled. Idle USDC can now be swept to the vault.')
                        break
                except Exception:
                    log.exception('FGV cycle failed; persisted reservations retained')
                try:
                    await asyncio.wait_for(stop.wait(), timeout=min(
                        config.market_data['price_poll_interval_seconds'], config.execution['reconcile_interval_seconds']))
                except asyncio.TimeoutError:
                    pass
    finally:
        stream.cancel()
        fallback.cancel()
        await asyncio.gather(stream, fallback, return_exceptions=True)
        await engine.drain()
        if legacy_job:
            await asyncio.gather(legacy_job, return_exceptions=True)
    return 0
