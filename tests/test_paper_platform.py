import json

import pandas as pd
import pytest

from kraken_quant.paper_cli import ReplayMarketData, export_ledger, replay, main
from kraken_quant.config import CONFIG
from kraken_quant.operational_shadow import OperationalShadowRunner
from kraken_quant.shadow_cli import FlatStrategy
from kraken_quant.strategy import Instrument, StrategyPipeline

KEY = Instrument("kraken", "BTC/USD", "BTC", "USD")


def frames():
    index = pd.date_range("2026-10-01", periods=5, tz="UTC")
    return {KEY: pd.DataFrame(dict(open=[100]*5, high=[102]*5, low=[98]*5,
                                  close=[100]*5, volume=[10]*5), index=index)}


def test_replay_persistent_restart_export_never_prospective(tmp_path):
    db = tmp_path / "paper.sqlite"
    first = replay(frames(), db)
    assert first["decisions"] == 4
    assert not first["evidence"]["reliability_gate_passed"]
    assert first["evidence"]["real_observations"] == 0
    again = replay(frames(), db)
    assert again["ledger"] == first["ledger"]
    output = tmp_path / "paper.json"
    export_ledger(db, output)
    body = json.loads(output.read_text())
    assert len(body["equity"]) == 4
    assert float(body["marked_pnl"]) == 0
    assert body["replay_manifest"]["prospective"] is False
    with pytest.raises(FileExistsError): export_ledger(db, output)


def test_replay_frozen_input_change_rejected(tmp_path):
    db = tmp_path / "paper.sqlite"
    replay(frames(), db)
    changed = frames()
    changed[KEY].iloc[0, 4] = 20
    with pytest.raises(PermissionError, match="changed"): replay(changed, db)


def test_replay_causal_history_and_next_open():
    history = frames()
    now = pd.Timestamp("2026-10-03T00:00:01Z")
    market = ReplayMarketData(history, lambda: now)
    bars = market.bars(KEY, start=history[KEY].index[0], end=now.normalize()-pd.Timedelta(days=1))
    assert len(bars) == 2
    assert market.quote(KEY).bid < 100 < market.quote(KEY).ask
    with pytest.raises(ValueError): market.bars(KEY, start=history[KEY].index[0], end=now.normalize())


def test_replay_cli_and_export(tmp_path, capsys):
    csv = tmp_path / "bars.csv"
    frames()[KEY].rename_axis("timestamp").to_csv(csv)
    db = tmp_path / "paper.sqlite"
    assert main(["replay", "--bars", f"BTC/USD={csv}", "--database", str(db)]) == 0
    assert main(["export", "--database", str(db), "--output", str(tmp_path/"out.json")]) == 0
    backup = tmp_path/"backup.sqlite"
    assert main(["backup", "--database", str(db), "--output", str(backup)]) == 0
    assert main(["restore", "--database", str(backup), "--output", str(tmp_path/"restored.sqlite")]) == 0
    assert main(["health", "--database", str(db)]) == 1
    assert "historical_replay" in capsys.readouterr().out


class Alternating:
    def target_weights(self, context):
        return {KEY: .1 if context.timestamp.day % 2 == 0 else 0}


def alternating_pipeline():
    return StrategyPipeline(Alternating())


def test_actual_strategy_risk_fills_fees_pnl_and_restart(tmp_path):
    db = tmp_path / "active.sqlite"
    result = replay(frames(), db, strategy="test_paper_platform:alternating_pipeline", strategy_id="alternating-fixture")
    assert result["ledger"]["fills"] == 4
    path = tmp_path/"active.json"
    export_ledger(db, path)
    body = json.loads(path.read_text())
    assert [fill["side"] for fill in body["fills"]] == ["buy", "sell", "buy", "sell"]
    assert float(body["total_fees"]) > 0
    assert float(body["marked_pnl"]) < 0
    assert all(order["status"] == "terminal" for order in body["orders"])
    restarted = replay(frames(), db, strategy="test_paper_platform:alternating_pipeline", strategy_id="alternating-fixture")
    assert restarted["decisions"] == 0
    assert restarted["ledger"] == result["ledger"]


def test_slow_multiasset_metadata_cannot_age_execution_quotes(tmp_path):
    now = [pd.Timestamp("2026-10-03T00:00:01Z")]
    clock = lambda: now[0]
    history = {Instrument("kraken", symbol, *symbol.split("/")): frames()[KEY].copy()
               for symbol in CONFIG.universe}
    class SlowMetadata(ReplayMarketData):
        def rules(self, instrument):
            now[0] += pd.Timedelta(seconds=3)
            return super().rules(instrument)
    source = SlowMetadata(history, clock)
    runner = OperationalShadowRunner(StrategyPipeline(FlatStrategy()), source, tmp_path/"slow.sqlite",
        instruments=list(history), start="2026-10-01T00:00:00Z", strategy_id="slow-metadata-fixture", clock=clock)
    try:
        assert runner.tick()["status"] == "processed"
        assert now[0] == pd.Timestamp("2026-10-03T00:00:16Z")
        assert not runner.ops.inspect()["halted"]
    finally:
        runner.close()
