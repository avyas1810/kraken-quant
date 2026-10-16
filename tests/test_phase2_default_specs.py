"""Full Monte Carlo budgets on synthetic contract fixtures, never market data."""

import numpy as np
import pandas as pd

from research.phase2.bootstrap import paired_block_bootstrap
from research.phase2.placebo import run_placebos


def test_default_bootstrap_budget_and_all_requested_blocks():
    rng = np.random.default_rng(103)
    data = pd.DataFrame(rng.normal(0, .005, (180, 2)), columns=["strategy", "baseline"],
                        index=pd.date_range("2020", periods=180, freq="4h", tz="UTC"))
    result = paired_block_bootstrap(data)
    assert set(result.block_length) == {12, 36, 72, 144}
    assert result.replications.eq(1999).all()
    assert np.isfinite(result.select_dtypes(include="number")).all().all()


def test_default_placebo_budget_both_methods_and_fixed_accounting_callback():
    index = pd.date_range("2020", periods=180, freq="4h", tz="UTC")
    mask = pd.Series(([1, 1, 0, 1, 0, 0] * 30), index=index).astype(bool)
    # Synthetic contract for computational verification only. It is never
    # accepted as the absent historical Phase 1 portfolio engine.
    signal = np.random.default_rng(104).normal(.0001, .004, len(index))
    def callback(candidate):
        active = candidate.to_numpy(dtype=float)
        turnover = np.abs(np.diff(np.r_[0, active]))
        return pd.DataFrame(dict(net_return=active*signal-.002*turnover,
                                 gross_exposure=active, turnover=turnover), index=index)
    result = run_placebos(mask, callback)
    assert len(result["trials"]) == 1998
    assert result["balance_diagnostics"].trials.eq(999).all()
    assert result["trials"].active_fraction.eq(mask.mean()).all()
