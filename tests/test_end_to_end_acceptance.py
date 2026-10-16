"""Independent process-boundary acceptance; generated fixtures are not findings."""

import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd

from kraken_quant.config import CONFIG


ROOT = Path(__file__).resolve().parents[1]


def command(*arguments, expected=0):
    result = subprocess.run([sys.executable, *arguments], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == expected, result.stdout + result.stderr
    return result.stdout


def daily_fixture(directory, start, days):
    index = pd.date_range(start, periods=days, tz="UTC")
    for symbol in CONFIG.universe:
        price = np.linspace(100, 110, days)
        pd.DataFrame(dict(timestamp=index, open=price, high=price+1, low=price-1,
                          close=price, volume=1000)).to_csv(directory/(symbol.replace("/", "_")+".csv"), index=False)


def test_custom_strategy_research_and_persistent_paper_via_real_clis(tmp_path):
    training = tmp_path/"training"
    training.mkdir()
    daily_fixture(training, "2022-01-01", 90)
    output = tmp_path/"backtest"
    command("-m", "kraken_quant.platform_cli", "backtest", "--data-dir", str(training),
            "--factory", "examples.custom_strategy_example:backtest_factory", "--start", "2022-01-01",
            "--end", "2022-03-31", "--output", str(output))
    trades = pd.read_csv(output/"trades.csv")
    assert len(trades) > 0 and trades.fee.sum() > 0
    assert json.loads((output/"manifest.json").read_text())["test_access"] == "locked"
    replay_data = tmp_path/"replay"
    replay_data.mkdir()
    daily_fixture(replay_data, "2026-10-01", 14)
    db = tmp_path/"paper.sqlite"
    args = ("-m", "kraken_quant.paper_cli", "replay", "--bars", f"BTC/USD={replay_data/'BTC_USD.csv'}",
            "--database", str(db), "--strategy", "examples.custom_strategy_example:paper_demo_factory",
            "--strategy-id", "independent-software-fixture")
    first = json.loads(command(*args))
    restarted = json.loads(command(*args))
    assert first["ledger"]["fills"] > 0
    assert restarted["ledger"] == first["ledger"]
    assert restarted["decisions"] == 0
    export = tmp_path/"paper.json"
    command("-m", "kraken_quant.paper_cli", "export", "--database", str(db), "--output", str(export))
    paper = json.loads(export.read_text())
    assert float(paper["total_fees"]) > 0
    assert paper["evidence"]["real_observations"] == 0
    assert not paper["evidence"]["reliability_gate_passed"]


def test_live_simulation_subprocess_recovery_and_default_disable(tmp_path):
    database = tmp_path/"live.sqlite"
    output = json.loads(command("-m", "kraken_quant.live_cli", "simulate", "--database", str(database)))
    assert output["real_order_submissions"] == 0
    assert output["simulated_submissions"] == 2
    assert output["fills"] == 2
    assert {row["state"] for row in output["orders"]} == {"closed", "canceled"}
    killed = json.loads(command("-m", "kraken_quant.live_cli", "kill", "--database", str(database)))
    assert killed["halted"]
    inspected = json.loads(command("-m", "kraken_quant.live_cli", "inspect", "--database", str(database)))
    assert inspected["halted"] and inspected["fills"] == 2
    blocked = json.loads(command("-m", "kraken_quant.live_cli", "submit", "--database", str(database), expected=1))
    assert blocked["error_type"] == "PermissionError"
