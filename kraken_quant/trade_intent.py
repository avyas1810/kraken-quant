"""Strategy-neutral desired trades; these objects never submit orders."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from .data import utc_instant
from .risk import OrderIntent, money
from .strategy import Instrument, StrategyContext


@dataclass(frozen=True)
class TradeIntent:
    decision_key: str
    instrument: Instrument
    timestamp: object
    side: str
    quantity: Decimal

    def __post_init__(self):
        if not isinstance(self.decision_key, str) or not self.decision_key.strip():
            raise ValueError("Decision identity required")
        if not isinstance(self.instrument, Instrument):
            raise TypeError("Expected Instrument")
        if self.side not in ("buy", "sell"):
            raise ValueError("Expected buy or sell")
        object.__setattr__(self, "timestamp", utc_instant(self.timestamp))
        object.__setattr__(self, "quantity", money(self.quantity))
        if self.quantity <= 0:
            raise ValueError("Quantity must be positive")

    def priced(self, *, order_id, limit_price, fee_rate=Decimal("0")):
        """Translate into the existing risk contract; caller must validate and reserve."""
        return OrderIntent(order_id, self.decision_key, self.instrument,
                           self.side, self.quantity, limit_price, fee_rate)


class IntentStrategy(Protocol):
    def trade_intents(self, context: StrategyContext) -> tuple[TradeIntent, ...]:
        """Return desired trades using the shared research/shadow context."""
        ...


@dataclass(frozen=True)
class NormalExit:
    """Normal lifecycle exit: wait for a replacement target, or flatten at exit_at."""
    policy: str = "next_target"
    exit_at: object = None

    def __post_init__(self):
        if self.policy not in ("next_target", "at_time"):
            raise ValueError("Unsupported normal exit policy")
        if self.policy == "next_target" and self.exit_at is not None:
            raise ValueError("next_target cannot specify exit_at")
        if self.policy == "at_time":
            if self.exit_at is None:
                raise ValueError("at_time requires exit_at")
            object.__setattr__(self, "exit_at", utc_instant(self.exit_at))


@dataclass(frozen=True)
class PortfolioTarget:
    """Version 1 desired portfolio state, independent of equity and order sizing."""
    decision_key: str
    instrument: Instrument
    target_weight: Decimal
    decision_timestamp: object
    valid_until: object
    normal_exit: NormalExit = NormalExit()
    version: int = 1

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported portfolio target version")
        if not isinstance(self.decision_key, str) or not self.decision_key.strip():
            raise ValueError("Decision identity required")
        if not isinstance(self.instrument, Instrument):
            raise TypeError("Expected Instrument")
        weight = money(self.target_weight)
        if abs(weight) > 1:
            raise ValueError("Target weight must be in [-1, 1]")
        object.__setattr__(self, "target_weight", weight)
        object.__setattr__(self, "decision_timestamp", utc_instant(self.decision_timestamp))
        object.__setattr__(self, "valid_until", utc_instant(self.valid_until))
        if self.valid_until <= self.decision_timestamp:
            raise ValueError("Validity deadline must follow decision")
        if not isinstance(self.normal_exit, NormalExit):
            raise TypeError("Expected NormalExit")
        exit_at = self.normal_exit.exit_at
        if exit_at is not None and not self.decision_timestamp < exit_at < self.valid_until:
            raise ValueError("Normal exit must fall inside instruction validity")

    def weight_at(self, *, now, supported_instruments):
        now = utc_instant(now)
        if self.instrument not in supported_instruments:
            raise ValueError("Unsupported instrument")
        if not self.decision_timestamp <= now < self.valid_until:
            raise ValueError("Instruction is stale or not yet valid")
        if self.normal_exit.policy == "at_time" and now >= self.normal_exit.exit_at:
            return Decimal("0")
        return self.target_weight


def target_weights(targets, *, now, supported_instruments, long_only=False, max_gross=1.0):
    """Bridge an explicit target batch into existing strategy weight validation.

    Omitted supported instruments have weight zero, matching validate_weights.
    """
    from .strategy import validate_weights
    supported_instruments = tuple(supported_instruments)
    weights = {}
    for target in targets:
        if not isinstance(target, PortfolioTarget):
            raise TypeError("Expected PortfolioTarget")
        if target.instrument in weights:
            raise ValueError("Duplicate instrument target")
        weights[target.instrument] = target.weight_at(now=now, supported_instruments=supported_instruments)
    return validate_weights(weights, supported_instruments, long_only=long_only, max_gross=max_gross)


class PortfolioTargetStrategy(Protocol):
    def portfolio_targets(self, context: StrategyContext) -> tuple[PortfolioTarget, ...]:
        ...


class PortfolioTargetAdapter:
    """Wrap a target strategy for the existing StrategyPipeline.target_weights API."""
    def __init__(self, strategy):
        self.strategy = strategy

    def target_weights(self, context: StrategyContext):
        targets = self.strategy.portfolio_targets(context)
        if targets is None:
            return None  # Existing hold-quantities behavior.
        return target_weights(targets, now=context.timestamp,
                              supported_instruments=context.historical_data)
