"""Reconstruct recorded trials and quantify sensitivity; unknown trials stay unknown."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from research.phase2.dsr import deflated_sharpe

def trial_audit(path, output):
    frame = pd.read_csv(path,index_col=0,parse_dates=True)
    if frame.columns.has_duplicates or not np.isfinite(frame.to_numpy()).all(): raise ValueError('Invalid trial archive')
    train = frame.loc[frame.index < pd.Timestamp('2024-01-01')]
    oos = frame.loc[frame.index >= pd.Timestamp('2024-01-01')]
    target = oos['4h_mom_low_correlation']
    sharpes = train.mean()/train.std(ddof=1)
    corr = train.corr()
    eigenvalues = np.linalg.eigvalsh(corr.to_numpy())
    descriptive_rank = float(eigenvalues.sum()**2/(eigenvalues**2).sum())
    rows = []
    for factor in (0.5,1,2):
        for row in deflated_sharpe(target,float(sharpes.std(ddof=1))*factor,trial_counts=(2,10,18,25,50,100,250)):
            rows.append(dict(row,dispersion_multiplier=factor))
    directory = Path(output); directory.mkdir(parents=True,exist_ok=False)
    pd.DataFrame(rows).to_csv(directory/'dsr_sensitivity.csv',index=False)
    corr.to_csv(directory/'recorded_trial_correlations.csv')
    record = {'recorded_trial_count':len(frame.columns),'recorded_names':list(frame.columns),'unknown_experiments':'Unknown; notebooks and human iterations not exhaustively logged','effective_independent_trials':'Not identified; correlation rank is descriptive only','correlation_participation_rank':descriptive_rank,'training_dispersion_per_period':float(sharpes.std(ddof=1)),'oos_inspected':True,'conclusion':'DSR is sensitive to unknown effective trial count and dispersion; no unique selection-adjusted probability'}
    (directory/'trial_audit.json').write_text(json.dumps(record,indent=2),encoding='utf8')
    print(json.dumps(record,indent=2))

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--returns',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();trial_audit(a.returns,a.output)
