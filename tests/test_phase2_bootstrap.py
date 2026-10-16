import numpy as np
import pandas as pd
import pytest

from research.phase2.bootstrap import paired_block_bootstrap


def sample():
    rng = np.random.default_rng(22)
    baseline = rng.normal(0, .01, 400)
    return pd.DataFrame({"strategy": baseline + .001, "baseline": baseline},
                        index=pd.date_range("2024-01-01", periods=400, freq="4h", tz="UTC"))


def test_paired_mean_difference_and_reproducibility():
    kwargs = dict(block_lengths=(12, 36, 72, 144), replications=199, seed=4)
    first = paired_block_bootstrap(sample(), **kwargs)
    pd.testing.assert_frame_equal(first, paired_block_bootstrap(sample(), **kwargs))
    differences = first[(first.metric == "annualised_mean") & (first.comparison == "paired_difference")]
    np.testing.assert_allclose(differences.estimate, 2.19)
    np.testing.assert_allclose(differences.ci_low, 2.19)
    np.testing.assert_allclose(differences.ci_high, 2.19)
    assert (differences.centered_null_one_sided_p == 1/200).all()
    assert len(first) == 24
    assert ((first.approximate_mean_ess_strategy > 0) & (first.approximate_mean_ess_strategy <= 400)).all()


def test_identical_strategies_have_zero_paired_difference():
    data = sample()
    data["strategy"] = data.baseline
    result = paired_block_bootstrap(data, block_lengths=(12,), replications=20)
    paired = result[result.comparison == "paired_difference"]
    assert (paired.estimate == 0).all()
    assert (paired.ci_low == 0).all()
    assert (paired.centered_null_one_sided_p == 1).all()


@pytest.mark.parametrize("defect", ["nan", "gap", "duplicate", "naive", "constant"])
def test_invalid_samples_rejected(defect):
    data = sample()
    if defect == "nan":
        data.iloc[0, 0] = np.nan
    elif defect == "gap":
        data = data.drop(data.index[3])
    elif defect == "duplicate":
        data.index = pd.DatetimeIndex([data.index[0], *data.index[:-1]])
    elif defect == "naive":
        data.index = data.index.tz_localize(None)
    else:
        data["strategy"] = 0
    with pytest.raises(ValueError):
        paired_block_bootstrap(data, block_lengths=(12,), replications=10)


def test_default_replication_count_is_1999():
    result = paired_block_bootstrap(sample(), block_lengths=(12,))
    assert (result.replications == 1999).all()


def test_invalid_block_length_rejected():
    with pytest.raises(ValueError):
        paired_block_bootstrap(sample(), block_lengths=(400,))


@pytest.mark.parametrize("kwargs", [dict(replications=True), dict(replications=2.5),
    dict(seed=-1), dict(seed=True), dict(annualisation=float("inf")), dict(block_lengths=()),
    dict(block_lengths=(True,))])
def test_invalid_specifications_rejected(kwargs):
    with pytest.raises(ValueError):
        paired_block_bootstrap(sample(), **kwargs)


@pytest.mark.parametrize("defect", ["ruin", "duplicate_columns", "nat"])
def test_unsafe_return_frames_rejected(defect):
    data = sample()
    if defect == "ruin":
        data.iloc[0, 0] = -1
    elif defect == "duplicate_columns":
        data = pd.concat([data, data.strategy], axis=1)
    else:
        data.index = pd.DatetimeIndex([pd.NaT, *data.index[1:]])
    with pytest.raises(ValueError):
        paired_block_bootstrap(data, block_lengths=(12,), replications=10)
