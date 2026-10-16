import numpy as np
import pandas as pd
import pytest

from research.phase2.stability import stability_report


def fixture():
    index = pd.date_range("2023-12-31T12:00Z", periods=6, freq="4h")
    returns = pd.Series([.1, -.1, .05, -.02, .03, .01], index=index)
    splits = {"train": {"start": index[0].isoformat(), "end": index[2].isoformat()},
              "oos": {"start": index[3].isoformat(), "end": index[5].isoformat()}}
    return returns, splits


def test_hand_calculated_period_accounting_and_sensitivity():
    returns, splits = fixture()
    report = stability_report(returns, splits, exposure=pd.Series([1, 0, 1, 1, 1, 0], index=returns.index),
                              turnover=pd.Series(.2, index=returns.index), remove_largest=(1,))
    train = report["periods"].query("split == 'train' and level == 'split'").iloc[0]
    assert train.cumulative_return == pytest.approx(1.1*.9*1.05-1)
    assert train.maximum_drawdown == pytest.approx(-.1)
    assert train.mean_exposure == pytest.approx(2/3)
    assert train.total_turnover == pytest.approx(.6)
    sensitivity = report["positive_return_sensitivity"].query("split == 'train'").iloc[0]
    assert sensitivity.cumulative_return == pytest.approx(.9*1.05-1)
    assert sensitivity.actual_removed == 1
    assert sensitivity.removed_arithmetic_return == pytest.approx(.1)


def test_overlap_and_missing_boundaries_rejected():
    returns, splits = fixture()
    splits["oos"]["start"] = splits["train"]["end"]
    with pytest.raises(ValueError, match="overlap"):
        stability_report(returns, splits)
    splits["oos"]["start"] = "2024-01-01T01:00Z"
    with pytest.raises(ValueError, match="boundary"):
        stability_report(returns, splits)


def test_auxiliary_alignment_and_nonfinite_rejected():
    returns, splits = fixture()
    with pytest.raises(ValueError, match="aligned"):
        stability_report(returns, splits, exposure=returns.iloc[1:])
    returns.iloc[1] = np.inf
    with pytest.raises(ValueError, match="finite"):
        stability_report(returns, splits)


def test_constant_returns_document_undefined_sharpe_without_unexplained_nan():
    returns, splits = fixture()
    returns[:] = 0
    report = stability_report(returns, splits)
    assert set(report["periods"].sharpe_status) == {"undefined_constant_or_single_observation"}
    assert report["periods"].sharpe.isna().all()


def test_period_log_accounting_reconciles_and_concentration_is_bounded():
    returns, splits = fixture()
    report = stability_report(returns, splits)
    concentration = report["period_concentration"]
    assert (concentration.reconciliation_absolute_error < 1e-12).all()
    for row in concentration.itertuples():
        subset = returns.loc[splits[row.split]["start"]:splits[row.split]["end"]]
        assert np.expm1(row.total_log_return) == pytest.approx(np.prod(1+subset)-1)
        assert 0 <= row.largest_share_positive_log_gains <= 1


def test_overflow_and_invalid_specifications_fail_clearly():
    returns, splits = fixture()
    with pytest.raises(ValueError):
        stability_report(returns, splits, annualisation=np.inf)
    with pytest.raises(ValueError):
        stability_report(returns, splits, remove_largest=(True,))
    returns[:] = 1e200
    with pytest.raises(ValueError, match="Nonfinite"):
        stability_report(returns, splits)
