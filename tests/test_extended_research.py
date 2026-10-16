import numpy as np
import pandas as pd
import pytest
from research.extended.universe import AssetHistory,eligibility
from research.extended.execution import execution_stress

def test_thirty_asset_point_in_time_listing_delisting_missing_data():
    index=pd.date_range('2022-01-01',periods=100,tz='UTC');columns=[f'A{i}' for i in range(30)]
    close=pd.DataFrame(100.,index=index,columns=columns);volume=close*1e6
    metadata={c:AssetHistory(c,index[0],index[80] if c=='A0' else None,index[0],'historical_listing_archive','a'*64) for c in columns}
    metadata['A1']=AssetHistory('A1',index[50],None,index[50],'archive','a'*64)
    close.loc[index[40],'A2']=np.nan
    mask=eligibility(close,volume,metadata)
    assert not mask.iloc[:30].any().any()
    assert mask.loc[index[40],'A0'] and not mask.loc[index[80],'A0']
    assert not mask.loc[index[60],'A1'] and mask.loc[index[80],'A1']
    assert not mask.loc[index[41:71],'A2'].any()
    changed=volume.copy();changed.loc[index[90]:,'A3']=0
    assert eligibility(close,changed,metadata).loc[:index[90]].equals(mask.loc[:index[90]])

def test_execution_delay_liquidity_and_financing():
    index=pd.date_range('2022-01-01',periods=100,tz='UTC');rng=np.random.default_rng(7)
    returns=pd.DataFrame(rng.normal(.001,.01,(100,2)),index=index,columns=['a','b']);weights=returns*0;weights['a']=.4;weights['b']=-.2
    volume=returns*0+1e7
    free=execution_stress(weights,returns,volume,fee=0,full_spread=0,slippage=0,impact=0,annual_short_financing=0)
    costs=execution_stress(weights,returns,volume)
    assert costs.net_return.sum()<free.net_return.sum() and costs.short_financing.sum()>0
    assert free.gross_return.iloc[0]==0
    with pytest.raises(ArithmeticError):execution_stress(weights,returns,volume*1e-7)
    with pytest.raises(ValueError):execution_stress(weights,returns,volume,delay=0)

def test_invalid_expanded_inputs_rejected():
    index=pd.date_range('2022-01-01',periods=40,tz='UTC');close=pd.DataFrame(100.,index=index,columns=['a']);volume=close*1e6
    metadata={'a':AssetHistory('a',index[0],None,index[0],'archive','a'*64)}
    for panel in ('close','volume'):
        bad=close.copy() if panel=='close' else volume.copy();bad.iloc[5,0]=float('inf')
        with pytest.raises(ValueError):eligibility(bad if panel=='close' else close,bad if panel=='volume' else volume,metadata,minimum_assets=1)
    with pytest.raises(ValueError):eligibility(close,volume,metadata,lookback=True,minimum_assets=1)
    with pytest.raises(ValueError):AssetHistory('a',index[0],None,index[0],'archive','z'*64)
    bad={'a':AssetHistory('other',index[0],None,index[0],'archive','a'*64)}
    with pytest.raises(ValueError):eligibility(close,volume,bad,minimum_assets=1)
    returns=close*0+.01;weights=close*0+.5
    with pytest.raises(ValueError):execution_stress(weights,returns,volume,fee=float('nan'))
    returns.iloc[4,0]=-1
    with pytest.raises(ValueError):execution_stress(weights,returns,volume)
