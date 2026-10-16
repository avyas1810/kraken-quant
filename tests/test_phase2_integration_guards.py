"""Synthetic operational tests, not historical strategy evidence."""

import json

import numpy as np
import pandas as pd
import pytest

from research.phase2.btc_regression import btc_hac_regression
from research.phase2.frozen_accounting import batch_frozen_accounting
from research.phase2.original_study import FROZEN, function_definitions
from research.phase2.original_validation import OriginalAdapter


def test_arch_detects_clustered_variance():
    rng = np.random.default_rng(204)
    n = 1200
    btc = rng.normal(0, .01, n)
    scale = np.where(np.arange(n)//40 % 2, .012, .001)
    returns = pd.DataFrame({"btc": btc, "strategy_net": .2*btc+rng.normal(size=n)*scale},
        index=pd.date_range("2024", periods=n, freq="4h", tz="UTC"))
    results = btc_hac_regression(returns)
    assert results.residual_arch_lm_pvalue.lt(.001).all()
    assert results.hac_to_ols_alpha_se_ratio.gt(0).all()


def test_original_features_do_not_change_when_future_bars_change():
    cells = json.loads((FROZEN/"source_cells.json").read_text())["cells"]
    namespace = {"pd": pd, "np": np}
    for cell in cells:
        if cell["prefix"] in ("CROSS-SECTIONAL MOMENTUM / REVERSAL WEIGHT CONSTRUCTION", "def average_pairwise_correlation("):
            exec(function_definitions(cell["source"]), namespace)
    rng = np.random.default_rng(205)
    index = pd.date_range("2021", periods=900, freq="4h", tz="UTC")
    returns = pd.DataFrame(rng.normal(0, .01, (900, 9)), index=index)
    prices = (1+returns).cumprod()*100
    volume = pd.DataFrame(100., index=index, columns=returns.columns)
    momentum = namespace["construct_cs_momentum_weights"]
    regimes = namespace["construct_regime_variables"]
    original_weights = momentum(prices, lookback=180)
    original_regime = regimes(returns, volume, vol_window=180, regime_window=540, corr_window=180)
    changed_prices, changed_returns, changed_volume = prices.copy(), returns.copy(), volume.copy()
    changed_prices.iloc[870:] *= 2
    changed_returns.iloc[870:] += .15
    changed_volume.iloc[870:] *= 5
    pd.testing.assert_frame_equal(original_weights.iloc[:870], momentum(changed_prices, lookback=180).iloc[:870])
    pd.testing.assert_frame_equal(original_regime.iloc[:870], regimes(changed_returns, changed_volume,
        vol_window=180, regime_window=540, corr_window=180).iloc[:870])


def test_batch_accounting_is_causal():
    rng = np.random.default_rng(206)
    returns = rng.normal(0, .01, (40, 2))
    weights = np.tile([.6, -.4], (40, 1))
    masks = np.ones((3, 40))
    original = batch_frozen_accounting(weights, returns, masks)
    changed = returns.copy()
    changed[-3:] += .2
    revised = batch_frozen_accounting(weights, changed, masks)
    for name in original:
        np.testing.assert_allclose(original[name][:, :-3], revised[name][:, :-3], atol=1e-12, rtol=0)


def test_cached_trials_reject_corruption_and_changed_masks(tmp_path):
    rng = np.random.default_rng(207)
    n = 40
    index = pd.date_range("2024", periods=n, freq="4h", tz="UTC")
    returns = pd.DataFrame(rng.normal(0, .01, (n, 2)), index=index)
    momentum = pd.DataFrame(np.tile([.6, -.4], (n, 1)), index=index)
    mask = np.array([1, 1, 0, 1, 0]*8)
    result = batch_frozen_accounting(momentum, returns, mask[None, :])
    frame = pd.DataFrame({"strategy_net": result["net_return"][0], "strategy_gross": result["gross_return"][0],
        "turnover": result["turnover"][0], "gross_exposure": result["gross_exposure"][0], "regime_active": mask}, index=index)
    prepared = dict(frame=frame, momentumweights=momentum, returns=returns, initial_held=np.zeros(2),
        source_hashes={}, trial_dispersion_train=.01, trial_dispersion=.01)
    adapter = OriginalAdapter(prepared, tmp_path, seed=3, trials=3)
    adapter.simulate_filter(frame.regime_active.astype(bool))
    second = OriginalAdapter(prepared, tmp_path, seed=3, trials=3)
    pd.testing.assert_frame_equal(adapter.simulate_filter(frame.regime_active.astype(bool)), second.simulate_filter(frame.regime_active.astype(bool)))
    changed = {**prepared, "frame": frame.copy()}
    changed["frame"]["regime_active"] = np.roll(mask, 1)
    with pytest.raises(ValueError, match="mask definition"):
        OriginalAdapter(changed, tmp_path, seed=3, trials=3).simulate_filter(changed["frame"].regime_active.astype(bool))
    next(tmp_path.glob("*.npz")).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="hash"):
        OriginalAdapter(prepared, tmp_path, seed=3, trials=3).simulate_filter(frame.regime_active.astype(bool))
