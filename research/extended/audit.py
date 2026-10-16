"""Independent replay receipt and training-only execution sensitivity."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from research.phase2.original_study import prepare_study
from research.phase2.verify_results import verify_results
from .execution import execution_stress

def audit(root, output):
    study=prepare_study(Path(root)); directory=Path(output);directory.mkdir(parents=True,exist_ok=False)
    oos=study['frame'].loc['2024-01-01':].strategy_net
    equity=(1+oos).cumprod(); equity=np.r_[1,equity.to_numpy()]
    receipt={'phase1_reconciliation':study['coverage']['net_reconciliation_max_absolute_error'],'phase2_integrity':verify_results('phase2_results/reviewed_20261016'),'oos_sharpe':float(oos.mean()/oos.std(ddof=1)*np.sqrt(2190)),'oos_cumulative_return':float(equity[-1]-1),'oos_maximum_drawdown':float((equity/np.maximum.accumulate(equity)-1).min()),'source_hashes':study['source_hashes'],'classification':'Inspected historical evidence, not untouched holdout','trained_new_candidate_against_oos':False}
    (directory/'audit_receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf8')
    ns=study['namespace'];weights=study['momentumweights'].loc[:'2023-12-31'];returns=study['returns'].loc[weights.index]
    volume=ns['volume_4h'].copy();close=ns['close_4h'].copy()
    volume.index=volume.index.tz_localize('UTC');close.index=close.index.tz_localize('UTC')
    dollar_volume=(volume*close).loc[weights.index,weights.columns]
    rows=[]
    for name,kwargs in [('base',{}),('double_fee',{'fee':.0052}),('wide_spread',{'full_spread':.004}),('high_slippage',{'slippage':.003}),('high_impact',{'impact':.02}),('delay_two_bars',{'delay':2}),('delay_three_bars',{'delay':3}),('short_financing_30pct',{'annual_short_financing':.30}),('tight_liquidity',{'max_participation':.00001})]:
        try:
            frame=execution_stress(weights,returns,dollar_volume,periods_per_year=2190,**kwargs)
            r=frame.net_return;wealth=np.r_[1,(1+r).cumprod().to_numpy()]
            rows.append({'scenario':name,'status':'computed_fixed_notional_approximation','sharpe':float(r.mean()/r.std(ddof=1)*np.sqrt(2190)),'total_return':float(wealth[-1]-1),'drawdown':float((wealth/np.maximum.accumulate(wealth)-1).min()),'cost_sum':float(frame.execution_cost.sum()),'financing_sum':float(frame.short_financing.sum()),'maximum_participation':float(frame.maximum_participation.max())})
        except ArithmeticError as error:rows.append({'scenario':name,'status':str(error)})
    pd.DataFrame(rows).to_csv(directory/'training_execution_sensitivity.csv',index=False)
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',default='C:/Users/avyas');p.add_argument('--output',required=True)
    a=p.parse_args();audit(a.root,a.output)
