"""Scheduler/failure fixtures are synthetic and isolated from production data."""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from urllib.error import HTTPError
import pytest
pytest.importorskip('pyarrow')
from test_ohlcv_collector import data_dir,Clock,Catalogue,discover,Public as CandlePublic
from test_book_collector import Public as BookPublic
from kraken_quant import data_pipeline as mod,market_data
from kraken_quant.asset_catalogue import _atomic_json
from kraken_quant.metadata_store import connect_metadata


def config(**changes):return replace(mod.PipelineConfig(),**changes)
def only(*tasks):return {t:t in tasks for t in mod.TASKS}
def files(root):return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def setup(root,settings=None,handler=None):
    clock=Clock();calls=[]
    def execute(task,recovery=False):
        calls.append((task,clock.monotonic(),recovery))
        return handler(task,clock) if handler else dict(status='ok')
    pipeline=mod.Pipeline(root,settings or config(),clock=clock,monotonic=clock.monotonic,sleep=clock.sleep,executor=execute)
    return pipeline,clock,calls


def test_order_once_and_optional_disabled(data_dir):
    p,c,calls=setup(data_dir)
    run=p.run();assert [v[0] for v in calls]==list(mod.TASKS[:-1])
    assert run['cycles']==1 and run['last_cycle']['success_count']==5
    assert p.run()['last_cycle']['tasks']==[]
    assert p.state['tasks']['ohlcv']['last_success_at']==mod.ohlc.sql_time(c())


def test_due_cadences_monotonic_no_completion_drift(data_dir):
    def work(task,clock):clock.sleep(8);return dict(status='ok')
    p,c,calls=setup(data_dir,config(enabled=only('ohlcv'),cadences={**mod.DEFAULT_CADENCES,'ohlcv':120}),work)
    result=p.run(continuous=True,max_cycles=3)
    assert [v[1] for v in calls]==[0,120,240]
    assert result['cycles']==3 and p.due['ohlcv']==360


def test_overrun_skips_slots_without_burst(data_dir):
    p,c,calls=setup(data_dir,config(enabled=only('ohlcv')),lambda t,c:(c.sleep(130) or dict(status='ok')))
    p.run(continuous=True,max_cycles=2)
    assert [v[1] for v in calls]==[0,180]
    assert p.state['tasks']['ohlcv']['next_due_at']==mod.ohlc.sql_time(c()+timedelta(seconds=50))


def test_shutdown_during_wait(data_dir):
    p,c,calls=setup(data_dir,config(enabled=only('ohlcv')))
    def stop(seconds):raise KeyboardInterrupt
    p.sleep=stop
    assert p.run(continuous=True)['status']=='stopped'
    # OS advisory lock was released.
    assert setup(data_dir)[0].run()['status']=='finished'


def test_interruption_is_persisted_and_recovered(data_dir):
    p,c,calls=setup(data_dir,handler=lambda t,c:(_ for _ in ()).throw(KeyboardInterrupt()))
    assert p.run()['status']=='stopped'
    interrupted=p._read_state()['active_cycle'];assert interrupted
    recovered,_,calls=setup(data_dir)
    result=recovered.run()['last_cycle']
    assert result['recovered_interrupted_cycle']==interrupted and all(v[2] for v in calls)
    assert recovered._read_state()['active_cycle'] is None


def test_dry_run_and_status_no_network_or_writes(data_dir,monkeypatch):
    monkeypatch.setattr(market_data,'_public_get',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network')))
    before=files(data_dir);p,c,_=setup(data_dir)
    assert p.dry_run()['tasks'][0]['due']
    assert len(p.status()['pairs'])==2
    assert before==files(data_dir)


def test_concurrent_instances_and_stale_constructed_state(data_dir):
    first,_,_=setup(data_dir);second,_,calls=setup(data_dir)
    first.root.mkdir();lock=mod.ProcessLock(first.root/'pipeline')
    try:
        with pytest.raises(PermissionError):second.run()
    finally:lock.close()
    first.run();second.run();assert not calls


def test_partial_failure_does_not_stop_other_tasks(data_dir):
    def work(t,c):return dict(status='partial',errors=[dict(pair_id='BTC-AUD',code='network_request_failed')]) if t=='ohlcv' else dict(status='ok')
    p,c,calls=setup(data_dir,handler=work);run=p.run()['last_cycle']
    assert run['status']=='partial' and len(calls)==5
    assert p.state['tasks']['ohlcv'].get('last_success_at') is None
    assert p.state['tasks']['ohlcv']['last_attempt_at']
    assert next(t for t in run['tasks'] if t['task']=='books')['status']=='ok'


def test_failed_refresh_blocks_collection(data_dir):
    p,c,calls=setup(data_dir,handler=lambda t,c:dict(status='error') if t=='metadata' else dict(status='ok'))
    run=p.run()['last_cycle'];assert [v[0] for v in calls]==['metadata','coverage']
    assert next(t for t in run['tasks'] if t['task']=='books')['error']=='blocked_by_metadata_refresh_failure'


def test_failure_preserves_last_success(data_dir):
    p,c,calls=setup(data_dir,config(enabled=only('ohlcv')));p.run();good=p.state['tasks']['ohlcv']['last_success_at']
    c.sleep(60);p.backend=lambda *a,**k:(_ for _ in ()).throw(TimeoutError('secret'))
    p.run();row=p.state['tasks']['ohlcv']
    assert row['last_success_at']==good and row['last_error']=='operation_failed'
    assert 'secret' not in json.dumps(p.state)


def policy(root,**kwargs):
    root.mkdir(exist_ok=True);clock=Clock();p=mod.PublicPolicy(root,config(**kwargs),clock,clock.monotonic,clock.sleep)
    return p,clock


def test_global_pacing_retries_and_context_scope(tmp_path,monkeypatch):
    p,c=policy(tmp_path);seen=[]
    def fetch(endpoint,params,**kwargs):seen.append((endpoint,c.monotonic()));return {'error':[]}
    monkeypatch.setattr(market_data,'_public_get',fetch)
    token=market_data.PUBLIC_REQUEST_POLICY.set(p)
    try:
        market_data.public_get('OHLC',{});market_data.public_get('Depth',{})
    finally:market_data.PUBLIC_REQUEST_POLICY.reset(token)
    assert seen==[('OHLC',0),('Depth',1.1)]
    assert all(e['request_started_at']<=e['response_received_at'] for e in p.events)
    market_data.public_get('Ticker',{}) # No policy: existing interface preserved.
    assert len(p.events)==2


@pytest.mark.parametrize('kind',['timeout','http429','exchange_throttle'])
def test_bounded_network_retry(tmp_path,kind):
    p,c=policy(tmp_path);calls=[]
    def fail():
        calls.append(c.monotonic())
        if kind=='timeout':raise TimeoutError('private')
        if kind=='http429':raise HTTPError('public',429,'slow',{'Retry-After':'10'},None)
        return {'error':['EAPI:Rate limit exceeded']}
    with pytest.raises(mod.PipelineFailure):p.request('OHLC',{},fail)
    assert len(calls)==2 and calls[1]>=5
    assert len(p.events)==2 and 'private' not in json.dumps(p.events)
    if kind=='timeout':assert all(e['response_received_at'] is None and e['request_finished_at'] for e in p.events)


def test_retry_after_bound_and_request_budget(tmp_path):
    p,c=policy(tmp_path,max_requests_per_cycle=1)
    p.request('OHLC',{},lambda:{'error':[]})
    with pytest.raises(mod.PipelineFailure,match='budget'):p.request('Depth',{},lambda:{'error':[]})
    p,c=policy(tmp_path)
    with pytest.raises(mod.PipelineFailure,match='retry_after'):
        p.request('OHLC',{},lambda:(_ for _ in ()).throw(HTTPError('x',429,'x',{'Retry-After':'1000'},None)))


def test_no_private_endpoints(tmp_path):
    p,c=policy(tmp_path)
    with pytest.raises(mod.PipelineFailure):p.request('AddOrder',{},lambda:None)
    with pytest.raises(mod.PipelineFailure):p.request('Balance',{},lambda:None)


@pytest.mark.parametrize('changes',[dict(public_interval_seconds=0.1),dict(request_attempts=0),dict(history_days=0),dict(book_depth=101),dict(cadences={}),dict(enabled={}),dict(pair='BTC-AUD'),dict(max_cycle_seconds=float('nan'))])
def test_invalid_config(changes):
    with pytest.raises(ValueError):config(**changes)


def test_real_collectors_reused_offline_validation_and_health(data_dir):
    clock=Clock();cat=Catalogue();discover(data_dir,transport=cat,clock=clock)
    candle=mod.ohlc.OhlcvCollector(data_dir,transport=CandlePublic(clock),clock=clock,sleeper=clock.sleep,monotonic=clock.monotonic)
    depth=mod.books.BookCollector(data_dir,transport=BookPublic(clock),clock=clock,sleeper=clock.sleep,monotonic=clock.monotonic)
    candle.collect(all_aud=True);depth.collect(all_aud=True)
    cfg=config(enabled=only('coverage'))
    before=files(data_dir);result=mod.validate(data_dir,cfg,clock())
    assert result['status']=='ok' and before==files(data_dir)
    p=mod.Pipeline(data_dir,cfg,clock=clock,monotonic=clock.monotonic,sleep=clock.sleep)
    rows=p.status()['pairs'];assert all(r['health']=='healthy' for r in rows)
    assert all(r['checkpoint_status']==r['book_checkpoint_status']==r['quote_checkpoint_status']=='matches' for r in rows)
    clock.sleep(300);assert all(r['health']=='delayed' for r in p.status()['pairs'])
    with connect_metadata(data_dir) as db:db.execute("UPDATE ingestion_state SET latest_observation_at='2026-11-01T00:00:00.000000Z' WHERE pair_id='BTC-AUD' AND dataset='prices_1m'")
    assert next(r for r in p.status()['pairs'] if r['pair_id']=='BTC-AUD')['health']=='failing'


def test_existing_backend_reuses_methods_and_public_fees(data_dir,monkeypatch):
    clock=Clock();backend=mod.ExistingTasks(data_dir,config(repair_on_start=False),clock,clock.monotonic,clock.sleep)
    discover(data_dir,transport=Catalogue(),clock=clock)
    calls=[]
    def candles(self,**selection):calls.append(('ohlcv',selection));return dict(results=[],status='finished')
    def depth(self,**selection):calls.append(('books',selection));return dict(results=[],status='finished',uncollected_pair_datasets=[])
    monkeypatch.setattr(mod.ohlc.OhlcvCollector,'collect',candles);monkeypatch.setattr(mod.books.BookCollector,'collect',depth)
    import kraken_quant.fee_data as fees
    def refresh(root,**kw):assert kw['readonly'] is None;calls.append(('fees',kw));return 'version'
    monkeypatch.setattr(fees,'refresh_fees',refresh)
    for name in ('ohlcv','books','fees'):assert backend.execute(name)['status']=='ok'
    assert [v[0] for v in calls]==['ohlcv','books','fees']
    assert calls[1][1]['duration_seconds']==0


def test_optional_eligibility_and_bounded_retention(data_dir):
    cfg=config(enabled=only('eligibility'),max_cycle_reports=2)
    p,c,calls=setup(data_dir,cfg)
    p.run(continuous=True,max_cycles=4)
    assert len(calls)==4 and len(list((p.root/'cycles').glob('*.json')))==2


def test_bounded_session(data_dir):
    p,c,calls=setup(data_dir,config(enabled=only('ohlcv')))
    assert p.run(continuous=True,max_seconds=10)['cycles']==1
    assert c.monotonic()==10


def test_offline_cli_dry_status(data_dir,monkeypatch,capsys):
    monkeypatch.setattr(market_data,'_public_get',lambda *a,**k:(_ for _ in ()).throw(AssertionError()))
    before=files(data_dir)
    for mode in ('dry-run','status'):
        assert mod.main([mode,'--all-aud','--data-dir',str(data_dir)])==0
        json.loads(capsys.readouterr().out)
    assert before==files(data_dir)


def test_scope_or_cadence_change_does_not_skip_new_work(data_dir):
    p,c,calls=setup(data_dir,config(pair='BTC-AUD',all_aud=False));p.run()
    other,c,calls=setup(data_dir,config())
    assert all(v['due'] for v in other.dry_run()['tasks'] if v['enabled'])
    other.run();assert len(calls)==5
    altered,c,calls=setup(data_dir,config(cadences={**mod.DEFAULT_CADENCES,'ohlcv':120}))
    altered.run();assert len(calls)==5


def test_recovery_and_recent_repair_use_existing_history_manager(data_dir,monkeypatch):
    import kraken_quant.history_manager as history
    clock=Clock();discover(data_dir,transport=Catalogue(),clock=clock)
    backend=mod.ExistingTasks(data_dir,config(),clock,clock.monotonic,clock.sleep);requests=[]
    monkeypatch.setattr(mod.ohlc.OhlcvCollector,'collect',lambda self,**kw:dict(status='finished',results=[]))
    def repair(self,**kwargs):requests.append(kwargs);return dict(status='finished',results=[])
    monkeypatch.setattr(history.HistoryManager,'run',repair)
    backend.execute('ohlcv');backend.execute('ohlcv');backend.execute('ohlcv',recovery=True)
    assert len(requests)==2
    assert all(r['repair'] and (r['end']-r['start'])==timedelta(minutes=720) for r in requests)


def test_book_interrupt_stops_pipeline(data_dir,monkeypatch):
    clock=Clock();discover(data_dir,transport=Catalogue(),clock=clock)
    backend=mod.ExistingTasks(data_dir,config(),clock,clock.monotonic,clock.sleep)
    monkeypatch.setattr(mod.books.BookCollector,'collect',lambda self,**kw:dict(status='interrupted'))
    p=mod.Pipeline(data_dir,config(enabled=only('books')),clock=clock,monotonic=clock.monotonic,sleep=clock.sleep,executor=backend.execute)
    assert p.run(continuous=True)['status']=='stopped'
    assert p._read_state()['active_cycle']


def test_restart_rate_pacing_and_budget_time(tmp_path):
    p,c=policy(tmp_path,max_cycle_seconds=2)
    p.request('OHLC',{},lambda:{'error':[]})
    other=mod.PublicPolicy(tmp_path,p.config,c,c.monotonic,c.sleep)
    other.request('Depth',{},lambda:{'error':[]})
    assert c.monotonic()==1.1
    with pytest.raises(mod.PipelineFailure,match='budget'):other.pause(2)


def test_no_financial_algorithm_or_credentials_import():
    import ast,inspect
    source=inspect.getsource(mod)
    tree=ast.parse(source)
    imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    assert not any(n in ('credentials','cost_algorithms','risk','strategy','execution') for n in imports)
    assert 'readonly=None' in source
    from kraken_quant.cost_algorithms import CostAlgorithms
    for name in ('calculate_spread_cost','calculate_book_execution','calculate_slippage','calculate_market_impact','calculate_total_cost'):
        with pytest.raises(NotImplementedError):getattr(CostAlgorithms(),name)(None)


def test_exhausted_throttle_defers_other_markets_and_restart(tmp_path):
    p,c=policy(tmp_path)
    with pytest.raises(mod.PipelineFailure):p.request('OHLC',{},lambda:{'error':['EAPI:Rate limit exceeded']})
    calls=[]
    with pytest.raises(mod.PipelineFailure,match='deferred_after_throttle'):p.request('Depth',{},lambda:calls.append(1))
    newer=mod.PublicPolicy(tmp_path,p.config,c,c.monotonic,c.sleep)
    with pytest.raises(mod.PipelineFailure,match='deferred_after_throttle'):newer.request('Depth',{},lambda:calls.append(1))
    assert not calls


def test_pipeline_end_to_end_reuses_real_collectors(data_dir,monkeypatch):
    clock=Clock();cat=Catalogue()
    original_discover=mod.catalogue.discover;original_candle=mod.ohlc.OhlcvCollector;original_book=mod.books.BookCollector
    monkeypatch.setattr(mod.catalogue,'discover',lambda root,**kw:original_discover(root,transport=cat,clock=clock))
    monkeypatch.setattr(mod.ohlc,'OhlcvCollector',lambda root,**kw:original_candle(root,transport=CandlePublic(clock),**kw))
    monkeypatch.setattr(mod.books,'BookCollector',lambda root,**kw:original_book(root,transport=BookPublic(clock),**kw))
    p=mod.Pipeline(data_dir,config(enabled=only('metadata','ohlcv','books','coverage'),repair_on_start=False),clock=clock,monotonic=clock.monotonic,sleep=clock.sleep)
    run=p.run()['last_cycle']
    assert run['status']=='finished' and [t['task'] for t in run['tasks']]==['metadata','ohlcv','books','coverage']
    assert all(r['checkpoint_status']=='matches' and r['stored_rows']==3 for r in p.status()['pairs'])
    before=files(data_dir/'market_data');assert p.run()['last_cycle']['tasks']==[]
    assert before==files(data_dir/'market_data')


def test_actual_collector_partial_market_failure_preserves_success(data_dir):
    clock=Clock();discover(data_dir,transport=Catalogue(),clock=clock)
    public=CandlePublic(clock)
    def transport(endpoint,params):
        if params['pair']=='XBTAUD':raise TimeoutError('private details')
        return public(endpoint,params)
    collector=mod.ohlc.OhlcvCollector(data_dir,transport=transport,clock=clock,sleeper=clock.sleep,monotonic=clock.monotonic)
    def execute(task,recovery=False):
        run=collector.collect(all_aud=True)
        return dict(status='partial',result=run,errors=[dict(pair_id=r['pair_id'],code=r['code']) for r in run['results'] if r['status']=='error'])
    p=mod.Pipeline(data_dir,config(enabled=only('ohlcv')),clock=clock,monotonic=clock.monotonic,sleep=clock.sleep,executor=execute)
    run=p.run()['last_cycle'];assert run['status']=='partial'
    assert collector.store.read_all('ETH-AUD','prices_1m').num_rows==3
    assert collector.store.read_all('BTC-AUD','prices_1m').num_rows==0
    with connect_metadata(data_dir,readonly=True) as db:
        assert db.execute("SELECT count(*) FROM ingestion_state WHERE pair_id='ETH-AUD'").fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM ingestion_state WHERE pair_id='BTC-AUD'").fetchone()[0]==0
    assert 'private details' not in json.dumps(run)


def test_scheduled_vs_actual_delay_reported(data_dir):
    p,c,calls=setup(data_dir,config(enabled=only('metadata','ohlcv')),lambda t,c:(c.sleep(40) or dict(status='ok')) if t=='metadata' else dict(status='ok'))
    tasks=p.run()['last_cycle']['tasks'];assert tasks[1]['delay_seconds']==40 and tasks[1]['excessive_delay']
    assert tasks[1]['scheduled_at']<tasks[1]['started_at']


def test_validate_rejects_incomplete_stored_candles(data_dir):
    clock=Clock();discover(data_dir,transport=Catalogue(),clock=clock)
    collector=mod.ohlc.OhlcvCollector(data_dir,transport=CandlePublic(clock),clock=clock,sleeper=clock.sleep,monotonic=clock.monotonic)
    collector.collect(all_aud=True)
    mod.books.BookCollector(data_dir,transport=BookPublic(clock),clock=clock,sleeper=clock.sleep,monotonic=clock.monotonic).collect(all_aud=True)
    import pyarrow as pa
    import pyarrow.parquet as pq
    from kraken_quant.storage_schema import dataset_schema
    rows=collector.store.read_all('BTC-AUD','prices_1m').to_pylist();rows[0]['is_complete']=False
    path=collector.store.partition_path('BTC-AUD','prices_1m',rows[0]['timestamp'].strftime('%Y-%m'))
    pq.write_table(pa.Table.from_pylist(rows,schema=dataset_schema('prices_1m')),path)
    before=files(data_dir)
    result=mod.validate(data_dir,config(),clock())
    assert result['status']=='error' and any(e['pair_id']=='BTC-AUD' for e in result['errors'])
    assert before==files(data_dir)
