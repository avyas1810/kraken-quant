"""Descriptive frozen-sample stability diagnostics, never parameter selection."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .bootstrap import _validate


def _metrics(values: pd.Series, annualisation: float) -> dict:
    array = values.to_numpy(dtype=float)
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        try:
            wealth = np.r_[1.0, np.cumprod(1 + array)]
            sd = np.std(array, ddof=1) if len(array) > 1 else 0.0
        except FloatingPointError as error:
            raise ValueError("Nonfinite stability accounting or variance") from error
    result = dict(observations=len(array), cumulative_return=float(wealth[-1]-1),
                sharpe=float(array.mean()/sd*np.sqrt(annualisation)) if sd > 0 else None,
                sharpe_status="defined" if sd > 0 else "undefined_constant_or_single_observation",
                maximum_drawdown=float(np.min(wealth/np.maximum.accumulate(wealth)-1)),
                arithmetic_mean=float(array.mean()))
    if not all(np.isfinite(value) for value in result.values() if isinstance(value, (int, float))):
        raise ValueError("Nonfinite stability metric")
    return result


def stability_report(
    returns: pd.Series, splits: dict, *, exposure: pd.Series | None = None,
    turnover: pd.Series | None = None, annualisation: float = 365*6,
    remove_largest: tuple[int, ...] = (1, 5, 10),
) -> dict[str, pd.DataFrame]:
    """Read boundaries from the caller's frozen manifest, not daily research config.

    Split endpoints are inclusive UTC bar timestamps. Positive-return deletion
    sets selected returns to zero while retaining timestamps; costs/weights are
    not rerun. It is a hindsight sensitivity diagnostic, not a trading rule.
    """
    checked = _validate(returns.rename("returns").to_frame())["returns"]
    if (checked <= -1).any() or not np.isfinite(annualisation) or annualisation <= 0:
        raise ValueError("Simple returns must exceed -1 and annualisation must be positive")
    auxiliaries = {}
    for name, series in (("exposure", exposure), ("turnover", turnover)):
        if series is not None:
            if not series.index.equals(checked.index) or not np.isfinite(series.to_numpy(dtype=float)).all() or (series < 0).any():
                raise ValueError(f"{name} must be aligned, finite and nonnegative")
            auxiliaries[name] = series
    intervals = []
    for name, bounds in splits.items():
        start, end = pd.Timestamp(bounds["start"]), pd.Timestamp(bounds["end"])
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError("Frozen boundaries require ordered timezone-aware timestamps")
        if start not in checked.index or end not in checked.index:
            raise ValueError("Required frozen boundary observations are missing")
        intervals.append((name, start, end))
    if not intervals:
        raise ValueError("Frozen train/OOS boundaries are required")
    ordered = sorted(intervals, key=lambda item: item[1])
    if any(left[2] >= right[1] for left, right in zip(ordered, ordered[1:])):
        raise ValueError("Frozen splits overlap")
    period_rows, sensitivity_rows, concentration_rows = [], [], []
    for name, start, end in intervals:
        subset = checked.loc[start:end]
        groups = [("split", name, subset)]
        groups += [("year", str(key), group) for key, group in subset.groupby(subset.index.year)]
        quarter_labels = subset.index.tz_localize(None).to_period("Q")
        groups += [("quarter", str(key), group) for key, group in subset.groupby(quarter_labels)]
        for level, period, group in groups:
            row = dict(split=name, level=level, period=period, start=group.index[0].isoformat(),
                       end=group.index[-1].isoformat(), **_metrics(group, annualisation))
            for aux_name, series in auxiliaries.items():
                selected = series.loc[group.index]
                row["mean_exposure" if aux_name == "exposure" else "total_turnover"] = float(selected.mean() if aux_name == "exposure" else selected.sum())
                if aux_name == "exposure":
                    row["active_fraction"] = float((selected > 0).mean())
            period_rows.append(row)
        for level in ("year", "quarter"):
            partition = [group for group_level, _, group in groups if group_level == level]
            log_gains = np.array([np.log1p(group.to_numpy()).sum() for group in partition])
            split_log_gain = float(np.log1p(subset.to_numpy()).sum())
            if not np.isclose(log_gains.sum(), split_log_gain, rtol=1e-12, atol=1e-12):
                raise ValueError("Period log returns do not reconcile to frozen split")
            positive = log_gains[log_gains > 0]
            concentration_rows.append(dict(split=name, level=level, periods=len(partition),
                                           positive_periods=len(positive), total_log_return=split_log_gain,
                                           best_period_log_return=float(log_gains.max()),
                                           largest_share_positive_log_gains=float(positive.max()/positive.sum()) if len(positive) else None,
                                           concentration_status="defined" if len(positive) else "undefined_no_positive_periods",
                                           reconciliation_absolute_error=float(abs(log_gains.sum()-split_log_gain))))
        for count in remove_largest:
            if isinstance(count, (bool, np.bool_)) or not isinstance(count, (int, np.integer)) or count < 1:
                raise ValueError("Deletion counts must be positive integers")
            selected = subset[subset > 0].nlargest(count)
            changed = subset.copy()
            changed.loc[selected.index] = 0.0
            sensitivity_rows.append(dict(split=name, requested_removed=count, actual_removed=len(selected),
                                         removed_arithmetic_return=float(selected.sum()),
                                         diagnostic="positive_returns_set_to_zero_no_reexecution",
                                         **_metrics(changed, annualisation)))
    return {"periods": pd.DataFrame(period_rows), "positive_return_sensitivity": pd.DataFrame(sensitivity_rows),
            "period_concentration": pd.DataFrame(concentration_rows)}
