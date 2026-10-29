"""Read-only exchange observation, isolated from the simulated paper ledger."""

from dataclasses import dataclass
import hashlib

import pandas as pd

from .data import canonical_json, utc_instant
from .risk import money, spot_only


READ_PERMISSIONS = frozenset({"query-funds", "query-open-trades", "query-closed-trades", "query-ledger"})
REQUIRED_PERMISSIONS = frozenset({"query-funds", "query-open-trades", "query-closed-trades"})
READ_ENDPOINTS = frozenset({"GetApiKeyInfo", "Balance", "OpenOrders", "TradesHistory", "TradeVolume"})


def check_permissions(info, *, now):
    permissions = info.get("permissions")
    if not isinstance(permissions, list) or not REQUIRED_PERMISSIONS <= set(permissions) <= READ_PERMISSIONS:
        raise PermissionError("Dedicated read-only key permissions are missing, unknown or unsafe")
    expiry = money(info.get("validUntil", "0"))
    if expiry and expiry <= money(utc_instant(now).timestamp()):
        raise PermissionError("Read-only key expired")
    if money(info.get("queryFrom", "0")) or money(info.get("queryTo", "0")):
        raise PermissionError("Restricted query window prevents complete reconciliation")
    # Return only permissions, never the key-info response (which contains the key).
    return sorted(permissions)


@dataclass(frozen=True)
class FeeSnapshot:
    instrument: object
    timestamp: pd.Timestamp
    taker_fraction: object
    account_fingerprint: str

    def __post_init__(self):
        spot_only(self.instrument)
        object.__setattr__(self, "timestamp", utc_instant(self.timestamp))
        value = money(self.taker_fraction)
        if not 0 <= value < 1 or not self.account_fingerprint:
            raise ValueError("Invalid account-specific fee")
        object.__setattr__(self, "taker_fraction", value)


@dataclass(frozen=True)
class ExchangeSnapshot:
    timestamp: pd.Timestamp
    window_start: pd.Timestamp
    balances: dict
    orders: dict
    trades: dict
    account_fingerprint: str

    def payload(self):
        return dict(timestamp=utc_instant(self.timestamp).isoformat(),
                    window_start=utc_instant(self.window_start).isoformat(),
                    balances=self.balances, orders=self.orders, trades=self.trades,
                    account_fingerprint=self.account_fingerprint)


class ReadOnlyKraken:
    """Injected private request callable; permits only specific read endpoints."""

    def __init__(self, request, account_fingerprint, clock=lambda: pd.Timestamp.now(tz="UTC")):
        if not account_fingerprint:
            raise ValueError("Account identity required")
        self._request, self.account_fingerprint, self.clock = request, account_fingerprint, clock
        self.pair_ids = {}
        self.pair_resolver = None

    def _read(self, endpoint, params=None):
        if endpoint not in READ_ENDPOINTS:
            raise PermissionError("Trading and funding endpoints prohibited")
        try:
            body = self._request(endpoint, params or {})
        except Exception:
            raise RuntimeError("Read-only Kraken request failed") from None
        if not isinstance(body, dict) or body.get("error") != [] or not isinstance(body.get("result"), dict):
            raise ValueError("Read-only Kraken response failed")
        return body["result"]

    def permissions(self):
        return check_permissions(self._read("GetApiKeyInfo"), now=self.clock())

    def fee(self, instrument):
        spot_only(instrument)
        self.permissions()
        stamp = utc_instant(self.clock())
        pair_id = self.pair_ids.get(instrument)
        if pair_id is None:
            pair_id = self.pair_resolver(instrument) if self.pair_resolver else instrument.symbol
            self.pair_ids[instrument] = pair_id
        row = self._read("TradeVolume", {"pair": pair_id})
        # Some private endpoints still use internal pair IDs. Require explicit
        # canonical pair response; do not guess a fee for a different pair.
        fee = row["fees"][pair_id]["fee"]
        return FeeSnapshot(instrument, stamp, money(fee) / 100, self.account_fingerprint)

    def snapshot(self, *, since):
        self.permissions()
        since, started = utc_instant(since), utc_instant(self.clock())
        if since > started:
            raise ValueError("Future reconciliation window")
        balances = self._read("Balance", {"assetVersion": 1})
        balances = {k: str(money(v)) for k, v in balances.items() if money(v) != 0}
        if any(money(v) < 0 for v in balances.values()):
            raise ValueError("Negative exchange balances unsupported")
        orders = self._read("OpenOrders", {"trades": True})["open"]
        trades, offset, expected = {}, 0, None
        while True:
            page = self._read("TradesHistory", {"start": since.timestamp(), "end": started.timestamp(), "ofs": offset})
            if expected is None:
                expected = int(page["count"])
            if int(page["count"]) != expected:
                raise ValueError("Trade history changed during pagination")
            rows = page["trades"]
            if set(rows) & set(trades):
                raise ValueError("Duplicate exchange trade IDs")
            for trade in rows.values():
                stamp = pd.Timestamp(trade["time"], unit="s", tz="UTC")
                if not since <= stamp <= started:
                    raise ValueError("Trade outside reconciliation window")
            trades.update(rows)
            offset += len(rows)
            if offset == expected:
                break
            if not rows or offset > expected or offset > 10000:
                raise ValueError("Incomplete/oversized trade history")
        after = self._read("Balance", {"assetVersion": 1})
        after = {k: str(money(v)) for k, v in after.items() if money(v) != 0}
        if balances != after or orders != self._read("OpenOrders", {"trades": True})["open"]:
            raise ValueError("Exchange account changed while taking snapshot")
        if utc_instant(self.clock()) - started > pd.Timedelta(seconds=60):
            raise ValueError("Exchange snapshot collection timed out")
        return ExchangeSnapshot(started, since, balances, orders, trades, self.account_fingerprint)


def discrepancies(expected, actual, *, now):
    """Compare real account mirror with real observations, never paper fills."""
    problems = []
    if not pd.Timedelta(0) <= utc_instant(now) - actual.timestamp <= pd.Timedelta(seconds=60):
        problems.append("stale_exchange_snapshot")
    if expected.account_fingerprint != actual.account_fingerprint:
        problems.append("account_identity_changed")
    if expected.window_start != actual.window_start:
        problems.append("trade_window_changed")
    for name in ("balances", "orders", "trades"):
        if getattr(expected, name) != getattr(actual, name):
            problems.append(name + "_discrepancy")
    return problems


def snapshot_from_payload(body):
    return ExchangeSnapshot(utc_instant(body["timestamp"]), utc_instant(body["window_start"]),
        body["balances"], body["orders"], body["trades"], body["account_fingerprint"])


def snapshot_hash(snapshot):
    return hashlib.sha256(canonical_json(snapshot.payload())).hexdigest()
