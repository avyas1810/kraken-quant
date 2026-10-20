"""Training-only fractional covariance Kelly and hard portfolio constraints."""
from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class KellyConfig:
    fraction: float = 0.25
    minimum_observations: int = 30
    shrinkage: float = 0.5
    ridge: float = 1e-6

    def __post_init__(self):
        if not np.isfinite([self.fraction,self.shrinkage,self.ridge]).all() or not 0 < self.fraction <= 1 or not 0 <= self.shrinkage <= 1 or self.ridge <= 0:
            raise ValueError('Invalid Kelly fraction/shrinkage/ridge')
        if not isinstance(self.minimum_observations,int) or isinstance(self.minimum_observations, bool) or self.minimum_observations < 3:
            raise ValueError('Kelly requires at least 3 observations')


@dataclass(frozen=True)
class PortfolioLimits:
    max_gross: float = 1.0
    max_position: float = 0.35
    max_drawdown: float = 0.20
    daily_loss: float = 0.05
    allow_short: bool = False
    allow_margin: bool = False
    cash_buffer: float = 0.02

    def __post_init__(self):
        values = (self.max_gross, self.max_position, self.max_drawdown, self.daily_loss, self.cash_buffer)
        if not np.isfinite(values).all() or self.max_gross <= 0 or self.max_position <= 0:
            raise ValueError('Finite positive gross/position limits required')
        if not 0 < self.max_drawdown <= 1 or not 0 < self.daily_loss <= 1 or not 0 <= self.cash_buffer < 1:
            raise ValueError('Invalid drawdown/daily-loss/cash buffer limits')
        if not isinstance(self.allow_short, bool) or not isinstance(self.allow_margin, bool):
            raise ValueError('Short/margin permissions must be boolean')


@dataclass(frozen=True)
class KellyEstimate:
    raw: pd.Series
    mean: pd.Series
    covariance: pd.DataFrame
    status: str
    observations: int
    fitted_through: pd.Timestamp


def estimate_kelly(net_returns, config=None):
    config = KellyConfig() if config is None else config
    if net_returns.empty or not isinstance(net_returns.index, pd.DatetimeIndex):
        raise ValueError('Kelly requires timestamped net return observations')
    if not net_returns.index.is_unique or not net_returns.index.is_monotonic_increasing:
        raise ValueError('Kelly observations must be ordered and unique')
    if not np.isfinite(net_returns.to_numpy()).all():
        raise ValueError('Nonfinite Kelly returns')
    mean = net_returns.mean()
    covariance = net_returns.cov().fillna(0)
    raw = pd.Series(0., index=net_returns.columns)
    if len(net_returns) < config.minimum_observations:
        return KellyEstimate(raw, mean, covariance, 'insufficient observations: flat', len(net_returns), net_returns.index[-1])
    matrix = covariance.to_numpy()
    matrix = (1-config.shrinkage)*matrix + config.shrinkage*np.diag(np.diag(matrix))
    floor = max(config.ridge, np.trace(matrix) / max(len(mean), 1) * 1e-3)
    matrix += np.eye(len(mean)) * floor
    drag = pd.Series(net_returns.attrs.get('directional_cost_drag', {}),dtype=float).reindex(mean.index).fillna(0)
    # A negative long net estimate is not evidence of profitable shorting: a
    # short pays costs too. Remove twice the charged long drag before taking
    # the opposite direction. Never manufacture short alpha from fee drag.
    directional_mean = mean.where(mean >= 0, np.minimum(mean+2*drag,0))
    raw[:] = config.fraction * np.linalg.solve(matrix, directional_mean.to_numpy())
    raw[(mean>=0)&(raw<0)] = 0
    raw[(mean<0)&((directional_mean==0)|(raw>0))] = 0
    if not np.isfinite(raw).all():
        raw[:] = 0
        status = 'unstable estimate: flat'
    else:
        status = 'regularized training-only cost-adjusted estimate'
    return KellyEstimate(raw, mean, pd.DataFrame(matrix, index=mean.index, columns=mean.index), status,
                         len(net_returns), net_returns.index[-1])


def constrain(signals, estimate, assets, limits, *, halted=False):
    """Signals set direction/strength; Kelly is a capacity, never a signal reversal."""
    keys = [a.instrument for a in assets]
    if not isinstance(signals, dict):
        signals = dict(signals)
    if set(signals) - set(keys):
        raise ValueError('Strategy returned unknown instruments')
    raw = pd.Series({k: float(signals.get(k, 0)) for k in keys})
    if not np.isfinite(raw).all() or (raw.abs() > 1).any():
        raise ValueError('Signal strengths must be finite in [-1,1]')
    # Negative expected return can support a short only if the strategy requests it.
    weights = pd.Series(0., index=keys)
    if not halted:
        for item in assets:
            k = item.instrument
            direction = np.sign(raw[k])
            if (not item.eligible or direction == 0
                    or (direction < 0 and (not limits.allow_short or not item.shortable))):
                continue
            capacity = max(0., direction * estimate.raw[k])
            weights[k] = direction * min(limits.max_position, capacity * abs(raw[k]))
        gross = weights.abs().sum()
        budget = limits.max_gross if limits.allow_margin else min(limits.max_gross, 1-limits.cash_buffer)
        if gross > budget:
            weights *= budget / gross
    return weights
