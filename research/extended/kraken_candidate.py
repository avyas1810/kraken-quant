"""Reproducible fixed-parameter training evaluation on trusted original local caches.

The data are Binance USDT proxies, not Kraken USD executions. Raw files remain local.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import pandas as pd
from kraken_quant.config import CONFIG
from kraken_quant.data import HistoricalSnapshot, DAY
from kraken_quant.strategy import VenueHistory, StrategyPipeline
from kraken_quant.strategies import multi_factory
from kraken_quant.research_execution import run_research, research_buy_and_hold, ResearchCosts
from kraken_quant.platform_cli import export_result
from kraken_quant.candidate import freeze_candidate

PARAMETERS = {'lookback':30,'regime_days':60,'volatility_days':20,'annual_vol_limit':1.0,'budget':0.8}

def evaluate(cache, output, revision):
    cache=Path(cache);output=Path(output)
    snapshots=[];hashes={}
    for symbol in CONFIG.universe:
        path=cache/('PX_'+symbol.replace('/USD','_USDT')+'_1d.pk')
        raw=path.read_bytes(); hashes[path.name]=hashlib.sha256(raw).hexdigest()
        # Explicitly trusted local TWSQ pickle; never load arbitrary downloaded pickle.
        bars=pd.read_pickle(path)
        if bars.index.tz is None: bars.index=bars.index.tz_localize('UTC')
        bars=bars.loc[CONFIG.splits[0].start:CONFIG.splits[0].end]
        snapshots.append(HistoricalSnapshot.create(bars,symbol=symbol,source='binance',source_symbol=symbol.replace('/USD','/USDT'),start=CONFIG.splits[0].start,end=CONFIG.splits[0].end,as_of=CONFIG.splits[0].end+DAY))
    history=VenueHistory(snapshots)
    costs=ResearchCosts(fee_rate=.0026,spread_rate=.001,slippage_rate=.0005)
    args=dict(split='train',start=CONFIG.splits[0].start,end=CONFIG.splits[0].end,costs=costs,initial_cash=10000)
    result=run_research(history,StrategyPipeline(multi_factory(PARAMETERS)),**args)
    benchmark=research_buy_and_hold(history,**args)
    identity={'strategy':'kraken_spot_multi_momentum_v1','parameters':PARAMETERS,'source':'Authentic local Binance USDT proxy training data; no Kraken fill claim','input_hashes':hashes,'split':'train','test_access':'locked','validation_read':False,'code_revision':revision,'costs':asdict(costs),'selection':'Single fixed specification, no optimisation'}
    metrics=export_result(result,benchmark,output,identity=identity)
    spec={'strategy':identity['strategy'],'parameters':PARAMETERS,'features':'Causal completed daily OHLCV; BTC trailing mean; trailing volatility; 30day momentum','costs':asdict(costs),'risk_limits':{'long_only':True,'max_gross':.8,'max_daily_loss':.02,'max_position_weight':.8},'input_hashes':hashes,'code_revision':revision,'prospective_start':(pd.Timestamp.now(tz='UTC').normalize()+pd.Timedelta(days=1)).isoformat(),'universe':list(CONFIG.universe),'evaluation_split':'train','data_limitation':'Binance USDT proxy; authentic Kraken prospective data still required'}
    spec['source_code_hashes']={name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in ['kraken_quant/strategies.py','kraken_quant/research_execution.py','kraken_quant/strategy.py','kraken_quant/candidate.py','research/extended/kraken_candidate.py']}
    frozen=freeze_candidate(output/'candidate.json',spec)
    from kraken_quant.candidate import qualify_candidate
    evidence={'candidate_sha256':frozen['sha256'],'sharpe':metrics['strategy']['sharpe'],'max_drawdown':metrics['strategy']['max_drawdown'],'source':'training_binance_proxy'}
    (output/'qualification.json').write_text(json.dumps(qualify_candidate(output/'candidate.json',evidence),indent=2),encoding='utf8')
    print(json.dumps(metrics,indent=2))
    return metrics

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--trusted-cache',required=True);p.add_argument('--output',required=True);p.add_argument('--code-revision',required=True)
    a=p.parse_args();evaluate(a.trusted_cache,a.output,a.code_revision)
