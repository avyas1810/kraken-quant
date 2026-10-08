"""Mocked public responses and synthetic paper accounting only."""

import builtins
import json
import socket
import io
import os

import pandas as pd
import pytest

from kraken_quant.market_data import KrakenMarketData, public_get
from kraken_quant.shadow import ShadowRunner
from kraken_quant.shadow_cli import main
from kraken_quant.strategy import Instrument, StrategyPipeline


KEY = Instrument("kraken", "BTC/USD", "BTC", "USD")
NOW = pd.Timestamp("2026-10-03T00:00:02Z")
START = pd.Timestamp("2026-10-01T00:00:00Z")


def response(endpoint, params):
    assert params["pair"] == KEY.symbol and params["assetVersion"] == 1
    if endpoint == "OHLC":
        assert params["since"] == int(START.timestamp()) and params["interval"] == 1440
        row = [[int(t.timestamp()), "100", "102", "98", "100", "100", "10", 2]
               for t in pd.date_range(START, NOW.normalize(), freq="D")]
    elif endpoint == "Ticker":
        row = {"b": ["100", "1", "1"], "a": ["100.1", "1", "1"]}
    elif endpoint == "AssetPairs":
        row = dict(base="BTC", quote="USD", lot_decimals=3, tick_size="0.1",
                   ordermin="0.001", costmin="0.1", status="online")
    else:
        pytest.fail("Unexpected endpoint")
    return {"error": [], "result": {KEY.symbol: row}}


def market(transport=response):
    return KrakenMarketData(transport, clock=lambda: NOW)


class Weight:
    def __init__(self, value=.1):
        self.value = value

    def target_weights(self, context):
        assert all((f.index + pd.Timedelta(days=1) <= context.timestamp).all()
                   for f in context.historical_data.values())
        return {KEY: self.value}


def runner(path, strategy=None, source=None):
    return ShadowRunner(StrategyPipeline(strategy or Weight()), source or market(), path,
        instruments=[KEY], start=START, strategy_id="fixture-v1", clock=lambda: NOW, fee_rate="0.01")


def test_market_completed_bars_quote_rules():
    adapter = market()
    frame = adapter.bars(KEY, start=START, end=NOW.normalize() - pd.Timedelta(days=1))
    assert len(frame) == 2 and frame.iloc[-1]["data_type"] == "ohlcv"
    assert frame.iloc[0]["venue"] == "kraken"
    assert adapter.quote(KEY).timestamp == NOW
    assert str(adapter.rules(KEY).quantity_step) == "0.001"


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "nan", "negative", "ohlc", "wrong", "error", "empty"])
def test_market_rejects_invalid_responses(mutation):
    def transport(endpoint, params):
        body = response(endpoint, params)
        rows = body["result"][KEY.symbol]
        if mutation == "duplicate": rows.insert(0, rows[0].copy())
        if mutation == "missing": rows.pop(0)
        if mutation == "nan": rows[0][4] = "nan"
        if mutation == "negative": rows[0][6] = "-1"
        if mutation == "ohlc": rows[0][2] = "90"
        if mutation == "wrong": body["result"] = {"ETH/USD": rows}
        if mutation == "error": body["error"] = ["fixture error"]
        if mutation == "empty": body["result"][KEY.symbol] = []
        return body
    with pytest.raises(ValueError):
        market(transport).bars(KEY, start=START, end=NOW.normalize() - pd.Timedelta(days=1))


def test_locked_history_rejected_before_transport():
    def forbidden(*args): pytest.fail("No request allowed")
    with pytest.raises(PermissionError):
        market(forbidden).bars(KEY, start=pd.Timestamp("2025-01-01T00:00:00Z"), end=START)


def test_latency_and_unfinished_rejected():
    ticks = iter([NOW, NOW + pd.Timedelta(seconds=6)])
    with pytest.raises(ValueError, match="latency"):
        KrakenMarketData(response, clock=lambda: next(ticks)).quote(KEY)
    with pytest.raises(ValueError, match="unfinished"):
        market().bars(KEY, start=START, end=NOW.normalize())


def test_private_endpoint_has_no_transport():
    with pytest.raises(PermissionError): public_get("AddOrder", {})


def test_paper_hand_accounting_restart_and_no_submission(tmp_path):
    path = tmp_path / "paper.sqlite"
    instance = runner(path)
    event = instance.step()
    # 10% of $100 at completed close100 = .1 BTC; ask100.1, fee1%.
    assert float(event["cash"]) == pytest.approx(89.8899)
    assert float(event["equity"]) == pytest.approx(99.8899)
    assert float(event["fills"][0]["quantity"]) == .1
    assert instance.gateway.connection.execute("SELECT status FROM reservations").fetchone() == ("terminal",)
    with pytest.raises(PermissionError): instance.submit()
    instance.close()
    reopened = runner(path)
    assert reopened.step()["status"] == "already_processed"
    assert len(reopened.gateway.connection.execute("SELECT * FROM shadow_fills").fetchall()) == 1
    reopened.close()


@pytest.mark.parametrize("weight", [-.1, 1.1, .3])
def test_shadow_rejects_short_leverage_or_order_cap(tmp_path, weight):
    instance = runner(tmp_path / "paper.sqlite", Weight(weight))
    with pytest.raises(ValueError): instance.step()
    assert instance._state()["cash"] == "100"
    with pytest.raises(PermissionError, match="Incomplete"): instance.step()
    instance.close()


def test_kill_and_crash_halt(tmp_path):
    path = tmp_path / "paper.sqlite"
    instance = runner(path)
    instance.gateway.kill()
    with pytest.raises(PermissionError, match="kill"): instance.step()
    instance.close()
    instance = runner(path)
    with pytest.raises(PermissionError, match="kill"): instance.step()
    instance.close()


def test_identity_change_rejected(tmp_path):
    path = tmp_path / "paper.sqlite"
    instance = runner(path)
    instance.close()
    with pytest.raises(ValueError, match="configuration"):
        ShadowRunner(StrategyPipeline(Weight()), market(), path, instruments=[KEY],
                     start=START, strategy_id="different")


def test_crash_after_reservation_halts_and_preserves_balance(tmp_path, monkeypatch):
    path = tmp_path / "crash.sqlite"
    instance = runner(path)
    def crash(*args): raise RuntimeError("simulated crash")
    monkeypatch.setattr(instance.gateway, "release_after_reconciliation", crash)
    with pytest.raises(RuntimeError): instance.step()
    assert instance._state()["cash"] == "100"
    assert instance.gateway.connection.execute("SELECT status FROM reservations").fetchone()[0] == "reserved"
    instance.close()
    instance = runner(path)
    with pytest.raises(PermissionError, match="Incomplete"): instance.step()
    instance.close()


def test_stale_quote_halts_before_accounting(tmp_path):
    source = market()
    from dataclasses import replace
    original = source.quote
    source.quote = lambda key: replace(original(key), timestamp=NOW - pd.Timedelta(seconds=6))
    instance = runner(tmp_path / "stale.sqlite", source=source)
    with pytest.raises(ValueError, match="Stale"): instance.step()
    assert instance._state()["cash"] == "100"
    instance.close()


def test_held_positions_day_boundary_halts(tmp_path):
    instance = runner(tmp_path / "day.sqlite")
    instance.step()
    next_day = NOW + pd.Timedelta(days=1)
    instance.clock = lambda: next_day
    instance.market_data = KrakenMarketData(response, clock=lambda: next_day)
    def bars(key, **kwargs):
        frame = market().bars(key, start=START, end=NOW.normalize() - pd.Timedelta(days=1))
        frame.loc[NOW.normalize()] = frame.iloc[-1]
        return frame
    instance.market_data.bars = bars
    with pytest.raises(PermissionError, match="baseline"): instance.step()
    instance.close()


def test_shadow_cli_no_network_or_credentials(tmp_path, monkeypatch, capsys):
    original = builtins.open
    original_io = io.open
    original_getenv = os.getenv
    def check(file):
        assert str(file).replace("\\", "/").split("/")[-1] not in (".env", "settings.yml")
    def guarded(file, *args, **kwargs):
        check(file)
        return original(file, *args, **kwargs)
    def guarded_io(file, *args, **kwargs):
        check(file)
        return original_io(file, *args, **kwargs)
    def guarded_env(key, *args):
        assert not any(word in key.upper() for word in ("KRAKEN", "SECRET", "API_KEY"))
        return original_getenv(key, *args)
    def forbidden(*args, **kwargs): pytest.fail("Network access")
    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(io, "open", guarded_io)
    monkeypatch.setattr(os, "getenv", guarded_env)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    main(["--database", str(tmp_path / "cli.sqlite"), "--start", START.isoformat(),
          "--strategy-id", "flat-v1"], market_data=market(), clock=lambda: NOW)
    event = json.loads(capsys.readouterr().out)
    assert event["mode"] == "shadow" and event["fills"] == []


def test_slow_features_reject_stale_execution(tmp_path):
    ticks = [NOW]
    def features(data, timestamp):
        ticks[0] = NOW + pd.Timedelta(seconds=6)
    instance = ShadowRunner(StrategyPipeline(Weight(), features), market(), tmp_path / "slow.sqlite",
        instruments=[KEY], start=START, strategy_id="slow", clock=lambda: ticks[0])
    with pytest.raises(ValueError, match="Stale"): instance.step()
    assert instance._state()["cash"] == "100"
    instance.close()
