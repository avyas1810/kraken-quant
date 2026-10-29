"""Offline portfolio-target sizing into the existing risk interface. No submission."""
from .data import utc_instant
from .risk import OrderIntent, money
from .trade_intent import PortfolioTarget


def size_target(target, *, now, supported_instruments, equity, current_quantity,
                market_price, order_id, limit_price, fee_rate="0"):
    """Use caller-supplied current account/market observations to rebalance.

    Existing execution/risk layers must still validate snapshot freshness, precision,
    minimums, gross exposure, available funds and reservations before any routing.
    """
    if not isinstance(target, PortfolioTarget):
        raise TypeError("Expected PortfolioTarget")
    weight = target.weight_at(now=now, supported_instruments=supported_instruments)
    equity, held, price = map(money, (equity, current_quantity, market_price))
    if equity <= 0 or price <= 0:
        raise ValueError("Equity and market price must be positive")
    delta = weight * equity / price - held
    if delta == 0:
        return None
    decision_key = target.decision_key
    if target.normal_exit.policy == "at_time" and utc_instant(now) >= target.normal_exit.exit_at:
        # A normal exit is a distinct idempotent decision from the entry rebalance.
        decision_key = f"{decision_key}:exit:{target.normal_exit.exit_at.isoformat()}"
    return OrderIntent(order_id, decision_key, target.instrument,
                       "buy" if delta > 0 else "sell", abs(delta), limit_price, fee_rate)
