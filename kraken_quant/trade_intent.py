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
