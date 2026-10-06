"""Daily funded-equity metrics with explicit definitions and undefined ratios."""

import math

import numpy as np
import pandas as pd


def performance_metrics(equity, fills=(), *, periods_per_year=365,
                        daily_risk_free=0.0, daily_target=0.0):
    """Include the initial funding mark in returns/drawdown, not average equity.

    Sharpe uses sample excess-return deviation. Sortino uses the mean squared
    shortfall across ALL days. Zero denominators return NaN (not infinity).
    Turnover counts both buys and sells. Trade count means executed nonzero orders,
    each modeled as one fill, not completed round trips. See docs/backtest_core.md.
    """
    if not isinstance(equity, pd.Series) or len(equity) < 2:
        raise ValueError("Equity requires initial funding plus at least one daily mark")
    if not isinstance(equity.index, pd.DatetimeIndex) or equity.index.tz is None:
        raise ValueError("Equity requires timezone-aware daily timestamps")
    if not equity.index.is_unique or not equity.index.is_monotonic_increasing:
        raise ValueError("Equity timestamps must be unique and increasing")
    if not (equity.index[1:] - equity.index[:-1] == pd.Timedelta(days=1)).all():
        raise ValueError("Equity marks must be consecutive daily observations")
    values = equity.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("Equity must be positive and finite")
    if not math.isfinite(periods_per_year) or periods_per_year <= 0:
        raise ValueError("Annualization periods must be positive and finite")
    if not math.isfinite(daily_risk_free) or not math.isfinite(daily_target):
        raise ValueError("Risk-free and target returns must be finite")
    returns = values[1:] / values[:-1] - 1
    excess = returns - daily_risk_free
    deviation = np.std(excess, ddof=1) if len(excess) > 1 else float("nan")
    downside = math.sqrt(float(np.mean(np.minimum(returns - daily_target, 0) ** 2)))
    annualizer = math.sqrt(periods_per_year)
    fills = tuple(fills)
    return {
        "total_return": float(values[-1] / values[0] - 1),
        "annualized_volatility": float(deviation * annualizer),
        "sharpe": float(np.mean(excess) / deviation * annualizer) if deviation > 0 else float("nan"),
        "sortino": float(np.mean(returns - daily_target) / downside * annualizer) if downside > 0 else float("nan"),
        "max_drawdown": float(np.max(1 - values / np.maximum.accumulate(values))),
        "turnover": math.fsum(fill.notional for fill in fills) / float(np.mean(values[1:])),
        "trade_count": len(fills),
        "fees": math.fsum(fill.fee for fill in fills),
        "slippage_cost": math.fsum(fill.slippage_cost for fill in fills),
    }
