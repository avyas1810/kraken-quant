import math

import numpy as np
import pytest
from scipy.integrate import quad
from scipy.stats import norm

from research.phase2.dsr import (deflated_sharpe, expected_maximum_sharpe,
                                probabilistic_sharpe, return_moments)


def test_psr_independent_normal_formula():
    sr, n = .08, 1001
    expected = .5 * (1 + math.erf(sr * math.sqrt(n - 1) / math.sqrt(2 * (1 + sr * sr / 2))))
    assert probabilistic_sharpe(sr, 0, n, 0, 3) == pytest.approx(expected)
    assert probabilistic_sharpe(.1, .1, n, 0, 3) == .5


def test_psr_nonnormal_direct_formula():
    sr, benchmark, skew, kurtosis, n = .14, .04, 1.3, 8.2, 300
    variance = 1 - skew * sr + (kurtosis - 1) * sr ** 2 / 4
    z = (sr - benchmark) * math.sqrt(n - 1) / math.sqrt(variance)
    expected = .5 * (1 + math.erf(z / math.sqrt(2)))
    assert probabilistic_sharpe(sr, benchmark, n, skew, kurtosis) == pytest.approx(expected)


def test_expected_max_against_independent_numerical_integral():
    # Quantile approximation is not exact: document its approximation error.
    for n in (2, 10, 25, 50, 100):
        exact = quad(lambda x: x * n * norm.pdf(x) * norm.cdf(x) ** (n - 1), -12, 12)[0]
        assert abs(expected_maximum_sharpe(n, 1) - exact) < .06
    assert expected_maximum_sharpe(1, .2, .05) == .05


def test_trial_sensitivity_and_sharpe_monotonicity():
    returns = np.random.default_rng(11).normal(.001, .01, 2000)
    rows = deflated_sharpe(returns, .03)
    assert [r["trials"] for r in rows] == [2, 10, 25, 50, 100]
    probabilities = [r["dsr_probability"] for r in rows]
    assert all(a > b for a, b in zip(probabilities, probabilities[1:]))
    probs = [probabilistic_sharpe(x, .01, 200, 0, 3) for x in (-.1, 0, .1, .2)]
    assert probs == sorted(probs)
    assert deflated_sharpe(returns, .03) == rows


def test_moments_independent_example_and_extreme_nonnormality():
    values = np.array([-.02, -.01, 0, .01, .02])
    m = return_moments(values)
    assert m.sharpe == pytest.approx(0)
    assert m.skewness == pytest.approx(0)
    assert m.kurtosis == pytest.approx(1.7)
    skewed = np.r_[np.full(10000, -.00001), .9]
    row = deflated_sharpe(skewed, .01)[0]
    assert 0 <= row["dsr_probability"] <= 1
    assert row["raw_kurtosis"] > 9000


@pytest.mark.parametrize("returns", [[0, 0, 0], [0, np.nan, .1], [0, np.inf, .1], [-1, 0, .1], [0, .1]])
def test_invalid_returns(returns):
    with pytest.raises(ValueError):
        return_moments(returns)


def test_invalid_moments_trials():
    with pytest.raises(ValueError):
        probabilistic_sharpe(.1, 0, 100, 2, 3)
    with pytest.raises(ValueError):
        expected_maximum_sharpe(0, .1)
    with pytest.raises(ValueError):
        expected_maximum_sharpe(10, float("nan"))
