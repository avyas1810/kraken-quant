import numpy as np
import pandas as pd
import pytest

from research.phase2.frequency_audit import audit


def test_compounded_utc_daily_returns_reconcile_and_are_labeled_inspected():
    index = pd.date_range("2024-01-01", periods=24, freq="4h", tz="UTC")
    one_day = np.array([.01, -.005, .002, .003, -.004, .001])
    returns = np.concatenate([one_day * (1 + .1 * day) for day in range(4)])
    frame = pd.DataFrame({"t": index, "r": returns})
    result = audit(frame, timestamp="t", columns=["r"])
    row = result["results"][0]
    assert row["utc_days"] == 4
    assert row["four_hour_observations"] == 24
    assert row["wealth_reconciliation_abs_error"] < 1e-12
    assert row["status"] == "historical_already_inspected_not_pristine_holdout"
    assert row["risk_free_rate"] == 0


def test_incomplete_utc_day_fails_closed():
    index = pd.date_range("2024-01-01", periods=11, freq="4h", tz="UTC")
    frame = pd.DataFrame({"t": index, "r": .001})
    with pytest.raises(ValueError, match="exactly six"):
        audit(frame, timestamp="t", columns=["r"])
