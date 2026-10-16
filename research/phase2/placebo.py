"""Historical filter randomisation; accounting must be supplied by frozen Phase 1.

The callback closes over unchanged signals, prices, costs and execution alignment.
It accepts only a mask and returns net_return, gross_exposure and turnover on the
same index. This module never computes fills or substitutes a portfolio engine.
"""
from __future__ import annotations

from collections.abc import Callable
import numpy as np
import pandas as pd


REQUIRED = ("net_return", "gross_exposure", "turnover")


def _mask(mask: pd.Series) -> pd.Series:
    if not isinstance(mask, pd.Series) or not isinstance(mask.index, pd.DatetimeIndex):
        raise ValueError("filter must be a timestamp-indexed Series")
    if mask.index.has_duplicates or not mask.index.is_monotonic_increasing or mask.isna().any():
        raise ValueError("filter timestamps must be ordered, unique and complete")
    if mask.index.tz is None or str(mask.index.tz) != "UTC":
        raise ValueError("filter timestamps must use UTC")
    if len(mask) > 1 and not np.all(np.diff(mask.index.asi8) == pd.Timedelta(hours=4).value):
        raise ValueError("filter must have complete regular four-hour observations")
    if not mask.isin([True, False]).all() or mask.nunique() != 2:
        raise ValueError("filter must contain both active and inactive binary observations")
    return mask.astype(bool).copy()


def clustered_mask(mask: pd.Series, rng: np.random.Generator) -> pd.Series:
    """Permute run lengths separately within each state, retaining endpoint state.

    Active count and the multiset of active/inactive run lengths are exact. The
    conditional null fixes endpoints and can be degenerate for few runs. It is a
    retrospective randomisation, never an implementable causal trading rule.
    """
    checked = _mask(mask)
    values = checked.to_numpy()
    starts = np.r_[0, np.flatnonzero(values[1:] != values[:-1]) + 1]
    lengths = np.diff(np.r_[starts, len(values)])
    states = values[starts]
    permuted = lengths.copy()
    for state in (False, True):
        locations = np.flatnonzero(states == state)
        permuted[locations] = rng.permutation(lengths[locations])
    return pd.Series(np.repeat(states, permuted), index=checked.index, name=checked.name)


def _evaluate(callback: Callable[[pd.Series], pd.DataFrame], mask: pd.Series,
              periods_per_year: float) -> dict[str, float]:
    frame = callback(mask.copy(deep=True))
    if not isinstance(frame, pd.DataFrame) or not frame.index.equals(mask.index):
        raise ValueError("frozen engine output must exactly align with the filter index")
    if any(column not in frame for column in REQUIRED):
        raise ValueError("frozen engine output needs net_return, gross_exposure, turnover")
    values = frame.loc[:, REQUIRED].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values[:, 0] <= -1).any() or (values[:, 1:] < 0).any():
        raise ValueError("engine output must be finite with valid returns/exposure/turnover")
    returns = values[:, 0]
    volatility = returns.std(ddof=1)
    if volatility <= 0:
        raise ValueError("Sharpe is undefined for constant return series")
    wealth = np.r_[1.0, np.cumprod(1.0 + returns)]
    if not np.isfinite(wealth).all():
        raise ValueError("wealth overflow")
    return {"net_sharpe": float(np.sqrt(periods_per_year) * returns.mean() / volatility),
            "cumulative_return": float(wealth[-1] - 1),
            "max_drawdown": float(np.min(wealth / np.maximum.accumulate(wealth) - 1)),
            "turnover": float(values[:, 2].sum()),
            "gross_exposure": float(values[:, 1].mean())}


def _relative_difference(value: float, observed: float) -> float:
    if observed == 0:
        if value != 0:
            raise ValueError("relative matching is undefined for zero observed exposure or turnover")
        return 0.0
    return (value - observed) / observed


def run_placebos(mask: pd.Series, frozen_engine: Callable[[pd.Series], pd.DataFrame], *,
                 seed: int = 20261014, trials: int = 999,
                 periods_per_year: float = 365 * 6,
                 exposure_tolerance: float = 0.05,
                 turnover_tolerance: float = 0.10) -> dict:
    """Return all trials, balance diagnostics and finite-sample tail probabilities.

    Both unconditional and predeclared balance-conditioned nulls are reported.
    P=(1+number(null statistic >= observed))/(1+number(null trials)); higher
    (less negative) drawdown is better. Conditional results may have too few
    accepted draws; they never claim exact equivalence or statistical power.
    """
    mask = _mask(mask)
    if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
        raise ValueError("trials must be a positive integer")
    if not np.isfinite(periods_per_year) or periods_per_year <= 0 or len(mask) < 3:
        raise ValueError("positive annualisation and at least three observations required")
    if not all(np.isfinite(x) and x >= 0 for x in (exposure_tolerance, turnover_tolerance)):
        raise ValueError("matching tolerances must be finite and nonnegative")
    observed = _evaluate(frozen_engine, mask, periods_per_year)
    rng = np.random.default_rng(seed)
    rows = []
    for method in ("run_length_permutation", "circular_shift"):
        for trial in range(trials):
            candidate = (clustered_mask(mask, rng) if method == "run_length_permutation" else
                         pd.Series(np.roll(mask.to_numpy(), rng.integers(1, len(mask))), index=mask.index))
            stats = _evaluate(frozen_engine, candidate, periods_per_year)
            exposure_gap = _relative_difference(stats["gross_exposure"], observed["gross_exposure"])
            turnover_gap = _relative_difference(stats["turnover"], observed["turnover"])
            rows.append({"method": method, "trial": trial, **stats,
                         "active_fraction": float(candidate.mean()),
                         "exposure_relative_gap": exposure_gap,
                         "turnover_relative_gap": turnover_gap,
                         "balanced": abs(exposure_gap) <= exposure_tolerance and
                         abs(turnover_gap) <= turnover_tolerance,
                         "same_as_observed": bool(candidate.equals(mask))})
    table = pd.DataFrame(rows)
    summaries = []
    for method, group in table.groupby("method", sort=False):
        for conditional in (False, True):
            selected = group[group.balanced] if conditional else group
            n = len(selected)
            for metric in ("net_sharpe", "cumulative_return", "max_drawdown"):
                summaries.append({"method": method, "balance_conditioned": conditional,
                                  "metric": metric, "observed": observed[metric], "trials": n,
                                  "status": ("no_accepted_trials" if n == 0 else
                                             "degenerate_null" if selected.same_as_observed.all() else
                                             "limited_monte_carlo_precision" if n < 999 else "ok"),
                                  "p_one_sided": ((1 + int((selected[metric] >= observed[metric]).sum())) /
                                                  (n + 1)) if n else None})
    diagnostics = table.groupby("method", sort=False).agg(
        trials=("trial", "size"), accepted=("balanced", "sum"),
        active_fraction=("active_fraction", "mean"),
        exposure_relative_gap_mean=("exposure_relative_gap", "mean"),
        exposure_relative_gap_min=("exposure_relative_gap", "min"),
        exposure_relative_gap_max=("exposure_relative_gap", "max"),
        turnover_relative_gap_mean=("turnover_relative_gap", "mean"),
        turnover_relative_gap_min=("turnover_relative_gap", "min"),
        turnover_relative_gap_max=("turnover_relative_gap", "max"),
        identical_masks=("same_as_observed", "sum")).reset_index()
    diagnostics["status"] = np.where(diagnostics.identical_masks == diagnostics.trials,
                                     "degenerate_null", "inspect_matching_imbalance")
    return {"observed": observed, "trials": table, "summary": pd.DataFrame(summaries),
            "balance_diagnostics": diagnostics,
            "configuration": {"seed": seed, "trials_per_method": trials,
                              "periods_per_year": periods_per_year,
                              "exposure_tolerance": exposure_tolerance,
                              "turnover_tolerance": turnover_tolerance,
                              "interpretation": "historical conditional randomisation; not a trading strategy"}}
