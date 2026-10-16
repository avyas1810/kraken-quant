"""Supervised shadow lifecycle: ledger recovery, real-account mirror and marks."""

from dataclasses import asdict
import hashlib
from pathlib import Path
import platform

import numpy as np
import pandas as pd

from .data import DAY, canonical_json, utc_instant, validate_bars
from .operations import Operations, ProcessLock
from .reconciliation import discrepancies, snapshot_from_payload, snapshot_hash
from .risk import money
from .shadow import ShadowRunner


class OperationalShadowRunner(ShadowRunner):
    def __init__(self, pipeline, market_data, database, *, readonly=None, **kwargs):
        self.process_lock = ProcessLock(database)
        self.ops = None
        try:
            super().__init__(pipeline, market_data, database, **kwargs)
            self.ops = Operations(database, clock=self.clock, connection=self.gateway.connection)
            manifest = dict(python=platform.python_version(), pandas=pd.__version__, numpy=np.__version__,
                sources={p.name: hashlib.sha256(p.read_text(encoding="utf-8").replace("\r\n", "\n").encode()).hexdigest()
                         for p in sorted(Path(__file__).parent.glob("*.py"))})
            previous_manifest = self.ops.value("engine_manifest")
            if previous_manifest is not None and previous_manifest != manifest:
                raise PermissionError("Shadow engine changed; start a new evidence database")
            self.ops.set_value("engine_manifest", manifest)
            origin = self.ops.value("ledger_origin")
            if origin is None:
                self.ops.set_value("ledger_origin", dict(identity=self.identity,
                    cash=str(money(kwargs.get("funding", "100"))), positions=["0"] * len(self.instruments),
                    instruments=[asdict(k) for k in self.instruments]))
            elif origin["identity"] != self.identity:
                raise PermissionError("Ledger origin identity mismatch")
            self.ops.ledger_check()
            self.readonly, self.fees = readonly, {}
        except BaseException:
            if hasattr(self, "gateway"):
                try:
                    self.gateway.kill()
                    self.gateway.close()
                except Exception:
                    pass
            self.process_lock.close()
            raise

    def close(self):
        if hasattr(self, "gateway"):
            super().close()
        self.process_lock.close()

    def _fee_for(self, instrument, now):
        if self.readonly is None:
            return super()._fee_for(instrument, now)
        fee = self.fees[instrument]
        if fee.instrument != instrument or fee.account_fingerprint != self.readonly.account_fingerprint:
            raise PermissionError("Account fee identity mismatch")
        if not pd.Timedelta(0) <= utc_instant(now) - fee.timestamp <= pd.Timedelta(hours=24):
            raise PermissionError("Stale account fee")
        return fee.taker_fraction

    def reconcile_exchange(self):
        if self.readonly is None:
            if self.ops.value("exchange_baseline") is not None:
                raise PermissionError("Pinned exchange observer cannot be disabled")
            return {"status": "not_configured"}
        expected_body = self.ops.value("exchange_baseline")
        if expected_body is None:
            raise PermissionError("Explicitly pin a dedicated real-account baseline before observation")
        expected = snapshot_from_payload(expected_body)
        actual = self.readonly.snapshot(since=expected.window_start)
        problems = discrepancies(expected, actual, now=self.clock())
        if actual.orders:
            problems.append("unexpected_open_exchange_orders")
        report = dict(timestamp=utc_instant(self.clock()).isoformat(), problems=problems,
                      snapshot_hash=snapshot_hash(actual))
        self.ops.set_value("reconciliation", report)
        self.ops.event("reconciliation", **report)
        if problems:
            self.ops.halt("exchange_discrepancy")
            raise PermissionError("Exchange mirror discrepancy; inspect read-only account externally")
        return report

    def tick(self):
        """Poll about every20s; strategy once/day, risk and paper marks every tick."""
        try:
            health = self.ops.watchdog()
            if health["armed"] and not health["healthy"]:
                raise PermissionError("Shadow watchdog expired; manual resume required")
            if self.ops.inspect()["halted"]:
                raise PermissionError("Shadow is manually/automatically halted")
            self.ops.ledger_check()
            if self.readonly is not None:
                report = self.ops.value("reconciliation")
                if report is None or utc_instant(self.clock()) - utc_instant(report["timestamp"]) >= pd.Timedelta(seconds=60):
                    self.reconcile_exchange()
                for key in self.instruments:
                    if key not in self.fees or utc_instant(self.clock()) - self.fees[key].timestamp >= pd.Timedelta(hours=1):
                        self.fees[key] = self.readonly.fee(key)
            elif self.ops.value("exchange_baseline") is not None:
                raise PermissionError("Read-only exchange observer required")
            decision = self.identity + ":" + (utc_instant(self.clock()).normalize() - DAY).isoformat()
            cycle = self.gateway.connection.execute("SELECT status FROM shadow_cycles WHERE decision=?", (decision,)).fetchone()
            if cycle == ("abandoned",):
                state = self._state()
                now = utc_instant(self.clock())
                if state["day"] != now.normalize().isoformat():
                    baseline = money(state["cash"])
                    for key, quantity in zip(self.instruments, map(money, state["positions"])):
                        if quantity:
                            frame = self.market_data.bars(key, start=self.start, end=now.normalize()-DAY)
                            frame = validate_bars(frame, self.start, now.normalize()-DAY, as_of=now)
                            baseline += quantity * money(frame.iloc[-1]["close"])
                    state.update(day=now.normalize().isoformat(), baseline=str(baseline),
                                 baseline_method="previous_utc_close", baseline_timestamp=now.normalize().isoformat())
                    self.gateway.connection.execute("UPDATE shadow_state SET body=? WHERE id=1", (canonical_json(state).decode(),))
            result = {"mode": "shadow", "status": "abandoned", "decision": decision} if cycle == ("abandoned",) else self.step()
            quotes = (self.market_data.quotes(self.instruments) if hasattr(self.market_data, "quotes")
                      else {k: self.market_data.quote(k) for k in self.instruments})
            now = utc_instant(self.clock())
            for key, quote in quotes.items():
                if key != quote.instrument or not pd.Timedelta(0) <= now - quote.timestamp <= pd.Timedelta(seconds=5):
                    raise ValueError("Stale/mismatched monitoring quote")
            state = self._state()
            positions = dict(zip(self.instruments, map(money, state["positions"])))
            equity = money(state["cash"]) + sum((q * quotes[k].bid for k, q in positions.items()), money(0))
            if state["day"] != now.normalize().isoformat():
                raise PermissionError("No current midnight baseline; recover before marking")
            if money(state["baseline"]) - equity >= self.gateway.MAX_DAILY_LOSS:
                raise PermissionError("$2 monitored daily-loss limit reached")
            exposures = [q * quotes[k].ask for k, q in positions.items()]
            if any(e > self.gateway.MAX_ASSET for e in exposures) or sum(exposures, money(0)) > self.gateway.MAX_TOTAL:
                raise PermissionError("Monitored exposure limit exceeded")
            self.ops.ledger_check()
            self.ops.heartbeat(source=getattr(self.market_data, "observation_source", "injected"), equity=equity,
                account_fee_verified=self.readonly is not None,
                event_time_verified=getattr(self.market_data, "event_time_verified", False))
            result["health"] = {"accounting_ok": True, "equity": str(equity),
                "account_fee_verified": self.readonly is not None,
                "quote_timestamp_kind": "exchange_event" if getattr(self.market_data, "event_time_verified", False) else "injected"}
            return result
        except Exception as error:
            self.ops.halt("shadow_tick_failure")
            self.ops.event("fault", code=type(error).__name__)
            raise


def pin_exchange_baseline(ops, readonly, *, since, reason):
    """Explicit operator acceptance of a real account, not paper synchronization."""
    if not reason.strip():
        raise ValueError("Operator reason required")
    snapshot = readonly.snapshot(since=since)
    previous = ops.value("exchange_baseline")
    if previous is not None and previous["account_fingerprint"] != snapshot.account_fingerprint:
        raise PermissionError("Changing the real account requires a new shadow database")
    if snapshot.orders:
        raise PermissionError("Cannot pin an account with open orders")
    ops.set_value("exchange_baseline", snapshot.payload())
    ops.set_value("reconciliation", dict(timestamp=utc_instant(ops.clock()).isoformat(), problems=[],
                                        snapshot_hash=snapshot_hash(snapshot)))
    ops.event("exchange_baseline_accepted", reason=reason, snapshot_hash=snapshot_hash(snapshot))
