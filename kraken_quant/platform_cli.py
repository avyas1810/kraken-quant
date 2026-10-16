"""Research CLI: real CSV data or explicitly synthetic software demonstrations."""
import argparse
from dataclasses import asdict
import hashlib
import importlib
import json
import inspect
import subprocess
from pathlib import Path
import numpy as np
import pandas as pd
from .config import CONFIG, utc_day
from .data import DAY, HistoricalSnapshot
from .strategy import VenueHistory, StrategyPipeline
from .strategies import btc_factory, multi_factory
from .research_execution import ResearchCosts, run_research, research_buy_and_hold
from .candidate import freeze_candidate, qualify_candidate

def load_history(*, data_dir=None, demo=False, start, end, symbols, venue="user_supplied"):
    if (data_dir is None) == (not demo):
        raise ValueError('Choose exactly one: --demo or --data-dir')
    start, end = utc_day(start), utc_day(end)
    snapshots = []
    for number, symbol in enumerate(symbols):
        if demo:
            index = pd.date_range(start, end, freq='D')
            rng = np.random.default_rng(17 + number)
            prices = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.018, len(index))))
            bars = pd.DataFrame({'open': prices, 'high': prices*1.02, 'low': prices*0.98, 'close': prices, 'volume': 10000.0}, index=index)
        else:
            path = Path(data_dir) / (symbol.replace('/', '_') + '.csv')
            bars = pd.read_csv(path)
            if 'timestamp' not in bars: raise ValueError(str(path) + ': timestamp column required')
            bars.index = pd.to_datetime(bars.pop('timestamp'), utc=True)
            # Validate the selected authorised window, never silently fill gaps.
            bars = bars.loc[(bars.index >= utc_day(start)) & (bars.index <= utc_day(end))]
        snapshots.append(HistoricalSnapshot.create(bars, symbol=symbol, source='synthetic_demo' if demo else venue, source_symbol=symbol.replace('/USD','/USDT') if venue == 'binance' else symbol, start=start, end=end, as_of=utc_day(end)+DAY))
    return VenueHistory(snapshots)

def export_result(result, benchmark, directory, *, identity):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    pd.DataFrame({'equity': result.equity, 'cash': result.cash, 'pnl': result.equity.diff().fillna(0), 'return': result.equity.pct_change().fillna(0), 'benchmark_equity': benchmark.equity, 'financing': result.financing}).to_csv(directory/'equity.csv')
    positions = result.positions.copy()
    positions.columns = [str(c.symbol) for c in positions.columns]
    positions.to_csv(directory/'positions.csv')
    trades = []
    for fill in result.fills:
        row = asdict(fill)
        row['instrument'] = fill.instrument.symbol
        trades.append(row)
    pd.DataFrame(trades, columns=['instrument','decision_time','timestamp','quantity','reference_price','price','fee','spread_cost','slippage_cost','data_type']).to_csv(directory/'trades.csv', index=False)
    clean = lambda x: {k: None if isinstance(v,float) and not np.isfinite(v) else v for k,v in x.items()}
    metrics = {'strategy': clean(result.metrics()), 'benchmark': clean(benchmark.metrics())}
    (directory/'metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf8')
    identity['output_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
    (directory/'manifest.json').write_text(json.dumps(identity, indent=2), encoding='utf8')
    return metrics

def backtest(args):
    if args.split == 'test': raise PermissionError('Locked test cannot be opened by this CLI')
    start,end = utc_day(args.start), utc_day(args.end)
    period = CONFIG.split(args.split)
    if not period.start <= start <= end <= period.end: raise PermissionError('Interval crosses authorised split')
    symbols = [CONFIG.universe[0]] if args.strategy == 'btc_momentum' and not args.factory else CONFIG.universe
    history = load_history(data_dir=args.data_dir, demo=args.demo, start=start, end=end, symbols=symbols, venue=args.venue)
    factory = btc_factory if args.strategy == 'btc_momentum' else multi_factory
    if args.factory:
        module, name = args.factory.split(':')
        factory = getattr(importlib.import_module(module), name)
    parameters = json.loads(Path(args.parameters_file).read_text(encoding='utf-8-sig')) if args.parameters_file else json.loads(args.parameters or '{}')
    feature_builder = None
    feature_hash = None
    if args.feature_factory:
        feature_module, feature_name = args.feature_factory.split(':')
        feature_factory = getattr(importlib.import_module(feature_module),feature_name)
        feature_builder = feature_factory(parameters,args.seed)
        feature_hash = hashlib.sha256(inspect.getsource(feature_factory).encode()).hexdigest()
    costs = ResearchCosts(fee_rate=args.fee, spread_rate=args.spread, slippage_rate=args.slippage)
    kwargs = dict(split=args.split, start=start, end=end, costs=costs, initial_cash=args.cash)
    result = run_research(history, StrategyPipeline(factory(parameters, args.seed),feature_builder), **kwargs)
    benchmark = research_buy_and_hold(history, **kwargs)
    revision = subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
    identity = {'code_revision':revision,'strategy_factory_sha256':hashlib.sha256(inspect.getsource(factory).encode()).hexdigest(),'feature_factory':args.feature_factory,'feature_factory_sha256':feature_hash,'strategy': args.factory or args.strategy, 'parameters': parameters, 'seed': args.seed, 'split': args.split, 'start': str(start), 'end': str(end), 'costs': asdict(costs), 'data_kind': 'SYNTHETIC SOFTWARE DEMO; NOT EMPIRICAL EVIDENCE' if args.demo else 'user_supplied_historical_csv', 'input_hashes': {key.symbol: manifest.sha256 for key,manifest in history.manifests.items()}, 'test_access': 'locked'}
    metrics = export_result(result, benchmark, args.output, identity=identity)
    print(json.dumps({'output': str(args.output), 'data_kind': identity['data_kind'], 'metrics': metrics}, indent=2))
    return result

def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    b = sub.add_parser('backtest')
    b.add_argument('--venue', choices=['user_supplied','kraken','binance'], default='user_supplied'); b.add_argument('--data-dir'); b.add_argument('--demo', action='store_true')
    b.add_argument('--strategy', choices=['btc_momentum','multi_momentum'], default='btc_momentum')
    b.add_argument('--factory'); b.add_argument('--feature-factory')
    params = b.add_mutually_exclusive_group()
    params.add_argument('--parameters'); params.add_argument('--parameters-file')
    b.add_argument('--split', choices=['train','validation','test'], default='train')
    b.add_argument('--start', default='2022-01-01'); b.add_argument('--end', default='2022-12-31')
    b.add_argument('--output', required=True); b.add_argument('--cash', type=float, default=10000)
    b.add_argument('--fee', type=float, default=0.0026); b.add_argument('--spread', type=float, default=0.001); b.add_argument('--slippage', type=float, default=0.0005)
    b.add_argument('--seed', type=int, default=17)
    f = sub.add_parser('freeze'); f.add_argument('--spec', required=True); f.add_argument('--output',required=True)
    q = sub.add_parser('qualify'); q.add_argument('--candidate',required=True); q.add_argument('--evidence',required=True); q.add_argument('--output',required=True)
    return p

def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'backtest': return backtest(args)
    if args.command == 'freeze':
        result = freeze_candidate(args.output, json.loads(Path(args.spec).read_text(encoding='utf-8-sig')))
    else:
        result = qualify_candidate(args.candidate, json.loads(Path(args.evidence).read_text(encoding='utf-8-sig')))
        Path(args.output).write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result, indent=2))

if __name__ == '__main__': main()
