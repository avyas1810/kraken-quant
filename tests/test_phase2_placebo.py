import numpy as np
import pandas as pd
import pytest

from research.phase2.placebo import clustered_mask, run_placebos


def mask_fixture():
    return pd.Series([1, 1, 0, 1, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0],
                     index=pd.date_range("2020-01-01", periods=16, freq="4h", tz="UTC"))


def runs(mask):
    values = mask.to_numpy(dtype=bool)
    starts = np.r_[0, np.flatnonzero(values[1:] != values[:-1]) + 1]
    lengths = np.diff(np.r_[starts, len(mask)])
    return {state: sorted(lengths[values[starts] == state]) for state in (False, True)}


def synthetic_contract(mask):
    # Contract fixture only, never a historical accounting replacement.
    signal = np.linspace(-0.02, 0.03, len(mask))
    active = mask.to_numpy(dtype=float)
    turnover = np.abs(np.diff(np.r_[0, active]))
    return pd.DataFrame({"net_return": signal * active - .002 * turnover,
                         "gross_exposure": active, "turnover": turnover}, index=mask.index)


def test_run_lengths_active_count_and_determinism():
    original = mask_fixture()
    a = clustered_mask(original, np.random.default_rng(7))
    b = clustered_mask(original, np.random.default_rng(7))
    pd.testing.assert_series_equal(a, b)
    assert a.sum() == original.sum()
    assert runs(a) == runs(original)


def test_trial_outputs_balance_and_finite_sample_probability():
    original = mask_fixture()
    original_copy = original.copy()
    result = run_placebos(original, synthetic_contract, trials=25, seed=1)
    pd.testing.assert_series_equal(original, original_copy)
    assert len(result["trials"]) == 50
    assert result["trials"].active_fraction.eq(original.mean()).all()
    assert result["summary"].p_one_sided.between(1 / 26, 1).all()
    assert result["balance_diagnostics"].trials.eq(25).all()
    second = run_placebos(original, synthetic_contract, trials=25, seed=1)
    pd.testing.assert_frame_equal(result["trials"], second["trials"])


def test_alignment_and_nonfinite_rejected():
    with pytest.raises(ValueError, match="align"):
        run_placebos(mask_fixture(), lambda x: synthetic_contract(x).iloc[1:], trials=1)
    def bad(mask):
        result = synthetic_contract(mask)
        result.iloc[0, 0] = np.nan
        return result
    with pytest.raises(ValueError, match="finite"):
        run_placebos(mask_fixture(), bad, trials=1)


@pytest.mark.parametrize("values", [[1, 1, 1], [0, 0, 0], [1, 2, 0], [1, None, 0]])
def test_invalid_filter_rejected(values):
    with pytest.raises(ValueError):
        run_placebos(pd.Series(values, index=pd.date_range("2020", periods=3)), synthetic_contract, trials=1)


def test_default_trial_count_is_999():
    import inspect
    assert inspect.signature(run_placebos).parameters["trials"].default == 999


def test_metrics_match_independent_hand_example():
    from research.phase2.placebo import _evaluate
    mask = pd.Series([True, False, True], index=pd.date_range("2020", periods=3, freq="4h", tz="UTC"))
    def engine(candidate):
        return pd.DataFrame({"net_return": [.1, -.1, .1],
                             "gross_exposure": [1., 0., 1.],
                             "turnover": [1., 1., 1.]}, index=candidate.index)
    result = _evaluate(engine, mask, 1)
    assert result["cumulative_return"] == pytest.approx(.089)
    assert result["max_drawdown"] == pytest.approx(-.1)
    assert result["net_sharpe"] == pytest.approx((.1 / 3) / np.sqrt(.04 / 3))
    assert result["turnover"] == 3
    assert result["gross_exposure"] == pytest.approx(2 / 3)


def test_non_utc_and_irregular_timestamps_rejected():
    original = mask_fixture()
    naive = original.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(ValueError, match="UTC"):
        run_placebos(naive, synthetic_contract, trials=1)
    with pytest.raises(ValueError, match="four-hour"):
        run_placebos(original.drop(original.index[2]), synthetic_contract, trials=1)


def test_undefined_balance_rejected_without_infinity():
    from research.phase2.placebo import _relative_difference
    with pytest.raises(ValueError, match="undefined"):
        _relative_difference(1, 0)
    assert _relative_difference(0, 0) == 0
