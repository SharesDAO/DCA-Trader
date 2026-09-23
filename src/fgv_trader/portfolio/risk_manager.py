from __future__ import annotations


class RiskManager:
    def __init__(
        self,
        allocation_pct_per_trade: float,
        risk_pct_per_trade: float,
        max_concurrent_positions: int,
        min_order_usdc: float,
        reserve_usdc: float,
    ):
        self.allocation_pct_per_trade = allocation_pct_per_trade
        self.risk_pct_per_trade = risk_pct_per_trade
        self.max_concurrent_positions = max_concurrent_positions
        self.min_order_usdc = min_order_usdc
        self.reserve_usdc = reserve_usdc

    def can_enter(self, open_positions: int) -> bool:
        return open_positions < self.max_concurrent_positions

    def trade_amount(self, total_usdc: float, price_risk_fraction: float) -> float:
        spendable = max(0.0, total_usdc - self.reserve_usdc)
        allocation_cap = spendable * self.allocation_pct_per_trade
        if price_risk_fraction <= 0:
            return 0.0
        risk_cap = spendable * self.risk_pct_per_trade / price_risk_fraction
        amount = min(allocation_cap, risk_cap)
        return amount if amount >= self.min_order_usdc else 0.0
