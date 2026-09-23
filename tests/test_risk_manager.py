from fgv_trader.portfolio import RiskManager


def test_entry_limit_depends_only_on_concurrent_positions():
    risk = RiskManager(
        allocation_pct_per_trade=0.5,
        risk_pct_per_trade=0.01,
        max_concurrent_positions=2,
        min_order_usdc=5,
        reserve_usdc=5,
    )

    assert risk.can_enter(0)
    assert risk.can_enter(1)
    assert not risk.can_enter(2)


def test_trade_amount_is_limited_by_stop_risk():
    risk = RiskManager(
        allocation_pct_per_trade=0.5,
        risk_pct_per_trade=0.01,
        max_concurrent_positions=2,
        min_order_usdc=5,
        reserve_usdc=0,
    )

    assert risk.trade_amount(10_000, price_risk_fraction=0.01) == 5_000
    assert risk.trade_amount(10_000, price_risk_fraction=0.05) == 2_000
