import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from kraken_quant.candidate import freeze_candidate, qualify_candidate
from kraken_quant.evidence import bind_candidate, candidate_pipeline, qualification_evidence
from kraken_quant.operational_shadow import OperationalShadowRunner
from kraken_quant.ops_cli import main as ops_main
from kraken_quant.operations import Operations
from kraken_quant.paper_cli import ReplayMarketData, replay
from kraken_quant.strategy import Instrument


KEY = Instrument("kraken", "BTC/USD", "BTC", "USD")


def candidate(tmp_path):
    source = Path(__file__).resolve().parents[1]/"examples/custom_strategy_example.py"
    spec = dict(strategy="examples.custom_strategy_example:backtest_factory",
        parameters=dict(lookback=3, weight=.05), features="Causal daily momentum", costs=dict(fee_rate=.004),
        risk_limits=dict(max_order=20, max_asset=40, max_total=100, max_daily_loss=2),
        input_hashes={"fixture": "a"*64}, code_revision="software-test-fixture",
        prospective_start="2099-01-01T00:00:00Z", universe=["BTC/USD"],
        source_code_hashes={"examples/custom_strategy_example.py": hashlib.sha256(source.read_bytes()).hexdigest()})
    path = tmp_path/"candidate.json"
    return path, freeze_candidate(path, spec)


def test_hash_bound_custom_factory_and_start_guard(tmp_path):
    path, record = candidate(tmp_path)
    with pytest.raises(PermissionError, match="not started"):
        candidate_pipeline(path, now=pd.Timestamp("2026-10-16T00:00:00Z"))
    pipeline, binding = candidate_pipeline(path, now=pd.Timestamp("2099-01-02T00:00:00Z"))
    assert binding["candidate_sha256"] == record["sha256"]
    assert pipeline.strategy.lookback == 3
    assert pipeline.strategy.weight == .05


def test_frozen_replay_evidence_has_zero_prospective_decisions(tmp_path):
    path, record = candidate(tmp_path)
    index = pd.date_range("2099-01-01", periods=10, tz="UTC")
    prices = [100+i/10 for i in range(10)]
    frame = pd.DataFrame(dict(open=prices, high=[p+1 for p in prices], low=[p-1 for p in prices],
                              close=prices, volume=100), index=index)
    db = tmp_path/"candidate.sqlite"
    result = replay({KEY:frame}, db, candidate=path)
    assert result["ledger"]["fills"] > 0
    observed = [pd.Timestamp("2099-01-11T00:00:00Z")]
    ops = Operations(db, clock=lambda: observed[0])
    try:
        # A mislabelled fixture sample still cannot turn a replay into actual
        # prospective observations. Resume also cannot erase protocol faults.
        for stamp, raw in ops.db.execute("SELECT timestamp,body FROM ops_samples").fetchall():
            sample = json.loads(raw)
            sample["source"] = "kraken_public"
            ops.db.execute("UPDATE ops_samples SET body=? WHERE timestamp=?", (json.dumps(sample), stamp))
        ops.halt("synthetic fault drill")
        observed[0] += pd.Timedelta(seconds=1)
        ops.resume("synthetic reviewed recovery")
        assert ops.evidence()["fault_events"] == 0
    finally:
        ops.close()
    output = tmp_path/"evidence.json"
    qualification_evidence(db, path, output)
    evidence = json.loads(output.read_text())
    assert evidence["source"] == "historical_replay"
    assert evidence["actual_daily_decisions"] == 0
    assert evidence["completed_lifecycle_ids"] == []
    assert evidence["candidate_sha256"] == record["sha256"]
    assert not qualify_candidate(path, evidence)["qualified_for_review"]
    assert not evidence["independent_review_passed"]
    assert evidence["fault_events"] == 1
    assert output.with_suffix(".equity.csv").exists()


def test_paper_candidate_refuses_strategy_override_before_network(tmp_path):
    path, _ = candidate(tmp_path)
    with pytest.raises(SystemExit):
        ops_main(["once", "--database", str(tmp_path/"db.sqlite"), "--start", "2026-10-01T00:00:00Z",
                  "--candidate", str(path), "--strategy", "examples.custom_strategy_example:paper_factory"])


def test_mock_public_evidence_derives_metrics_and_missing_protocol_days(tmp_path):
    """Injected future-clock fixture; qualification rejects it as real evidence."""
    path, record = candidate(tmp_path)
    index = pd.date_range("2099-01-01", periods=10, tz="UTC")
    frame = pd.DataFrame(dict(open=100, high=101, low=99, close=100, volume=10), index=index)
    now = [pd.Timestamp("2099-01-05T00:00:01Z")]
    clock = lambda: now[0]
    pipeline, binding = candidate_pipeline(path, now=clock())
    source = ReplayMarketData({KEY:frame}, clock)
    source.observation_source = "kraken_public"  # Explicit test-only classification fixture.
    db = tmp_path/"mock-public.sqlite"
    runner = OperationalShadowRunner(pipeline, source, db, instruments=[KEY],
        start=index[0], strategy_id=record["sha256"], clock=clock)
    try:
        bind_candidate(runner.ops, binding)
        runner.tick()
        runner.ops.set_value("watchdog", {"armed":False})
        now[0] += pd.Timedelta(days=2)
        runner.tick()
    finally:
        runner.close()
    output = tmp_path/"public-fixture.json"
    qualification_evidence(db, path, output)
    body = json.loads(output.read_text())
    assert body["actual_daily_decisions"] == 2
    assert body["missing_days"] == 5
    assert body["heartbeat_gaps"] == 1
    assert body["net_return"] == 0
    assert body["max_drawdown"] == 0
    assert body["benchmark_relative_return"] > 0  # Flat avoids benchmark round-trip fees/spread.
    assert not body["reliability_passed"]
    assert not qualify_candidate(path, body)["qualified_for_review"]
    bad_report = tmp_path/"unbound-statistics.json"
    bad_report.write_text('{"schema":1,"source_sha256":"wrong"}')
    with pytest.raises(PermissionError, match="does not match"):
        qualification_evidence(db, path, tmp_path/"bad.json", validation_report=bad_report)
