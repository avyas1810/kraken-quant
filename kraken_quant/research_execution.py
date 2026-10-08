"""Signed, funded offline research execution; never an exchange spot broker."""

from dataclasses import dataclass
import math

import pandas as pd

from .config import CONFIG, utc_day
from .data import DAY
from .metrics import performance_metrics
from .strategy import StrategyPipeline, VenueHistory, validate_weights


@dataclass(frozen=True)
class ResearchCosts:
    fee_rate: float = 0.0
    spread_rate: float = 0.0  # full bid/ask spread; half charged each side
    slippage_rate: float = 0.0
    short_borrow_rate: float = 0.0  # per day, not annual
    cash_borrow_rate: float = 0.0

    def __post_init__(self):
        for value in self.__dict__.values():
            if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value < 1:
                raise ValueError("Costs must be finite fractions in [0,1)")
        if self.spread_rate / 2 + self.slippage_rate >= 1:
            raise ValueError("Adverse execution adjustment must stay below 100%")


@dataclass(frozen=True)
class ResearchPolicy:
    max_gross: float = 1.0
    allow_cash_borrowing: bool = False

    def __post_init__(self):
        if isinstance(self.max_gross, bool) or not math.isfinite(self.max_gross) or self.max_gross <= 0:
            raise ValueError("Invalid research gross budget")
        if not isinstance(self.allow_cash_borrowing, bool):
            raise ValueError("Cash borrowing must be explicitly boolean")


@dataclass(frozen=True)
class ResearchFill:
    instrument: object
    decision_time: pd.Timestamp
    timestamp: pd.Timestamp
    quantity: float
    reference_price: float
    price: float
    fee: float
    spread_cost: float
    slippage_cost: float
    data_type: str = "trade"

    @property
    def notional(self):
        return abs(self.quantity) * self.price


@dataclass(frozen=True)
class ResearchResult:
    equity: pd.Series
    cash: pd.Series
    positions: pd.DataFrame
    fills: tuple
    financing: pd.Series

    def metrics(self):
        result = performance_metrics(self.equity, self.fills)
        result["spread_cost"] = math.fsum(fill.spread_cost for fill in self.fills)
        result["financing_cost"] = float(self.financing.sum())
        return result


def run_research(history, pipeline, *, split, start, end, instruments=None,
                 initial_cash=1000.0, costs=None, policy=None, warmup_days=0):
    if not isinstance(history, VenueHistory) or not isinstance(pipeline, StrategyPipeline):
        raise TypeError("Expected VenueHistory and the shared StrategyPipeline")
    period = CONFIG.split(split)
    if period.name == "test":
        raise PermissionError("Test-period access is locked")
    start, end = utc_day(start), utc_day(end)
    if not period.start <= start <= end <= period.end:
        raise PermissionError("Research interval crosses split boundaries")
    keys = history.instruments if instruments is None else tuple(instruments)
    if not keys or len(set(keys)) != len(keys) or set(keys) - set(history.instruments):
        raise ValueError("Select unique available venue instruments")
    if len({key.quote for key in keys}) != 1:
        raise ValueError("Mixed quote currencies require an explicit FX model")
    if isinstance(initial_cash, bool) or not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("Initial cash must be finite and positive")
    costs = ResearchCosts() if costs is None else costs
    policy = ResearchPolicy() if policy is None else policy
    if not isinstance(costs, ResearchCosts) or not isinstance(policy, ResearchPolicy):
        raise TypeError("Expected research cost/policy settings")
    frames = {key: history.window(key, split=split, start=start, end=end,
        decision_time=end + DAY, warmup_days=warmup_days) for key in keys}
    cash, positions, pending = float(initial_cash), dict.fromkeys(keys, 0.0), None
    fills, times = [], [start]
    equities, cash_values, holdings, financing = [cash], [cash], [positions.copy()], [0.0]
    epsilon = initial_cash * 1e-10

    def projected(changes, opens):
        updated_cash, updated = cash, positions.copy()
        for key, quantity in changes.items():
            price = opens[key] * (1 + math.copysign(costs.spread_rate / 2 + costs.slippage_rate, quantity))
            updated_cash -= quantity * price + abs(quantity) * price * costs.fee_rate
            updated[key] += quantity
        equity = updated_cash + math.fsum(updated[key] * opens[key] for key in keys)
        gross = math.fsum(abs(updated[key]) * opens[key] for key in keys)
        return (math.isfinite(equity) and equity > 0
                and (policy.allow_cash_borrowing or updated_cash >= -epsilon)
                and gross <= policy.max_gross * equity + epsilon)

    def execute(changes, opens, timestamp, decision_time):
        nonlocal cash
        # Deterministic ledger order; feasibility was evaluated as one funded batch.
        for key in sorted(changes):
            quantity = changes[key]
            if abs(quantity) * opens[key] <= initial_cash * 1e-12:
                continue
            price = opens[key] * (1 + math.copysign(costs.spread_rate / 2 + costs.slippage_rate, quantity))
            fee = abs(quantity) * price * costs.fee_rate
            cash -= quantity * price + fee
            positions[key] += quantity
            fills.append(ResearchFill(key, decision_time, timestamp, quantity, opens[key], price, fee,
                abs(quantity) * opens[key] * costs.spread_rate / 2,
                abs(quantity) * opens[key] * costs.slippage_rate))

    for day in pd.date_range(start, end, freq="D"):
        opens = {key: float(frames[key].loc[day, "open"]) for key in keys}
        if pending is not None:
            decision_time, desired = pending
            reductions, additions = {}, {}
            for key in keys:
                old, new = positions[key], desired[key]
                kept = math.copysign(min(abs(old), abs(new)), old) if old * new > 0 else 0.0
                reductions[key], additions[key] = kept - old, new - kept
            # Reductions cannot be financed with unrelated new short-sale proceeds.
            # Insufficient funds/collateral stops the run, never invents a fill.
            if not projected(reductions, opens):
                raise ArithmeticError("Research margin/cash breach; reduce risk or fund the account")
            execute(reductions, opens, day, decision_time)
            if projected(additions, opens):
                scale = 1.0
            else:
                low, high = 0.0, 1.0
                for _ in range(64):
                    middle = (low + high) / 2
                    if projected({key: value * middle for key, value in additions.items()}, opens):
                        low = middle
                    else:
                        high = middle
                scale = low
            execute({key: value * scale for key, value in additions.items()}, opens, day, decision_time)
            pending = None
        if not policy.allow_cash_borrowing:
            if cash < -epsilon:
                raise ArithmeticError("Research cash invariant violated")
            cash = max(cash, 0.0)
        closes = {key: float(frames[key].loc[day, "close"]) for key in keys}
        charge = math.fsum(max(-positions[key], 0) * closes[key] * costs.short_borrow_rate for key in keys)
        charge += max(-cash, 0) * costs.cash_borrow_rate
        cash -= charge
        equity = cash + math.fsum(positions[key] * closes[key] for key in keys)
        gross = math.fsum(abs(positions[key]) * closes[key] for key in keys)
        if not math.isfinite(equity) or equity <= 0 or gross > policy.max_gross * equity + epsilon:
            raise ArithmeticError("Research mark-to-market margin breach")
        if not policy.allow_cash_borrowing and cash < -epsilon:
            raise ArithmeticError("Financing charge requires unavailable cash")
        timestamp = day + DAY
        times.append(timestamp)
        equities.append(equity)
        cash_values.append(cash)
        holdings.append(positions.copy())
        financing.append(charge)
        if day < end:
            target = pipeline.evaluate(timestamp=timestamp,
                historical_data={key: frame.loc[:day] for key, frame in frames.items()},
                positions=positions, cash=cash, equity=equity)
            if target is not None:
                weights = validate_weights(target, keys, max_gross=policy.max_gross)
                pending = (timestamp, {key: weights[key] * equity / closes[key] for key in keys})
    index = pd.DatetimeIndex(times, name="time_utc")
    return ResearchResult(pd.Series(equities, index=index, name="equity"),
        pd.Series(cash_values, index=index, name="cash"), pd.DataFrame(holdings, index=index),
        tuple(fills), pd.Series(financing, index=index, name="financing"))


def research_buy_and_hold(history, *, weights=None, instruments=None, **kwargs):
    keys = history.instruments if instruments is None else tuple(instruments)
    target = validate_weights({key: 1 / len(keys) for key in keys} if weights is None else weights,
                              keys, long_only=True)
    class Hold:
        first = True
        def target_weights(self, context):
            if self.first:
                self.first = False
                return target
            return None
    return run_research(history, StrategyPipeline(Hold()), instruments=keys, **kwargs)
