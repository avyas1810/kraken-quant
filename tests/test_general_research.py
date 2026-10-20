"""Deterministic accounting, timeframes, arbitrary universes and causal research."""
from dataclasses import replace
import json
from urllib.parse import urlparse, parse_qs

import numpy as np
import pandas as pd
import pytest

from kraken_quant.config import CONFIG
from kraken_quant.strategy import Instrument, StrategyPipeline
from kraken_quant.research_market import (Asset, asset, clean_hourly, resample_hourly,
    research_interval, historical_fx, CoinbaseHistory, load_market)
from kraken_quant.research_sizing import KellyEstimate, KellyConfig, PortfolioLimits, estimate_kelly, constrain
from kraken_quant.research_ledger import (CostModel, simulate, estimate_net_returns, validate_panel)
from kraken_quant.research_report import net_metrics, rolling_sharpe, dashboard
from kraken_quant.research_workflow import (ResearchSpec, run_research_workflow,
    momentum_factory, walk_forward, recommendations)
from kraken_quant.research_handoff import DryRunExecution


def free_cost(**overrides):
    return replace(CostModel(0.,0.,0.,0.,0.,0.,0.,0.,0.,label='explicit test zero costs'),**overrides)


def hourly(start='2023-11-01',end='2024-03-01'):
    index=pd.date_range(start,end,freq='h',tz='UTC',inclusive='left')
    x=100+np.arange(len(index))*.01
    return pd.DataFrame(dict(open=x,high=x+2,low=x-1,close=x+1,volume=2.),index=index)


def panel(n=3,days=100,shortable=False):
    index=pd.date_range('2023-11-01',periods=days,freq='D',tz='UTC')
    selected=[asset(f'A{i}/AUD',provider='fixture',shortable=shortable) for i in range(n)]
    rng=np.random.default_rng(42)
    bars={}
    for i,a in enumerate(selected):
        close=100*np.cumprod(1+rng.normal(.003,.01,len(index)))
        opening=np.r_[close[0],close[:-1]]
        bars[a.instrument]=pd.DataFrame(dict(open=opening,high=np.maximum(opening,close)*1.01,
            low=np.minimum(opening,close)*.99,close=close,volume=1e5,
            bar_end=index+pd.Timedelta(days=1),fx_open=1.,fx_close=1.),index=index)
    return selected,bars


def estimate(keys,raw=1.):
    zero=pd.Series(0.,index=keys)
    return KellyEstimate(pd.Series(raw,index=keys),zero,pd.DataFrame(np.eye(len(keys)),index=keys,columns=keys),
                         'explicit test fixture',100,pd.Timestamp('2023-10-31',tz='UTC'))


class Once:
    def __init__(self,weight=.5): self.weight=weight; self.first=True
    def target_weights(self,c):
        if self.first:
            self.first=False
            return {k:self.weight/len(c.historical_data) for k in c.historical_data}
        return None


def simulate_fixture(cost=None,weight=.5,short=False,margin=False,days=8):
    assets,bars=panel(1,days,shortable=short)
    keys=list(bars)
    limits=PortfolioLimits(max_position=3,max_gross=3,allow_short=short,allow_margin=margin,max_drawdown=1,daily_loss=1,cash_buffer=0)
    result=simulate(bars,assets,StrategyPipeline(Once(weight)),{k:cost or free_cost() for k in keys},
        estimate(keys,raw=-3. if short else 3.),limits=limits)
    return result


@pytest.mark.parametrize('timeframe,hours',[('4h',4),('8h',8),('16h',16),('24h',24),('weekly',168),('monthly',30*24)])
def test_every_timeframe_known_ohlcv(timeframe,hours):
    source=hourly()
    result,diagnostics=resample_hourly(source,timeframe,as_of='2024-03-01T00:00:00Z')
    left=result.index[0]; right=result.bar_end.iloc[0]
    subset=source.loc[(source.index>=left)&(source.index<right)]
    assert len(subset)==hours
    assert result.open.iloc[0]==subset.open.iloc[0]
    assert result.close.iloc[0]==subset.close.iloc[-1]
    assert result.high.iloc[0]==subset.high.max()
    assert result.low.iloc[0]==subset.low.min()
    assert result.volume.iloc[0]==subset.volume.sum()
    assert (result.bar_end<=pd.Timestamp('2024-03-01',tz='UTC')).all()
    assert diagnostics['fabricated_prices'] is False


def test_monthly_leap_year_weekly_monday_and_16h_continuity():
    source=hourly()
    month,_=resample_hourly(source,'monthly',as_of='2024-03-01T00:00:00Z')
    assert month.loc['2024-02-01','volume']==29*24*2
    week,_=resample_hourly(source,'weekly',as_of='2024-03-01T00:00:00Z')
    assert all(t.weekday()==0 and t.hour==0 for t in week.index)
    bars,_=resample_hourly(source,'16h',as_of='2024-03-01T00:00:00Z')
    assert (bars.index[1:]-bars.index[:-1]==pd.Timedelta(hours=16)).all()
    changed=source.copy(); changed.loc['2024-02-01':,['open','high','low','close']]*=4
    later,_=resample_hourly(changed,'16h',as_of='2024-03-01T00:00:00Z')
    pd.testing.assert_frame_equal(bars.loc[bars.bar_end<=pd.Timestamp('2024-02-01',tz='UTC')],
                                 later.loc[later.bar_end<=pd.Timestamp('2024-02-01',tz='UTC')])


def test_cleaning_duplicate_missing_conflicting_and_incomplete():
    frame=hourly('2023-11-01','2023-11-03')
    same=pd.concat([frame,frame.iloc[:1]])
    result,quality=clean_hourly(same,frame.index[0],'2023-11-03T00:00:00Z',as_of='2023-11-03T00:00:00Z')
    assert quality['identical_duplicates_removed']==1
    assert len(result)==48
    conflict=frame.iloc[:1].copy(); conflict.close+=.5
    with pytest.raises(ValueError,match='Conflicting'): clean_hourly(pd.concat([frame,conflict]),frame.index[0],'2023-11-03T00:00:00Z',as_of='2023-11-03T00:00:00Z')
    with pytest.raises(ValueError,match='missing bars'): clean_hourly(frame.iloc[1:],frame.index[0],'2023-11-03T00:00:00Z',as_of='2023-11-03T00:00:00Z')
    with pytest.raises(ValueError,match='incomplete'): clean_hourly(frame,frame.index[0],'2023-11-03T00:00:00Z',as_of='2023-11-02T23:59:00Z')
    zone=frame.copy(); zone.index=zone.index.tz_convert('Australia/Sydney')
    checked,_=clean_hourly(zone,frame.index[0],'2023-11-03T00:00:00Z',as_of='2023-11-03T00:00:00Z')
    pd.testing.assert_frame_equal(result,checked,check_freq=False)


@pytest.mark.parametrize('n',[1,3,10,50])
def test_arbitrary_asset_counts_shared_capital_and_workflow(n,tmp_path):
    selected,bars=panel(n)
    costs={k:free_cost(commission=.0001,slippage=.0001) for k in bars}
    run=run_research_workflow(bars,selected,momentum_factory,parameters={'lookback':3},
        costs=costs,spec=ResearchSpec(timeframe='24h',calibration_bars=20),kelly=KellyConfig(minimum_observations=10))
    assert run.train.reconcile() and run.validation.reconcile()
    assert run.train.equity.iloc[0]==5000
    assert len(run.recommendation)==n
    assert run.validation.ledger.cash.min()>=-1e-7
    assert run.validation.ledger.gross_exposure.max()<=1+1e-7
    assert run.recommendation.gross_exposure.max()<=1
    path=run.write(tmp_path)
    assert run.write(tmp_path)==path
    assert json.loads((path/'manifest.json').read_text())['final_holdout']=='locked'


def test_commission_and_slippage_reduce_identical_fill_net_pnl():
    free=simulate_fixture(weight=.1)
    for higher in (free_cost(commission=.01),free_cost(slippage=.01),free_cost(full_spread=.02),free_cost(impact=.01)):
        costly=simulate_fixture(higher,weight=.1)
        assert np.allclose(free.trades.quantity,costly.trades.quantity)
        assert costly.equity.iloc[-1]<free.equity.iloc[-1]
        assert costly.reconcile()


def test_borrow_reduces_short_and_financing_reduces_leveraged_net_pnl():
    no_borrow=simulate_fixture(weight=-.5,short=True)
    borrow=simulate_fixture(free_cost(annual_borrow=.5),weight=-.5,short=True)
    assert borrow.costs.borrow.sum()>0
    assert borrow.equity.iloc[-1]<no_borrow.equity.iloc[-1]
    no_interest=simulate_fixture(weight=1.,margin=True)
    interest=simulate_fixture(free_cost(annual_margin=.5),weight=1.,margin=True)
    assert interest.costs.margin.sum()>0
    assert interest.equity.iloc[-1]<no_interest.equity.iloc[-1]


def test_net_metrics_match_net_equity_and_cost_reconciliation():
    result=simulate_fixture(free_cost(commission=.01,slippage=.003,annual_borrow=.3),weight=-.5,short=True)
    metrics=net_metrics(result)
    r=result.equity.pct_change().iloc[1:]
    assert metrics['net_sharpe']==pytest.approx(r.mean()/r.std(ddof=1)*np.sqrt(metrics['periods_per_year']))
    assert metrics['net_max_drawdown']==pytest.approx((1-result.equity/result.equity.cummax()).max())
    assert metrics['net_pnl_aud']==pytest.approx(metrics['gross_mark_pnl_aud']-metrics['total_costs_aud'])
    assert result.ledger.short_liabilities.max()>0
    assert result.reconcile()


def test_unknown_costs_refused_and_unsupported_assets_and_asynchronous_markets():
    selected,bars=panel(1)
    with pytest.raises(ValueError,match='Incomplete cost'):
        simulate(bars,selected,StrategyPipeline(Once()),{k:CostModel() for k in bars},estimate(list(bars)))
    with pytest.raises(ValueError,match='equities/FX/futures'):
        Asset(Instrument('test','ES/USD','ES','USD','future'),'ES')
    with pytest.raises(ValueError,match='equities/FX/futures'):
        replace(selected[0],calendar='XNYS')
    a,b=panel(3); b[a[1].instrument]=b[a[1].instrument].iloc[1:]
    with pytest.raises(ValueError,match='Asynchronous'): validate_panel(b,a)
    with pytest.raises(PermissionError,match='locked'): research_interval('2024-01-01T00:00:00Z','2025-01-02T00:00:00Z')


def test_reversals_exits_fifo_trade_profitability_and_fx():
    selected,bars=panel(1,days=8,shortable=True); k=selected[0].instrument
    f=bars[k]; f['close']=np.arange(8)*2+100.; f['open']=f.close
    f['high']=f.close+2; f['low']=f.close-2
    f['fx_close']=np.arange(8)*.01+1; f['fx_open']=f.fx_close
    class Rotate:
        def __init__(self): self.n=0
        def target_weights(self,c):
            self.n+=1
            return {k: .5 if self.n<3 else -.5 if self.n<5 else 0.}
    limits=PortfolioLimits(max_position=1,max_gross=1,allow_short=True,max_drawdown=1,daily_loss=1)
    result=simulate(bars,selected,StrategyPipeline(Rotate()),{k:free_cost(commission=.001,annual_borrow=.05)},None,limits=limits,use_kelly=False)
    assert len(result.closed_trades)>0
    assert result.closed_trades.allocated_cost_aud.min()>0
    assert net_metrics(result)['closed_trade_count']>0
    assert result.reconcile()
    assert result.positions[k].max()>0 and result.positions[k].min()<0
    assert result.positions[k].iloc[-1]==pytest.approx(0)
    short=simulate_fixture(free_cost(commission=.001,annual_borrow=.05),weight=-.5,short=True)
    assert short.reconcile()


def test_training_only_fits_covariance_and_oos_invariance():
    selected,bars=panel(3)
    fitted=[]
    class Model:
        def fit(self,frames): fitted.append(max(f.bar_end.max() for f in frames.values()))
        def target_weights(self,c): return {k:1 for k in c.historical_data}
    def factory(parameters,seed): return Model()
    args=dict(strategy_factory=factory,costs={k:free_cost() for k in bars},
              spec=ResearchSpec(timeframe='24h',calibration_bars=20),kelly=KellyConfig(minimum_observations=10))
    first=run_research_workflow(bars,selected,**args)
    altered={k:f.copy() for k,f in bars.items()}
    for f in altered.values(): f.loc[f.index>=pd.Timestamp('2024-01-01',tz='UTC'),['open','high','low','close']]*=1.5
    second=run_research_workflow(altered,selected,**args)
    pd.testing.assert_frame_equal(first.train.ledger,second.train.ledger)
    pd.testing.assert_series_equal(first.estimates['out-of-sample'].raw,second.estimates['out-of-sample'].raw)
    assert max(fitted)<=pd.Timestamp('2024-01-01',tz='UTC')
    assert first.estimates['in-sample'].fitted_through<=first.train.equity.index[0]
    assert first.estimates['out-of-sample'].fitted_through<=first.validation.equity.index[0]


def test_kelly_insufficient_unstable_negative_and_cost_induced_short_safety():
    index=pd.date_range('2023-01-01',periods=50,tz='UTC')
    returns=pd.DataFrame({'a':.01,'b':.01},index=index)
    fitted=estimate_kelly(returns)
    assert np.isfinite(fitted.raw).all()
    assert (estimate_kelly(returns.iloc[:3]).raw==0).all()
    zero=returns*0-.005; zero.attrs['directional_cost_drag']={'a':.005,'b':.005}
    assert (estimate_kelly(zero).raw==0).all()
    bad=returns.copy(); bad.iloc[-1,0]=np.nan
    with pytest.raises(ValueError,match='Nonfinite'): estimate_kelly(bad)
    assets,bars=panel(3)
    weights=constrain({k:1 for k in bars},estimate(list(bars),100),assets,PortfolioLimits(max_position=.2))
    assert weights.abs().max()<=.2
    assert weights.abs().sum()<=1


def test_future_bar_and_future_kelly_rejected():
    selected,bars=panel(1); k=selected[0].instrument
    future=replace(estimate([k]),fitted_through=pd.Timestamp('2024-01-01',tz='UTC'))
    with pytest.raises(PermissionError,match='Kelly fit'):
        simulate(bars,selected,StrategyPipeline(Once()),{k:free_cost()},future)
    with pytest.raises(PermissionError,match='future or unfinished'):
        StrategyPipeline(Once()).evaluate(timestamp=bars[k].index[0],historical_data=bars,
            positions={},cash=5000,equity=5000,bar_ends={k:bars[k].bar_end})


def test_public_provider_pagination_cache_and_eligibility(tmp_path):
    calls=[]
    def transport(url):
        calls.append(url)
        if '?' not in url: return {'id':'BTC-USD','base_currency':'BTC','quote_currency':'USD'}
        query=parse_qs(urlparse(url).query)
        start=pd.Timestamp(query['start'][0]); end=pd.Timestamp(query['end'][0])
        return [[int(t.timestamp()),99,102,100,101,10] for t in pd.date_range(start,end,freq='h')][::-1]
    provider=CoinbaseHistory(tmp_path,transport)
    a=asset('BTC/USD')
    frame,manifest=provider.retrieve(a,'2023-11-01T00:00:00Z','2023-11-15T00:00:00Z',as_of='2023-11-16T00:00:00Z')
    assert len(frame)==14*24 and manifest['genuine_market_data']
    assert len(calls)==3
    provider.retrieve(a,'2023-11-01T00:00:00Z','2023-11-15T00:00:00Z',as_of='2023-11-16T00:00:00Z')
    assert len(calls)==4
    with pytest.raises(ValueError,match='Unsupported'): provider.retrieve(asset('NO/USD'),'2023-11-01T00:00:00Z','2023-11-03T00:00:00Z')
    path=next(tmp_path.glob('*.json')); body=json.loads(path.read_text()); body['rows'][0][4]*=2; path.write_text(json.dumps(body))
    with pytest.raises(ValueError,match='integrity'): provider.retrieve(a,'2023-11-01T00:00:00Z','2023-11-15T00:00:00Z')


def test_fx_uses_last_published_rate_never_future_and_stale_refused(tmp_path):
    payload={'base':'USD','rates':{'2023-11-01':{'AUD':1.5},'2023-11-02':{'AUD':1.6}}}
    times=pd.DatetimeIndex(['2023-11-02T12:00:00Z','2023-11-03T12:00:00Z'])
    fx,manifest=historical_fx('USD','AUD',times,tmp_path,lambda url:payload)
    assert fx.tolist()==[1.5,1.6]
    assert 'not an executable' in manifest['limitation']
    with pytest.raises(ValueError,match='stale'):
        historical_fx('USD','AUD',pd.DatetimeIndex(['2023-11-20T00:00:00Z']),tmp_path,lambda url:payload)


def test_both_periods_same_net_cost_rules_dashboard_and_recommendations(tmp_path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    selected,bars=panel(1)
    args=dict(parameters={'lookback':2},spec=ResearchSpec(timeframe='24h',calibration_bars=20),
              kelly=KellyConfig(minimum_observations=10))
    run=run_research_workflow(bars,selected,momentum_factory,costs={k:free_cost(commission=.0001) for k in bars},**args)
    for result in (run.train,run.validation):
        assert result.costs.commission.sum()>0
        assert result.reconcile()
        if len(result.trades):
            assert result.costs.commission.sum()==pytest.approx(sum(result.trades.reference_price_aud*result.trades.quantity.abs())*.0001)
    figs=dashboard(run)
    assert len(figs)==8
    plt.close('all')
    row=run.recommendation.iloc[0]
    assert row.decision_timestamp==run.validation.equity.index[-1]
    assert row.next_execution_timestamp>=row.decision_timestamp
    assert row.target_weight<=.35+1e-8
    assert row.cash_allocation>=-1e-8
    assert row.estimated_cost_aud>=0
    assert run.write(tmp_path).exists()


def test_dry_run_handoff_reconciles_and_refuses_live_stale_short_and_lots():
    k=asset('BTC/USD').instrument; now=pd.Timestamp('2024-02-01',tz='UTC')
    rec=pd.DataFrame([dict(instrument=k,target_weight=.2,decision_timestamp=now,risk_status='ok')])
    account=dict(timestamp=now,cash=5000.,positions={},base_currency='AUD',unresolved_orders=False,risk_approved=True)
    quotes={k:dict(timestamp=now,bid_aud=99.,ask_aud=101.)}
    rules={k:dict(timestamp=now,status='online',quantity_step=.01,minimum_quantity=.01,minimum_notional_aud=1.,fee_rate=.001)}
    adapter=DryRunExecution()
    args=dict(now=now,account=account,quotes=quotes,rules=rules)
    reply=adapter.submit_target_weights(rec,**args)
    assert not reply['live_supported'] and len(reply['intents'])==1
    assert reply['intents'][0]['quantity']==10
    with pytest.raises(PermissionError,match='dry-run'): adapter.submit_target_weights(rec,mode='live',**args)
    with pytest.raises(ValueError,match='Stale/future'): adapter.submit_target_weights(rec,**dict(args,now=now+pd.Timedelta(days=1)))
    short=rec.copy(); short['target_weight']=-.2
    with pytest.raises(ValueError,match='long-only'): adapter.submit_target_weights(short,**args)
    with pytest.raises(ValueError,match='unresolved'): adapter.submit_target_weights(rec,**dict(args,account=dict(account,unresolved_orders=True)))


def test_walk_forward_and_overlap_guard():
    selected,bars=panel(1,days=120)
    config=ResearchSpec(timeframe='24h',calibration_bars=20)
    args=dict(costs={k:free_cost() for k in bars},kelly=KellyConfig(minimum_observations=10))
    folds=[(config,'2024-02-01T00:00:00Z'),(replace(config,train_end='2024-02-01T00:00:00Z'),'2024-02-20T00:00:00Z')]
    runs=walk_forward(bars,selected,momentum_factory,folds,**args)
    assert len(runs)==2
    assert runs[1].estimates['out-of-sample'].fitted_through<=runs[1].validation.equity.index[0]
    with pytest.raises(ValueError,match='overlap'):
        walk_forward(bars,selected,momentum_factory,[folds[0],folds[0]],**args)


def test_hand_calculated_net_round_trip_and_next_open_sizing():
    selected,bars=panel(1,days=3); k=selected[0].instrument
    f=bars[k]
    f['open']=[100.,110.,130.]; f['close']=[100.,120.,130.]
    f['high']=[101.,121.,131.]; f['low']=[99.,109.,129.]
    class EnterExit:
        def __init__(self): self.n=0
        def target_weights(self,c):
            self.n+=1
            return {k:.1 if self.n==1 else 0.}
    costs={k:free_cost(commission=.01,slippage=.01)}
    result=simulate(bars,selected,StrategyPipeline(EnterExit()),costs,None,
        limits=PortfolioLimits(max_position=1,max_gross=1),use_kelly=False)
    # Decision $100 sizes 5 units. Fill $111.10, exit $128.70, fees
    # charged on reference notional: $5.50+$6.50. Net=$88-$12=$76.
    assert result.trades.quantity.tolist()==[5.,-5.]
    assert result.trades.price_aud.tolist()==pytest.approx([111.1,128.7])
    assert result.equity.iloc[-1]==pytest.approx(5076.)
    assert result.costs.sum().sum()==pytest.approx(24.)
    assert result.gross_attribution.sum().sum()==pytest.approx(100.)
    assert result.closed_trades.net_pnl_aud.sum()==pytest.approx(76.)
    assert result.ledger.realized_pnl.iloc[-1]==pytest.approx(76.)
    assert result.ledger.unrealized_pnl.iloc[-1]==pytest.approx(0.)
    assert (result.trades.execution_time>=result.trades.decision_time).all()


def test_shared_capital_simultaneous_orders_and_shorts_have_liabilities():
    selected,bars=panel(10,days=5)
    class All:
        def target_weights(self,c): return {k:1. for k in c.historical_data}
    limits=PortfolioLimits(max_gross=1,max_position=.5,cash_buffer=.01)
    result=simulate(bars,selected,StrategyPipeline(All()),{k:free_cost(commission=.02) for k in bars},
                    estimate(list(bars),100),limits=limits)
    first=result.trades.loc[result.trades.execution_time==result.trades.execution_time.min()]
    assert len(first)==10
    assert (first.reference_price_aud*first.quantity).sum()+first.commission.sum()<=5000+1e-7
    assert result.ledger.cash.min()>=-1e-7
    short=simulate_fixture(weight=-.1,short=True)
    assert short.ledger.short_liabilities.max()>0
    assert short.ledger.cash.max()>5000
    assert short.ledger.equity.max()<short.ledger.cash.max()


def test_drawdown_and_daily_loss_halt_at_next_open():
    selected,bars=panel(1,days=6); k=selected[0].instrument
    f=bars[k]; f['open']=[100,100,80,80,80,80]; f['close']=[100,80,80,80,80,80]
    f['high']=f[['open','close']].max(axis=1)+1; f['low']=f[['open','close']].min(axis=1)-1
    limits=PortfolioLimits(max_position=1,max_gross=1,max_drawdown=.05,daily_loss=.05)
    result=simulate(bars,selected,StrategyPipeline(Once(.8)),{k:free_cost()},estimate([k],1),limits=limits)
    assert result.state['halted']
    assert result.positions[k].iloc[-1]==0
    assert result.trades.execution_time.iloc[-1]==f.index[2]
    assert result.equity.iloc[-1]==pytest.approx(4200.)


def test_feature_model_fits_only_training_and_requires_builder():
    selected,bars=panel(1)
    fits=[]; decisions=[]
    class Features:
        def fit(self,frames): fits.append(max(f.bar_end.max() for f in frames.values()))
        def __call__(self,frames,timestamp):
            assert all((f.bar_end<=timestamp).all() for f in frames.values())
            decisions.append(timestamp)
            return {'momentum':{k:float(f.close.iloc[-1]>f.close.iloc[0]) for k,f in frames.items()}}
    class Strategy:
        required_features=('momentum',)
        def target_weights(self,c): return c.features['momentum']
    def factory(parameters,seed): return Strategy()
    def features(parameters,seed): return Features()
    args=dict(costs={k:free_cost() for k in bars},spec=ResearchSpec(timeframe='24h',calibration_bars=20),
              kelly=KellyConfig(minimum_observations=10))
    with pytest.raises(ValueError,match='requires features'):
        run_research_workflow(bars,selected,factory,**args)
    run=run_research_workflow(bars,selected,factory,feature_factory=features,**args)
    assert len(fits)==2 and max(fits)<=pd.Timestamp('2024-01-01',tz='UTC')
    assert len(decisions)>0 and run.validation.reconcile()


def test_fx_conversion_fees_reduce_net_pnl_and_aud_marking():
    selected,bars=panel(1,days=5)
    old=selected[0].instrument; item=asset('A0/USD',provider='fixture')
    f=bars.pop(old); bars[item.instrument]=f; k=item.instrument
    f['fx_open']=1.5; f['fx_close']=1.5
    args=dict(bars=bars,assets=[item],estimate=estimate([k]),limits=PortfolioLimits(max_position=1),base_currency='AUD')
    free=simulate(pipeline=StrategyPipeline(Once(.1)),costs={k:free_cost()},**args)
    fee=simulate(pipeline=StrategyPipeline(Once(.1)),costs={k:free_cost(fx=.01)},**args)
    assert fee.equity.iloc[-1]<free.equity.iloc[-1]
    assert fee.costs.fx.sum()>0 and fee.reconcile()
    assert fee.trades.reference_price_aud.iloc[0]==pytest.approx(f.open.iloc[1]*1.5)


def test_ineligible_and_unborrowable_instruments_stay_flat():
    selected,bars=panel(1,days=5)
    selected=[replace(selected[0],eligible=False)]
    result=simulate(bars,selected,StrategyPipeline(Once()),{k:free_cost() for k in bars},estimate(list(bars)))
    assert result.trades.empty
    assert result.equity.iloc[-1]==5000
    selected=[replace(selected[0],eligible=True)]
    result=simulate(bars,selected,StrategyPipeline(Once(-1)),{k:free_cost() for k in bars},estimate(list(bars),-1),
        limits=PortfolioLimits(allow_short=True))
    assert result.trades.empty


@pytest.mark.parametrize('timeframe,start,end,calibration',[
    ('4h','2023-12-20','2024-01-08',6),('8h','2023-12-20','2024-01-08',6),
    ('16h','2023-12-20','2024-01-08',6),('24h','2023-12-01','2024-01-08',6),
    ('weekly','2023-09-04','2024-02-26',4),('monthly','2023-01-01','2024-03-01',4)])
def test_every_timeframe_full_workflow_and_elapsed_annualization(timeframe,start,end,calibration):
    from kraken_quant.research_fixtures import fixture_market
    from kraken_quant.research_ledger import YEAR_SECONDS
    selected=[asset('BTC/USD')]
    bars,provenance=fixture_market(selected,pd.Timestamp(start,tz='UTC'),pd.Timestamp(end,tz='UTC'),timeframe)
    run=run_research_workflow(bars,selected,momentum_factory,
        spec=ResearchSpec(timeframe=timeframe,calibration_bars=calibration),parameters={'lookback':1},
        costs={k:free_cost(commission=.0001) for k in bars},kelly=KellyConfig(minimum_observations=3),provenance=provenance)
    assert run.validation.reconcile()
    years=(run.validation.equity.index[-1]-run.validation.equity.index[0]).total_seconds()/YEAR_SECONDS
    assert run.table.loc['out-of-sample','periods_per_year']==pytest.approx(len(run.validation.net_returns)/years)
    assert run.estimates['out-of-sample'].fitted_through<=run.validation.equity.index[0]


def test_training_parameter_sensitivity_never_uses_validation():
    from kraken_quant.research_workflow import training_sensitivity
    selected,bars=panel(1)
    spec=ResearchSpec(timeframe='24h',calibration_bars=20)
    costs={k:free_cost() for k in bars}
    first=training_sensitivity(bars,selected,momentum_factory,[{'lookback':2},{'lookback':4}],spec=spec,costs=costs)
    changed={k:f.copy() for k,f in bars.items()}
    for f in changed.values(): f.loc[f.index>=pd.Timestamp('2024-01-01',tz='UTC'),['open','high','low','close']]*=100
    second=training_sensitivity(changed,selected,momentum_factory,[{'lookback':2},{'lookback':4}],spec=spec,costs=costs)
    pd.testing.assert_frame_equal(first,second)
    assert (first.period=='training only').all()
