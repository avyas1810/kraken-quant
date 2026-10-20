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
