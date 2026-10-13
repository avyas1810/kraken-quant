"""Single-writer durable spot paper trading. No exchange submission interface."""

from dataclasses import asdict
from decimal import ROUND_FLOOR
import hashlib
import json

import pandas as pd

from .boundary import ShadowAdapter
from .config import CONFIG
from .data import DAY, canonical_json, utc_instant, validate_bars
from .risk import AccountSnapshot, OrderIntent, RiskGateway, money, spot_only


class ShadowRunner:
    def __init__(self, pipeline, market_data, database, *, instruments, start,
                 strategy_id, funding="100", fee_rate="0.004", clock=None):
        if str(database) == ":memory:":
            raise ValueError("Shadow requires a persistent database")
        self.adapter = ShadowAdapter(pipeline, spot_rehearsal=True)
        self.market_data = market_data
        self.instruments = tuple(sorted(instruments))
        if not self.instruments or len(set(self.instruments)) != len(self.instruments):
            raise ValueError("Select unique instruments")
        for key in self.instruments:
            spot_only(key)
        self.start = utc_instant(start)
        if self.start != self.start.normalize() or self.start <= CONFIG.data_end:
            raise PermissionError("Shadow start must follow the locked dataset")
        if not strategy_id:
            raise ValueError("Frozen strategy identity required")
        self.clock = clock or (lambda: pd.Timestamp.now(tz="UTC"))
        self.fee = money(fee_rate)
        funding = money(funding)
        if not 0 <= self.fee < 1 or funding <= 0:
            raise ValueError("Invalid funding/fee")
        self.gateway = RiskGateway(database)
        db = self.gateway.connection
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS shadow_state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS shadow_events (decision TEXT PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS shadow_cycles (decision TEXT PRIMARY KEY, status TEXT NOT NULL)")
        self.identity = hashlib.sha256(canonical_json(dict(strategy=strategy_id, start=self.start.isoformat(),
            instruments=[asdict(k) for k in self.instruments], funding=str(funding), fee=str(self.fee)))).hexdigest()
        initial = dict(identity=self.identity, cash=str(funding), positions=["0"] * len(self.instruments),
                       day=None, baseline=str(funding))
        db.execute("INSERT OR IGNORE INTO shadow_state VALUES (1, ?)", (canonical_json(initial).decode(),))
        if self._state()["identity"] != self.identity:
            self.close()
            raise ValueError("Persistent shadow configuration mismatch")

    def _state(self):
        return json.loads(self.gateway.connection.execute("SELECT body FROM shadow_state WHERE id=1").fetchone()[0])

    def close(self):
        self.gateway.close()

    def _fee_for(self, instrument, now):
        """Override with a validated per-instrument/account fee provider."""
        return self.fee

    def step(self):
        db = self.gateway.connection
        decision = self.identity + ":" + (utc_instant(self.clock()).normalize() - DAY).isoformat()
        db.execute("BEGIN IMMEDIATE")
        try:
            if db.execute("SELECT value FROM controls WHERE name='kill'").fetchone()[0] == "1":
                raise PermissionError("Emergency kill switch is active")
            incomplete = db.execute("SELECT 1 FROM shadow_cycles WHERE status='started'").fetchone()
            if incomplete:
                raise PermissionError("Incomplete shadow cycle requires operator reconciliation")
            if db.execute("SELECT 1 FROM shadow_events WHERE decision=?", (decision,)).fetchone():
                db.execute("COMMIT")
                return {"mode": "shadow", "status": "already_processed", "decision": decision}
            db.execute("INSERT INTO shadow_cycles VALUES (?, 'started')", (decision,))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return self._step(decision)

    def _step(self, claimed_decision):
        now = utc_instant(self.clock())
        end = now.normalize() - DAY
        decision = self.identity + ":" + end.isoformat()
        if decision != claimed_decision:
            raise ValueError("UTC day changed while claiming cycle")
        db = self.gateway.connection
        if db.execute("SELECT 1 FROM reservations WHERE status='reserved'").fetchone():
            raise PermissionError("Unresolved paper reservation: operator reconciliation required")
        if db.execute("SELECT 1 FROM shadow_events WHERE decision=?", (decision,)).fetchone():
            return {"mode": "shadow", "status": "already_processed", "decision": decision}
        state = self._state()
        cash = money(state["cash"])
        positions = dict(zip(self.instruments, map(money, state["positions"])))
        histories = {}
        for key in self.instruments:
            frame = self.market_data.bars(key, start=self.start, end=end)
            clean = validate_bars(frame, self.start, end, as_of=now)
            for field, value in (("venue", key.venue), ("symbol", key.symbol), ("base", key.base),
                                 ("quote", key.quote), ("data_type", "ohlcv")):
                clean[field] = value
            clean["timestamp"] = clean.index
            histories[key] = clean
        quotes = {k: self.market_data.quote(k) for k in self.instruments}
        rules = {k: self.market_data.rules(k) for k in self.instruments}
        now = utc_instant(self.clock())
        if now.normalize() - DAY != end:
            raise ValueError("UTC day changed during data collection; retry")
        for key, quote in quotes.items():
            if quote.instrument != key or not pd.Timedelta(0) <= now - quote.timestamp <= pd.Timedelta(seconds=5):
                raise ValueError("Stale or mismatched quote")
            rule = rules[key]
            if rule.instrument != key or rule.status != "online" or not pd.Timedelta(0) <= now - rule.timestamp <= pd.Timedelta(hours=24):
                raise ValueError("Stale, mismatched or offline instrument metadata")
        equity = cash + sum((positions[k] * quotes[k].bid for k in positions), money(0))
        day = now.normalize().isoformat()
        if state["day"] != day:
            # Paper midnight mark: previous completed daily close, not a late
            # restart quote. Persist once; never reset losses within a UTC day.
            baseline = cash + sum((positions[k] * money(histories[k].iloc[-1]["close"])
                                   for k in positions), money(0))
            state.update(day=day, baseline=str(baseline), baseline_method="previous_utc_close",
                         baseline_timestamp=now.normalize().isoformat())
        target = self.adapter.evaluate(timestamp=now, historical_data=histories,
            positions=positions, cash=cash, equity=equity)
        now = utc_instant(self.clock())
        if now.normalize().isoformat() != day:
            raise ValueError("UTC day changed during strategy evaluation")
        if money(state["baseline"]) - equity >= self.gateway.MAX_DAILY_LOSS:
            raise PermissionError("$2 daily-loss limit reached")
        for quote in quotes.values():
            if not pd.Timedelta(0) <= now - quote.timestamp <= pd.Timedelta(seconds=5):
                raise ValueError("Stale quote after strategy evaluation")
        fills, skipped_orders = [], []
        if target is not None:
            quantities = {k: money(target[k]) * equity / money(histories[k].iloc[-1]["close"]) - positions[k]
                          for k in self.instruments}
            for key in sorted(self.instruments, key=lambda k: quantities[k] > 0):
                delta = quantities[key]
                if delta == 0:
                    continue
                side = "buy" if delta > 0 else "sell"
                price = quotes[key].ask if side == "buy" else quotes[key].bid
                rounded = (abs(delta) / rules[key].quantity_step).to_integral_value(rounding=ROUND_FLOOR) * rules[key].quantity_step
                if rounded < rules[key].minimum_quantity or rounded * price < rules[key].minimum_notional:
                    skipped_orders.append(dict(instrument=asdict(key), reason="below_exchange_minimum", requested_quantity=str(abs(delta))))
                    continue
                order_id = hashlib.sha256((decision + key.symbol).encode()).hexdigest()
                fee_rate = self._fee_for(key, now)
                intent = OrderIntent(order_id, decision + ":" + key.symbol, key, side, abs(delta), price, fee_rate)
                account = AccountSnapshot(now, now.normalize(), cash, positions, money(state["baseline"]))
                # Reservation persists before paper accounting. Crash in between halts on restart.
                now = utc_instant(self.clock())
                approved = self.gateway.reserve(intent, account=account, quotes=quotes, rules=rules[key], now=now)
                signed = approved.quantity if side == "buy" else -approved.quantity
                fee = approved.quantity * price * fee_rate
                cash -= signed * price + fee
                positions[key] += signed
                state.update(cash=str(cash), positions=[str(positions[k]) for k in self.instruments])
                fill = dict(order_id=order_id, instrument=asdict(key), side=side,
                            quantity=str(approved.quantity), price=str(price), fee=str(fee),
                            fee_rate=str(fee_rate), timestamp=now.isoformat())
                # Partial cycle crash: committed fills remain, decision reservations prevent replay.
                db.execute("BEGIN IMMEDIATE")
                try:
                    db.execute("UPDATE shadow_state SET body=? WHERE id=1", (canonical_json(state).decode(),))
                    db.execute("CREATE TABLE IF NOT EXISTS shadow_fills (order_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
                    db.execute("INSERT INTO shadow_fills VALUES (?, ?)", (order_id, canonical_json(fill).decode()))
                    self.gateway.release_after_reconciliation(order_id)
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                fills.append(fill)
        event = dict(mode="shadow", status="processed", decision=decision, timestamp=now.isoformat(),
                     fills=fills, skipped_orders=skipped_orders, cash=str(cash), equity=str(cash + sum((positions[k] * quotes[k].bid for k in positions), money(0))))
        event["inputs"] = [dict(instrument=asdict(k),
            history_sha256=hashlib.sha256(histories[k].to_csv().encode()).hexdigest(),
            history_start=self.start.isoformat(), history_end=end.isoformat(),
            bid=str(quotes[k].bid), ask=str(quotes[k].ask), quote_time=quotes[k].timestamp.isoformat(),
            quantity_step=str(rules[k].quantity_step), price_tick=str(rules[k].price_tick),
            minimum_quantity=str(rules[k].minimum_quantity), minimum_notional=str(rules[k].minimum_notional),
            rules_time=rules[k].timestamp.isoformat()) for k in self.instruments]
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("UPDATE shadow_state SET body=? WHERE id=1", (canonical_json(state).decode(),))
            db.execute("INSERT INTO shadow_events VALUES (?, ?)", (decision, canonical_json(event).decode()))
            db.execute("UPDATE shadow_cycles SET status='complete' WHERE decision=?", (decision,))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return event

    def submit(self, *args, **kwargs):
        raise PermissionError("Shadow cannot submit orders")
