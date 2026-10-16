import json
import numpy as np
import pandas as pd
import pytest
from kraken_quant.platform_cli import main, load_history
from kraken_quant.strategy import StrategyPipeline
from kraken_quant.strategies import BTCMomentum, MultiAssetMomentum
from kraken_quant.research_execution import ResearchCosts, run_research

@pytest.mark.parametrize('strategy',['btc_momentum','multi_momentum'])
def test_actual_cli_fills_accounting_and_exports(tmp_path,strategy):
    output=tmp_path/strategy
    result=main(['backtest','--demo','--strategy',strategy,'--start','2022-01-01','--end','2022-04-30','--output',str(output)])
    assert result.fills and (result.cash>=0).all() and (result.positions>=0).all().all()
    assert all(f.timestamp>=f.decision_time for f in result.fills)
    assert json.loads((output/'manifest.json').read_text())['data_kind'].startswith('SYNTHETIC')
    assert {'trades.csv','positions.csv','equity.csv','metrics.json','manifest.json'} <= {p.name for p in output.iterdir()}
    equity=pd.read_csv(output/'equity.csv');assert np.isclose(equity.pnl.sum(),equity.equity.iloc[-1]-equity.equity.iloc[0])
    assert result.metrics()['fees']>0

def test_locked_test_rejected_before_data_access(tmp_path):
    with pytest.raises(PermissionError):main(['backtest','--data-dir','nonexistent','--split','test','--start','2025-01-01','--end','2025-02-01','--output',str(tmp_path/'x')])

def test_custom_factory_real_csv_and_gap_rejection(tmp_path):
    index=pd.date_range('2022-01-01','2022-03-31',tz='UTC')
    for symbol in ('BTC','ETH','XRP','SOL','ADA'):
        p=np.linspace(100,150,len(index))
        pd.DataFrame({'timestamp':index,'open':p,'high':p*1.01,'low':p*.99,'close':p,'volume':1000}).to_csv(tmp_path/(symbol+'_USD.csv'),index=False)
    result=main(['backtest','--data-dir',str(tmp_path),'--start','2022-01-01','--end','2022-03-31','--factory','kraken_quant.strategies:btc_factory','--output',str(tmp_path/'out')])
    assert result.fills
    path=tmp_path/'BTC_USD.csv';pd.read_csv(path).iloc[1:].to_csv(path,index=False)
    with pytest.raises(ValueError):load_history(data_dir=tmp_path,start=index[0],end=index[-1],symbols=['BTC/USD'])

def test_prefix_invariance_and_cost_drag():
    history=load_history(demo=True,start='2022-01-01',end='2022-06-30',symbols=['BTC/USD'])
    args=dict(split='train',start='2022-01-01',end='2022-06-30')
    free=run_research(history,StrategyPipeline(BTCMomentum()),**args)
    costly=run_research(history,StrategyPipeline(BTCMomentum()),costs=ResearchCosts(fee_rate=.01),**args)
    assert costly.equity.iloc[-1]<free.equity.iloc[-1]
    key=history.instruments[0];frame=history.window(key,split='train',start=args['start'],end=args['end'],decision_time=pd.Timestamp('2022-07-01',tz='UTC'))
    a=StrategyPipeline(BTCMomentum()).evaluate(timestamp=pd.Timestamp('2022-04-01',tz='UTC'),historical_data={key:frame.loc[:'2022-03-31']},positions={},cash=1000,equity=1000)
    frame.loc['2022-04-01':,'close']*=100
    b=StrategyPipeline(BTCMomentum()).evaluate(timestamp=pd.Timestamp('2022-04-01',tz='UTC'),historical_data={key:frame.loc[:'2022-03-31']},positions={},cash=1000,equity=1000)
    assert a==b


def test_parameters_file_and_daily_validation(tmp_path):
    from research.extended.validation import validate_daily
    parameters=tmp_path/'params.json';parameters.write_text('{"lookback":10,"budget":0.5}',encoding='utf-8-sig')
    output=tmp_path/'run'
    main(['backtest','--demo','--parameters-file',str(parameters),'--output',str(output)])
    result=validate_daily(output/'equity.csv',trial_dispersion=.05,replications=99)
    assert len(result['bootstrap'])==9 and len(result['dsr'])==5 and len(result['benchmark_hac'])==4
    assert not result['qualified_for_live']
    with pytest.raises(ValueError):validate_daily(output/'equity.csv',trial_dispersion=-1,replications=99)
    equity=pd.read_csv(output/'equity.csv');equity.iloc[1:].iloc[::2].to_csv(tmp_path/'gap.csv',index=False)
    with pytest.raises(ValueError):validate_daily(tmp_path/'gap.csv',trial_dispersion=.05,replications=99)

def test_operator_tutorial_freeze_verified_manifest(tmp_path):
    from examples.freeze_operator_demo import freeze_demo
    from kraken_quant.candidate import qualify_candidate
    output=tmp_path/'run'
    main(['backtest','--demo','--factory','examples.custom_strategy_example:backtest_factory','--output',str(output)])
    candidate=tmp_path/'tutorial.json'
    record=freeze_demo(output/'manifest.json',candidate)
    assert record['specification']['research_only'] is True
    assert not qualify_candidate(candidate,{})['qualified_for_review']
    (output/'trades.csv').write_text('tampered')
    with pytest.raises(ValueError):freeze_demo(output/'manifest.json',tmp_path/'tampered.json')

def test_validation_never_fills_missing_equity(tmp_path):
    from research.extended.validation import validate_daily
    output=tmp_path/'run';main(['backtest','--demo','--output',str(output)])
    frame=pd.read_csv(output/'equity.csv');frame.loc[30,'equity']=float('nan');frame.to_csv(tmp_path/'missing.csv',index=False)
    with pytest.raises(ValueError,match='complete equity'):validate_daily(tmp_path/'missing.csv',trial_dispersion=.05,replications=99)
    frame=pd.read_csv(output/'equity.csv',index_col=0,parse_dates=True);frame.index=frame.index.tz_convert('Australia/Sydney');frame.to_csv(tmp_path/'tz.csv')
    with pytest.raises(ValueError,match='UTC'):validate_daily(tmp_path/'tz.csv',trial_dispersion=.05,replications=99)
