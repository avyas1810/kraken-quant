"""Batch evaluation of the frozen Phase 1 equations, not a new cost model.

The trial axis alone is vectorised. Each bar retains price drift, post-fee
target sizing, 55 bisection iterations and final-bar no-rebalance semantics.
Initial held weights allow exact continuation at an OOS boundary. Their equity
is normalised to one, which leaves proportional returns and turnover invariant.
"""
from __future__ import annotations

import numpy as np


def batch_frozen_accounting(momentum, returns, masks, *, initial_held=None,
                            cost_bps=20.0):
    momentum = np.asarray(momentum, dtype=float)
    returns = np.asarray(returns, dtype=float)
    masks = np.asarray(masks)
    if momentum.ndim != 2 or momentum.shape != returns.shape or len(momentum) < 1:
        raise ValueError("momentum and returns must share nonempty bar/asset shape")
    if masks.ndim != 2 or masks.shape[1] != len(momentum) or not np.isin(masks, [0, 1]).all():
        raise ValueError("masks must be binary trial/bar arrays")
    if not np.isfinite(momentum).all() or not np.isfinite(returns).all() or (returns <= -1).any():
        raise ValueError("invalid weights or asset returns")
    if (np.abs(momentum).sum(axis=1) > 1 + 1e-9).any():
        raise ValueError("target gross exposure exceeds one")
    if not np.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("invalid fee")
    trials, bars = masks.shape
    assets = momentum.shape[1]
    initial = np.zeros(assets) if initial_held is None else np.asarray(initial_held, dtype=float)
    if initial.shape != (assets,) or not np.isfinite(initial).all():
        raise ValueError("initial held weights must be finite and match assets")
    positions = np.broadcast_to(initial, (trials, assets)).copy()
    equity = np.ones(trials)
    net = np.empty((trials, bars))
    gross = np.empty_like(net)
    turnover = np.empty_like(net)
    exposure = np.empty_like(net)
    fees = np.empty_like(net)
    rate = cost_bps / 10000
    for t in range(bars):
        previous = equity
        exposure[:, t] = np.abs(positions / previous[:, None]).sum(axis=1)
        pnl = (positions * returns[t]).sum(axis=1)
        before = previous + pnl
        positions = positions * (1 + returns[t])
        if not np.isfinite(before).all() or (before <= 0).any():
            raise ValueError(f"insolvent or nonfinite portfolio at bar {t}")
        gross[:, t] = pnl / previous
        pretrade = positions / before[:, None]
        if t == bars - 1 or rate == 0:
            after = before.copy()
        else:
            target = masks[:, t, None] * momentum[t]
            low, high = np.zeros(trials), before.copy()
            for _ in range(55):
                mid = (low + high) / 2
                traded = np.abs(target * mid[:, None] - positions).sum(axis=1)
                upper = mid + rate * traded > before
                high = np.where(upper, mid, high)
                low = np.where(upper, low, mid)
            after = (low + high) / 2
        if t != bars - 1:
            positions = masks[:, t, None] * momentum[t] * after[:, None]
        notional = np.abs(positions - pretrade * before[:, None]).sum(axis=1)
        fees[:, t] = (before - after) / previous
        turnover[:, t] = notional / previous
        net[:, t] = after / previous - 1
        equity = after
    return {"net_return": net, "gross_return": gross, "turnover": turnover,
            "gross_exposure": exposure, "trading_costs": fees}
