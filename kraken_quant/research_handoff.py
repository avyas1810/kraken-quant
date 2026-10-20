"""Tested target-weight handoff contract. This adapter never routes live orders."""
from dataclasses import dataclass
import numpy as np
import pandas as pd

from .data import utc_instant
from .strategy import validate_weights


@dataclass(frozen=True)
class TargetRequest:
    targets: dict
    decision_timestamp: pd.Timestamp
    base_currency: str
    mode: str = 'dry-run'


class DryRunExecution:
    """Preflight contract for a future reconciled broker adapter.

    Current position/quote snapshots must be supplied separately, not inferred
    from research. Metadata has quantity_step, minimum_quantity and min_notional
    in base currency. This produces reviewable order intents, not broker orders.
    Existing ProductionAdapter has no live transport; it remains disabled.
    """
    def submit_target_weights(self,recommendation,*,now,account,quotes,rules,
                              max_gross=1.,max_position=.35,mode='dry-run'):
        if mode!='dry-run':
            raise PermissionError('Research handoff supports dry-run only; no live transport or authorization')
        if not np.isfinite([max_gross,max_position]).all() or max_gross<=0 or max_position<=0:
            raise ValueError('Finite positive execution risk limits required')
        now=utc_instant(now)
        if recommendation.empty or recommendation.instrument.duplicated().any():
            raise ValueError('Unique recommended instruments required')
        if recommendation.decision_timestamp.nunique()!=1:
            raise ValueError('One decision timestamp per request required')
        decision=utc_instant(recommendation.decision_timestamp.iloc[0])
        if not pd.Timedelta(0)<=now-decision<=pd.Timedelta(minutes=5):
            raise ValueError('Stale/future research recommendation; recompute from latest completed observations')
        if 'next_execution_timestamp' in recommendation and (pd.to_datetime(recommendation.next_execution_timestamp,utc=True)>now).any():
            raise ValueError('Next eligible rebalance timestamp has not arrived; recompute at execution time')
        account_time=utc_instant(account['timestamp'])
        if not pd.Timedelta(0)<=now-account_time<=pd.Timedelta(seconds=60):
            raise ValueError('Reconcile a fresh account snapshot')
        if account.get('base_currency')!='AUD' or account.get('unresolved_orders',True):
            raise ValueError('AUD account with no unresolved orders required')
        targets=dict(zip(recommendation.instrument,recommendation.target_weight))
        # Production spot contract: signed research allocations must be adapted
        # separately for a tested margin/borrow broker; never send them as spot.
        for k in targets:
            if k.market_type!='spot': raise ValueError('Only spot dry-run contract implemented')
        targets=validate_weights(targets,targets,long_only=True,max_gross=min(1.,max_gross))
        if any(v>max_position+1e-10 for v in targets.values()): raise ValueError('Position risk limit exceeded')
        if recommendation.risk_status.isin(['requires manual reconciliation']).any():
            raise ValueError('Recommendation requires risk reconciliation')
        if 'eligible' in recommendation and ((~recommendation.eligible.astype(bool)) & (recommendation.target_weight!=0)).any():
            raise ValueError('Ineligible instruments cannot receive nonzero targets')
        positions=dict(account['positions'])
        if set(positions)-set(targets): raise ValueError('Account positions outside target universe must be reconciled')
        if not np.isfinite([account['cash'],*positions.values()]).all() or account['cash']<0 or any(q<0 for q in positions.values()):
            raise ValueError('Dry-run requires finite funded spot balances')
        equity=float(account['cash'])
        for k in targets:
            q=quotes[k]
            if not pd.Timedelta(0)<=now-utc_instant(q['timestamp'])<=pd.Timedelta(seconds=30):
                raise ValueError('Stale/future quote')
            if not np.isfinite([q['bid_aud'],q['ask_aud']]).all() or not 0<q['bid_aud']<=q['ask_aud']:
                raise ValueError('Invalid AUD bid/ask')
            equity+=positions.get(k,0)*(q['bid_aud']+q['ask_aud'])/2
        if equity<=0: raise ValueError('Positive reconciled equity required')
        if not account.get('risk_approved',False): raise ValueError('Execution-layer drawdown/daily loss approval required')
        intents=[]
        buy_cost=sell_proceeds=0.
        for k,w in targets.items():
            r=rules[k]; q=quotes[k]
            if not pd.Timedelta(0)<=now-utc_instant(r['timestamp'])<=pd.Timedelta(hours=24) or r.get('status')!='online':
                raise ValueError('Stale/offline trading metadata')
            if not np.isfinite([r['quantity_step'],r['minimum_quantity'],r['minimum_notional_aud'],r['fee_rate']]).all() or min(r['quantity_step'],r['minimum_quantity'],r['minimum_notional_aud'])<=0 or not 0<=r['fee_rate']<1:
                raise ValueError('Invalid lot/minimum/fee metadata')
            mark=(q['bid_aud']+q['ask_aud'])/2
            qty=w*equity/mark-positions.get(k,0)
            # Round quantity toward zero and never sell more than reconciled units.
            qty=np.sign(qty)*np.floor(abs(qty)/r['quantity_step'])*r['quantity_step']
            price=q['ask_aud'] if qty>0 else q['bid_aud']
            notional=abs(qty)*price
            if abs(qty)<r['minimum_quantity'] or notional<r['minimum_notional_aud']:
                continue
            fee=notional*r['fee_rate']
            if qty>0: buy_cost+=notional+fee
            else: sell_proceeds+=notional-fee
            intents.append(dict(instrument=k,quantity=float(qty),limit_price_aud=price,estimated_fee_aud=fee))
        if buy_cost>account['cash']+sell_proceeds+1e-8:
            raise ValueError('Insufficient reconciled buying power after estimated fees')
        request=TargetRequest(targets,decision,'AUD')
        return dict(request=request,status='dry-run validated; no orders submitted',
                    intents=sorted(intents,key=lambda row:row['quantity']>0),equity_aud=equity,
                    live_supported=False)


def submit_recommended_weights(recommendation, *, production=None, mode='dry-run', **snapshots):
    """Callable research handoff; never calls a live submit method.

    Without production, use the AUD preflight contract. With an existing
    ProductionAdapter, map explicitly to Kraken USD spot, value a fresh USD
    account in AUD, and reserve intents through an in-memory copy of its actual
    RiskGateway. Original reservations/kill controls are preserved, never written.
    This validates integration, not permission to execute. LiveEngine's separate
    authorization, reconciliation and transport are deliberately not invoked.
    """
    if mode!='dry-run':
        raise PermissionError('Research handoff is dry-run only; live authorization is separate')
    if production is None:
        return DryRunExecution().submit_target_weights(recommendation,mode=mode,**snapshots)
    import hashlib
    from .boundary import ProductionAdapter
    from .data import canonical_json
    from .risk import AccountSnapshot, OrderIntent, RiskGateway, money, spot_only

    if not isinstance(production,ProductionAdapter) or not isinstance(production.gateway,RiskGateway):
        raise TypeError('Supply the existing ProductionAdapter with its RiskGateway')
    now=utc_instant(snapshots['now'])
    fx=money(snapshots['aud_per_usd'])
    if fx<=0 or not pd.Timedelta(0)<=now-utc_instant(snapshots['fx_timestamp'])<=pd.Timedelta(seconds=60):
        raise ValueError('Fresh positive USD-to-AUD execution FX is required')
    account=snapshots['account']
    if not isinstance(account,AccountSnapshot):
        raise TypeError('Production handoff requires a reconciled USD AccountSnapshot')
    mapping=dict(snapshots['instrument_map'])
    selected=set(recommendation.instrument)
    if set(mapping)!=selected or len(set(mapping.values()))!=len(mapping):
        raise ValueError('Explicit one-to-one research-to-Kraken instrument mapping required')
    for key in mapping.values(): spot_only(key)
    if any(source.base!=target.base for source,target in mapping.items()):
        raise ValueError('Production mapping cannot change the underlying asset')
    if set(account.positions)-set(mapping.values()):
        raise ValueError('Reconcile account instruments outside the selected production mapping')
    quotes=snapshots['quotes']; rules=snapshots['rules']
    converted=recommendation.copy(deep=True)
    converted['instrument']=converted.instrument.map(mapping)
    preflight_account=dict(timestamp=account.timestamp,cash=float(account.cash*fx),
        positions={k:float(q) for k,q in account.positions.items()},base_currency='AUD',
        unresolved_orders=snapshots.get('unresolved_orders',True),
        risk_approved=snapshots.get('risk_approved',False))
    aud_quotes={}; aud_rules={}
    for key in mapping.values():
        quote=quotes[key]; rule=rules[key]
        if quote.instrument!=key or rule.instrument!=key:
            raise ValueError('Quotes/rules must be bound to their production instrument')
        aud_quotes[key]=dict(timestamp=quote.timestamp,bid_aud=float(quote.bid*fx),ask_aud=float(quote.ask*fx))
        aud_rules[key]=dict(timestamp=rule.timestamp,status=rule.status,
            quantity_step=float(rule.quantity_step),minimum_quantity=float(rule.minimum_quantity),
            minimum_notional_aud=float(rule.minimum_notional*fx),fee_rate=float(snapshots['fee_rates'][key]))
    preview=DryRunExecution().submit_target_weights(converted,now=now,account=preflight_account,
        quotes=aud_quotes,rules=aud_rules,max_gross=snapshots.get('max_gross',1.),
        max_position=snapshots.get('max_position',.35))
    isolated=RiskGateway(':memory:')
    approved=[]
    try:
        production.gateway.connection.backup(isolated.connection)
        for name in ('MAX_ORDER','MAX_ASSET','MAX_TOTAL','MAX_DAILY_LOSS'):
            setattr(isolated,name,getattr(production.gateway,name))
        validator=ProductionAdapter(production.pipeline,isolated,enabled=True)
        for row in preview['intents']:
            key=row['instrument']
            side='buy' if row['quantity']>0 else 'sell'
            quote=quotes[key]
            identity=dict(decision=preview['request'].decision_timestamp.isoformat(),
                symbol=key.symbol,venue=key.venue,side=side,quantity=str(row['quantity']))
            decision=hashlib.sha256(canonical_json(identity)).hexdigest()
            intent=OrderIntent('research-'+decision,decision,key,side,abs(money(row['quantity'])),
                quote.ask if side=='buy' else quote.bid,money(snapshots['fee_rates'][key]))
            approved.append(validator.reserve(intent,account=account,quotes=quotes,rules=rules[key],now=now))
    finally:
        isolated.close()
    return dict(preview,production_intents=tuple(approved),
        status='dry-run: existing ProductionAdapter/RiskGateway validated isolated reservations; no orders submitted',
        gateway_unchanged=True,live_supported=False)
