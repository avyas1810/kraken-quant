"""Daily candidate validation, separate from frozen four-hour historical inference."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
import statsmodels.api as sm
from research.phase2.dsr import deflated_sharpe

def validate_daily(equity_path, *, trial_dispersion, trial_counts=(2,10,25,50,100), replications=1999, seed=17, block_lengths=(5,10,20)):
    frame = pd.read_csv(equity_path,index_col=0,parse_dates=True)
    index=frame.index
    if not isinstance(index,pd.DatetimeIndex) or str(index.tz) != 'UTC' or not index.is_unique or not index.equals(pd.date_range(index[0],index[-1],freq='D')): raise ValueError('Complete UTC daily equity required')
    for column in ('equity','benchmark_equity'):
        values=frame[column].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values<=0).any(): raise ValueError('Finite positive complete equity required')
    strategy=frame.equity.pct_change(fill_method=None).iloc[1:]
    benchmark=frame.benchmark_equity.pct_change(fill_method=None).iloc[1:]
    values=np.column_stack([strategy,benchmark])
    if len(values)<30 or not np.isfinite(values).all() or (values<=-1).any(): raise ValueError('At least30 finite daily returns required')
    if replications<99 or trial_dispersion<0: raise ValueError('At least99 replications and nonnegative empirical/assumed trial dispersion required')
    sharpe=lambda x: x.mean(axis=0)/x.std(axis=0,ddof=1)*np.sqrt(365)
    observed=sharpe(values)
    if not np.isfinite(observed).all(): raise ValueError('Constant return series')
    bootstrap=[]
    for length in block_lengths:
        if not 1<=length<len(values): raise ValueError('Invalid block length')
        rng=np.random.default_rng(np.random.SeedSequence([seed,length]))
        draws=[];mean_draws=[]
        for _ in range(replications):
            starts=rng.integers(0,len(values)-length+1,size=int(np.ceil(len(values)/length)))
            selected=(starts[:,None]+np.arange(length)).ravel()[:len(values)]
            draws.append(sharpe(values[selected]));mean_draws.append(values[selected].mean(axis=0))
        draws=np.asarray(draws)
        if not np.isfinite(draws).all(): raise ValueError('Degenerate bootstrap sample')
        for label,vector,estimate in [('strategy',draws[:,0],observed[0]),('paired_difference',draws[:,0]-draws[:,1],observed[0]-observed[1])]:
            low,high=np.quantile(vector,[.025,.975]);bootstrap.append({'block_days':length,'metric':'sharpe','comparison':label,'sharpe':float(estimate),'ci_low':float(low),'ci_high':float(high),'centered_one_sided_p':float((1+np.count_nonzero(vector-estimate>=estimate))/(replications+1))})
        mean_draws=np.asarray(mean_draws)
        excess=mean_draws[:,0]-mean_draws[:,1]
        low,high=np.quantile(excess,[.025,.975])
        bootstrap.append({'block_days':length,'metric':'mean_excess_per_day','comparison':'paired_difference','estimate':float((values[:,0]-values[:,1]).mean()),'ci_low':float(low),'ci_high':float(high),'confidence':.95})
    model=sm.OLS(strategy.to_numpy(),sm.add_constant(benchmark.to_numpy())).fit()
    regressions=[]
    for lag in (1,5,10,20):
        fit=model.get_robustcov_results(cov_type='HAC',maxlags=lag,use_correction=True,use_t=True)
        regressions.append({'lag_days':lag,'alpha_annual_arithmetic':float(fit.params[0]*365),'alpha_pvalue':float(fit.pvalues[0]),'beta':float(fit.params[1])})
    # Circular relative-return alignment null, explicitly descriptive and not a strategy-mask placebo.
    rng=np.random.default_rng(seed);obs=float((strategy-benchmark).mean());null=[]
    for _ in range(replications):
        shifted=np.roll(strategy.to_numpy(),rng.integers(1,len(values)))
        null.append(float(np.corrcoef(shifted,benchmark)[0,1]))
    actual=float(np.corrcoef(strategy,benchmark)[0,1])
    return {'schema':1,'source_sha256':hashlib.sha256(Path(equity_path).read_bytes()).hexdigest(),'seed':seed,'replications':replications,'bootstrap':bootstrap,'dsr':deflated_sharpe(strategy,trial_dispersion,trial_counts),'benchmark_hac':regressions,'alignment_placebo':{'metric':'absolute strategy/benchmark correlation; not profitability inference','observed':actual,'two_sided_p':float((1+sum(abs(x)>=abs(actual) for x in null))/(replications+1))},'holdout_status':'Retrospective train/authorised validation only; no untouched claim','qualified_for_live':False}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--equity',required=True);p.add_argument('--trial-dispersion',required=True,type=float);p.add_argument('--replications',default=1999,type=int);p.add_argument('--output',required=True)
    a=p.parse_args();Path(a.output).write_text(json.dumps(validate_daily(a.equity,trial_dispersion=a.trial_dispersion,replications=a.replications),indent=2),encoding='utf8')
