"""Offline Kraken spot approval boundary with transactional durable reservations.

No HTTP, exchange clients or credentials. Account/quote/rule snapshots must be
supplied by a future reconciled adapter. Approval is not exchange submission.
"""

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
import sqlite3

import pandas as pd

from .config import CONFIG
from .data import canonical_json, utc_instant
from .strategy import Instrument


def money(value):
    if isinstance(value, bool):
        raise ValueError("Boolean monetary values are invalid")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Monetary values must be finite")
    return result


def spot_only(instrument):
    if (instrument.venue != "kraken" or instrument.market_type != "spot"
            or instrument.quote != "USD" or instrument.symbol not in CONFIG.universe):
        raise ValueError("Production supports only locked Kraken USD spot instruments")


@dataclass(frozen=True)
class InstrumentRules:
    quantity_step: Decimal
    price_tick: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    timestamp: pd.Timestamp
    instrument: Instrument
    status: str = "online"

    def __post_init__(self):
        for name in ("quantity_step", "price_tick", "minimum_quantity", "minimum_notional"):
            value = money(getattr(self, name))
            if value <= 0:
                raise ValueError("Instrument increments/minimums must be positive")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "timestamp", utc_instant(self.timestamp))


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    timestamp: pd.Timestamp
    bid: Decimal
    ask: Decimal
    data_type: str = "quote"

    def __post_init__(self):
        object.__setattr__(self, "timestamp", utc_instant(self.timestamp))
        object.__setattr__(self, "bid", money(self.bid))
        object.__setattr__(self, "ask", money(self.ask))
        if self.bid <= 0 or self.ask < self.bid or self.data_type != "quote":
            raise ValueError("Invalid bid/ask quote")


@dataclass(frozen=True)
class AccountSnapshot:
    timestamp: pd.Timestamp
    day_start: pd.Timestamp
    cash: Decimal
    positions: object
    day_start_equity: Decimal
    borrowing: bool = False
    derivatives: bool = False

    def __post_init__(self):
        from types import MappingProxyType
        object.__setattr__(self, "timestamp", utc_instant(self.timestamp))
        object.__setattr__(self, "day_start", utc_instant(self.day_start))
        object.__setattr__(self, "cash", money(self.cash))
        object.__setattr__(self, "day_start_equity", money(self.day_start_equity))
        object.__setattr__(self, "positions", MappingProxyType({k: money(v) for k, v in self.positions.items()}))
        if self.cash < 0 or self.day_start_equity <= 0 or any(v < 0 for v in self.positions.values()):
            raise ValueError("Production balances must be funded and long-only")


@dataclass(frozen=True)
class OrderIntent:
    order_id: str
    decision_key: str
    instrument: Instrument
    side: str
    quantity: Decimal
    limit_price: Decimal
    fee_rate: Decimal = Decimal("0")
    leverage: Decimal = Decimal("1")
    borrowing: bool = False

    def __post_init__(self):
        for name in ("quantity", "limit_price", "fee_rate", "leverage"):
            object.__setattr__(self, name, money(getattr(self, name)))
        if not self.order_id or not self.decision_key or self.side not in ("buy", "sell"):
            raise ValueError("Order/decision identity and buy/sell side are required")
        if self.quantity <= 0 or self.limit_price <= 0 or not 0 <= self.fee_rate < 1:
            raise ValueError("Invalid order quantity, price or fee")


class RiskGateway:
    MAX_ORDER = Decimal("20")
    MAX_ASSET = Decimal("40")
    MAX_TOTAL = Decimal("100")
    MAX_DAILY_LOSS = Decimal("2")

    def __init__(self, database=":memory:"):
        self.connection = sqlite3.connect(str(database), timeout=10, isolation_level=None)
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.execute("CREATE TABLE IF NOT EXISTS controls (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.connection.execute("INSERT OR IGNORE INTO controls VALUES ('kill', '0')")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS reservations (
            order_id TEXT PRIMARY KEY, decision_key TEXT UNIQUE NOT NULL,
            instrument TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,
            price TEXT NOT NULL, fee TEXT NOT NULL, status TEXT NOT NULL)""")

    def close(self):
        self.connection.close()

    def kill(self):
        self.connection.execute("UPDATE controls SET value='1' WHERE name='kill'")

    def clear_kill(self):
        """Explicit operator action; never called automatically on restart."""
        self.connection.execute("UPDATE controls SET value='0' WHERE name='kill'")

    def release_after_reconciliation(self, order_id):
        """Caller must have reconciled terminal order/fills and updated balances.

        Keep identity forever to reject repeated decisions after release/restart.
        This is not a cancel command and must not be used for UNKNOWN submissions.
        """
        self.connection.execute("UPDATE reservations SET status='terminal' WHERE order_id=?", (order_id,))

    def reserve(self, intent, *, account, quotes, rules, now):
        now = utc_instant(now)
        spot_only(intent.instrument)
        if intent.leverage != 1 or intent.borrowing or account.borrowing or account.derivatives:
            raise ValueError("Production leverage, borrowing and derivatives are prohibited")
        if account.day_start != now.normalize() or not account.day_start <= account.timestamp <= now:
            raise ValueError("Daily loss baseline must belong to the current UTC day")
        if now - account.timestamp > pd.Timedelta(seconds=60):
            raise ValueError("Stale account snapshot")
        if not isinstance(rules, InstrumentRules) or rules.instrument != intent.instrument:
            raise ValueError("Instrument rules are missing or bound to another market")
        if rules.status != "online" or not pd.Timedelta(0) <= now - rules.timestamp <= pd.Timedelta(hours=24):
            raise ValueError("Stale/offline instrument metadata")
        for key in set(account.positions) | {intent.instrument}:
            spot_only(key)
            quote = quotes[key]
            if quote.instrument != key or not pd.Timedelta(0) <= now - quote.timestamp <= pd.Timedelta(seconds=5):
                raise ValueError("Stale/mismatched quote")
        quantity = (intent.quantity / rules.quantity_step).to_integral_value(rounding=ROUND_FLOOR) * rules.quantity_step
        rounding = ROUND_FLOOR if intent.side == "buy" else ROUND_CEILING
        price = (intent.limit_price / rules.price_tick).to_integral_value(rounding=rounding) * rules.price_tick
        approved = replace(intent, quantity=quantity, limit_price=price)
        notional = quantity * price
        if quantity < rules.minimum_quantity or notional < rules.minimum_notional:
            raise ValueError("Order below quantity/notional minimum after precision formatting")
        if notional > self.MAX_ORDER:
            raise ValueError("Order exceeds $20 limit")
        quote = quotes[intent.instrument]
        if (intent.side == "buy" and price < quote.ask) or (intent.side == "sell" and price > quote.bid):
            raise ValueError("Only price-bounded marketable orders are approved")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if self.connection.execute("SELECT value FROM controls WHERE name='kill'").fetchone()[0] == "1":
                raise PermissionError("Emergency kill switch is active")
            if self.connection.execute("SELECT 1 FROM reservations WHERE order_id=? OR decision_key=?",
                    (intent.order_id, intent.decision_key)).fetchone():
                raise ValueError("Duplicate order or strategy decision")
            pending = self.connection.execute("SELECT instrument, side, quantity, price, fee FROM reservations WHERE status='reserved'").fetchall()
            buy_quantities, buy_costs, sell_quantities = {}, Decimal(0), {}
            premiums, pending_loss = {}, Decimal(0)
            for raw, side, qty, limit, fee in pending:
                key = Instrument(**json.loads(raw))
                if key not in quotes:
                    raise ValueError("Missing quote for pending exposure")
                if quotes[key].instrument != key or not pd.Timedelta(0) <= now - quotes[key].timestamp <= pd.Timedelta(seconds=5):
                    raise ValueError("Stale pending-exposure quote")
                qty, limit, fee = money(qty), money(limit), money(fee)
                if side == "buy":
                    buy_quantities[key] = buy_quantities.get(key, Decimal(0)) + qty
                    buy_costs += qty * limit * (1 + fee)
                    premiums[key] = premiums.get(key, Decimal(0)) + qty * max(limit - quotes[key].ask, Decimal(0))
                    pending_loss += qty * max(limit - quotes[key].bid, Decimal(0)) + qty * limit * fee
                else:
                    sell_quantities[key] = sell_quantities.get(key, Decimal(0)) + qty
                    pending_loss += qty * max(quotes[key].bid - limit, Decimal(0)) + qty * limit * fee
            equity = account.cash + sum((qty * quotes[key].bid for key, qty in account.positions.items()), Decimal(0))
            if account.day_start_equity - equity >= self.MAX_DAILY_LOSS:
                raise ValueError("$2 daily-loss limit reached")
            expected_loss = quantity * max(price - quote.bid, Decimal(0)) if intent.side == "buy" else quantity * max(quote.bid - price, Decimal(0))
            expected_loss += notional * intent.fee_rate
            if account.day_start_equity - equity + pending_loss + expected_loss >= self.MAX_DAILY_LOSS:
                raise ValueError("Order and pending costs would reach the $2 daily-loss limit")
            if intent.side == "buy":
                cost = notional * (1 + intent.fee_rate)
                if cost > account.cash - buy_costs:
                    raise ValueError("Insufficient available cash including pending orders and fees")
                buy_quantities[intent.instrument] = buy_quantities.get(intent.instrument, Decimal(0)) + quantity
            else:
                available = account.positions.get(intent.instrument, Decimal(0)) - sell_quantities.get(intent.instrument, Decimal(0))
                if quantity > available:
                    raise ValueError("Insufficient available base balance; short selling prohibited")
            # Pending sells do not offset pending buys: their fills are uncertain.
            exposures = {}
            for key in set(account.positions) | set(buy_quantities):
                value = (account.positions.get(key, Decimal(0)) + buy_quantities.get(key, Decimal(0))) * quotes[key].ask
                value += premiums.get(key, Decimal(0))
                if key == intent.instrument and intent.side == "buy":
                    value += quantity * max(price - quotes[key].ask, Decimal(0))
                exposures[key.base] = exposures.get(key.base, Decimal(0)) + value
            if any(value > self.MAX_ASSET for value in exposures.values()):
                raise ValueError("Asset exposure exceeds $40 limit")
            if sum(exposures.values(), Decimal(0)) > self.MAX_TOTAL:
                raise ValueError("Total exposure exceeds $100 limit")
            self.connection.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved')",
                (intent.order_id, intent.decision_key, canonical_json(asdict(intent.instrument)).decode(),
                 intent.side, str(quantity), str(price), str(intent.fee_rate)))
            self.connection.execute("COMMIT")
            return approved
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
