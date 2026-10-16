"""Freeze an actual tutorial backtest manifest for a research/paper exercise only."""
import argparse
import hashlib
import json
from pathlib import Path
import pandas as pd
from kraken_quant.candidate import freeze_candidate
from kraken_quant.config import CONFIG


def freeze_demo(manifest_path, output):
    manifest_path=Path(manifest_path)
    manifest=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    expected='examples.custom_strategy_example:backtest_factory'
    if manifest.get('strategy') != expected:
        raise ValueError('Tutorial manifest must use '+expected)
    if manifest.get('split') not in ('train','validation') or manifest.get('test_access') != 'locked':
        raise PermissionError('Tutorial requires an authorised retrospective backtest')
    if not manifest.get('input_hashes') or not manifest.get('output_sha256'):
        raise ValueError('Actual hashed platform backtest manifest required')
    for name,digest in manifest['output_sha256'].items():
        path=manifest_path.parent/name
        if path.parent.resolve()!=manifest_path.parent.resolve() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('Backtest output identity mismatch')
    root=Path(__file__).resolve().parents[1]
    sources=['examples/custom_strategy_example.py','kraken_quant/strategy.py','kraken_quant/research_execution.py','kraken_quant/candidate.py']
    spec=dict(strategy=expected,parameters=manifest.get('parameters',{'lookback':20,'weight':.05}),features='none',costs=manifest['costs'],risk_limits={'long_only':True,'max_gross':.1,'max_position_weight':.1,'max_daily_loss':.02},input_hashes=manifest['input_hashes'],code_revision=manifest['code_revision'],prospective_start=(pd.Timestamp.now(tz='UTC').normalize()+pd.Timedelta(days=1)).isoformat(),universe=list(CONFIG.universe),evaluation_split=manifest['split'],source_code_hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sources},seed=manifest.get('seed',17),research_only=True,data_kind=manifest.get('data_kind','unknown'),tutorial_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),limitation='Software tutorial only. Synthetic observations and accelerated replay never qualify live or count as actual future evidence.')
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    return freeze_candidate(output,spec)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();record=freeze_demo(args.manifest,args.output)
    print(json.dumps({'candidate_sha256':record['sha256'],'research_only':True,'live_authorized':False}))
