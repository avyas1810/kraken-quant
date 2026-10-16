import json
import pytest
import pandas as pd
from kraken_quant.candidate import freeze_candidate,qualify_candidate,read_candidate

def specification():
    return dict(strategy='x',parameters={},features='none',costs={},risk_limits={},input_hashes={'x':'a'*64},code_revision='abc',prospective_start=(pd.Timestamp.now(tz='UTC')+pd.Timedelta(days=2)).isoformat(),universe=['BTC/USD'])

def test_frozen_candidate_cannot_be_overwritten_or_promoted_by_replay(tmp_path):
    p=tmp_path/'candidate.json';record=freeze_candidate(p,specification())
    with pytest.raises(FileExistsError):freeze_candidate(p,specification())
    report=qualify_candidate(p,dict(sharpe=2,max_drawdown=.01,actual_daily_decisions=100,simulated_order_lifecycles=100,candidate_sha256=record['sha256'],source='accelerated_replay',reliability_passed=True,independent_review_passed=True))
    assert not report['qualified_for_review'] and not report['live_authorized']
    data=json.loads(p.read_text());data['specification']['parameters']={'lookback':2};p.write_text(json.dumps(data))
    with pytest.raises(ValueError):read_candidate(p)

def test_inspected_test_not_holdout(tmp_path):
    spec=specification();spec['evaluation_split']='test'
    with pytest.raises(PermissionError):freeze_candidate(tmp_path/'x',spec)


def test_all_qualification_gates_fail_closed(tmp_path):
    p=tmp_path/'candidate.json';record=freeze_candidate(p,specification())
    # Use a future observation start: invented retrospective dates cannot qualify.
    evidence=dict(candidate_sha256=record['sha256'],sharpe=2,max_drawdown=.1,actual_daily_decisions=30,simulated_order_lifecycles=20,source='prospective_public_feed',reliability_passed=True,independent_review_passed=True,decision_dates_utc=[str(d) for d in pd.date_range('2020-01-01',periods=30,tz='UTC')],completed_lifecycle_ids=[str(i) for i in range(20)],net_return=.1,benchmark_relative_return=.01,positive_assets=3,neighbour_parameters_positive=2,round_trips=30,completion_fraction=1,doubled_spread_slippage_return=0,bootstrap_mean_excess_ci_low=.0001,elapsed_seconds=29*86400,bootstrap_confidence=.95,fault_events=0,missing_days=0)
    assert not qualify_candidate(p,evidence)['qualified_for_review']
    for field in ('net_return','benchmark_relative_return','positive_assets','neighbour_parameters_positive','round_trips','completion_fraction','doubled_spread_slippage_return','bootstrap_mean_excess_ci_low','elapsed_seconds','bootstrap_confidence','fault_events','missing_days'):
        bad=evidence.copy();bad.pop(field)
        report=qualify_candidate(p,bad)
        assert not report['qualified_for_review']
    for dates in ([str(d) for d in pd.date_range(pd.Timestamp.now(tz='UTC')+pd.Timedelta(days=2),periods=30)],['2020-01-01']*30):
        bad=evidence.copy();bad['decision_dates_utc']=dates
        assert not qualify_candidate(p,bad)['qualified_for_review']
    for ids in (['one']*20,[],[{}]*20):
        bad=evidence.copy();bad['completed_lifecycle_ids']=ids
        assert not qualify_candidate(p,bad)['qualified_for_review']

def test_missing_and_nonfinite_research_metrics(tmp_path):
    p=tmp_path/'candidate.json';freeze_candidate(p,specification())
    for value in (float('nan'),float('inf'),-float('inf')):
        report=qualify_candidate(p,{'sharpe':value,'max_drawdown':value})
        assert not report['qualified_for_review']
