"""Live acceptance against deterministic Kraken-style exchange, never real orders."""
from dataclasses import replace
from decimal import Decimal
import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs

import pandas as pd
import pytest

from kraken_quant.live import (LiveEngine, KrakenTransport, AmbiguousSubmission, ExchangeRejected,
                              approval_check, check_trading_permissions, PERMISSIONS)
from kraken_quant.live_sim import SimulatedKraken
from kraken_quant.risk import AccountSnapshot, OrderIntent, Quote, InstrumentRules
from kraken_quant.strategy import Instrument

NOW = pd.Timestamp('2026-10-16T10:00:00Z')
BTC = Instrument('kraken','BTC/USD','BTC','USD')


def context(now=NOW, cash='1000', positions=None):
    return dict(account=AccountSnapshot(now, now.normalize(), cash, positions or {}, '1000'),
                quotes={BTC: Quote(BTC, now, '99.99', '100')},
                rules=InstrumentRules('.001','.01','.001','1',now,BTC),
                pair='XXBTZUSD',base_id='XXBT',quote_id='ZUSD')


def intent(key='order-1', quantity='.1'):
    return OrderIntent(key, 'decision-' + key, BTC, 'buy', quantity, '100', '.004')


@pytest.fixture
def setup(tmp_path):
    exchange = SimulatedKraken(clock=lambda: NOW)
    database = tmp_path/'live.sqlite'
    engine = LiveEngine(database, exchange, clock=lambda: NOW, enabled=True)
    yield engine, exchange, database
    engine.close()


def test_success_persistent_fills_and_duplicate_prevention(setup):
    engine, exchange, database = setup
    engine.submit(intent(), **context())
    engine.reconcile()
    engine.reconcile()
    assert engine.inspect()['fills'] == 1
    assert engine.inspect()['orders'][0]['state'] == 'closed'
    assert engine.db.execute('SELECT status FROM reservations').fetchone()[0] == 'terminal'
    assert exchange.balances['ZUSD'] == Decimal('989.9600')
    engine.close()
    recovered = LiveEngine(database,exchange,clock=lambda:NOW,enabled=True)
    try:
        recovered.reconcile()
        assert recovered.inspect()['fills'] == 1
        with pytest.raises(ValueError,match='Duplicate'):
            recovered.submit(intent(), **context(cash='989.96', positions={BTC:Decimal('.1')}))
    finally:
        recovered.close()
    # Fixture close is harmless except sqlite, which permits repeated close.


@pytest.mark.parametrize('mode,state', [('partial','canceled'),('partial_open','open')])
def test_partial_fills_and_cancel(setup, mode,state):
    engine, exchange, _ = setup
    exchange.mode = mode
    engine.submit(intent(),**context())
    engine.reconcile()
    assert engine.inspect()['orders'][0]['state'] == state
    assert exchange.balances['XXBT'] == Decimal('.05')
    if mode == 'partial_open':
        assert engine.db.execute('SELECT status FROM reservations').fetchone()[0] == 'reserved'
        engine.cancel('order-1')
        assert engine.inspect()['orders'][0]['state'] == 'canceled'
        assert engine.db.execute('SELECT status FROM reservations').fetchone()[0] == 'terminal'


def test_lost_ack_recovers_without_second_submission(setup):
    engine, exchange, _ = setup
    exchange.mode='lost_ack'
    with pytest.raises(AmbiguousSubmission):
        engine.submit(intent(), **context())
    assert engine.inspect()['orders'][0]['state'] == 'UNKNOWN'
    assert engine.inspect()['halted']
    engine.reconcile()
    assert engine.inspect()['orders'][0]['state'] == 'closed'
    engine.resume()
    assert not engine.inspect()['halted']
    assert sum(endpoint=='AddOrder' for endpoint,_ in exchange.calls) == 1


def test_timeout_before_acceptance_never_blind_retries(setup):
    engine, exchange, _ = setup
    exchange.mode='timeout_before_acceptance'
    with pytest.raises(AmbiguousSubmission):
        engine.submit(intent(),**context())
    engine.reconcile()
    assert engine.inspect()['orders'][0]['state'] == 'UNKNOWN'
    with pytest.raises(PermissionError,match='UNKNOWN'):
        engine.resume()
    with pytest.raises(PermissionError,match='Unsettled'):
        engine.submit(intent('other'),**context())
    assert sum(endpoint=='AddOrder' for endpoint,_ in exchange.calls)==1


def test_explicit_rejection_releases_only_after_reconciliation(setup):
    engine, exchange, _ = setup
    exchange.mode='reject'
    with pytest.raises(ExchangeRejected):
        engine.submit(intent(),**context())
    assert engine.inspect()['orders'][0]['state']=='rejected'
    assert engine.db.execute('SELECT status FROM reservations').fetchone()[0]=='terminal'
    assert engine.inspect()['fills']==0


@pytest.mark.parametrize('failure',['stale','funds','loss','order','kill','position'])
def test_safety_blocks_before_submission(setup,failure):
    engine, exchange, _=setup
    args=context()
    order=intent()
    if failure=='stale':
        args['quotes']={BTC:Quote(BTC,NOW-pd.Timedelta(seconds=6),'99.99','100')}
    elif failure=='funds':
        exchange.balances={'ZUSD':Decimal('1')}
        args=context(cash='1')
        args['account']=replace(args['account'],day_start_equity=Decimal('1'))
    elif failure=='loss':
        args['account']=replace(args['account'],day_start_equity=Decimal('1003'))
    elif failure=='order':
        order=intent(quantity='.21')
    elif failure=='kill':
        engine.kill()
    elif failure=='position':
        exchange.balances={'ZUSD':Decimal('1000'),'XXBT':Decimal('.35')}
        args=context(positions={BTC:Decimal('.35')})
        args['account']=replace(args['account'],day_start_equity=Decimal('1035'))
    with pytest.raises((ValueError,PermissionError)):
        engine.submit(order,**args)
    assert not any(endpoint=='AddOrder' for endpoint,_ in exchange.calls)


def test_disabled_default(setup):
    engine,exchange,_=setup
    engine.enabled=False
    with pytest.raises(PermissionError,match='disabled'):
        engine.submit(intent(),**context())
    assert exchange.calls==[]


def test_fill_mutation_and_unexpected_balance_halt(setup):
    engine,exchange,_=setup
    engine.submit(intent(),**context())
    engine.reconcile()
    trade=next(iter(exchange.trades.values()))
    trade['fee']='0.01'
    with pytest.raises(PermissionError):
        engine.reconcile()
    assert engine.inspect()['halted']


def test_unknown_restart_and_process_lock(setup):
    engine,exchange,database=setup
    with pytest.raises(PermissionError,match='Another'):
        LiveEngine(database,exchange,clock=lambda:NOW)
    exchange.mode='timeout_before_acceptance'
    with pytest.raises(AmbiguousSubmission):
        engine.submit(intent(),**context())
    engine.db.execute("UPDATE live_orders SET state='SUBMITTING'")
    engine.close()
    restarted=LiveEngine(database,exchange,clock=lambda:NOW,enabled=True)
    try:
        assert restarted.inspect()['orders'][0]['state']=='UNKNOWN'
        assert restarted.inspect()['halted']
    finally:
        restarted.close()


def test_deadman_cancels_resting_order(setup):
    engine,exchange,_=setup
    exchange.mode='resting'
    engine.submit(intent(),**context())
    exchange.clock=lambda:NOW+pd.Timedelta(seconds=61)
    exchange.expire_deadman()
    engine.clock=exchange.clock
    engine.reconcile()
    assert engine.inspect()['orders'][0]['state']=='canceled'


def test_permission_funding_denied():
    with pytest.raises(PermissionError):
        check_trading_permissions({'permissions': sorted(PERMISSIONS|{'withdraw-funds'})},NOW)


def test_signature_nonce_default_write_block_and_redaction(tmp_path):
    calls=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,*args): return json.dumps({'error':[],'result':{'permissions':sorted(PERMISSIONS)}}).encode()
    secret=base64.b64encode(b'not-a-real-secret').decode()
    def opener(req,timeout):
        calls.append(req)
        values=parse_qs(req.data.decode())
        nonce=values['nonce'][0]
        message=req.selector.encode()+hashlib.sha256(nonce.encode()+req.data).digest()
        expected=base64.b64encode(hmac.new(b'not-a-real-secret',message,hashlib.sha512).digest()).decode()
        assert req.get_header('Api-sign')==expected
        assert timeout==15
        return Response()
    transport=KrakenTransport(tmp_path/'nonce.sqlite','fake-key',secret,'separate-read-key',opener=opener,clock=lambda:NOW,sleeper=lambda _:None)
    try:
        transport.request('GetApiKeyInfo')
        transport.request('Balance')
        assert int(parse_qs(calls[1].data.decode())['nonce'][0])>int(parse_qs(calls[0].data.decode())['nonce'][0])
        with pytest.raises(PermissionError,match='disabled'):
            transport.request('AddOrder',{})
        with pytest.raises(PermissionError):
            transport.request('Withdraw',{})
        def failure(*args,**kwargs): raise RuntimeError('SECRET_KEY_SHOULD_NEVER_APPEAR')
        transport._opener=failure
        with pytest.raises(AmbiguousSubmission) as caught:
            transport.request('Balance')
        assert 'SECRET_KEY' not in str(caught.value)
    finally:
        transport.close()


def test_evidence_gate_does_not_accept_accelerated_replay(tmp_path):
    evidence={'candidate_sha256':'a'*64,'qualification_sha256':'b'*64,'shadow_export_sha256':'c'*64,
        'qualified':True,'reliability_gate_passed':True,'actual_daily_decisions':30,
        'simulated_order_lifecycles':20,'elapsed_seconds':29*86400,'completion_fraction':1,
        'fault_events':0,'missing_days':0,'evidence_type':'accelerated_replay'}
    from kraken_quant.data import canonical_json
    approval={'action':'AUTHORISE_KRAKEN_SPOT_LIVE_ORDERS','operator':'test operator',
              'account_fingerprint':'test','candidate_sha256':'a'*64,
              'evidence_sha256':hashlib.sha256(canonical_json(evidence)).hexdigest(),
              'approved_at':NOW.isoformat(),'expires_at':(NOW+pd.Timedelta(hours=1)).isoformat()}
    with pytest.raises(PermissionError,match='prospective'):
        approval_check(approval,evidence,fingerprint='test',now=NOW)
    evidence['evidence_type']='prospective_public_feed'
    from kraken_quant.candidate import REQUIREMENTS, qualify_candidate
    specification={'acceptance_rules':REQUIREMENTS,'prospective_start':'2026-09-01T00:00:00Z',
        'universe':['BTC/USD','ETH/USD','SOL/USD'],'strategy':'test-only','parameters':{},'features':'test',
        'costs':{},'risk_limits':{},'input_hashes':{'test':'a'*64},'code_revision':'test-only',
        'frozen_at':'2026-08-31T00:00:00Z','test_access':'locked','evaluation_split':'train'}
    candidate={'sha256':hashlib.sha256(canonical_json(specification)).hexdigest(),'specification':specification}
    candidate_path=tmp_path/'candidate.json'
    candidate_path.write_bytes(canonical_json(candidate))
    evidence['candidate_sha256']=candidate['sha256']
    approval['candidate_sha256']=candidate['sha256']
    shadow={'sharpe':.6,'max_drawdown':.1,'actual_daily_decisions':30,'simulated_order_lifecycles':20,
        'candidate_sha256':candidate['sha256'],'source':'prospective_public_feed','reliability_passed':True,
        'independent_review_passed':True,'net_return':.1,'benchmark_relative_return':.05,
        'positive_assets':3,'neighbour_parameters_positive':2,'round_trips':30,'completion_fraction':1,
        'doubled_spread_slippage_return':.01,'bootstrap_mean_excess_ci_low':.001,
        'elapsed_seconds':29*86400,'bootstrap_confidence':.95,'fault_events':0,'missing_days':0,
        'decision_dates_utc':[d.isoformat() for d in pd.date_range('2026-09-01',periods=30,tz='UTC')],
        'completed_lifecycle_ids':[str(i) for i in range(20)]}
    shadow_path=tmp_path/'shadow.json'
    shadow_path.write_bytes(canonical_json(shadow))
    report=qualify_candidate(candidate_path,shadow)
    report_path=tmp_path/'qualification.json'
    report_path.write_bytes(canonical_json(report))
    evidence['artifact_directory']=str(tmp_path)
    for prefix,path in [('candidate',candidate_path),('qualification',report_path),('shadow_export',shadow_path)]:
        evidence[prefix+'_file']=path.name
        key='candidate_file_sha256' if prefix=='candidate' else prefix+'_sha256'
        evidence[key]=hashlib.sha256(path.read_bytes()).hexdigest()
    approval['evidence_sha256']=hashlib.sha256(canonical_json(evidence)).hexdigest()
    approval_check(approval,evidence,fingerprint='test',now=NOW)
    with pytest.raises(PermissionError):
        approval_check(approval,evidence,fingerprint='different',now=NOW)

def test_signed_transport_partial_fill_reconciliation(tmp_path,monkeypatch):
    """In-memory HTTP endpoint verifies actual signatures; no network allowed.

    Approval is mocked only in this simulated transport integration. Separate
    tests exercise real default-disable and content-bound approval validation.
    """
    import kraken_quant.live as live
    monkeypatch.setattr(live,'approval_check',lambda *args,**kwargs:None)
    exchange=SimulatedKraken(clock=lambda:NOW)
    exchange.mode='partial'
    secret=base64.b64encode(b'simulation-hmac-key').decode()
    signatures=[]
    class Response:
        def __init__(self,body): self.body=body
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,*args): return json.dumps(self.body).encode()
    def opener(req,timeout):
        payload={k:v[0] for k,v in parse_qs(req.data.decode()).items()}
        nonce=payload.pop('nonce')
        expected=base64.b64encode(hmac.new(b'simulation-hmac-key',req.selector.encode()+hashlib.sha256(nonce.encode()+req.data).digest(),hashlib.sha512).digest()).decode()
        assert req.get_header('Api-sign')==expected
        signatures.append(expected)
        endpoint=req.selector.rsplit('/',1)[-1]
        return Response({'error':[],'result':exchange.request(endpoint,payload)})
    database=tmp_path/'signed.sqlite'
    transport=KrakenTransport(database,'test-only-key',secret,'test-only-read-key',approval={'test':True},evidence={'test':True},opener=opener,clock=lambda:NOW,sleeper=lambda _:None)
    transport.simulated=True
    engine=LiveEngine(database,transport,clock=lambda:NOW,enabled=True)
    try:
        engine.submit(intent(),**context())
        engine.reconcile()
        assert engine.inspect()['orders'][0]['state']=='canceled'
        assert engine.inspect()['fills']==1
        assert len(signatures)>=15
        assert exchange.balances['XXBT']==Decimal('.05')
    finally:
        engine.close()
        transport.close()


def test_day_baseline_cannot_reset_during_day(setup):
    engine,exchange,_=setup
    engine.submit(intent(),**context())
    engine.reconcile()
    with pytest.raises(PermissionError,match='baseline changed'):
        changed=context(cash='989.96',positions={BTC:Decimal('.1')})
        changed['account']=replace(changed['account'],day_start_equity=Decimal('999'))
        engine.submit(intent('other'),**changed)
    with pytest.raises(PermissionError,match='already frozen'):
        engine.set_day_baseline('999',observed_at=NOW.normalize())


def test_cli_simulation_and_local_kill_inspection(tmp_path,capsys):
    from kraken_quant.live_cli import main
    database=tmp_path/'cli.sqlite'
    assert main(['simulate','--database',str(database)])==0
    result=json.loads(capsys.readouterr().out)
    assert result['real_order_submissions']==0 and result['fills']==2
    assert result['prospective_daily_decisions']==0
    assert main(['kill','--database',str(database)])==0
    assert json.loads(capsys.readouterr().out)['halted']
    assert main(['inspect','--database',str(database)])==0
    assert json.loads(capsys.readouterr().out)['halted']
    assert main(['submit','--database',str(database)])==1
    assert json.loads(capsys.readouterr().out)['error_type']=='PermissionError'


def test_orphan_reservation_crash_fails_closed(tmp_path):
    from kraken_quant.risk import RiskGateway
    database=tmp_path/'orphan.sqlite'
    gateway=RiskGateway(database)
    args=context()
    gateway.reserve(intent(),account=args['account'],quotes=args['quotes'],rules=args['rules'],now=NOW)
    gateway.close()
    exchange=SimulatedKraken(clock=lambda:NOW)
    engine=LiveEngine(database,exchange,clock=lambda:NOW,enabled=True)
    try:
        assert engine.inspect()['halted']
        with pytest.raises(PermissionError,match='Orphan'):
            engine.resume()
        assert not any(endpoint=='AddOrder' for endpoint,_ in exchange.calls)
    finally:
        engine.close()

def test_emergency_kill_works_while_writer_lock_is_held(setup,capsys):
    engine,exchange,database=setup
    from kraken_quant.live_cli import main
    assert main(['kill','--database',str(database)])==0
    assert json.loads(capsys.readouterr().out)['local_kill_persisted']
    assert engine.inspect()['halted']
    with pytest.raises(PermissionError):
        engine.submit(intent(),**context())
    with pytest.raises(PermissionError):
        engine.refresh_deadman()
    assert not any(endpoint in {'AddOrder','CancelAllOrdersAfter'} for endpoint,_ in exchange.calls)


def test_slow_deadman_cannot_send_stale_quote(tmp_path):
    from kraken_quant.live import NotSubmitted
    tick=[NOW]
    exchange=SimulatedKraken(clock=lambda:tick[0])
    original=exchange.request
    def request(endpoint,params=None,**kwargs):
        response=original(endpoint,params,**kwargs)
        if endpoint=='CancelAllOrdersAfter':
            tick[0]+=pd.Timedelta(seconds=10)
        return response
    exchange.request=request
    engine=LiveEngine(tmp_path/'stale.sqlite',exchange,clock=lambda:tick[0],enabled=True)
    try:
        with pytest.raises(NotSubmitted,match='Quote became stale'):
            engine.submit(intent(),**context())
        assert not any(endpoint=='AddOrder' for endpoint,_ in exchange.calls)
        assert engine.inspect()['orders'][0]['state']=='rejected'
        assert engine.inspect()['halted']
    finally:
        engine.close()


def test_wire_guard_runs_after_transport_throttle(tmp_path,monkeypatch):
    import kraken_quant.live as live
    monkeypatch.setattr(live,'approval_check',lambda *args,**kwargs:None)
    tick=[NOW]
    sent=[]
    def sleeper(delay): tick[0]+=pd.Timedelta(seconds=10)
    def opener(*args,**kwargs):
        sent.append(True)
        raise AssertionError('No HTTP call permitted after guard failure')
    transport=KrakenTransport(tmp_path/'nonce.sqlite','test-key',base64.b64encode(b'test-secret').decode(),'read-key',
        approval={'test':True},evidence={'test':True},clock=lambda:tick[0],sleeper=sleeper,opener=opener)
    transport._permissions_observed_at=NOW
    def guard():
        if tick[0]-NOW>pd.Timedelta(seconds=5):
            raise live.NotSubmitted('Quote expired during throttle')
    try:
        with pytest.raises(live.NotSubmitted):
            transport.request('AddOrder',{},pre_send=guard)
        assert not sent
    finally:
        transport.close()


def test_market_metadata_retains_real_observation_time_and_refreshes(tmp_path,monkeypatch):
    import ccxt
    tick=[NOW]
    fetches=[]
    class Client:
        def load_markets(self):
            fetches.append(tick[0])
            return {'BTC/USD':{'id':'XXBTZUSD','info':{'base':'XXBT','quote':'ZUSD','lot_decimals':3,
                'pair_decimals':2,'tick_size':'.01','ordermin':'.001','costmin':'1','status':'online'}}}
    monkeypatch.setattr(ccxt,'kraken',lambda *_:Client())
    transport=KrakenTransport(tmp_path/'meta.sqlite','test-key',base64.b64encode(b'test-secret').decode(),'read-key',clock=lambda:tick[0],sleeper=lambda _:None)
    try:
        first=transport.market(BTC)
        tick[0]+=pd.Timedelta(minutes=30)
        second=transport.market(BTC)
        assert second['rules'].timestamp==first['rules'].timestamp==NOW
        assert len(fetches)==1
        tick[0]+=pd.Timedelta(minutes=31)
        third=transport.market(BTC)
        assert third['rules'].timestamp==tick[0]
        assert len(fetches)==2
    finally:
        transport.close()


def test_frozen_strategy_shared_pipeline_to_live_simulated_fill(tmp_path):
    from kraken_quant.live_strategy import FrozenLiveStrategy
    from kraken_quant.candidate import REQUIREMENTS
    from kraken_quant.data import canonical_json
    root=__import__('pathlib').Path(__file__).resolve().parents[1]
    spec={'strategy':'btc','parameters':{'lookback':2,'budget':.01},'features':'completed daily close',
        'costs':{'fee_rate':.004},'risk_limits':{'max_gross':.01},'input_hashes':{'synthetic-unit-test':'a'*64},
        'code_revision':'synthetic-unit-test','prospective_start':'2026-09-01T00:00:00Z',
        'frozen_at':'2026-08-31T00:00:00Z','universe':['BTC/USD'],'test_access':'locked','acceptance_rules':REQUIREMENTS,
        'source_code_hashes':{path:hashlib.sha256((root/path).read_bytes()).hexdigest() for path in ['kraken_quant/strategies.py','kraken_quant/strategy.py']}}
    candidate=tmp_path/'synthetic-candidate.json'
    candidate.write_bytes(canonical_json({'sha256':hashlib.sha256(canonical_json(spec)).hexdigest(),'specification':spec}))
    runner=FrozenLiveStrategy(candidate)
    history=pd.DataFrame({'close':[98.,99.,100.]},index=pd.date_range('2026-10-13',periods=3,tz='UTC'))
    args=context()
    exchange=SimulatedKraken(clock=lambda:NOW)
    engine=LiveEngine(tmp_path/'strategy.sqlite',exchange,clock=lambda:NOW,enabled=True)
    try:
        result=engine.execute_strategy(runner,historical_data={BTC:history},account=args['account'],quotes=args['quotes'],
            rules={BTC:args['rules']},fee_rates={BTC:Decimal('.004')},
            metadata={BTC:{'pair':'XXBTZUSD','base_id':'XXBT','quote_id':'ZUSD'}})
        engine.reconcile()
        assert engine.inspect()['fills']==1
        assert engine.db.execute('SELECT decision_key FROM reservations').fetchone()[0].startswith(runner.candidate_sha256)
        # Same candidate/day cannot submit another order after restart/re-plan.
        again=engine.execute_strategy(runner,historical_data={BTC:history},account=args['account'],quotes=args['quotes'],
            rules={BTC:args['rules']},fee_rates={BTC:Decimal('.004')},metadata={})
        assert again['decision']=='already_processed'
        future_history=history.copy()
        future_history.index=future_history.index+pd.Timedelta(days=10)
        with pytest.raises(PermissionError,match='future'):
            runner.plan(historical_data={BTC:future_history},account=args['account'],quotes=args['quotes'],rules={BTC:args['rules']},timestamp=NOW,fee_rates={BTC:Decimal('.004')})
        spec['source_code_hashes']['kraken_quant/strategies.py']='f'*64
        candidate.write_bytes(canonical_json({'sha256':hashlib.sha256(canonical_json(spec)).hexdigest(),'specification':spec}))
        with pytest.raises(PermissionError,match='source changed'):
            FrozenLiveStrategy(candidate)
    finally:
        engine.close()

def test_multi_asset_frozen_rebalance_sells_then_two_buys(tmp_path):
    from pathlib import Path
    from kraken_quant.live_strategy import FrozenLiveStrategy
    from kraken_quant.candidate import REQUIREMENTS
    from kraken_quant.data import canonical_json
    root=Path(__file__).resolve().parents[1]
    eth=Instrument('kraken','ETH/USD','ETH','USD')
    sol=Instrument('kraken','SOL/USD','SOL','USD')
    keys=[BTC,eth,sol]
    spec={'strategy':'multi','parameters':{'lookback':2,'regime_days':2,'volatility_days':2,'annual_vol_limit':1000,'budget':.08},
        'features':'synthetic-unit-test-completed-bars','costs':{'fee_rate':.004},'risk_limits':{'max_gross':.08},
        'input_hashes':{'synthetic-unit-test':'a'*64},'code_revision':'synthetic-unit-test',
        'prospective_start':'2026-09-01T00:00:00Z','frozen_at':'2026-08-31T00:00:00Z',
        'universe':[k.symbol for k in keys],'test_access':'locked','acceptance_rules':REQUIREMENTS,
        'source_code_hashes':{path:hashlib.sha256((root/path).read_bytes()).hexdigest() for path in ['kraken_quant/strategies.py','kraken_quant/strategy.py']}}
    candidate=tmp_path/'synthetic-multi-candidate.json'
    candidate.write_bytes(canonical_json({'sha256':hashlib.sha256(canonical_json(spec)).hexdigest(),'specification':spec}))
    runner=FrozenLiveStrategy(candidate)
    history=pd.DataFrame({'close':[98.,99.,100.]},index=pd.date_range('2026-10-13',periods=3,tz='UTC'))
    exchange=SimulatedKraken(clock=lambda:NOW,cash='960')
    exchange.balances['XXBT']=Decimal('.4')
    engine=LiveEngine(tmp_path/'multi.sqlite',exchange,clock=lambda:NOW,enabled=True)
    quotes={k:Quote(k,NOW,'99.99','100') for k in keys}
    rules={k:InstrumentRules('.001','.01','.001','1',NOW,k) for k in keys}
    metadata={BTC:{'pair':'XXBTZUSD','base_id':'XXBT','quote_id':'ZUSD'},
              eth:{'pair':'XETHZUSD','base_id':'XETH','quote_id':'ZUSD'},
              sol:{'pair':'SOLZUSD','base_id':'SOL','quote_id':'ZUSD'}}
    try:
        result=engine.rebalance_strategy(runner,historical_data={k:history for k in keys},quotes=lambda:quotes,
            rules=rules,fee_rates={k:Decimal('.004') for k in keys},metadata=metadata,day_start_equity='1000',max_orders=3)
        calls=[p for name,p in exchange.calls if name=='AddOrder']
        assert [p['type'] for p in calls]==['sell','buy','buy']
        assert result['orders_submitted']==3 and result['fills']==3
        assert result['remaining_rebalances']==0
        assert all(row['state']=='closed' for row in result['orders'])
        again=engine.rebalance_strategy(runner,historical_data={k:history for k in keys},quotes=lambda:quotes,
            rules=rules,fee_rates={k:Decimal('.004') for k in keys},metadata=metadata,day_start_equity='1000',max_orders=3)
        assert again['orders_submitted']==0
        assert sum(name=='AddOrder' for name,_ in exchange.calls)==3
        with pytest.raises(ValueError):
            engine.rebalance_strategy(runner,historical_data={},quotes={},rules={},fee_rates={},metadata=metadata,day_start_equity='1000',max_orders=4)
    finally:
        engine.close()

def test_real_manual_intent_cannot_bypass_frozen_strategy(setup,monkeypatch):
    import kraken_quant.live as live
    engine,exchange,_=setup
    exchange.simulated=False
    exchange.evidence={'candidate_sha256':'a'*64}
    exchange.approval={'test_only':True}
    monkeypatch.setattr(live,'approval_check',lambda *args,**kwargs:None)
    with pytest.raises(PermissionError,match='verified frozen strategy'):
        engine.submit(intent(),**context())
    assert not any(endpoint=='AddOrder' for endpoint,_ in exchange.calls)
