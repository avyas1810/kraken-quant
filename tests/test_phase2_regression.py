import numpy as np
import pandas as pd
import pytest
from scipy.stats import t

from research.phase2.btc_regression import btc_hac_regression


def sample():
    rng = np.random.default_rng(41)
    btc = rng.normal(0, .01, 1000)
    residual = rng.normal(0, .002, 1000)
    for i in range(1, len(residual)):
        residual[i] += .5 * residual[i - 1]
    return pd.DataFrame({"btc": btc, "strategy_net": .0001 + .3 * btc + residual},
                        index=pd.date_range("2024-01-01", periods=1000, freq="4h", tz="UTC"))


def test_hac_against_independent_matrix_calculation():
    data = sample()
    result = btc_hac_regression(data, lags=(12,)).iloc[0]
    x = np.column_stack([np.ones(len(data)), data.btc.to_numpy()])
    y = data.strategy_net.to_numpy()
    inv = np.linalg.inv(x.T @ x)
    coeff = inv @ x.T @ y
    residual = y - x @ coeff
    scores = x * residual[:, None]
    meat = scores.T @ scores
    for lag in range(1, 13):
        cross = scores[lag:].T @ scores[:-lag]
        meat += (1 - lag / 13) * (cross + cross.T)
    covariance = inv @ meat @ inv * len(y) / (len(y) - 2)
    se = np.sqrt(covariance[0, 0])
    assert result.alpha_per_period == pytest.approx(coeff[0])
    assert result.beta == pytest.approx(coeff[1])
    assert result.alpha_standard_error == pytest.approx(se)
    bound = t.ppf(.975, len(y) - 2) * se
    assert result.alpha_annual_ci_low == pytest.approx((coeff[0] - bound) * 2190)
    assert result.alpha_annual_ci_high == pytest.approx((coeff[0] + bound) * 2190)


def test_all_lags_identical_point_estimates_and_reproducibility():
    first = btc_hac_regression(sample())
    pd.testing.assert_frame_equal(first, btc_hac_regression(sample()))
    assert first.hac_lag.tolist() == [12, 36, 72, 144]
    assert first.alpha_per_period.nunique() == 1
    assert first.beta.nunique() == 1
    assert first.alpha_standard_error.nunique() == 4
    assert (first.residual_ljung_box_pvalue < .01).all()


@pytest.mark.parametrize("defect", ["naive", "missing", "duplicate", "nan", "constant", "bankrupt"])
def test_invalid_alignment_and_values(defect):
    data = sample()
    if defect == "naive":
        data.index = data.index.tz_localize(None)
    elif defect == "missing":
        data = data.drop(data.index[8])
    elif defect == "duplicate":
        data = pd.concat([data.iloc[:1], data])
    elif defect == "nan":
        data.iloc[1, 0] = np.nan
    elif defect == "constant":
        data["btc"] = 0
    else:
        data.iloc[1, 0] = -1
    with pytest.raises(ValueError):
        btc_hac_regression(data)


def test_reject_underpowered_lag_and_perfect_fit():
    with pytest.raises(ValueError):
        btc_hac_regression(sample().iloc[:100])
    data = sample()
    data["strategy_net"] = .3 * data.btc
    with pytest.raises(ValueError):
        btc_hac_regression(data)
