"""Shared-capital, signed spot accounting in base currency; full audit ledger.

No derivatives or real margin/borrow execution is claimed. Short/margin runs are
explicit research scenarios requiring supplied rates and instrument eligibility.
"""
from dataclasses import dataclass, asdict
import math
import numpy as np
import pandas as pd

from .research_market import research_interval, OHLCV
from .research_sizing import PortfolioLimits, constrain
from .strategy import StrategyPipeline

YEAR_SECONDS = 365.2425 * 86400
EXECUTION_COSTS = ('commission', 'spread', 'slippage', 'impact', 'fx', 'exchange', 'regulatory')
COST_COLUMNS = EXECUTION_COSTS + ('borrow', 'margin', 'funding')


@dataclass(frozen=True)
class CostModel:
    commission: float | None = None
    full_spread: float | None = None
    slippage: float | None = None
    impact: float | None = None
    fx: float | None = None
    exchange: float | None = None
    regulatory: float | None = None
    annual_borrow: float | None = None
    annual_margin: float | None = None
    # Spot has no perpetual funding; other market types are rejected at eligibility.
    funding: float = 0.0
    label: str = 'unspecified'

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name != 'label' and value is not None and (isinstance(value, bool) or not math.isfinite(value) or not 0 <= value < 1):
                raise ValueError('Costs must be explicit finite fractions in [0,1)')
        if self.funding != 0:
            raise ValueError('Perpetual funding requires a derivative settlement adapter; spot only')
        adjustment = sum(v or 0 for v in (self.slippage, self.impact, None if self.full_spread is None else self.full_spread/2))
        if adjustment >= 1:
            raise ValueError('Execution adjustment must be below 100%')

    def validate(self, item, limits, base_currency):
        required = ['commission', 'full_spread', 'slippage', 'impact', 'exchange', 'regulatory']
        if item.instrument.quote != base_currency:
            required.append('fx')
        if limits.allow_short and item.shortable:
            required.append('annual_borrow')
        if limits.allow_margin:
            required.append('annual_margin')
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError(f'Incomplete cost coverage for {item.instrument.symbol}: {missing}; supply assumptions, never implicit zeros')


def scenario_costs():
    """Disclosed conservative scenario, not verified broker/account pricing."""
    return CostModel(commission=.006, full_spread=.001, slippage=.0005, impact=.0002,
                     fx=.001, exchange=0., regulatory=0., label='assumed spot scenario; no verified historical quotes/impact')


def validate_panel(bars, assets):
    keys = [a.instrument for a in assets]
    if not keys or len(set(keys)) != len(keys) or set(keys) != set(bars):
        raise ValueError('Unique selected assets must exactly match price inputs')
    reference = bars[keys[0]]
    for key in keys:
        frame = bars[key]
        if frame.empty or not frame.index.is_unique or not frame.index.is_monotonic_increasing or frame.index.tz is None:
            raise ValueError('Expected ordered unique timezone-aware bars')
        if set(OHLCV + ['bar_end', 'fx_open', 'fx_close']) - set(frame):
            raise ValueError('Expected OHLCV, explicit bar_end and causal FX observations')
        research_interval(frame.index[0], frame.bar_end.iloc[-1])
        if not frame.index.equals(reference.index) or not frame.bar_end.equals(reference.bar_end):
            raise ValueError('Asynchronous markets need a calendar scheduler; only aligned 24/7 spot inputs are operational')
        ends = pd.DatetimeIndex(frame.bar_end)
        if (ends <= frame.index).any() or (len(frame)>1 and not ends[:-1].equals(frame.index[1:])):
            raise ValueError('Bars must be contiguous with explicit exclusive ends')
        numeric = frame[OHLCV+['fx_open','fx_close']].to_numpy(float)
        if not np.isfinite(numeric).all() or (numeric[:, :4] <= 0).any() or (numeric[:, 5:] <= 0).any() or (numeric[:, 4] < 0).any():
            raise ValueError('Invalid prices, FX or volume')
        if (frame.low > frame[['open','close']].min(axis=1)).any() or (frame.high < frame[['open','close']].max(axis=1)).any():
            raise ValueError('Invalid OHLC range')


def estimate_net_returns(bars, assets, costs, base_currency='AUD'):
    """Kelly proxy: one-bar, round-trip cost-adjusted long returns, indexed at close.

    Deliberately charges a round trip each bar, discloses conservatism; actual
    realized strategy metrics always come from the funded execution ledger.
    """
    series, estimation_drag = {}, {}
    for item in assets:
        k, c = item.instrument, costs[item.instrument]
        values = bars[k].close * bars[k].fx_close * item.multiplier
        drag = 2*(c.commission + c.full_spread/2 + c.slippage + c.impact + c.exchange + c.regulatory
                  + ((c.fx or 0) if k.quote != base_currency else 0))
        # Include applicable carry conservatively in the estimation proxy as well.
        dt = (pd.DatetimeIndex(bars[k].bar_end)-bars[k].index).total_seconds()/YEAR_SECONDS
        carry = ((c.annual_borrow or 0) + (c.annual_margin or 0)) * dt
        s = values.pct_change() - drag - carry
        s.index = pd.DatetimeIndex(bars[k].bar_end)
        series[k] = s
        estimation_drag[k] = float(drag + np.mean(carry))
    result = pd.DataFrame(series).iloc[1:]
    result.attrs['directional_cost_drag'] = estimation_drag
    return result


@dataclass
class LedgerResult:
    ledger: pd.DataFrame
    positions: pd.DataFrame
    weights: pd.DataFrame
    attribution: pd.DataFrame
    gross_attribution: pd.DataFrame
    costs: pd.DataFrame
    trades: pd.DataFrame
    closed_trades: pd.DataFrame
    kelly_weights: pd.DataFrame
    signals: pd.DataFrame
    state: dict

    @property
    def equity(self):
        return self.ledger.equity

    @property
    def net_returns(self):
        return self.equity.pct_change().iloc[1:]

    def reconcile(self):
        assert np.allclose(self.ledger.cash + self.ledger.long_assets - self.ledger.short_liabilities, self.equity)
        assert np.allclose(self.attribution.sum(axis=1).cumsum(), self.equity - self.equity.iloc[0], atol=1e-7)
        assert np.allclose(self.gross_attribution.sum(axis=1) - self.costs.sum(axis=1), self.attribution.sum(axis=1), atol=1e-7)
        assert np.allclose(self.ledger.realized_pnl + self.ledger.unrealized_pnl, self.equity-self.equity.iloc[0], atol=1e-7)
        return True


def simulate(bars, assets, pipeline, costs, estimate, *, initial_cash=5000.,
             limits=None, base_currency='AUD', rebalance_every=1, start=None, end=None,
             use_kelly=True):
    """Signals at completed close, fixed desired units at next bar open.

    Sells/reductions precede additions as a funded batch. No future opening price
    enters sizing. New risk is scaled together to available cash/collateral.
    Hard breaches halt further risk and schedule flattening at the next open;
    gaps can exceed loss thresholds and are recorded, not hidden.
    """
    limits = PortfolioLimits() if limits is None else limits
    validate_panel(bars, assets)
    if not isinstance(pipeline, StrategyPipeline):
        raise TypeError('Use the shared StrategyPipeline interface')
    if not math.isfinite(initial_cash) or initial_cash <= 0 or isinstance(initial_cash, bool):
        raise ValueError('Positive funded initial cash required')
    if not isinstance(rebalance_every, int) or isinstance(rebalance_every, bool) or rebalance_every < 1:
        raise ValueError('Rebalancing frequency must be a positive number of bars')
    keys = [a.instrument for a in assets]
    info = {a.instrument:a for a in assets}
    for k in keys:
        costs[k].validate(info[k], limits, base_currency)
    reference = bars[keys[0]]
    start = reference.index[0] if start is None else pd.Timestamp(start)
    end = reference.bar_end.iloc[-1] if end is None else pd.Timestamp(end)
    research_interval(start, end)
    index = reference.index[(reference.index >= start) & (reference.bar_end <= end)]
    if len(index) < 2:
        raise ValueError('At least two complete scored bars required')
    if estimate is not None and estimate.fitted_through > start:
        raise PermissionError('Kelly fit includes observations after evaluation start')
    cash, positions, basis = initial_cash, dict.fromkeys(keys,0.), dict.fromkeys(keys,0.)
    realized = dict.fromkeys(keys,0.)
    funding_realized = 0.
    previous = {k: float(bars[k].loc[index[0],'open']*bars[k].loc[index[0],'fx_open'])*info[k].multiplier for k in keys}
    pending = None
    peak, day_equity, day = initial_cash, initial_cash, index[0].normalize()
    halted, reason = False, 'ok'
    records, holdings, weights_history, attrib, gross_attrib, cost_history = [], [], [], [], [], []
    trades, closed_trades, khistory, signal_history = [], [], [], []
    lots = {k:[] for k in keys}
    times = [index[0]]
    records.append(dict(cash=cash, long_assets=0., short_liabilities=0., equity=cash,
        gross_exposure=0., net_exposure=0., realized_pnl=0., unrealized_pnl=0., risk_status='ok'))
    holdings.append(positions.copy()); weights_history.append(positions.copy())
    attrib.append(positions.copy()); gross_attrib.append(positions.copy()); cost_history.append(dict.fromkeys(COST_COLUMNS,0.))
    khistory.append(positions.copy()); signal_history.append(positions.copy())
    last_signals = positions.copy()

    def trade_cost(k, qty, opening):
        c = costs[k]
        notional = abs(qty)*opening
        return dict(commission=notional*c.commission, spread=notional*c.full_spread/2,
            slippage=notional*c.slippage, impact=notional*c.impact,
            fx=notional*((c.fx or 0) if k.quote != base_currency else 0),
            exchange=notional*c.exchange, regulatory=notional*c.regulatory)

    def feasible(changes, opening):
        projected_cash, projected = cash, positions.copy()
        for k, qty in changes.items():
            projected_cash -= qty*opening[k] + sum(trade_cost(k,qty,opening[k]).values())
            projected[k] += qty
        equity = projected_cash + sum(projected[k]*opening[k] for k in keys)
        gross = sum(abs(projected[k])*opening[k] for k in keys)
        return (equity > 0 and gross <= limits.max_gross*equity+1e-8
                and all(abs(projected[k])*opening[k] <= limits.max_position*equity+1e-8 for k in keys)
                and (limits.allow_margin or projected_cash >= -1e-8))

    for number, t in enumerate(index):
        closing_time = reference.loc[t,'bar_end']
        if t.normalize() != day:
            day, day_equity = t.normalize(), records[-1]['equity']
        opening = {k:float(bars[k].loc[t,'open']*bars[k].loc[t,'fx_open'])*info[k].multiplier for k in keys}
        close = {k:float(bars[k].loc[t,'close']*bars[k].loc[t,'fx_close'])*info[k].multiplier for k in keys}
        gross_step = {k: positions[k]*(opening[k]-previous[k]) for k in keys}
        step_costs, asset_costs = dict.fromkeys(COST_COLUMNS,0.), dict.fromkeys(keys,0.)

        def execute(changes):
            nonlocal cash
            for k in keys:
                qty = changes[k]
                if abs(qty)*opening[k] < initial_cash*1e-12:
                    continue
                breakdown = trade_cost(k,qty,opening[k])
                total = sum(breakdown.values())
                adverse = sum(breakdown[name] for name in ('spread','slippage','impact'))
                fill = opening[k]+math.copysign(adverse/abs(qty),qty)
                fee = total-adverse
                old = positions[k]
                # FIFO round-trip attribution allocates entry, exit and holding
                # costs to closed quantities, leaving open costs with open lots.
                remainder = abs(qty)
                if old*qty < 0:
                    while remainder > 1e-12 and lots[k]:
                        lot=lots[k][0]
                        taken=min(remainder,lot['quantity'])
                        fraction=taken/lot['quantity']
                        entry_cost=lot['cost']*fraction
                        exit_cost=total*taken/abs(qty)
                        net_pnl=taken*(opening[k]-lot['reference'])*lot['direction']-entry_cost-exit_cost
                        closed_trades.append(dict(instrument=k,entry_time=lot['time'],exit_time=t,
                            quantity=taken,direction=lot['direction'],net_pnl_aud=net_pnl,
                            allocated_cost_aud=entry_cost+exit_cost))
                        lot['quantity']-=taken; lot['cost']-=entry_cost; remainder-=taken
                        if lot['quantity']<1e-12: lots[k].pop(0)
                if remainder > 1e-12:
                    lots[k].append(dict(quantity=remainder,reference=opening[k],direction=np.sign(qty),
                                        cost=total*remainder/abs(qty),time=t))
                closed = min(abs(qty),abs(old)) if old*qty < 0 else 0.
                gross_realized = closed * (fill-basis[k])*np.sign(old)
                # Allocate all entry/exit fees at occurrence; trade win rate is disclosed accordingly.
                realized[k] += gross_realized - fee
                new = old+qty
                if abs(new) < 1e-12:
                    new, basis[k] = 0., 0.
                elif old*qty >= 0:
                    basis[k] = (abs(old)*basis[k]+abs(qty)*fill)/abs(new)
                elif old*new < 0:
                    basis[k] = fill
                positions[k] = new
                cash -= qty*fill+fee
                asset_costs[k] += total
                for name,value in breakdown.items():
                    step_costs[name] += value
                trades.append(dict(instrument=k, decision_time=pending[0], execution_time=t,
                    quantity=qty, reference_price_aud=opening[k], price_aud=fill,
                    notional_aud=abs(qty)*fill, closed_quantity=closed,
                    realized_net_pnl=gross_realized-fee, **breakdown))

        if pending is not None:
            if pending[0] > t:
                raise AssertionError('Execution precedes decision')
            desired = pending[1]
            reductions, additions = {}, {}
            for k in keys:
                old,new = positions[k],desired[k]
                kept = math.copysign(min(abs(old),abs(new)),old) if old*new>0 else 0.
                reductions[k], additions[k] = kept-old,new-kept
            execute(reductions)
            if feasible(additions,opening):
                scale=1.
            else:
                lo,hi=0.,1.
                for _ in range(48):
                    mid=(lo+hi)/2
                    if feasible({k:q*mid for k,q in additions.items()},opening): lo=mid
                    else: hi=mid
                scale=lo
            execute({k:q*scale for k,q in additions.items()})
            pending=None
        dt=(closing_time-t).total_seconds()/YEAR_SECONDS
        for k in keys:
            gross_step[k] += positions[k]*(close[k]-opening[k])
            borrow=max(-positions[k],0)*opening[k]*(costs[k].annual_borrow or 0)*dt
            cash-=borrow; step_costs['borrow']+=borrow; asset_costs[k]+=borrow
            realized[k]-=borrow
            lot_units=sum(lot['quantity'] for lot in lots[k])
            if lot_units:
                for lot in lots[k]: lot['cost']+=borrow*lot['quantity']/lot_units
        margin=max(-cash,0)*max((costs[k].annual_margin or 0) for k in keys)*dt
        cash-=margin; step_costs['margin']+=margin; funding_realized-=margin
        denominator=sum(abs(positions[k])*close[k] for k in keys)
        for k in keys:
            allocated_margin=margin*(abs(positions[k])*close[k]/denominator if denominator else 1/len(keys))
            asset_costs[k] += allocated_margin
            lot_units=sum(lot['quantity'] for lot in lots[k])
            if lot_units:
                for lot in lots[k]: lot['cost']+=allocated_margin*lot['quantity']/lot_units
        long_assets=sum(max(positions[k],0)*close[k] for k in keys)
        short_liabilities=sum(max(-positions[k],0)*close[k] for k in keys)
        equity=cash+long_assets-short_liabilities
        if not math.isfinite(equity) or equity<=0:
            raise ArithmeticError('Portfolio insolvent; research stopped, no fabricated liquidation')
        peak=max(peak,equity)
        if 1-equity/peak>=limits.max_drawdown: halted,reason=True,'drawdown halt'
        if 1-equity/day_equity>=limits.daily_loss: halted,reason=True,'daily loss halt'
        if denominator>limits.max_gross*equity+1e-7 or (not limits.allow_margin and cash < -1e-7):
            halted,reason=True,'margin/cash breach: flatten next open'
        if any(abs(positions[k])*close[k]>limits.max_position*equity+1e-7 for k in keys):
            halted,reason=True,'position limit breach: flatten next open'
        current_weights={k:positions[k]*close[k]/equity for k in keys}
        available={k: bars[k].loc[bars[k].bar_end<=closing_time].copy() for k in keys}
        # The last completed bar also produces a fresh recommendation preview;
        # it cannot alter historical fills because the scored interval has ended.
        if number % rebalance_every == 0 or halted or number == len(index)-1:
            target = pipeline.evaluate(timestamp=closing_time, historical_data=available,
                positions=positions, cash=cash, equity=equity,
                bar_ends={k: frame.bar_end for k,frame in available.items()})
            if target is not None:
                last_signals=dict(target)
            if halted or target is not None:
                if use_kelly:
                    if estimate is None:
                        raise ValueError('Kelly estimate required')
                    chosen=constrain(last_signals,estimate,assets,limits,halted=halted)
                else:
                    # Explicit signal-weight sizing also supports arbitrary signed
                    # strategies; Kelly remains the notebook default.
                    values={k:float(last_signals.get(k,0.)) for k in keys}
                    if set(last_signals)-set(keys) or not np.isfinite(list(values.values())).all() or any(abs(v)>1 for v in values.values()):
                        raise ValueError('Signal weights must be finite in [-1,1] over selected instruments')
                    for k in keys:
                        if halted or not info[k].eligible or (values[k]<0 and (not limits.allow_short or not info[k].shortable)):
                            values[k]=0.
                    chosen=pd.Series(values).clip(lower=-limits.max_position,upper=limits.max_position)
                    cap=limits.max_gross if limits.allow_margin else min(limits.max_gross,1-limits.cash_buffer)
                    if chosen.abs().sum()>cap: chosen*=cap/chosen.abs().sum()
                pending=(closing_time,{k:chosen[k]*equity/close[k] for k in keys})
            else:
                chosen=pd.Series(current_weights)
        else:
            chosen=pd.Series(current_weights)
        unrealized=sum(positions[k]*(close[k]-basis[k]) for k in keys)
        records.append(dict(cash=cash,long_assets=long_assets,short_liabilities=short_liabilities,equity=equity,
            gross_exposure=denominator/equity,net_exposure=(long_assets-short_liabilities)/equity,
            realized_pnl=sum(realized.values())+funding_realized,unrealized_pnl=unrealized,risk_status=reason))
        times.append(closing_time); holdings.append(positions.copy()); weights_history.append(current_weights)
        gross_attrib.append(gross_step); attrib.append({k:gross_step[k]-asset_costs[k] for k in keys})
        cost_history.append(step_costs); khistory.append(chosen.to_dict()); signal_history.append(last_signals.copy())
        previous=close
    idx=pd.DatetimeIndex(times,name='timestamp')
    result=LedgerResult(pd.DataFrame(records,index=idx),pd.DataFrame(holdings,index=idx),
        pd.DataFrame(weights_history,index=idx),pd.DataFrame(attrib,index=idx),pd.DataFrame(gross_attrib,index=idx),
        pd.DataFrame(cost_history,index=idx),pd.DataFrame(trades),pd.DataFrame(closed_trades),pd.DataFrame(khistory,index=idx),
        pd.DataFrame(signal_history,index=idx),
        dict(cash=cash,positions=positions,prices=previous,equity=equity,halted=halted,risk_status=reason,
             timestamp=times[-1],pending=pending,base_currency=base_currency,peak=peak,day_start_equity=day_equity,
             open_lots=lots,rebalance_every=rebalance_every,bar_number=len(index)-1))
    result.reconcile()
    open_net=sum(lot['quantity']*(previous[k]-lot['reference'])*lot['direction']-lot['cost']
                 for k in keys for lot in lots[k])
    closed_net=sum(row['net_pnl_aud'] for row in closed_trades)
    if not np.isclose(open_net+closed_net,equity-initial_cash,atol=1e-7):
        raise AssertionError('FIFO net trade/open-position attribution does not reconcile')
    return result
