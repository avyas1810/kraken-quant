"""Synthetic fault drills; no credentials, private connection or real orders."""

from dataclasses import replace
import json

import pandas as pd
import pytest

from kraken_quant.credentials import readonly_from_env
from kraken_quant.operational_data import EventTimeMarketData
from kraken_quant.operational_shadow import OperationalShadowRunner, pin_exchange_baseline
from kraken_quant.operations import Operations, ProcessLock
from kraken_quant.ops_cli import main
from kraken_quant.reconciliation import ReadOnlyKraken, check_permissions, discrepancies
from kraken_quant.strategy import Instrument, StrategyPipeline


KEY = Instrument("kraken", "BTC/USD", "BTC", "USD")
START = pd.Timestamp("2026-10-01T00:00:00Z")


class Clock:
    def __init__(self, stamp="2026-10-03T12:00:00Z"):
        self.now = pd.Timestamp(stamp)

    def __call__(self): return self.now

    def advance(self, seconds): self.now += pd.Timedelta(seconds=seconds)


class Market(EventTimeMarketData):
    def __init__(self, clock):
        self.time = clock
        self.bid, self.ask, self.close_price = 100, 100.1, 100
        super().__init__(self.response, clock=clock)

    def response(self, endpoint, params):
        assert params["pair"] == KEY.symbol
        if endpoint == "OHLC":
            rows = [[int(t.timestamp()), str(self.close_price), str(self.close_price+2),
                     str(self.close_price-2), str(self.close_price), str(self.close_price), "10", 1]
                    for t in pd.date_range(START, self.time().normalize(), freq="D")]
            result = {KEY.symbol: rows, "last": int(self.time().timestamp())}
        elif endpoint == "Spread":
            result = {KEY.symbol: [[int(self.time().timestamp()), str(self.bid), str(self.ask)]]}
        elif endpoint == "AssetPairs":
            result = {KEY.symbol: dict(base="BTC", quote="USD", lot_decimals=3, tick_size="0.1",
                ordermin="0.001", costmin="0.1", status="online")}
        else: pytest.fail("Unexpected endpoint")
        return dict(error=[], result=result)


class Strategy:
    def __init__(self, weight=.1): self.weight = weight

    def target_weights(self, context):
        return None if self.weight is None else {KEY: self.weight}


class Private:
    def __init__(self, clock):
        self.clock = clock
        self.balances, self.orders, self.trades = {"USD": "100"}, {}, {}
        self.calls, self.permissions = [], ["query-funds", "query-open-trades", "query-closed-trades"]

    def request(self, endpoint, params):
        self.calls.append(endpoint)
        if endpoint == "GetApiKeyInfo":
            row = dict(apiKey="SENTINEL_DO_NOT_LOG", permissions=self.permissions, validUntil="0")
        elif endpoint == "Balance": row = self.balances.copy()
        elif endpoint == "OpenOrders": row = {"open": self.orders.copy()}
        elif endpoint == "TradeVolume": row = {"fees": {KEY.symbol: {"fee": "0.25"}}}
        elif endpoint == "TradesHistory": row = {"trades": self.trades.copy(), "count": len(self.trades)}
        else: pytest.fail("No trading or funding request allowed")
        return dict(error=[], result=row)

    def adapter(self): return ReadOnlyKraken(self.request, "mock-account", clock=self.clock)


def build(tmp_path, clock=None, strategy=None, readonly=None):
    clock = clock or Clock()
    strategy = strategy or Strategy()
    source = Market(clock)
    instance = OperationalShadowRunner(StrategyPipeline(strategy), source, tmp_path / "paper.sqlite",
        instruments=[KEY], start=START, strategy_id="ops-fixture", fee_rate="0.01", clock=clock, readonly=readonly)
    return instance, source, clock, strategy


def test_midnight_carried_position_baseline_and_real_loss(tmp_path):
    runner, market, clock, strategy = build(tmp_path, Clock("2026-10-03T23:59:50Z"))
    runner.tick()
    strategy.weight = None
    clock.advance(12)
    market.close_price, market.bid, market.ask = 110, 109, 109.1
    runner.tick()
    baseline = runner._state()["baseline"]
    assert float(baseline) == pytest.approx(100.8899)
    # Midnight uses close110, not the quote109; same-day polls cannot reset loss.
    clock.advance(20)
    market.bid, market.ask = 80, 80.1
    with pytest.raises(PermissionError, match="daily-loss"): runner.tick()
    assert runner._state()["baseline"] == baseline
    assert runner.ops.inspect()["halted"]
    runner.close()


def test_restart_lock_recovery_and_manual_resume(tmp_path):
    runner, source, clock, strategy = build(tmp_path)
    runner.tick()
    with pytest.raises(PermissionError, match="owns"): ProcessLock(tmp_path / "paper.sqlite")
    runner.ops.halt("operator_requested")
    runner.close()
    restarted, _, _, _ = build(tmp_path, clock, strategy)
    with pytest.raises(PermissionError, match="halted"): restarted.tick()
    restarted.ops.resume("reviewed ledger")
    assert restarted.tick()["status"] == "already_processed"
    assert restarted.ops.ledger_check()["fills"] == 1
    restarted.close()


def test_crash_reservation_recover_without_replay(tmp_path, monkeypatch):
    runner, _, clock, strategy = build(tmp_path)
    original = runner.gateway.release_after_reconciliation
    def crash(*args): raise RuntimeError("DO_NOT_LOG_SECRET")
    monkeypatch.setattr(runner.gateway, "release_after_reconciliation", crash)
    with pytest.raises(RuntimeError): runner.tick()
    assert runner.ops.inspect()["unresolved_reservations"]
    assert runner.ops.ledger_check()["fills"] == 0
    runner.close()
    restarted, _, _, _ = build(tmp_path, clock, strategy)
    restarted.ops.recover("paper-only crash reviewed")
    assert restarted.ops.inspect()["halted"]
    assert not restarted.ops.inspect()["unresolved_reservations"]
    restarted.ops.resume("ledger verified")
    # Abandoned cycle is skipped and safely re-marked, never retried.
    assert restarted.tick()["status"] == "abandoned"
    assert restarted.ops.ledger_check()["fills"] == 0
    raw = restarted.gateway.connection.execute("SELECT body FROM ops_events").fetchall()
    assert "DO_NOT_LOG_SECRET" not in str(raw)
    restarted.close()


def test_corrupt_ledger_cannot_resume(tmp_path):
    runner, _, _, _ = build(tmp_path)
    runner.tick()
    state = runner._state()
    state["cash"] = "1000"
    runner.gateway.connection.execute("UPDATE shadow_state SET body=? WHERE id=1", (json.dumps(state),))
    with pytest.raises(PermissionError, match="discrepancy"): runner.ops.resume("cannot bypass")
    with pytest.raises(PermissionError): runner.ops.recover("cannot silently repair money")
    assert runner.ops.inspect()["halted"]
    runner.close()


def test_watchdog_detects_process_death_and_requires_resume(tmp_path):
    runner, _, clock, _ = build(tmp_path)
    runner.tick()
    clock.advance(61)
    assert runner.ops.watchdog() == {"armed": True, "healthy": False}
    assert runner.ops.inspect()["halted"]
    with pytest.raises(PermissionError): runner.tick()
    runner.ops.resume("restart and ledger checked")
    assert runner.tick()["status"] == "already_processed"
    runner.close()


@pytest.mark.parametrize("permission", ["modify-trades", "cancel-trades", "withdraw-funds", "deposit-funds", "unknown"])
def test_readonly_rejects_unsafe_or_unknown_permissions(permission):
    info = dict(permissions=["query-funds", "query-open-trades", "query-closed-trades", permission])
    with pytest.raises(PermissionError): check_permissions(info, now=Clock()())


def test_permissions_expiry_and_missing_privileges():
    private = Private(Clock())
    info = dict(permissions=private.permissions, validUntil="1")
    with pytest.raises(PermissionError, match="expired"): check_permissions(info, now=private.clock())
    with pytest.raises(PermissionError): check_permissions(dict(permissions=["query-funds"]), now=private.clock())


def test_account_fee_and_separate_real_mirror(tmp_path):
    clock = Clock()
    private = Private(clock)
    runner, _, _, _ = build(tmp_path, clock, readonly=private.adapter())
    pin_exchange_baseline(runner.ops, runner.readonly, since=START, reason="dedicated mock account")
    event = runner.tick()
    assert float(event["cash"]) == pytest.approx(89.964975)
    assert event["fills"][0]["fee_rate"] == "0.0025"
    clock.advance(20)
    # Paper bought BTC; real exchange still hasUSD100. That is expected in shadow.
    assert runner.tick()["status"] == "already_processed"
    assert runner.reconcile_exchange()["problems"] == []
    private.balances["USD"] = "99"
    with pytest.raises(PermissionError, match="discrepancy"): runner.reconcile_exchange()
    assert runner.ops.inspect()["halted"]
    with pytest.raises(PermissionError): runner.ops.resume("cannot bypass real discrepancy")
    assert "SENTINEL" not in str(runner.ops.inspect())
    runner.close()


@pytest.mark.parametrize("kind", ["orders", "trades", "balances", "stale", "account"])
def test_exchange_discrepancy_detection(kind):
    clock = Clock()
    private = Private(clock)
    expected = private.adapter().snapshot(since=START)
    actual = expected
    if kind == "orders": actual = replace(actual, orders={"unexpected": {"status": "open"}})
    if kind == "trades": actual = replace(actual, trades={"unexpected": {"fee": "1"}})
    if kind == "balances": actual = replace(actual, balances={"USD": "101"})
    if kind == "stale": actual = replace(actual, timestamp=clock() - pd.Timedelta(seconds=61))
    if kind == "account": actual = replace(actual, account_fingerprint="different")
    assert discrepancies(expected, actual, now=clock())


def test_incomplete_trade_pagination_and_private_errors_sanitized():
    private = Private(Clock())
    original = private.request
    def request(endpoint, params):
        if endpoint == "TradesHistory": return dict(error=[], result={"count": 1, "trades": {}})
        return original(endpoint, params)
    with pytest.raises(ValueError, match="Incomplete"):
        ReadOnlyKraken(request, "mock", private.clock).snapshot(since=START)
    def failure(*args): raise RuntimeError("secret-api-key")
    with pytest.raises(RuntimeError) as error:
        ReadOnlyKraken(failure, "mock", private.clock).permissions()
    assert "secret-api-key" not in str(error.value)
    with pytest.raises(PermissionError): private.adapter()._read("CancelAllOrdersAfter")


def test_event_time_quotes_reject_stale_future_and_regression():
    clock = Clock()
    source = Market(clock)
    assert source.quote(KEY).timestamp == clock()
    original = source.response
    for seconds in (-6, 1):
        def response(endpoint, params):
            body = original(endpoint, params)
            body["result"][KEY.symbol][0][0] += seconds
            return body
        source.transport = response
        with pytest.raises(ValueError): source.quote(KEY)
    source.transport = original
    clock.advance(-1)
    with pytest.raises(ValueError, match="regressed"): source.quote(KEY)


def test_synthetic_evidence_never_counts_as_sustained(tmp_path):
    runner, _, clock, _ = build(tmp_path)
    runner.tick()
    for _ in range(10):
        clock.advance(20)
        runner.tick()
    evidence = runner.ops.evidence()
    assert evidence["synthetic_observations"] == 11
    assert evidence["real_observations"] == 0
    assert evidence["reliability_gate_passed"] is False
    assert evidence["live_authorized"] is False
    runner.close()


def test_readonly_env_optin_and_key_separation(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("KRAKEN_READONLY_API_KEY=one\nKRAKEN_READONLY_API_SECRET=two\nKRAKEN_TRADING_API_KEY=one\n")
    import ccxt
    monkeypatch.setattr(ccxt, "kraken", lambda *a: pytest.fail("No network/client before separation check"))
    with pytest.raises(PermissionError, match="separate"):
        readonly_from_env(path, clock=Clock(), nonce=lambda: 1)
    path.write_text("KRAKEN_API_KEY=legacy\nKRAKEN_API_SECRET=legacy\n")
    with pytest.raises(ValueError, match="Dedicated"):
        readonly_from_env(path, clock=Clock(), nonce=lambda: 1)


def test_operator_cli_inspect_halt_recover_resume(tmp_path, capsys):
    runner, _, clock, _ = build(tmp_path)
    runner.tick()
    runner.close()
    common = ["--database", str(tmp_path / "paper.sqlite")]
    assert main(["inspect", *common], clock=clock) == 0
    assert main(["halt", *common, "--reason", "operator drill"], clock=clock) == 0
    assert main(["recover", *common, "--reason", "ledger audit"], clock=clock) == 0
    assert main(["resume", *common, "--reason", "verified"], clock=clock) == 0
    assert main(["evidence", *common], clock=clock) == 0
    assert "live_authorized" in capsys.readouterr().out


def test_persistent_nonce_monotonic_on_clock_rollback(tmp_path):
    runner, _, clock, _ = build(tmp_path)
    first = runner.ops.nonce()
    clock.advance(-10)
    assert runner.ops.nonce() == first+1
    runner.close()
