"""Offline, long-only funded simulation. No TWSQ API or credential imports.

Targets use decision-close equity/prices; fixed quantities execute on the next
bar's open. Sells fund buys, and insufficient cash scales all buys proportionally.
See docs/backtest_core.md for timing, accounting and modeling limitations.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import math
from types import MappingProxyType

import pandas as pd

from .config import CONFIG, utc_day
from .data import DAY, RestrictedHistory
from .metrics import performance_metrics


@dataclass(frozen=True)
class ExecutionCosts:
    fee_rate: float = 0.0
    slippage_rate: float = 0.0

    def __post_init__(self):
        for name in ("fee_rate", "slippage_rate"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite fraction in [0, 1)")
            if not math.isfinite(value) or not 0 <= value < 1:
                raise ValueError(f"{name} must be a finite fraction in [0, 1)")


@dataclass(frozen=True)
class DecisionContext:
    time: pd.Timestamp
    cash: float
    equity: float
    positions: Mapping
    bars: Mapping


@dataclass(frozen=True)
class Fill:
    decision_time: pd.Timestamp
    execution_time: pd.Timestamp
    symbol: str
    quantity: float  # signed: positive buy, negative sell
    reference_price: float
    price: float
    fee: float
    slippage_cost: float

    @property
    def notional(self):
        return abs(self.quantity) * self.price


@dataclass(frozen=True)
class BacktestResult:
    equity: pd.Series
    cash: pd.Series
    positions: pd.DataFrame
    fills: tuple

    def metrics(self, **kwargs):
        return performance_metrics(self.equity, self.fills, **kwargs)


def _weights(target, symbols):
    if not isinstance(target, Mapping) or set(target) - set(symbols):
        raise ValueError("Targets must map selected symbols to weights")
    result = {}
    for symbol in symbols:
        value = target.get(symbol, 0.0)
        if isinstance(value, bool):
            raise ValueError("Weights must be finite nonnegative fractions")
        try:
            value = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Weights must be finite nonnegative fractions") from error
        if not math.isfinite(value) or value < 0:
            raise ValueError("Weights must be finite nonnegative fractions")
        result[symbol] = value
    if math.fsum(result.values()) > 1.0 + 1e-12:
        raise ValueError("Weights cannot exceed 100%; leverage is disabled")
    return result


def run_backtest(history, strategy, *, symbols, split, start, end,
                 initial_cash=1000.0, costs=None, warmup_days=0):
    """Call strategy(context) at each scored close except the final close.

    Return a weight mapping to rebalance, or None to hold existing quantities.
    Missing symbols in a mapping target zero. Warm-up bars are visible for causal
    features but excluded from execution/scoring. Final holdings are marked, not
    liquidated. Symbols must share a quote currency; no FX conversion is modeled.
    """
    if not isinstance(history, RestrictedHistory):
        raise TypeError("Backtests require the locked RestrictedHistory interface")
    period = CONFIG.split(split)
    if period.name == "test":
        raise PermissionError("Test-period access is locked")
    start, end = utc_day(start), utc_day(end)
    if not period.start <= start <= end <= period.end:
        raise PermissionError("Backtest dates cross split boundaries")
    symbols = tuple(symbols)
    if not symbols or len(set(symbols)) != len(symbols) or set(symbols) - set(CONFIG.universe):
        raise ValueError("Select unique symbols from the locked universe")
    if isinstance(initial_cash, bool):
        raise ValueError("Initial cash must be positive and finite")
    initial_cash = float(initial_cash)
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("Initial cash must be positive and finite")
    costs = ExecutionCosts() if costs is None else costs
    if not isinstance(costs, ExecutionCosts):
        raise TypeError("costs must be ExecutionCosts")
    # Only the engine sees later execution/marking bars. Callbacks receive copies
    # ending at their completed decision bar, never this prefetched full history.
    frames = {symbol: history.window(symbol, split=split, start=start, end=end,
              decision_time=end + DAY, warmup_days=warmup_days) for symbol in symbols}
    cash = initial_cash
    positions = dict.fromkeys(symbols, 0.0)
    times, equities, cash_values, holdings = [start], [cash], [cash], [positions.copy()]
    fills = []
    pending = None
    tolerance = initial_cash * 1e-12

    def execute(symbol, quantity, day, decision_time):
        nonlocal cash
        reference = float(frames[symbol].loc[day, "open"])
        direction = 1 if quantity > 0 else -1
        price = reference * (1 + direction * costs.slippage_rate)
        notional = abs(quantity) * price
        fee = notional * costs.fee_rate
        cash -= quantity * price + fee
        positions[symbol] += quantity
        fills.append(Fill(decision_time, day, symbol, quantity, reference, price,
                          fee, abs(quantity) * abs(price - reference)))

    for day in pd.date_range(start, end, freq="D"):
        if pending is not None:
            decision_time, desired = pending
            changes = {symbol: desired[symbol] - positions[symbol] for symbol in symbols}
            for symbol in symbols:
                quantity = changes[symbol]
                if quantity < 0 and abs(quantity) * float(frames[symbol].loc[day, "open"]) > tolerance:
                    execute(symbol, max(quantity, -positions[symbol]), day, decision_time)
            required = math.fsum(
                max(changes[symbol], 0) * float(frames[symbol].loc[day, "open"])
                * (1 + costs.slippage_rate) * (1 + costs.fee_rate) for symbol in symbols
            )
            scale = min(1.0, max(cash, 0.0) / required) if required else 1.0
            for symbol in symbols:
                quantity = max(changes[symbol], 0) * scale
                if quantity * float(frames[symbol].loc[day, "open"]) > tolerance:
                    execute(symbol, quantity, day, decision_time)
            pending = None
        if cash < -tolerance or any(quantity < -tolerance for quantity in positions.values()):
            raise ArithmeticError("Funded portfolio invariant violated")
        cash = max(cash, 0.0)  # only sub-tolerance floating-point residue
        close_time = day + DAY
        prices = {symbol: float(frames[symbol].loc[day, "close"]) for symbol in symbols}
        equity = cash + math.fsum(positions[symbol] * prices[symbol] for symbol in symbols)
        if not math.isfinite(equity) or equity <= 0:
            raise ArithmeticError("Portfolio equity must stay positive and finite")
        times.append(close_time)
        equities.append(equity)
        cash_values.append(cash)
        holdings.append(positions.copy())
        if day < end:
            context = DecisionContext(close_time, cash, equity,
                MappingProxyType(positions.copy()),
                MappingProxyType({symbol: frames[symbol].loc[:day].copy(deep=True) for symbol in symbols}))
            target = strategy(context)
            if target is not None:
                weights = _weights(target, symbols)
                desired = {symbol: weights[symbol] * equity / prices[symbol] for symbol in symbols}
                pending = (close_time, desired)

    index = pd.DatetimeIndex(times, name="time_utc")
    return BacktestResult(pd.Series(equities, index=index, name="equity"),
                          pd.Series(cash_values, index=index, name="cash"),
                          pd.DataFrame(holdings, index=index), tuple(fills))


def buy_and_hold(history, *, symbols, weights=None, **kwargs):
    """Buy once after the first scored close using identical timing and costs."""
    symbols = tuple(symbols)
    target = {symbol: 1 / len(symbols) for symbol in symbols} if symbols and weights is None else weights
    target = _weights(target, symbols)
    first = True

    def strategy(context):
        nonlocal first
        if first:
            first = False
            return target
        return None

    return run_backtest(history, strategy, symbols=symbols, **kwargs)
