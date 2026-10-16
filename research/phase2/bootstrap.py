"""Paired moving-block uncertainty estimates; no strategy selection or data loading."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _validate(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.columns.has_duplicates:
        raise ValueError("Return columns must be unique")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError("Returns require a timezone-aware DatetimeIndex")
    if str(frame.index.tz) != "UTC" or frame.index.hasnans or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("Returns require unique increasing UTC timestamps")
    if len(frame) < 3 or not np.all(np.diff(frame.index.asi8) == pd.Timedelta(hours=4).value):
        raise ValueError("Returns require contiguous four-hour observations")
    if not np.isfinite(frame.to_numpy(dtype=float)).all():
        raise ValueError("Returns must be finite and complete")
    if (frame.to_numpy(dtype=float) <= -1).any():
        raise ValueError("Simple returns must exceed -1")
    return frame.astype(float)


def _sharpe(values: np.ndarray, annualisation: float) -> np.ndarray:
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        try:
            sd = np.std(values, axis=-1, ddof=1)
            if np.any(sd <= 0):
                raise ValueError("Sharpe is undefined for a constant return sample")
            result = np.mean(values, axis=-1) / sd * np.sqrt(annualisation)
        except FloatingPointError as error:
            raise ValueError("Nonfinite bootstrap Sharpe calculation") from error
    if not np.isfinite(result).all():
        raise ValueError("Nonfinite bootstrap Sharpe calculation")
    return result


def _ess(values: np.ndarray, maximum_lag: int) -> float:
    """Approximate mean ESS using initial positive autocorrelation sequence."""
    centered = values - values.mean()
    denominator = float(centered @ centered)
    if denominator == 0:
        raise ValueError("ESS is undefined for constant returns")
    total = 0.0
    for lag in range(1, min(maximum_lag, len(values) - 1) + 1):
        rho = float(centered[:-lag] @ centered[lag:] / denominator)
        if rho <= 0:
            break
        total += rho
    return float(min(len(values), len(values) / (1 + 2 * total)))


def paired_block_bootstrap(
    frame: pd.DataFrame, *, block_lengths=(12, 36, 72, 144),
    replications: int = 1999, seed: int = 240214, annualisation: float = 365 * 6,
    confidence: float = 0.95,
) -> pd.DataFrame:
    """Percentile CIs and centered paired-null tests; rows retain paired timestamps.

    Required columns are strategy and baseline, containing simple net returns.
    Overlapping noncircular blocks preserve within-block dependence. Annualised
    mean is arithmetic, not compounded alpha. The mean-difference null centers
    each series separately; the Sharpe-difference null centers the bootstrap
    statistic around its observed difference (an approximate bootstrap test).
    """
    frame = _validate(frame[["strategy", "baseline"]])
    if (isinstance(replications, (bool, np.bool_)) or not isinstance(replications, (int, np.integer))
            or replications < 2 or not np.isfinite(annualisation) or annualisation <= 0
            or not np.isfinite(confidence) or not 0 < confidence < 1
            or isinstance(seed, (bool, np.bool_)) or not isinstance(seed, (int, np.integer)) or seed < 0):
        raise ValueError("Invalid bootstrap specification")
    if not block_lengths or len(set(block_lengths)) != len(block_lengths):
        raise ValueError("Block lengths must be unique")
    values = frame.to_numpy().T
    observed_sharpe = _sharpe(values, annualisation)
    observed_mean = values.mean(axis=1) * annualisation
    tail = (1 - confidence) / 2
    rows = []
    for length in block_lengths:
        if isinstance(length, (bool, np.bool_)) or not isinstance(length, (int, np.integer)) or length < 1 or length >= len(frame):
            raise ValueError("Block length must be positive and shorter than sample")
        rng = np.random.default_rng(np.random.SeedSequence([seed, int(length)]))
        sharpe_draws = np.empty((replications, 2))
        mean_draws = np.empty((replications, 2))
        for draw in range(replications):
            starts = rng.integers(0, len(frame) - length + 1, size=int(np.ceil(len(frame) / length)))
            indices = (starts[:, None] + np.arange(length)).ravel()[:len(frame)]
            sample = values[:, indices]
            sharpe_draws[draw] = _sharpe(sample, annualisation)
            mean_draws[draw] = sample.mean(axis=1) * annualisation
        for metric, draws, observed in (("sharpe", sharpe_draws, observed_sharpe),
                                        ("annualised_mean", mean_draws, observed_mean)):
            for label, vector, estimate in (("strategy", draws[:, 0], observed[0]),
                                             ("baseline", draws[:, 1], observed[1]),
                                             ("paired_difference", draws[:, 0] - draws[:, 1], observed[0] - observed[1])):
                low, high = np.quantile(vector, [tail, 1-tail])
                # P(null statistic >= observed), including the observed trial.
                null = vector - estimate
                p = (1 + np.count_nonzero(null >= estimate)) / (replications + 1)
                rows.append(dict(block_length=length, metric=metric, comparison=label,
                                 estimate=float(estimate), ci_low=float(low), ci_high=float(high),
                                 centered_null_one_sided_p=float(p), replications=replications,
                                 seed=seed, observations=len(frame), blocks_per_draw=int(np.ceil(len(frame)/length)),
                                 approximate_mean_ess_strategy=_ess(values[0], length),
                                 approximate_mean_ess_baseline=_ess(values[1], length),
                                 annualisation=annualisation, confidence=confidence))
    return pd.DataFrame(rows)
