"""Explicit synthetic input mode for offline tests/demos; never a provider fallback."""
import numpy as np
import pandas as pd
from .research_market import OHLCV, resample_hourly, research_interval


def fixture_market(selected,start,end,timeframe,seed=7):
    start,end=research_interval(start,end)
    index=pd.date_range(start,end,freq='h',inclusive='left')
    rng=np.random.default_rng(seed)
    bars={}
    for item in selected:
        close=100*np.exp(np.cumsum(rng.normal(.0002,.004,len(index))))
        opening=np.r_[close[0],close[:-1]]
        hourly=pd.DataFrame(dict(open=opening,high=np.maximum(opening,close)*1.002,
            low=np.minimum(opening,close)*.998,close=close,volume=rng.uniform(100,1000,len(index))),index=index)
        frame,_=resample_hourly(hourly,timeframe,as_of=end)
        frame['fx_open']=frame['fx_close']=1.5 if item.instrument.quote!='AUD' else 1.
        bars[item.instrument]=frame
    return bars,dict(source='SYNTHETIC FIXTURE',genuine_market_data=False,seed=seed,
                     fx='synthetic constant valuation, not historical FX')
