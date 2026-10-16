"""Operator CLI for disabled-by-default execution and deterministic simulation."""
import argparse
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pandas as pd

from .config import CONFIG
from .data import canonical_json, utc_instant
from .live import LiveEngine, KrakenTransport, approval_check, check_trading_permissions
from .live_sim import SimulatedKraken
from .risk import AccountSnapshot, InstrumentRules, OrderIntent, Quote, money
from .strategy import Instrument


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def validate_evidence_files(evidence, directory):
    from .live import evidence_files_check
    return evidence_files_check(evidence)


def simulated_demo(database):
    if Path(database).exists():
        raise ValueError('Simulation demo requires a new database; never reset existing evidence')
    now = pd.Timestamp('2026-10-16T12:00:00Z')
    exchange = SimulatedKraken(clock=lambda:now)
    instrument = Instrument('kraken','BTC/USD','BTC','USD')
    engine = LiveEngine(database,exchange,clock=lambda:now,enabled=True)
    try:
        rules=InstrumentRules('.001','.01','.001','1',now,instrument)
        exchange.mode='partial'
        engine.submit(OrderIntent('demo-partial','demo-decision-1',instrument,'buy','.1','100','.004'),
            account=AccountSnapshot(now,now.normalize(),'1000',{},'1000'),
            quotes={instrument:Quote(instrument,now,'99.99','100')},rules=rules,
            pair='XXBTZUSD',base_id='XXBT',quote_id='ZUSD')
        engine.reconcile()
        engine.close()
        engine=LiveEngine(database,exchange,clock=lambda:now,enabled=True)
        exchange.mode='lost_ack'
        from .live import AmbiguousSubmission
        try:
            engine.submit(OrderIntent('demo-lost-ack','demo-decision-2',instrument,'buy','.1','100','.004'),
                account=AccountSnapshot(now,now.normalize(),exchange.balances['ZUSD'],{instrument:exchange.balances['XXBT']},'1000'),
                quotes={instrument:Quote(instrument,now,'99.99','100')},rules=rules,
                pair='XXBTZUSD',base_id='XXBT',quote_id='ZUSD')
        except AmbiguousSubmission:
            pass
        engine.reconcile()
        engine.resume()
        result=engine.inspect()
        result.update(mode='SIMULATION_ONLY',real_order_submissions=0,
                      simulated_submissions=sum(endpoint=='AddOrder' for endpoint,_ in exchange.calls),
                      prospective_daily_decisions=0)
        return result
    finally:
        engine.close()


def local_engine(database):
    if not Path(database).is_file():
        raise ValueError('Existing live database required')
    connection=sqlite3.connect(str(database))
    try:
        row=connection.execute("SELECT body FROM live_values WHERE name='fingerprint'").fetchone()
        fingerprint=json.loads(row[0])
    finally:
        connection.close()
    class Local:
        simulated=False
        def request(self,*args,**kwargs):
            raise PermissionError('Local operator inspection has no exchange access')
    transport=Local()
    transport.fingerprint=fingerprint
    return LiveEngine(database,transport)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['simulate','inspect','kill','account-check','reconcile','resume',
        'submit','strategy-once','cancel','deadman','watch','day-baseline','validate-approval'])
    parser.add_argument('--database',default='.shadow-runtime/live.sqlite')
    parser.add_argument('--env-file',default='.env')
    parser.add_argument('--approval')
    parser.add_argument('--eligibility')
    parser.add_argument('--enable-live-orders',action='store_true')
    parser.add_argument('--intent',help='JSON with order_id, decision_key, symbol, side, quantity, limit_price, fee_rate')
    parser.add_argument('--order-id')
    parser.add_argument('--history-start',default=(CONFIG.data_end+pd.Timedelta(days=1)).isoformat())
    parser.add_argument('--day-start-equity')
    parser.add_argument('--observed-at')
    parser.add_argument('--account-fingerprint')
    parser.add_argument('--seconds',type=int,default=60)
    parser.add_argument('--max-orders',type=int,default=1)
    args=parser.parse_args(argv)
    engine=transport=None
    try:
        if args.command=='simulate':
            output=simulated_demo(args.database)
        elif args.command=='kill':
            if not Path(args.database).is_file():
                raise ValueError('Existing live database required')
            from .risk import RiskGateway
            gateway=RiskGateway(args.database)
            try:
                gateway.kill()
                output={'halted':True,'local_kill_persisted':True,'exchange_cancellation_confirmed':False}
            finally:
                gateway.close()
        elif args.command in {'inspect','day-baseline'}:
            engine=local_engine(args.database)
            if args.command=='day-baseline':
                if not args.day_start_equity or not args.observed_at:
                    raise ValueError('Recorded --day-start-equity and --observed-at required')
                engine.set_day_baseline(args.day_start_equity,observed_at=args.observed_at)
                output=engine.inspect()
            else:
                output=engine.inspect()
        elif args.command=='validate-approval':
            if not args.approval or not args.eligibility or not args.account_fingerprint:
                raise ValueError('Approval, eligibility and account fingerprint required')
            evidence=read_json(args.eligibility)
            validate_evidence_files(evidence,Path(args.eligibility).parent)
            approval_check(read_json(args.approval),evidence,fingerprint=args.account_fingerprint,now=pd.Timestamp.now(tz='UTC'))
            output={'approval_valid':True,'orders_submitted':0}
        else:
            mutation=args.command in {'submit','strategy-once','cancel','deadman','watch'}
            if mutation and (not args.enable_live_orders or not args.approval or not args.eligibility):
                raise PermissionError('Real mutation requires separate approval, reviewed eligibility and --enable-live-orders')
            evidence=read_json(args.eligibility) if args.eligibility else None
            approval=read_json(args.approval) if args.approval else None
            if mutation:
                validate_evidence_files(evidence,Path(args.eligibility).parent)
            Path(args.database).parent.mkdir(parents=True,exist_ok=True)
            transport=KrakenTransport.from_env(args.database,args.env_file,approval=approval,evidence=evidence)
            engine=LiveEngine(args.database,transport,enabled=mutation)
            if args.command in {'account-check','reconcile'}:
                output=engine.reconcile()
                output['verified_permissions']=check_trading_permissions(transport.request('GetApiKeyInfo'),pd.Timestamp.now(tz='UTC'))
                output['account_fingerprint']=transport.fingerprint
                output['identity_scope']='Dedicated API-key fingerprint; legal account identity requires operator verification'
            elif args.command=='resume':
                engine.resume()
                output=engine.inspect()
            elif args.command=='cancel':
                if not args.order_id:
                    raise ValueError('--order-id required')
                output=engine.cancel(args.order_id)
            elif args.command=='deadman':
                engine.refresh_deadman()
                output={'deadman_seconds':60}
            elif args.command=='watch':
                if not 1<=args.seconds<=3600:
                    raise ValueError('--seconds must be 1..3600')
                end=time.monotonic()+args.seconds
                while True:
                    engine.reconcile()
                    engine.refresh_deadman()
                    if time.monotonic()>=end:
                        break
                    time.sleep(min(20,max(0,end-time.monotonic())))
                output=engine.inspect()
            elif args.command=='strategy-once':
                if not args.day_start_equity:
                    raise ValueError('Recorded --day-start-equity required')
                from .live_strategy import FrozenLiveStrategy
                from .streaming import WebSocketMarketData
                candidate_path=Path(evidence['artifact_directory'])/evidence['candidate_file']
                runner=FrozenLiveStrategy(candidate_path)
                instruments=[Instrument('kraken',s,*s.split('/')) for s in runner.universe]
                metadata={k:transport.market(k) for k in instruments}
                provider=WebSocketMarketData()
                try:
                    now=pd.Timestamp.now(tz='UTC')
                    end=now.normalize()-pd.Timedelta(days=1)
                    histories={k:provider.bars(k,start=args.history_start,end=end) for k in instruments}
                    rates={k:money(transport.request('TradeVolume',{'pair':v['pair']})['fees'][v['pair']]['fee'])/100+Decimal('.0001') for k,v in metadata.items()}
                    output=engine.rebalance_strategy(runner,historical_data=histories,
                        quotes=lambda:provider.quotes(instruments),rules={k:v['rules'] for k,v in metadata.items()},
                        fee_rates=rates,metadata=metadata,day_start_equity=args.day_start_equity,
                        max_orders=args.max_orders)
                finally:
                    provider.close()
            else:
                if not args.intent or not args.day_start_equity:
                    raise ValueError('--intent and recorded --day-start-equity required')
                body=read_json(args.intent)
                symbol=body['symbol']
                if symbol not in CONFIG.universe:
                    raise ValueError('Instrument outside locked Kraken universe')
                instruments=[Instrument('kraken',s,*s.split('/')) for s in CONFIG.universe]
                metadata={k:transport.market(k) for k in instruments}
                instrument=next(k for k in instruments if k.symbol==symbol)
                meta=metadata[instrument]
                snapshot=engine.reconcile()['snapshot']
                balances={k:money(v) for k,v in snapshot['balances'].items()}
                quote_ids={v['quote_id'] for v in metadata.values()}
                if len(quote_ids)!=1:
                    raise PermissionError('Ambiguous USD balance identity')
                quote_id=next(iter(quote_ids))
                allowed={v['base_id'] for v in metadata.values()}|{quote_id}
                if set(balances)-allowed:
                    raise PermissionError('Unsupported account assets present')
                account=AccountSnapshot(snapshot['timestamp'],pd.Timestamp.now(tz='UTC').normalize(),balances.get(quote_id,Decimal(0)),
                    {k:balances[v['base_id']] for k,v in metadata.items() if balances.get(v['base_id'],0)},args.day_start_equity)
                order=OrderIntent(body['order_id'],body['decision_key'],instrument,body['side'],body['quantity'],body['limit_price'],body['fee_rate'])
                from .streaming import WebSocketMarketData
                provider=WebSocketMarketData()
                keys=set(account.positions)|{instrument}
                def quotes():
                    return provider.quotes(keys)
                try:
                    engine.submit(order,account=account,quotes=quotes,rules=meta['rules'],pair=meta['pair'],base_id=meta['base_id'],quote_id=meta['quote_id'])
                    output=engine.reconcile()
                finally:
                    provider.close()
        print(json.dumps(output,indent=2,sort_keys=True))
        return 0
    except Exception as exc:
        # Exception type only: credentials, signatures and raw responses excluded.
        if engine and args.command in {'submit','strategy-once','cancel','watch','deadman'}:
            engine.kill()
        print(json.dumps({'ok':False,'error_type':type(exc).__name__,'message':'Command failed closed; inspect local audit and operator documentation'}))
        return 1
    finally:
        if engine:
            engine.close()
        if transport:
            transport.close()


if __name__=='__main__':
    raise SystemExit(main())
