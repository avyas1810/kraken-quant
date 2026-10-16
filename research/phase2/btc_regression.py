"""Aligned BTC-factor OLS with HAC inference, not a market-independence claim.

r_strategy,t = alpha + beta*r_BTC,t + error_t. HAC uses Bartlett weights and
finite-sample correction. Annual alpha is P*alpha (arithmetic intercept units),
with both CI endpoints multiplied by P; it is not compounded realised wealth.
"""
import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.stats.diagnostic import acorr_ljungbox, acorr_lm, het_breuschpagan


def btc_hac_regression(frame: pd.DataFrame, strategy_column="strategy_net",
                       btc_column="btc", lags=(12, 36, 72, 144),
                       periods_per_year=2190, confidence=0.95) -> pd.DataFrame:
    """Return one row per HAC lag, with residual diagnostic p-values.

    Caller supplies an already aligned frame. No dropping, filling, resampling or
    changing sample by lag is permitted. Four-hour regular UTC bars are required.
    """
    if not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("Datetime-indexed dataframe required")
    index = frame.index
    if index.tz is None or str(index.tz) != "UTC" or not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("Strictly increasing unique UTC index required")
    if len(frame) < 5 or not (index[1:] - index[:-1] == pd.Timedelta(hours=4)).all():
        raise ValueError("At least five regular four-hour observations required")
    if strategy_column == btc_column or strategy_column not in frame or btc_column not in frame:
        raise ValueError("Distinct strategy and BTC return columns required")
    values = frame[[strategy_column, btc_column]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= -1).any():
        raise ValueError("Finite aligned simple returns > -1 required")
    if not np.isfinite(periods_per_year) or periods_per_year <= 0 or not 0 < confidence < 1:
        raise ValueError("Invalid annualisation or confidence")
    if np.std(values[:, 1]) == 0:
        raise ValueError("BTC factor must vary")
    design = sm.add_constant(values[:, 1], has_constant="add")
    model = sm.OLS(values[:, 0], design).fit()
    if np.var(model.resid) < np.finfo(float).eps ** 2:
        raise ValueError("Residual variance too small for inference")
    bp_p = float(het_breuschpagan(model.resid, design)[1])
    arch_lag = min(12, max(1, (len(frame)-3)//5))
    # ARCH LM is LM on squared residuals. Pin the documented return interface
    # explicitly instead of depending on statsmodels' changing tuple default.
    arch_p = float(acorr_lm(model.resid**2, nlags=arch_lag, ddof=2, result_object=False)[1])
    rows = []
    for lag in lags:
        if isinstance(lag, bool) or not isinstance(lag, (int, np.integer)) or lag < 0 or lag >= len(frame) - 2:
            raise ValueError("HAC lag must be integer >= 0 and < observations - 2")
        fit = model.get_robustcov_results(cov_type="HAC", maxlags=lag,
                                         use_correction=True, use_t=True)
        ci = fit.conf_int(alpha=1 - confidence)[0]
        diagnostic_lag = max(1, lag)
        lb_p = float(acorr_ljungbox(model.resid, lags=[diagnostic_lag], return_df=True)["lb_pvalue"].iloc[0])
        rows.append({"strategy": strategy_column, "observations": len(frame), "hac_lag": lag,
                     "alpha_per_period": float(fit.params[0]), "beta": float(fit.params[1]),
                     "r_squared": float(model.rsquared), "alpha_standard_error": float(fit.bse[0]),
                     "ols_alpha_standard_error": float(model.bse[0]),
                     "hac_to_ols_alpha_se_ratio": float(fit.bse[0]/model.bse[0]),
                     "ols_alpha_pvalue": float(model.pvalues[0]),
                     "alpha_pvalue": float(fit.pvalues[0]),
                     "alpha_annual_arithmetic": float(fit.params[0] * periods_per_year),
                     "alpha_annual_ci_low": float(ci[0] * periods_per_year),
                     "alpha_annual_ci_high": float(ci[1] * periods_per_year),
                     "residual_ljung_box_pvalue": lb_p, "residual_bp_pvalue": bp_p,
                     "residual_arch_lm_pvalue": arch_p, "arch_lag": arch_lag,
                     "periods_per_year": periods_per_year, "confidence": confidence})
    result = pd.DataFrame(rows)
    if result.empty or not np.isfinite(result.select_dtypes(include="number").to_numpy()).all():
        raise ValueError("Empty specifications or undefined regression diagnostic")
    return result
