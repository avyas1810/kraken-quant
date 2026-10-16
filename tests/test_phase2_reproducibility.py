"""Input contracts/reproducibility only; fixtures are explicitly synthetic."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.phase2.common import (REQUIRED_COLUMNS, STUDY_ASSETS, load_returns,
                                    safe_artifact, sha256_file, verify_artifacts)
from research.phase2.run_validation import (_publish, blocked_inventory, load_frozen_engine,
                                           main, verify_replay)


def frame():
    rng = np.random.default_rng(90210)
    result = pd.DataFrame(index=pd.date_range("2024-01-01", periods=300, freq="4h", tz="UTC"))
    for name in REQUIRED_COLUMNS:
        result[name] = rng.normal(.0001, .003, len(result)) if name.endswith("net") or name.endswith("gross") or name == "btc_return" else .2
    result["regime_active"] = np.arange(len(result)) % 2
    return result


def manifest(root):
    values = frame()
    values.rename_axis("timestamp").to_csv(root / "returns.csv", float_format="%.17g")
    (root / "data.csv").write_text("synthetic fixture,not historical\n")
    (root / "engine.py").write_text("# Synthetic contract fixture only\n")
    return dict(schema_version=1, study_status="historical_already_inspected",
        universe=sorted(STUDY_ASSETS), annualisation=2190, cost_per_gross_dollar=.002,
        return_timestamp="bar_end_utc", accounting="exact_self_financing_drift_aware",
        returns_file="returns.csv", engine_file="engine.py", data_files=["data.csv"],
        files={name: sha256_file(root/name) for name in ("returns.csv", "data.csv", "engine.py")})


def test_missing_artifacts_write_only_explicit_blocked_schemas(tmp_path):
    assert main(["--artifact-dir", str(tmp_path/"absent"), "--output", str(tmp_path/"out")]) == 2
    result = json.loads((tmp_path/"out"/"phase2_manifest.json").read_text())
    assert result["historical_results_generated"] is False
    assert result["status"] == "blocked_missing_frozen_phase1_artifacts"
    for name, digest in result["output_hashes"].items():
        path = tmp_path/"out"/name
        assert sha256_file(path) == digest
        assert pd.read_csv(path).empty
    before = {p.name: p.read_bytes() for p in (tmp_path/"out").iterdir()}
    blocked_inventory(tmp_path/"out")
    assert before == {p.name: p.read_bytes() for p in (tmp_path/"out").iterdir()}


def test_artifact_hashes_and_cost_definition_immutable(tmp_path):
    spec = manifest(tmp_path)
    assert verify_artifacts(tmp_path, spec) == spec["files"]
    loaded = load_returns(tmp_path, spec)
    assert len(loaded) == 300
    spec["cost_per_gross_dollar"] = .001
    with pytest.raises(ValueError, match="cost"): verify_artifacts(tmp_path, spec)
    spec["cost_per_gross_dollar"] = .002
    (tmp_path/"data.csv").write_text("changed\n")
    with pytest.raises(ValueError, match="hash mismatch"): verify_artifacts(tmp_path, spec)


def test_synthetic_status_cannot_enter_historical_validation(tmp_path):
    spec = manifest(tmp_path)
    spec["study_status"] = "synthetic_fixture"
    with pytest.raises(ValueError, match="synthetic"): verify_artifacts(tmp_path, spec)


def test_replay_checks_every_column_and_time_alignment():
    expected = frame()
    verify_replay(expected, expected.copy())
    changed = expected.copy()
    changed.iloc[1, 0] += 1e-8
    with pytest.raises(ValueError, match="changed"): verify_replay(expected, changed)
    changed = expected.copy()
    changed.index += pd.Timedelta(hours=4)
    with pytest.raises(ValueError, match="index"): verify_replay(expected, changed)


@pytest.mark.parametrize("name", ["../outside.csv", ".env", "private.pem", "a.pickle"])
def test_no_sensitive_or_outside_artifact_access(tmp_path, name):
    with pytest.raises(ValueError): safe_artifact(tmp_path, name)


def test_output_refuses_overwrite_infinite_or_unexplained_nan(tmp_path):
    _publish(tmp_path/"out", {"t.csv": pd.DataFrame({"value": [1.]})}, {"seed": 1})
    with pytest.raises(FileExistsError):
        _publish(tmp_path/"out", {"t.csv": pd.DataFrame({"value": [2.]})}, {"seed": 1})
    with pytest.raises(ValueError, match="Infinite"):
        _publish(tmp_path/"bad", {"t.csv": pd.DataFrame({"value": [np.inf]})}, {})
    with pytest.raises(ValueError, match="Unexplained"):
        _publish(tmp_path/"bad", {"t.csv": pd.DataFrame({"value": [np.nan]})}, {})
    _publish(tmp_path/"labelled", {"t.csv": pd.DataFrame({"value": [None], "status": ["undefined_constant"]})}, {})


def test_returns_missing_dates_or_values_rejected(tmp_path):
    spec = manifest(tmp_path)
    data = pd.read_csv(tmp_path/"returns.csv").drop(index=1)
    data.to_csv(tmp_path/"returns.csv", index=False)
    with pytest.raises(ValueError, match="four-hour"): load_returns(tmp_path, spec)
    data = frame()
    data.iloc[0, 0] = np.nan
    data.rename_axis("timestamp").to_csv(tmp_path/"returns.csv")
    with pytest.raises(ValueError, match="nonfinite"): load_returns(tmp_path, spec)


def test_optional_gross_and_explicit_timezone_contract(tmp_path):
    spec = manifest(tmp_path)
    data = pd.read_csv(tmp_path/"returns.csv").drop(columns="strategy_gross")
    data.to_csv(tmp_path/"returns.csv", index=False)
    assert "strategy_gross" not in load_returns(tmp_path, spec)
    data["timestamp"] = pd.to_datetime(data.timestamp).dt.tz_localize(None).astype(str)
    data.to_csv(tmp_path/"returns.csv", index=False)
    with pytest.raises(ValueError, match="Naive"): load_returns(tmp_path, spec)
