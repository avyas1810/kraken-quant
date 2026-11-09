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
    def admission(*a,**k):events.append('admission');return [dict(admissible=False)]
    monkeypatch.setattr(m,'research_audit',admission)
    config=dict(archive=str(archive),archive_sha256='a'*64,start_month='2026-07',end_month='2026-09',validate_research=True,
                research_start='2026-07-01T00:00:00Z',research_end='2026-10-01T00:00:00Z')
    result=m.run_cycle(tmp_path,config)
    assert result['status']=='success'
    assert events==['catalogue','backfill','incremental','admission']
    assert result['research_admissible_pairs']==0

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
