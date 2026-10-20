"""Notebook research orchestration: explicit assets, trained strategies, net ledger.

The legacy frozen study and locked universe are untouched. General experiments
are separately identified, and final holdout dates are still inaccessible.
"""
from dataclasses import dataclass, asdict
import copy
import hashlib
import inspect
import json
import types
from pathlib import Path
import platform
import subprocess

import numpy as np
import pandas as pd

from .config import CONFIG
from .data import canonical_json, utc_instant
from .experiment import ParameterRegistry, digest
from .research_market import research_interval, TIMEFRAMES, next_boundary, boundaries
from .research_sizing import KellyConfig, PortfolioLimits, estimate_kelly
from .research_ledger import simulate, estimate_net_returns, validate_panel, scenario_costs, EXECUTION_COSTS
from .research_report import net_metrics
from .strategy import StrategyPipeline


@dataclass(frozen=True)
class ResearchSpec:
    timeframe: str = '4h'
    train_end: str = '2024-01-01T00:00:00Z'
    initial_cash: float = 5000.
    base_currency: str = 'AUD'
    calibration_bars: int = 36
    embargo_bars: int = 0
    rebalance_every: int = 1
    seed: int = 7
    strategy_id: str = 'user-strategy'

    def __post_init__(self):
        if self.timeframe not in TIMEFRAMES or self.base_currency != 'AUD':
            raise ValueError('Choose a supported timeframe; reporting currently supports AUD base accounts')
        research_interval(CONFIG.data_start, utc_instant(self.train_end))
        for field,minimum in (('calibration_bars',3),('embargo_bars',0),('rebalance_every',1),('seed',0)):
            value=getattr(self,field)
            if not isinstance(value,int) or isinstance(value,bool) or value<minimum:
                raise ValueError(f'Invalid {field}')
        if not np.isfinite(self.initial_cash) or self.initial_cash<=0 or not self.strategy_id.strip():
            raise ValueError('Positive capital and strategy identity required')


class MomentumExample:
    """Example only: arbitrary strategies implement target_weights(context).

    Signals [-1,1] are direction/strength before Kelly and risk constraints.
    Optional fit(training_frames) learns from training only, with no OOS access.
    Optional feature_builder.fit(training_frames) follows the same contract.
    """
    required_features=()
    def __init__(self,lookback=6):
        if not isinstance(lookback,int) or lookback<1: raise ValueError('Positive lookback required')
        self.lookback=lookback

    def target_weights(self,context):
        return {k:float(np.sign(f.close.iloc[-1]/f.close.iloc[-1-self.lookback]-1))
            if len(f)>self.lookback else 0. for k,f in context.historical_data.items()}


def momentum_factory(parameters,seed):
    return MomentumExample(**parameters)


class HoldBenchmark:
    def __init__(self): self.first=True
    def target_weights(self,context):
        if self.first:
            self.first=False
            return {k:1/len(context.historical_data) for k in context.historical_data}
        return None


def fit_pipeline(factory,parameters,seed,frames,feature_factory=None):
    strategy=factory(copy.deepcopy(parameters),seed)
    if callable(getattr(strategy,'fit',None)):
        strategy.fit({k:f.copy(deep=True) for k,f in frames.items()})
    builder=None if feature_factory is None else feature_factory(copy.deepcopy(parameters),seed)
    if builder is not None and callable(getattr(builder,'fit',None)):
        builder.fit({k:f.copy(deep=True) for k,f in frames.items()})
    if getattr(strategy,'required_features',()) and builder is None:
        raise ValueError('Strategy requires features: supply feature_factory')
    return StrategyPipeline(strategy,builder)


def panel_hash(bars):
    return {str(k):digest(f.to_csv(lineterminator='\n').encode()) for k,f in bars.items()}


def source_identity(factory):
    """Notebook and module factories supported; capture callable code and class source."""
    try: return inspect.getsource(factory)
    except (OSError,TypeError):
        def fingerprint(code):
            return dict(bytecode=code.co_code.hex(),names=code.co_names,
                constants=[fingerprint(c) if isinstance(c,types.CodeType) else repr(c) for c in code.co_consts])
        code=getattr(factory,'__code__',None)
        if code is not None: return canonical_json(fingerprint(code)).decode()
        if isinstance(factory,type):
            methods={name:fingerprint(method.__code__) for name,method in vars(factory).items() if hasattr(method,'__code__')}
            if methods: return canonical_json(methods).decode()
        raise ValueError('Supply an inspectable Python strategy/feature factory')


@dataclass
class ResearchRun:
    train: object
    validation: object
    benchmarks: dict
    table: pd.DataFrame
    estimates: dict
    recommendation: pd.DataFrame
    manifest: dict
    sensitivity: pd.DataFrame | None = None

    def write(self,directory):
        """Immutable ledger exports plus content hashes, including every report input."""
        payloads={}
        for name,result in dict(train=self.train,validation=self.validation,
                                benchmark_train=self.benchmarks['in-sample'],benchmark_validation=self.benchmarks['out-of-sample']).items():
            for field in ('ledger','positions','weights','attribution','gross_attribution','costs','trades','closed_trades','kelly_weights','signals'):
                payloads[f'{name}_{field}.csv']=getattr(result,field).to_csv(lineterminator='\n').encode()
        payloads['report.csv']=self.table.to_csv(lineterminator='\n').encode()
        payloads['recommendations.csv']=self.recommendation.to_csv(index=False,lineterminator='\n').encode()
        if self.sensitivity is not None:
            payloads['training_sensitivity.csv']=self.sensitivity.to_csv(index=False,lineterminator='\n').encode()
        receipt=dict(self.manifest,artifacts={name:digest(body) for name,body in payloads.items()})
        path=Path(directory)/self.manifest['run_id']
        if path.exists():
            if (path/'manifest.json').read_bytes()!=canonical_json(receipt) or any((path/name).read_bytes()!=body for name,body in payloads.items()):
                raise ValueError('Existing experiment artifacts differ; refusing overwrite')
            return path
        path.mkdir(parents=True)
        for name,body in payloads.items(): (path/name).write_bytes(body)
        (path/'manifest.json').write_bytes(canonical_json(receipt))
        return path


def recommendations(result,assets,costs,spec,limits,estimate):
    state=result.state
    timestamp=state['timestamp']
    if estimate.fitted_through>timestamp: raise PermissionError('Future Kelly estimate')
    pending=state['pending']
    desired=state['positions'] if pending is None else pending[1]
    current=state['positions']; equity=state['equity']; prices=state['prices']
    # Reserve estimated execution costs using only the latest completed marks.
    def estimated(changes):
        return sum(abs(changes[a.instrument])*prices[a.instrument]*(costs[a.instrument].commission+
            costs[a.instrument].full_spread/2+costs[a.instrument].slippage+costs[a.instrument].impact+
            costs[a.instrument].exchange+costs[a.instrument].regulatory+
            ((costs[a.instrument].fx or 0) if a.instrument.quote!=spec.base_currency else 0)) for a in assets)
    change={k:desired[k]-current[k] for k in current}
    # Do not shrink required de-risking; scale additions only for forecast cash/gross.
    reductions,adds={},{}
    for k,old in current.items():
        new=desired[k]
        kept=np.sign(old)*min(abs(old),abs(new)) if old*new>0 else 0.
        reductions[k]=kept-old; adds[k]=new-kept
    def valid(scale):
        delta={k:reductions[k]+adds[k]*scale for k in current}
        fee=estimated(delta)
        cash=state['cash']-sum(delta[k]*prices[k] for k in current)-fee
        target={k:current[k]+delta[k] for k in current}
        gross=sum(abs(target[k])*prices[k] for k in current)
        return ((limits.allow_margin or cash>=-1e-8) and gross<=limits.max_gross*(equity-fee)+1e-8
                and all(abs(target[k])*prices[k] <= limits.max_position*(equity-fee)+1e-8 for k in current))
    lo,hi=0.,1.
    if valid(1): lo=1.
    else:
        for _ in range(48):
            mid=(lo+hi)/2
            if valid(mid): lo=mid
            else: hi=mid
    change={k:reductions[k]+adds[k]*lo for k in current}
    target={k:(current[k]+change[k])*prices[k]/equity for k in current}
    total_cost=estimated(change)
    cash_weight=state['cash']/equity-sum(change[k]*prices[k]/equity for k in current)-total_cost/equity
    gross=sum(abs(w) for w in target.values()); net=sum(target.values())
    rows=[]
    execution_time=timestamp
    # A final-bar preview does not override a strategy's rebalance schedule.
    wait=0 if state['halted'] else (-state['bar_number']) % spec.rebalance_every
    for _ in range(wait): execution_time=next_boundary(execution_time,spec.timeframe)
    # At the close/open boundary the next eligible 24/7 bar opens now. This is a
    # historical target, never a current executable quote; freshness blocks handoff.
    for a in assets:
        k=a.instrument; value=target[k]
        rows.append(dict(asset=k.symbol,instrument=k,decision_timestamp=timestamp,
            next_execution_timestamp=execution_time,following_bar_end=next_boundary(execution_time,spec.timeframe),
            current_weight=current[k]*prices[k]/equity,target_weight=value,
            required_weight_change=change[k]*prices[k]/equity,direction='long' if value>0 else 'short' if value<0 else 'flat',
            estimated_cost_aud=estimated({j:change[j] if j==k else 0 for j in current}),
            cash_allocation=cash_weight,gross_exposure=gross,net_exposure=net,
            kelly_raw=float(estimate.raw[k]),eligible=a.eligible,
            risk_status=state['risk_status'] if valid(lo) else 'requires manual reconciliation',
            valuation_price_aud=prices[k],portfolio_equity_aud=equity,
            note='historical target; latest quotes/reconciliation required; fills not guaranteed'))
    return pd.DataFrame(rows)


def run_research_workflow(bars,assets,strategy_factory,*,parameters=None,spec=None,costs=None,
                          limits=None,kelly=None,feature_factory=None,provenance=None,registry_directory=None,
                          parameter_grid=None):
    spec=ResearchSpec() if spec is None else spec
    limits=PortfolioLimits() if limits is None else limits
    kelly=KellyConfig() if kelly is None else kelly
    parameters={} if parameters is None else parameters
    validate_panel(bars,assets)
    costs={a.instrument:scenario_costs() for a in assets} if costs is None else costs
    for a in assets: costs[a.instrument].validate(a,limits,spec.base_currency)
    first=bars[assets[0].instrument]
    edges=boundaries(first.index[0],first.bar_end.iloc[-1],spec.timeframe)
    if any(t not in edges or e not in edges or next_boundary(t,spec.timeframe)!=e for t,e in zip(first.index,first.bar_end)):
        raise ValueError('Bar boundaries do not match the specified timeframe')
    cut=utc_instant(spec.train_end)
    train={k:f.loc[f.bar_end<=cut].copy() for k,f in bars.items()}
    if len(next(iter(train.values())))<=spec.calibration_bars+spec.embargo_bars+2:
        raise ValueError('Extend training data: calibration/embargo leaves too few scored bars')
    prefix={k:f.iloc[:spec.calibration_bars].copy() for k,f in train.items()}
    # Calibration labels end before the first scored open; embargo removes further bars.
    train_start=next(iter(train.values())).index[spec.calibration_bars+spec.embargo_bars]
    validation={k:f.loc[f.index>=cut].iloc[spec.embargo_bars:].copy() for k,f in bars.items()}
    if len(next(iter(validation.values())))<2:
        raise ValueError('At least two non-overlapping validation bars required')
    training_estimate=estimate_kelly(estimate_net_returns(prefix,assets,costs,spec.base_currency),kelly)
    validation_estimate=estimate_kelly(estimate_net_returns(train,assets,costs,spec.base_currency),kelly)
    train_pipeline=fit_pipeline(strategy_factory,parameters,spec.seed,prefix,feature_factory)
    validation_pipeline=fit_pipeline(strategy_factory,parameters,spec.seed,train,feature_factory)
    args=dict(assets=assets,costs=costs,initial_cash=spec.initial_cash,limits=limits,
              base_currency=spec.base_currency,rebalance_every=spec.rebalance_every)
    root=Path(__file__).resolve().parents[1]
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    trial=None
    if registry_directory is not None:
        # Register before scoring either period, not retroactively after results.
        trial=ParameterRegistry(registry_directory,max_trials_per_hypothesis=1000).register(
            'general reusable research workflow',spec.strategy_id,dict(parameters=parameters,spec=asdict(spec),
                risk=asdict(limits),kelly=asdict(kelly),costs={str(k):asdict(c) for k,c in costs.items()},
                source_sha256=digest(source_identity(strategy_factory).encode()),parameter_grid=parameter_grid),
            code_revision=revision,seed=spec.seed,input_hashes=panel_hash(bars))
    sensitivity=None if parameter_grid is None else training_sensitivity(bars,assets,strategy_factory,
        parameter_grid,spec=spec,costs=costs,limits=limits,kelly=kelly,feature_factory=feature_factory)
    tr=simulate(train,pipeline=train_pipeline,estimate=training_estimate,start=train_start,**args)
    # Include prior training history for causal feature warmup, never score it twice.
    validation_start=next(iter(validation.values())).index[0]
    va=simulate(bars,pipeline=validation_pipeline,estimate=validation_estimate,start=validation_start,**args)
    benchmarks={}
    benchmark_limits=PortfolioLimits(max_gross=1.,max_position=1.,max_drawdown=1.,daily_loss=1.,cash_buffer=0.)
    for name,panel,start in (('in-sample',train,train_start),('out-of-sample',bars,validation_start)):
        benchmarks[name]=simulate(panel,pipeline=StrategyPipeline(HoldBenchmark()),estimate=None,
                                  start=start,use_kelly=False,**dict(args,limits=benchmark_limits))
    table=pd.DataFrame({name:net_metrics(result) for name,result in
        dict(**{'in-sample':tr,'out-of-sample':va},**{f'{k} benchmark':v for k,v in benchmarks.items()}).items()}).T
    recommendation=recommendations(va,assets,costs,spec,limits,validation_estimate)
    code={p.name:digest(p.read_bytes()) for p in Path(__file__).parent.glob('research_*.py')}
    code['strategy.py']=digest((Path(__file__).parent/'strategy.py').read_bytes())
    identity=dict(schema=2,spec=asdict(spec),assets=[asdict(a) for a in assets],
        strategy_source=source_identity(strategy_factory),feature_source=None if feature_factory is None else source_identity(feature_factory),
        strategy_class_source=source_identity(type(validation_pipeline.strategy)),
        feature_class_source=None if validation_pipeline.feature_builder is None else source_identity(
            validation_pipeline.feature_builder if isinstance(validation_pipeline.feature_builder,types.FunctionType) else type(validation_pipeline.feature_builder)),
        parameters=parameters,costs={str(k):asdict(c) for k,c in costs.items()},limits=asdict(limits),kelly=asdict(kelly),
        training_parameter_grid=parameter_grid,
        benchmark_limits=asdict(benchmark_limits),benchmark='equal-weight buy-and-hold, fully funded, same execution costs, no Kelly/stops',
        input_hashes=panel_hash(bars),provenance=provenance or {'warning':'caller-supplied data; authenticity not asserted'},
        code_revision=revision,engine_hashes=code,python=platform.python_version(),numpy=np.__version__,pandas=pd.__version__,
        score_windows=dict(train=[train_start.isoformat(),next(iter(train.values())).bar_end.iloc[-1].isoformat()],
                           validation=[validation_start.isoformat(),va.equity.index[-1].isoformat()]),
        final_holdout='locked',kelly_proxy='one-bar round-trip cost-adjusted AUD returns; conservative constant carry',
        cost_coverage='all applicable spot categories explicit; execution/FX estimates are assumptions, historical quote coverage incomplete',
        trade_statistics='FIFO closed quantities include allocated entry/exit/holding costs; open lots reported separately')
    if trial is not None:
        identity['trial_id']=trial.trial_id
    identity['run_id']=digest(canonical_json(identity))
    # JSON-safe undefined metrics, without replacing displayed NaNs with zero.
    identity['performance']=json.loads(table.to_json(orient='index'))
    return ResearchRun(tr,va,benchmarks,table,{'in-sample':training_estimate,'out-of-sample':validation_estimate},recommendation,identity,sensitivity)


def walk_forward(bars,assets,strategy_factory,folds,**kwargs):
    """Expanding historical folds; next fold may learn prior folds, never its own OOS.

    folds are (ResearchSpec, validation_end_exclusive). No final-holdout unlock.
    Independent capital per fold; overlapping validation windows are rejected.
    """
    results=[]; previous_end=None
    for spec,end in folds:
        cut,end=research_interval(spec.train_end,end)
        if previous_end is not None and cut<previous_end:
            raise ValueError('Walk-forward validation windows overlap')
        panel={k:f.loc[f.bar_end<=end].copy() for k,f in bars.items()}
        results.append(run_research_workflow(panel,assets,strategy_factory,spec=spec,**kwargs))
        previous_end=end
    return results


def training_sensitivity(bars,assets,strategy_factory,parameter_grid,*,spec,costs,limits=None,
                         kelly=None,feature_factory=None):
    """Explicit grid evaluated on training only; no validation construction/access.

    No automatic selection. Record the grid with the experiment specification;
    once OOS is inspected, changing the grid/selection is a new research trial.
    """
    validate_panel(bars,assets)
    limits=PortfolioLimits() if limits is None else limits
    kelly=KellyConfig() if kelly is None else kelly
    train={k:f.loc[f.bar_end<=utc_instant(spec.train_end)].copy() for k,f in bars.items()}
    first=next(iter(train.values()))
    if len(first)<=spec.calibration_bars+spec.embargo_bars+2:
        raise ValueError('Training sensitivity needs more calibration/scoring bars')
    prefix={k:f.iloc[:spec.calibration_bars].copy() for k,f in train.items()}
    for a in assets: costs[a.instrument].validate(a,limits,spec.base_currency)
    estimate=estimate_kelly(estimate_net_returns(prefix,assets,costs,spec.base_currency),kelly)
    rows=[]
    for parameters in parameter_grid:
        pipeline=fit_pipeline(strategy_factory,parameters,spec.seed,prefix,feature_factory)
        result=simulate(train,assets,pipeline,costs,estimate,initial_cash=spec.initial_cash,
            limits=limits,base_currency=spec.base_currency,rebalance_every=spec.rebalance_every,
            start=first.index[spec.calibration_bars+spec.embargo_bars])
        rows.append(dict(parameters=json.dumps(parameters,sort_keys=True),period='training only',**net_metrics(result)))
    return pd.DataFrame(rows)
