from datetime import datetime,timezone
from types import SimpleNamespace
from pathlib import Path
from copy import deepcopy
import pytest
pytest.importorskip("pyarrow")
from kraken_quant.automated_collection import choose_universe,months,member_name,archive_jobs
from kraken_quant.ohlcv_collector import collection_universe,CollectionError
from kraken_quant.metadata_store import initialize_metadata
from kraken_quant.asset_catalogue import discover


def test_month_boundaries():
    assert list(months('2025-12','2026-02'))==['2025-12','2026-01','2026-02']


def test_exact_archive_aliases():
    assert member_name(dict(base_asset='BTC',quote_asset='USD'))=='XBTUSD_1.csv'
    assert member_name(dict(base_asset='DOGE',quote_asset='EUR'))=='XDGEUR_1.csv'


def test_selection_preserves_aud_and_uses_actual_archive_availability():
    assets=[dict(pair_id='BTC-AUD',base_asset='BTC',quote_asset='AUD')]
    assets += [dict(pair_id=f'A{i}-USD',base_asset=f'A{i}',quote_asset='USD') for i in range(80)]
    members={member_name(a):SimpleNamespace(file_size=i+1) for i,a in enumerate(assets[:75])}
    result=choose_universe(assets,members,64)
    assert len(result)==64 and result[0]['pair_id']=='BTC-AUD'
    assert all(member_name(a) in members for a in result)
    assert result==choose_universe(assets,members,64)


@pytest.mark.parametrize('target',[0,49,101])
def test_invalid_target(target):
    with pytest.raises(ValueError):choose_universe([],{},target)


def test_zero_budget_does_not_open_archive(tmp_path):
    result=archive_jobs(tmp_path,tmp_path/'absent.zip',[],start_month='2026-07',end_month='2026-09',max_months=0)
    assert result['completed_month_jobs']==0


def test_unsupported_scope_fails_before_access(tmp_path):
    with pytest.raises(CollectionError,match='unsupported_quote_scope'):
        collection_universe(tmp_path,('GBP',))


def test_only_aud_does_not_silently_expand_to_unavailable_sources():
    assert choose_universe([dict(pair_id='BTC-AUD',base_asset='BTC',quote_asset='AUD')],{},64)[0]['pair_id']=='BTC-AUD'

def test_complete_trade_evidence_is_hashed_and_point_in_time(tmp_path):
    from datetime import timedelta
    from kraken_quant.historical_population import encoded,sha,immutable
    from kraken_quant.continuity_sources import trade_windows
    start=datetime(2026,1,1,tzinfo=timezone.utc);end=start+timedelta(minutes=3);known=end+timedelta(hours=1)
    page=dict(request_since=str(int(start.timestamp())*10**9),kraken_pair_id='XBTUSD',received_at=known,
              body={'error':[],'result':{'BTC/USD':[['100','1',str(int(start.timestamp())+1),'b','m','',1]],'last':str(int(start.timestamp())*10**9+10**9)}})
    name='historical_imports/trades/BTC-USD/page.json';raw=encoded(page);immutable(tmp_path/name,raw)
    proof=dict(version=1,pair_id='BTC-USD',kraken_pair_id='XBTUSD',symbol='BTC/USD',start=start,end=end,known_at=known,
               dependencies=[dict(path=name,sha256=sha(raw))])
    blob=encoded(proof);immutable(tmp_path/'historical_imports/trade_windows'/(sha(blob)+'.json'),blob)
    reader=SimpleNamespace(data_dir=tmp_path,as_of=known,source_selection=None,
        _metadata=lambda pair:({'base_asset':'BTC','quote_asset':'USD','kraken_pair_id':'XBTUSD'},None))
    windows,errors=trade_windows(reader,'BTC-USD','prices_1m',start,end)
    assert not errors and len(windows)==1 and windows[0].complete and windows[0].no_trade_semantics
    assert windows[0].observation_times==(start,)
    reader.as_of=known-timedelta(seconds=1)
    assert trade_windows(reader,'BTC-USD','prices_1m',start,end)[0]==()
    reader.as_of=known
    (tmp_path/name).write_bytes(raw+b' ')
    with pytest.raises(ValueError,match='trade_window_dependency_changed'):
        trade_windows(reader,'BTC-USD','prices_1m',start,end)

def test_pipeline_order_reuses_existing_collectors(tmp_path,monkeypatch):
    import zipfile
    from kraken_quant import automated_collection as m
    from kraken_quant.metadata_store import initialize_metadata
    initialize_metadata(tmp_path)
    events=[];archive=tmp_path/'source.zip'
    with zipfile.ZipFile(archive,'w') as source:source.writestr('MANIFEST.json','{}')
    asset=dict(pair_id='BTC-AUD',base_asset='BTC',quote_asset='AUD')
    def discovery(root):events.append('catalogue');return {}
    monkeypatch.setattr(m,'discover',discovery)
    monkeypatch.setattr(m,'collection_universe',lambda *a:([asset],'audit',0))
    monkeypatch.setattr(m,'inventory',lambda *a:dict(integrity='ok',foreign_key_errors=0,markets=[]))
    def backfill(*a,**k):events.append('backfill');return {}
    monkeypatch.setattr(m,'archive_jobs',backfill)
    class Collector:
        def __init__(self,*a):pass
        def _binding(self):pass
        def collect(self,**kw):
            assert kw['pair_ids']==['BTC-AUD']
            events.append('incremental');return dict(results=[dict(pair_id='BTC-AUD',status='ok')])
    monkeypatch.setattr(m,'OhlcvCollector',Collector)
    def admission(*a,**k):events.append('admission');return dict(markets=[dict(admissible=False)],original_universe={},explicit_proposal={})
    from kraken_quant import research_validation
    monkeypatch.setattr(research_validation,'bounded_validation',admission)
    config=dict(archive=str(archive),archive_sha256='a'*64,start_month='2026-07',end_month='2026-09',validate_research=True,
                research_start='2026-07-01T00:00:00Z',research_end='2026-10-01T00:00:00Z')
    result=m.run_cycle(tmp_path,config)
    assert result['status']=='success'
    assert events==['catalogue','incremental']
    assert result['research_validation']['status']=='deferred' and result['maintenance']['status']=='deferred'

def test_enrolled_assets_are_not_dropped_when_archive_rank_changes():
    aud=dict(pair_id='BTC-AUD',base_asset='BTC',quote_asset='AUD')
    old=dict(pair_id='OLD-USD',base_asset='OLD',quote_asset='USD')
    candidates=[dict(pair_id=f'A{i}-USD',base_asset=f'A{i}',quote_asset='USD') for i in range(70)]
    members={member_name(a):SimpleNamespace(file_size=1000) for a in candidates}
    result=choose_universe([aud,old,*candidates],members,64,enrolled=('OLD-USD',))
    assert len(result)==64 and old in result


def test_remembered_ohlc_retention_gap_is_not_classified_no_trade(tmp_path):
    import json
    from kraken_quant.automated_collection import remember_gaps
    folder=tmp_path/'ohlcv_collection/responses/BTC-USD';folder.mkdir(parents=True)
    (tmp_path/'automated_collection').mkdir()
    fetched='2026-11-03T00:01:00.000000Z'
    stamp=int(datetime(2026,11,3,tzinfo=timezone.utc).timestamp())
    (folder/'response.json').write_text(json.dumps(dict(fetched_at=fetched,body={'result':{'BTC/USD':[[stamp],[stamp+60]],'last':stamp}})))
    results={'results':[dict(pair_id='BTC-USD',status='ok',fetched_at=fetched)]}
    queue=remember_gaps(tmp_path,{'BTC-USD':'2026-11-01T00:00:00Z'},results)
    assert len(queue)==1 and queue[0]['classification']=='MISSING_OR_UNVERIFIED'
    assert queue[0]['first']=='2026-11-01T00:01:00.000000Z'
    assert queue[0]['last']=='2026-11-02T23:59:00.000000Z'
    assert remember_gaps(tmp_path,{'BTC-USD':'2026-11-01T00:00:00Z'},results)==queue

@pytest.mark.parametrize('name,value',[('backfill_month_jobs',1000),('backfill_seconds',100000),('gap_requests',100000),('gap_jobs',100000)])
def test_unsafe_operational_budgets_rejected_before_work(name,value):
    import json
    from kraken_quant.automated_collection import validate_configuration
    policy=json.loads(Path('examples/automated_collection.json').read_text())
    policy[name]=value
    with pytest.raises(ValueError,match='invalid_bounded'):
        validate_configuration(policy)

def test_joint_proposal_preserves_original_rejection_and_fixed_dates(tmp_path,monkeypatch):
    from datetime import timedelta
    import pyarrow as pa
    from kraken_quant import research_data,parquet_store
    from kraken_quant.automated_collection import research_audit
    from kraken_quant.partition_algorithm import phase_cuts
    from kraken_quant.partition_contracts import PartitionRequest
    start=datetime(2026,7,1,tzinfo=timezone.utc);end=datetime(2026,10,1,tzinfo=timezone.utc)
    request=PartitionRequest(('A-USD','NEW-USD'),'fixture',datetime(2026,11,1,tzinfo=timezone.utc),frequency=timedelta(hours=1))
    cuts=phase_cuts(request,start,end)
    times=[cuts[i]+timedelta(minutes=n) for i,count in enumerate((1000,250,250)) for n in range(count)]
    calls=[]
    class Reader:
        def __init__(self,*a,**k):pass
        def generate_partition_plan(self,req):
            calls.append(req)
            ready=req.assets==('A-USD',)
            quality={'groups':[dict(phases={phase:dict(assets={'A-USD':{'prices_1m':{'quality':{'passed':True}}}}) for phase in ('training','validation','test')})]} if ready else {'failures':[dict(asset='NEW-USD',reason='insufficient_history')]}
            return SimpleNamespace(ready=ready,status='READY' if ready else 'INSUFFICIENT_HISTORY',manifest_id='a'*64 if ready else None,report=quality)
    class Store:
        def __init__(self,*a):pass
        def months(self,*a):return ('2026-07','2026-08','2026-09')
        def read(self,pair,dataset,month):
            values=times if pair=='A-USD' else [end-timedelta(minutes=1)]
            return pa.table({'timestamp':pa.array([v for v in values if v.strftime('%Y-%m')==month],type=pa.timestamp('us',tz='UTC'))})
    monkeypatch.setattr(research_data,'ResearchData',Reader);monkeypatch.setattr(parquet_store,'MonthlyParquetStore',Store)
    report=research_audit(tmp_path,[dict(pair_id='A-USD'),dict(pair_id='NEW-USD')],start=start,end=end,now=datetime(2026,11,1,tzinfo=timezone.utc))
    assert [r.assets for r in calls]==[('A-USD','NEW-USD'),('A-USD',)]
    assert all(r.start==start and r.end==end and r.window_mode=='FIXED_WINDOW' for r in calls)
    assert report['original_universe']['status']=='INSUFFICIENT_HISTORY'
    assert report['explicit_proposal']['excluded'][0]['pair_id']=='NEW-USD'
    assert [r['admissible'] for r in report['markets']]==[True,False]

def test_wall_clock_resume_expires_even_if_monotonic_is_unchanged(tmp_path):
    from datetime import timedelta
    from kraken_quant.data_pipeline import PublicPolicy,PipelineConfig,PipelineFailure
    now=[datetime(2026,11,1,tzinfo=timezone.utc)]
    policy=PublicPolicy(tmp_path,PipelineConfig(max_cycle_seconds=60),lambda:now[0],lambda:0,lambda _:None)
    now[0]+=timedelta(days=1)
    called=[]
    with pytest.raises(PipelineFailure,match='cycle_request_budget_exhausted'):
        policy.request('OHLC',{},lambda:called.append(True))
    assert not called


def test_validation_worker_timeout_is_bounded(tmp_path,monkeypatch):
    from datetime import timedelta
    from kraken_quant import research_validation as m
    (tmp_path/'automated_collection').mkdir()
    monkeypatch.setattr(m,'cached_result',lambda *a:None)
    now=[datetime(2026,11,1,tzinfo=timezone.utc)];killed=[]
    class Process:
        def poll(self):return None
        def kill(self):killed.append(True)
        def wait(self):return 1
    monkeypatch.setattr(m.subprocess,'Popen',lambda *a,**k:Process())
    result=m.bounded_validation(tmp_path,[dict(pair_id='BTC-USD')],{'research_validation_seconds':2},
        clock=lambda:now[0],sleep=lambda _:now.__setitem__(0,now[0]+timedelta(seconds=3)))
    assert killed and result['validation_pending']
    assert not result['markets'][0]['admissible']

def test_research_fingerprint_tracks_history_but_not_new_live_rows(tmp_path):
    import json,hashlib
    import pyarrow as pa
    import pyarrow.parquet as pq
    from kraken_quant.metadata_store import initialize_metadata
    from kraken_quant.research_validation import fingerprint
    initialize_metadata(tmp_path)
    archive=tmp_path/'Full.zip';archive.write_bytes(b'pinned-source')
    config=json.loads(Path('examples/automated_collection.json').read_text());config['archive']=str(archive)
    config['archive_sha256']=hashlib.sha256(archive.read_bytes()).hexdigest()
    folder=tmp_path/'market_data/BTC-USD/prices_1m';folder.mkdir(parents=True)
    historical=folder/'2026-07.parquet';historical.write_bytes(b'validated historical bytes')
    future=folder/'2026-11.parquet';pq.write_table(pa.table({'timestamp':[1]}),future)
    assets=[dict(pair_id='BTC-USD',kraken_pair_id='XBTUSD',status='online',last_seen='first')]
    first=fingerprint(tmp_path,assets,config)
    assets[0]['last_seen']='refreshed'
    pq.write_table(pa.table({'timestamp':[1,2]}),future)
    assert fingerprint(tmp_path,assets,config)==first
    historical.write_bytes(b'changed historical bytes')
    assert fingerprint(tmp_path,assets,config)!=first


def test_corrupted_validation_cache_never_admits(tmp_path):
    import json
    from kraken_quant.research_validation import cached_result
    folder=tmp_path/'automated_collection';(folder/'research_cache').mkdir(parents=True)
    (folder/'research-cache.json').write_text(json.dumps(dict(cache_id='a'*64)))
    (folder/'research_cache'/('a'*64+'.json')).write_text('{"markets":[{"admissible":true}]}')
    assert cached_result(tmp_path,[],{}) is None


def test_child_owned_utc_deadline_does_not_depend_on_scheduler_parent():
    from datetime import timedelta
    from kraken_quant.research_validation import enforce_worker_deadline
    now=[datetime(2026,11,1,tzinfo=timezone.utc)];exits=[]
    enforce_worker_deadline(now[0]+timedelta(seconds=2),clock=lambda:now[0],
        sleep=lambda _:now.__setitem__(0,now[0]+timedelta(seconds=3)),terminate=lambda code:exits.append(code))
    assert exits==[124]


def test_old_source_domain_does_not_expand_query_budget():
    from datetime import timedelta
    from kraken_quant.market_continuity import SourceWindow,ContinuityRequest
    start=datetime(2026,7,1,tzinfo=timezone.utc);end=datetime(2026,10,1,tzinfo=timezone.utc)
    source=SourceWindow(start,end,end,'official','a'*64,domain_start=datetime(2013,9,1,tzinfo=timezone.utc),domain_end=end)
    assert source.domain_start.year==2013
    with pytest.raises(ValueError,match='bounded_ordered_time_range_required'):
        ContinuityRequest(('BTC-USD',),source.domain_start,end)
    with pytest.raises(ValueError,match='ordered_source_domain_required'):
        SourceWindow(start,end,end,'official','a'*64,domain_start=end,domain_end=start)
