"""Nonzero deterministic acceptance checks, separate from historical findings.

These deliberately engineered return paths verify mechanics and constraints.
No prices/fees are selected from inspected historical returns to force trades.
"""
from dataclasses import dataclass, replace
import numpy as np
import pandas as pd

from .boundary import ProductionAdapter
from .config import CONFIG
from .research_handoff import submit_recommended_weights
from .research_ledger import CostModel
from .research_market import asset
from .research_sizing import KellyConfig, PortfolioLimits, estimate_kelly
from .research_workflow import ResearchSpec, run_research_workflow
from .risk import AccountSnapshot, Quote, InstrumentRules, RiskGateway
from .strategy import Instrument, StrategyPipeline


class DirectionStrategy:
    """An independent plug-in exercising simultaneous signed signals."""
    def __init__(self,directions): self.directions=dict(directions)
    def target_weights(self,context):
        return {key:self.directions[key.symbol] for key in context.historical_data}


def direction_factory(parameters,seed):
    return DirectionStrategy(parameters['directions'])


def directional_fixture():
    selected=[asset(symbol,provider='fixture',shortable=True) for symbol in CONFIG.universe[:2]]
    index=pd.date_range('2023-12-01','2024-01-15',freq='4h',inclusive='left',tz='UTC')
    bars={}
    for number,item in enumerate(selected):
        direction=1 if number==0 else -1
        returns=direction*.004+.0003*np.sin(np.arange(len(index)))
        close=100*np.cumprod(1+returns)
        opening=np.r_[100.,close[:-1]]
        bars[item.instrument]=pd.DataFrame(dict(open=opening,high=np.maximum(opening,close)*1.001,
            low=np.minimum(opening,close)*.999,close=close,volume=1e6,
            bar_end=index+pd.Timedelta(hours=4),fx_open=1.5,fx_close=1.5),index=index)
    return selected,bars


def acceptance_costs(selected):
    return {a.instrument:CostModel(commission=.0002,full_spread=.0002,slippage=.0001,impact=.00005,
        fx=.0001,exchange=.00002,regulatory=.00001,annual_borrow=.05,annual_margin=.08,
        label='synthetic acceptance assumptions; not broker pricing') for a in selected}


def nonzero_production_preview():
    """Synthetic fresh snapshots; offline production class, no network or orders."""
    now=pd.Timestamp('2024-02-01T00:00:00Z')
    source=asset(CONFIG.universe[0]).instrument
    destination=Instrument('kraken',source.symbol,source.base,source.quote)
    rec=pd.DataFrame([dict(instrument=source,asset=source.symbol,target_weight=.003,
        current_weight=0.,required_weight_change=.003,decision_timestamp=now,
        next_execution_timestamp=now,risk_status='ok',eligible=True)])
    gateway=RiskGateway(':memory:')
    production=ProductionAdapter(StrategyPipeline(DirectionStrategy({source.symbol:1})),gateway,enabled=False)
    account=AccountSnapshot(now,now.normalize(),'1000',{},'1000')
    quotes={destination:Quote(destination,now,'99.99','100')}
    rules={destination:InstrumentRules('.001','.01','.001','1',now,destination)}
    try:
        reply=submit_recommended_weights(rec,production=production,now=now,account=account,
            quotes=quotes,rules=rules,instrument_map={source:destination},aud_per_usd=1.5,
            fx_timestamp=now,fee_rates={destination:.001},unresolved_orders=False,risk_approved=True)
        if not reply['production_intents'] or not all(intent.quantity>0 for intent in reply['production_intents']):
            raise AssertionError('Nonzero production dry-run intents required')
        if gateway.connection.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]!=0:
            raise AssertionError('Dry-run must preserve original production reservations')
        if production.enabled:
            raise AssertionError('Production must stay disabled')
        return reply
    finally:
        gateway.close()


@dataclass
class AcceptanceReport:
    checks: pd.DataFrame
    metrics: pd.DataFrame
    trades: pd.DataFrame
    recommendations: pd.DataFrame
    handoff: dict
    signed_run: object
    leveraged_run: object


def run_acceptance_checks():
    selected,bars=directional_fixture()
    directions={a.instrument.symbol:1 if n==0 else -1 for n,a in enumerate(selected)}
    spec=ResearchSpec(timeframe='4h',calibration_bars=36,rebalance_every=4,
                      strategy_id='SYNTHETIC-acceptance-directions')
    config=KellyConfig(fraction=.25,minimum_observations=20)
    limits=PortfolioLimits(max_gross=.8,max_position=1.,allow_short=True,max_drawdown=.9,daily_loss=.9)
    costs=acceptance_costs(selected)
    run=run_research_workflow(bars,selected,direction_factory,parameters={'directions':directions},
        spec=spec,costs=costs,limits=limits,kelly=config,
        provenance={'source':'synthetic acceptance fixture','genuine_market_data':False})
    rows=[]; trade_frames=[]
    for period,result in (('in-sample',run.train),('out-of-sample',run.validation)):
        if not (result.positions.min().min()<0 and result.positions.max().max()>0):
            raise AssertionError('Acceptance must actually hold both long and short positions')
        if not result.reconcile() or len(result.trades)==0:
            raise AssertionError('Acceptance must reconcile nonzero fills')
        for name in ('commission','spread','slippage','impact','fx','borrow'):
            if result.costs[name].sum()<=0: raise AssertionError(f'{period}: missing {name} deduction')
        if not np.isclose(result.equity.iloc[-1]-5000,result.gross_attribution.sum().sum()-result.costs.sum().sum()):
            raise AssertionError('Net PnL must reconcile to gross mark PnL less all costs')
        rows.append(dict(check=f'{period}: actual long/short fills and net costs',passed=True))
        trade_frames.append(result.trades.assign(period=period))
    if not (run.recommendation.target_weight.gt(0).any() and run.recommendation.target_weight.lt(0).any()):
        raise AssertionError('Next timestamp must contain nonzero long and short targets')
    if not (run.recommendation.next_execution_timestamp>=run.recommendation.decision_timestamp).all():
        raise AssertionError('Recommendation execution must not precede observation')
    # Explicit leverage scenario exercises cash financing, not only short borrow.
    long_assets=selected[:1]; long_bars={long_assets[0].instrument:bars[long_assets[0].instrument]}
    leveraged=run_research_workflow(long_bars,long_assets,direction_factory,
        parameters={'directions':{long_assets[0].instrument.symbol:1}},spec=spec,costs={long_assets[0].instrument:costs[long_assets[0].instrument]},
        limits=PortfolioLimits(max_gross=1.5,max_position=2.,allow_margin=True,max_drawdown=.9,daily_loss=.9),kelly=config,
        provenance={'source':'synthetic leveraged acceptance','genuine_market_data':False})
    for result in (leveraged.train,leveraged.validation):
        if result.costs.margin.sum()<=0 or not result.reconcile():
            raise AssertionError('Leveraged fixture must pay financing and reconcile')
    # Verify fraction without hard constraints masking the difference.
    dates=pd.date_range('2023-01-01',periods=60,tz='UTC')
    estimates=pd.DataFrame({'long':.003+.001*np.sin(np.arange(60)),
                            'short':-.003+.001*np.cos(np.arange(60))},index=dates)
    quarter=estimate_kelly(estimates,config)
    full=estimate_kelly(estimates,replace(config,fraction=1.))
    if not np.allclose(quarter.raw,full.raw*.25):
        raise AssertionError('Fractional Kelly must scale unconstrained estimates')
    handoff=nonzero_production_preview()
    rows.extend([dict(check='quarter Kelly and nonzero next-timestamp signed weights',passed=True),
                 dict(check='leveraged IS/OOS financing deductions',passed=True),
                 dict(check='nonzero existing-production dry-run; original gateway unchanged',passed=True)])
    metrics=pd.concat([run.table.assign(scenario='SYNTHETIC long/short'),
                       leveraged.table.assign(scenario='SYNTHETIC leveraged')])
    return AcceptanceReport(pd.DataFrame(rows),metrics,pd.concat(trade_frames,ignore_index=True),
                            run.recommendation,handoff,run,leveraged)
