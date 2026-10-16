"""Explicit post-hoc execution stress; not an executable exchange fill model."""
import numpy as np
import pandas as pd

def execution_stress(weights, returns, dollar_volume, *, equity=10000, fee=0.0026, full_spread=0.001, slippage=0.0005, impact=0.001, max_participation=0.01, delay=1, annual_short_financing=0.10, periods_per_year=365):
    if not weights.index.equals(returns.index) or not weights.columns.equals(returns.columns) or not weights.index.equals(dollar_volume.index) or not weights.columns.equals(dollar_volume.columns): raise ValueError('Aligned weight/return/liquidity panels required')
    if any(not np.isfinite(f.to_numpy()).all() for f in (weights,returns,dollar_volume)) or (dollar_volume<=0).any().any(): raise ValueError('Finite values and positive volume required')
    if isinstance(delay,bool) or not isinstance(delay,int) or delay < 1: raise ValueError('At least one bar causal execution delay required')
    if any(isinstance(x,bool) or not np.isfinite(x) for x in (equity,fee,full_spread,slippage,impact,max_participation,annual_short_financing,periods_per_year)): raise ValueError('Finite execution assumptions required')
    if (returns<=-1).any().any(): raise ValueError('Simple returns must exceed -1')
    if not 0 < max_participation <= 1 or equity <= 0 or periods_per_year <= 0 or min(fee,full_spread,slippage,impact,annual_short_financing)<0: raise ValueError('Invalid execution assumptions')
    held = weights.shift(delay).fillna(0)
    # Drift-aware rebalance from previous close weights, including prior PnL.
    previous = held.shift(1).fillna(0)
    prior_return = returns.shift(1).fillna(0)
    portfolio_prior = (previous*prior_return).sum(axis=1)
    if (portfolio_prior<=-1).any(): raise ArithmeticError('Insolvent gross stress path')
    drifted = previous*(1+prior_return).div(1+portfolio_prior,axis=0)
    traded = (held-drifted).abs()
    participation = traded*equity/dollar_volume.shift(1)
    # Missing prior executable liquidity is a halt, never a manufactured fill.
    breaches = participation.gt(max_participation) | (traded.gt(0)&participation.isna())
    if breaches.any().any(): raise ArithmeticError('Participation cap or missing prior liquidity: halt scenario')
    cost = traded*(fee+full_spread/2+slippage+impact*np.sqrt(participation.fillna(0)))
    financing = held.clip(upper=0).abs().sum(axis=1)*annual_short_financing/periods_per_year
    gross = (held*returns).sum(axis=1)
    net = gross-cost.sum(axis=1)-financing
    if (net <= -1).any(): raise ArithmeticError('Insolvent stress path')
    return pd.DataFrame({'gross_return':gross,'net_return':net,'turnover':traded.sum(axis=1),'execution_cost':cost.sum(axis=1),'short_financing':financing,'maximum_participation':participation.max(axis=1).fillna(0)})
