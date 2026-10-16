"""Per-period PSR/DSR diagnostics; annualisation belongs only in presentation.

PSR = Phi((SR-SR0)*sqrt(T-1) / sqrt(1-skew*SR+(kurtosis-1)*SR**2/4)).
Kurtosis is raw (Gaussian=3). This approximation assumes stationary independent
observations; serial dependence needs the separate block-bootstrap analysis.
DSR is PSR against an estimated maximum under multiple trials, not a p-value.
Across-trial Sharpe dispersion is an explicit input, never inferred from one run.
"""
from dataclasses import dataclass
import math

import numpy as np
from scipy.special import ndtr
from scipy.stats import norm


@dataclass(frozen=True)
class SharpeMoments:
    observations: int
    sharpe: float
    skewness: float
    kurtosis: float


def return_moments(returns) -> SharpeMoments:
    """Estimate SR with sample standard deviation (ddof=1).

    Skewness and raw kurtosis use population central-moment ratios (ddof=0),
    without finite-sample bias corrections. This convention is explicit because
    changing moment estimators changes the finite-sample approximation.
    """
    values = np.asarray(returns, dtype=float)
    if values.ndim != 1 or len(values) < 3 or not np.isfinite(values).all():
        raise ValueError("At least three finite one-dimensional returns required")
    if (values <= -1).any():
        raise ValueError("Simple returns must exceed -1")
    centered = values - values.mean()
    scale = np.std(values, ddof=1)
    population_scale = np.std(values, ddof=0)
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("Sharpe undefined for constant returns")
    standardized = centered / population_scale
    return SharpeMoments(len(values), float(values.mean() / scale),
                         float(np.mean(standardized ** 3)),
                         float(np.mean(standardized ** 4)))


def probabilistic_sharpe(sharpe: float, benchmark: float, observations: int,
                         skewness: float, kurtosis: float) -> float:
    """Approximate probability that population SR exceeds benchmark SR.

    Both Sharpe inputs must be per observation, with the same risk-free convention.
    No effective-sample-size correction or serial-correlation adjustment is hidden.
    """
    if not all(math.isfinite(x) for x in (sharpe, benchmark, skewness, kurtosis)):
        raise ValueError("All moments and benchmarks must be finite")
    if isinstance(observations, bool) or not isinstance(observations, (int, np.integer)) or observations < 3:
        raise ValueError("observations must be an integer >= 3")
    if kurtosis < skewness * skewness + 1 - 1e-12:
        raise ValueError("Inconsistent skewness and raw kurtosis")
    # Complete the square to avoid cancellation for large skew or Sharpe.
    a = (kurtosis - 1) / 4
    variance = (1.0 if a == 0 else
                a * (sharpe - skewness / (2 * a)) ** 2 + 1 - skewness ** 2 / (4 * a))
    if not math.isfinite(variance) or variance <= 0:
        raise ValueError("PSR approximation has nonpositive or nonfinite variance")
    return float(ndtr((sharpe - benchmark) * math.sqrt(observations - 1) / math.sqrt(variance)))


def expected_maximum_sharpe(trials: int, trial_sharpe_std: float,
                            null_mean: float = 0.0) -> float:
    """Gaussian independent-trial expected maximum approximation.

    Trial dispersion and null mean are per-period SR units. Effective independent
    trial count is unknown in real research; sensitivity is required. N=1 gives
    the null mean. N>1 uses the Euler-Mascheroni weighted quantile approximation.
    """
    if isinstance(trials, bool) or not isinstance(trials, (int, np.integer)) or trials < 1:
        raise ValueError("trials must be a positive integer")
    if not math.isfinite(trial_sharpe_std) or trial_sharpe_std < 0 or not math.isfinite(null_mean):
        raise ValueError("Finite nonnegative trial dispersion and finite null mean required")
    if trials == 1:
        return float(null_mean)
    gamma = 0.5772156649015329
    # isf avoids rounding 1-small_probability to exactly 1.
    maximum = (1 - gamma) * norm.isf(1 / trials) + gamma * norm.isf((1 / trials) / math.e)
    return float(null_mean + trial_sharpe_std * maximum)


def deflated_sharpe(returns, trial_sharpe_std: float,
                    trial_counts=(2, 10, 25, 50, 100)) -> list[dict]:
    moments = return_moments(returns)
    result = []
    for trials in trial_counts:
        benchmark = expected_maximum_sharpe(trials, trial_sharpe_std)
        result.append({"trials": trials, "observations": moments.observations,
                       "sharpe_per_period": moments.sharpe, "skewness": moments.skewness,
                       "raw_kurtosis": moments.kurtosis,
                       "trial_sharpe_std_per_period": trial_sharpe_std,
                       "benchmark_sharpe_per_period": benchmark,
                       "dsr_probability": probabilistic_sharpe(
                           moments.sharpe, benchmark, moments.observations,
                           moments.skewness, moments.kurtosis)})
    return result
