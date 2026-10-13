"""Durable operator controls, ledger recovery, watchdog and reliability evidence."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3

import pandas as pd

from .data import canonical_json, utc_instant
from .risk import money
from .strategy import Instrument


class ProcessLock:
    """OS releases advisory lock on process exit, including a crash."""

    def __init__(self, database):
        self.handle = open(str(database) + ".lock", "a+b")
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if Path(str(database) + ".lock").stat().st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.handle.close()
                raise PermissionError("Another shadow/operator process owns the database") from None
        else:
            import fcntl
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.handle.close()
                raise PermissionError("Another shadow/operator process owns the database") from None

    def close(self):
        self.handle.close()


class Operations:
    def __init__(self, database, *, clock=lambda: pd.Timestamp.now(tz="UTC"), connection=None):
        if not Path(database).is_file():
            raise ValueError("Existing shadow database required")
        self.clock, self.owns_connection = clock, connection is None
        self.db = connection or sqlite3.connect(str(database), timeout=10, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS ops_events (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS ops_values (name TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS ops_samples (timestamp TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS nonce_state (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)")

    def close(self):
        if self.owns_connection:
            self.db.close()

    def value(self, name):
        row = self.db.execute("SELECT body FROM ops_values WHERE name=?", (name,)).fetchone()
        return None if row is None else json.loads(row[0])

    def set_value(self, name, value):
        self.db.execute("INSERT OR REPLACE INTO ops_values VALUES (?, ?)", (name, canonical_json(value).decode()))

    def event(self, kind, **body):
        # Callers supply enumerated codes/quantities, never exception text or API responses.
        self.db.execute("INSERT INTO ops_events(timestamp,kind,body) VALUES (?,?,?)",
            (utc_instant(self.clock()).isoformat(), kind, canonical_json(body).decode()))

    def nonce(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT value FROM nonce_state WHERE id=1").fetchone()
            value = max(int(utc_instant(self.clock()).timestamp() * 1000), 1 if row is None else row[0] + 1)
            self.db.execute("INSERT OR REPLACE INTO nonce_state VALUES (1,?)", (value,))
            self.db.execute("COMMIT")
            return value
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def halt(self, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Operator reason is required")
        self.db.execute("UPDATE controls SET value='1' WHERE name='kill'")
        self.event("halt", reason=reason)

    def inspect(self):
        return dict(state=json.loads(self.db.execute("SELECT body FROM shadow_state WHERE id=1").fetchone()[0]),
            halted=self.db.execute("SELECT value FROM controls WHERE name='kill'").fetchone()[0] == "1",
            incomplete_cycles=self.db.execute("SELECT decision FROM shadow_cycles WHERE status='started'").fetchall(),
            unresolved_reservations=self.db.execute("SELECT order_id FROM reservations WHERE status='reserved'").fetchall(),
            heartbeat=self.value("heartbeat"), exchange=self.value("reconciliation"))

    def ledger_check(self):
        origin = self.value("ledger_origin")
        if origin is None:
            raise PermissionError("Ledger origin not initialized")
        state = self.inspect()["state"]
        cash, positions = money(origin["cash"]), list(map(money, origin["positions"]))
        keys = [Instrument(**key) for key in origin["instruments"]]
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_fills'").fetchone()
        fills = [] if not exists else self.db.execute("SELECT body FROM shadow_fills ORDER BY rowid").fetchall()
        for raw, in fills:
            fill = json.loads(raw)
            key = Instrument(**fill["instrument"])
            qty, price, fee = (money(fill[name]) for name in ("quantity", "price", "fee"))
            if fill["side"] not in {"buy", "sell"} or qty <= 0 or price <= 0 or fee < 0:
                raise PermissionError("Corrupt paper fill")
            reservation = self.db.execute("SELECT instrument,side,quantity,price,fee,status FROM reservations WHERE order_id=?", (fill["order_id"],)).fetchone()
            if reservation is None or Instrument(**json.loads(reservation[0])) != key or reservation[1] != fill["side"]:
                raise PermissionError("Fill/reservation identity discrepancy")
            limit, rate = money(reservation[3]), money(reservation[4])
            if qty != money(reservation[2]) or fee != qty * price * rate or reservation[5] != "terminal":
                raise PermissionError("Fill/reservation accounting discrepancy")
            if (fill["side"] == "buy" and price > limit) or (fill["side"] == "sell" and price < limit):
                raise PermissionError("Paper price violated approved bound")
            signed = qty if fill["side"] == "buy" else -qty
            cash -= signed * price + fee
            positions[keys.index(key)] += signed
            if cash < 0 or any(q < 0 for q in positions):
                raise PermissionError("Paper ledger is unfunded or short")
        if cash != money(state["cash"]) or positions != list(map(money, state["positions"])):
            raise PermissionError("Paper cash/position ledger discrepancy")
        return dict(cash=str(cash), positions=list(map(str, positions)), fills=len(fills))

    def recover(self, reason):
        """Called only under ProcessLock; skips uncertain paper cycle, never replays."""
        self.halt(reason)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            ledger = self.ledger_check()
            # Shadow has no submission transport: an uncommitted paper reservation
            # cannot have an exchange fill. Keep its identity; record abandonment.
            unresolved = [r[0] for r in self.db.execute("SELECT order_id FROM reservations WHERE status='reserved'")]
            for order_id in unresolved:
                self.db.execute("UPDATE reservations SET status='terminal' WHERE order_id=?", (order_id,))
            cycles = [r[0] for r in self.db.execute("SELECT decision FROM shadow_cycles WHERE status='started'")]
            for decision in cycles:
                self.db.execute("UPDATE shadow_cycles SET status='abandoned' WHERE decision=?", (decision,))
            self.event("recovery", reason=reason, abandoned_reservations=unresolved, abandoned_cycles=cycles, ledger=ledger)
            self.db.execute("COMMIT")
            return ledger
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def resume(self, reason):
        self.ledger_check()
        state = self.inspect()
        if state["incomplete_cycles"] or state["unresolved_reservations"]:
            raise PermissionError("Recover incomplete cycles before resuming")
        reconciliation = self.value("reconciliation")
        if self.value("exchange_baseline") is not None:
            if not reconciliation or reconciliation["problems"] or not pd.Timedelta(0) <= utc_instant(self.clock()) - utc_instant(reconciliation["timestamp"]) <= pd.Timedelta(seconds=60):
                raise PermissionError("Fresh clean exchange reconciliation required")
        if not reason.strip():
            raise ValueError("Resume reason required")
        self.db.execute("UPDATE controls SET value='0' WHERE name='kill'")
        self.set_value("watchdog", {"armed": False})
        self.set_value("evidence_start", utc_instant(self.clock()).isoformat())
        self.event("resume", reason=reason)

    def heartbeat(self, *, source, equity, accounting_ok=True, account_fee_verified=False, event_time_verified=False):
        stamp = utc_instant(self.clock()).isoformat()
        body = dict(timestamp=stamp, source=source, equity=str(money(equity)), accounting_ok=bool(accounting_ok),
                    account_fee_verified=bool(account_fee_verified), event_time_verified=bool(event_time_verified))
        self.set_value("heartbeat", body)
        self.set_value("watchdog", {"armed": True, "timeout_seconds": 60})
        self.db.execute("INSERT OR REPLACE INTO ops_samples VALUES (?, ?)", (stamp, canonical_json(body).decode()))

    def watchdog(self):
        setting, beat = self.value("watchdog"), self.value("heartbeat")
        if not setting or not setting["armed"]:
            return {"armed": False, "healthy": False}
        healthy = beat is not None and pd.Timedelta(0) <= utc_instant(self.clock()) - utc_instant(beat["timestamp"]) <= pd.Timedelta(seconds=setting["timeout_seconds"])
        if not healthy:
            self.halt("heartbeat_expired")
        return {"armed": True, "healthy": healthy}

    def evidence(self):
        samples = [json.loads(raw) for raw, in self.db.execute("SELECT body FROM ops_samples ORDER BY timestamp")]
        events = [json.loads(raw) for raw, in self.db.execute("SELECT body FROM shadow_events ORDER BY rowid")]
        begin = self.value("evidence_start")
        if begin is not None:
            samples = [s for s in samples if utc_instant(s["timestamp"]) >= utc_instant(begin)]
            events = [e for e in events if utc_instant(e["timestamp"]) >= utc_instant(begin)]
        live = [s for s in samples if s["source"] == "kraken_public"]
        days = sorted({utc_instant(s["timestamp"]).normalize() for s in live})
        expected_days = 0 if not days else (days[-1] - days[0]).days + 1
        decisions = {utc_instant(e["timestamp"]).normalize() for e in events} & set(days)
        completion = 0 if not expected_days else len(decisions) / expected_days
        intervals = [utc_instant(b["timestamp"]) - utc_instant(a["timestamp"]) for a, b in zip(live, live[1:])]
        gaps = sum(delta > pd.Timedelta(seconds=60) for delta in intervals)
        duration = 0 if len(live) < 2 else (utc_instant(live[-1]["timestamp"]) - utc_instant(live[0]["timestamp"])).total_seconds()
        fills = sum(len(e["fills"]) for e in events if utc_instant(e["timestamp"]).normalize() in days)
        faults = self.db.execute("SELECT count(*) FROM ops_events WHERE kind IN ('halt','fault','recovery') AND timestamp>=?", (begin or "",)).fetchone()[0]
        prerequisites = bool(live) and all(s["accounting_ok"] and s["account_fee_verified"] and s["event_time_verified"] for s in live)
        eligible = len(decisions) >= 30 and duration >= 29 * 86400 and completion >= .995 and fills >= 20 and gaps == 0 and faults == 0 and prerequisites
        return dict(real_observations=len(live), synthetic_observations=len(samples)-len(live),
                    observed_days=len(days), expected_days=expected_days, completed_decisions=len(decisions),
                    completion_fraction=completion, paper_fills=fills, heartbeat_gaps=gaps,
                    elapsed_seconds=duration, fault_events=faults, reliability_gate_passed=eligible,
                    account_fees_and_event_times_verified=prerequisites,
                    live_authorized=False)
