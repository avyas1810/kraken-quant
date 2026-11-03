"""Synthetic historical integrity tests; no financial algorithms or data filling."""
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import pytest
pytest.importorskip('pyarrow')
import pyarrow as pa
import pyarrow.parquet as pq
from test_ohlcv_collector import data_dir,Clock,NOW,candle,response
from test_book_collector import setup as book_setup
from kraken_quant import history_manager as mod
from kraken_quant.history_provider import HistoricalDataProvider,KrakenOHLCProvider
from kraken_quant.history_coverage import minute_coverage,missing_timestamps,inventory,coverage_report,availability
from kraken_quant.storage_schema import dataset_schema,table_from_records
from kraken_quant.metadata_store import connect_metadata
from kraken_quant.cost_algorithms import CostAlgorithms

END=mod.ohlc.minute_start(NOW)
START=END-timedelta(minutes=10)

class Public:
    def __init__(self,clock):self.clock=clock;self.calls=[];self.failure=None;self.change=None
    def __call__(self,endpoint,params):
        assert endpoint=='OHLC' and params['interval']==1 and params['assetVersion']==1
        self.calls.append((endpoint,dict(params),self.clock.monotonic()))
        if self.failure:raise self.failure
        key={'XBTAUD':'BTC/AUD','ETHAUD':'ETH/AUD'}[params['pair']]
        current=mod.ohlc.minute_start(self.clock())
        rows=[candle(current-timedelta(minutes=i)) for i in (3,2,1,0)]
        raw=response(rows=rows,key=key,now=self.clock())
        if self.change:self.change(raw)
        return raw

def setup(data_dir,clock=None):
    clock=clock or Clock();public=Public(clock)
    manager=mod.HistoryManager(data_dir,transport=public,clock=clock,sleeper=clock.sleep,monotonic=clock.monotonic)
    return manager,public,clock

def rows(stamps):
    return [dict(pair_id='BTC-AUD',timestamp=t,open='100',high='101',low='99',close='100',volume_base='0',trade_count=0,is_complete=True) for t in stamps]

def write_raw(manager,records,month='2026-11'):
    path=manager.collector.store.partition_path('BTC-AUD','prices_1m',month);path.parent.mkdir(parents=True,exist_ok=True)
    normalized=[]
    for row in records:
        copy=dict(row)
        from decimal import Decimal
        for name in ('open','high','low','close','volume_base'):copy[name]=Decimal(copy[name])
        normalized.append(copy)
    pq.write_table(pa.Table.from_pylist(normalized,schema=dataset_schema('prices_1m')),path)
    return path

def test_counts_gaps_intervals_and_edges():
    times=[START+timedelta(minutes=i) for i in (2,3,6)]
    cov=minute_coverage(times,START,END,NOW)
    assert cov['expected_completed_minutes']==10 and cov['present_minutes']==3
    assert cov['missing_minutes']==7 and cov['coverage_pct']==30
    assert cov['longest_continuous_minutes']==2
    assert cov['missing_beginning_minutes']==2 and cov['missing_ending_minutes']==3
    assert len(list(missing_timestamps(cov)))==7

def test_missing_classifications_and_uncommitted():
    source=dict(floor_verified=True,oldest_accessible=mod.ohlc.sql_time(END-timedelta(minutes=3)))
    cov=minute_coverage([],START,END+timedelta(minutes=1),NOW,source=source,invalid_times=[END-timedelta(minutes=2)])
    assert cov['unavailable_at_probe_minutes']==7 and cov['uncommitted_or_future_minutes']==1
    assert any(r['classification']=='confirmed_invalid_local_observation' for r in cov['missing_intervals'])
    assert cov['expected_completed_minutes']==10

def test_provider_interface_unimplemented_and_unapproved_sources():
    with pytest.raises(NotImplementedError):HistoricalDataProvider().fetch('BTC-AUD',START,END)
    with pytest.raises(SystemExit):mod.main(['backfill','--pair','BTC-AUD','--source','unapproved'])

def test_retention_probe_native_pair_and_no_older_pagination(data_dir):
    manager,public,clock=setup(data_dir)
    fetched=manager.provider.fetch('BTC-AUD',START,END)
    assert len(public.calls)==2 and public.calls[1][1]['since']==0
    assert public.calls[0][1]['pair']=='XBTAUD'
    assert fetched.evidence['floor_verified'] and fetched.evidence['accessible_history_exhausted']
    assert fetched.evidence['additional_probe_retrieved_older'] is False
    assert fetched.evidence['response_completed_counts']==[3,3]
    assert all(len(b.records)==3 and b.uncommitted_rows==1 for b in fetched.batches)
    calls=len(public.calls)
    empty=manager.provider.fetch('BTC-AUD',START,END-timedelta(minutes=4))
    assert not empty.batches and len(public.calls)==calls
    assert public.calls[1][2]-public.calls[0][2]>=1.1

def test_backfill_partial_and_idempotent_no_false_success(data_dir):
    manager,public,_=setup(data_dir)
    run=manager.run(pair='BTC-AUD',start=START,end=END)
    result=run['results'][0]
    assert result['status']=='partial_unavailable' and result['new_rows']==3
    assert result['coverage_after']['coverage_pct']==30
    assert result['coverage_after']['unavailable_at_probe_minutes']==7
    count=len(public.calls)
    repeated=manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    assert repeated['new_rows']==0 and repeated['request_skipped']
    assert len(public.calls)==count
    assert mod.ohlc._checkpoint(data_dir,'BTC-AUD')['latest_observation_at']==mod.ohlc.sql_time(END-timedelta(minutes=1))

def test_accessible_repair_without_erasing_history(data_dir):
    manager,public,clock=setup(data_dir)
    first=manager.run(pair='BTC-AUD',start=START,end=END-timedelta(minutes=2))['results'][0]
    assert first['new_rows']==1
    repaired=manager.run(pair='BTC-AUD',start=START,end=END,repair=True)['results'][0]
    assert repaired['new_rows']==2 and repaired['coverage_after']['present_minutes']==3
    assert len(manager.collector.store.read_all('BTC-AUD','prices_1m'))==3
    assert repaired['status']=='partial_unavailable'
    assert public.calls[-1][1]['since']>=int((END-timedelta(minutes=3)).timestamp())

def test_complete_requested_window_and_zero_trade_rows(data_dir):
    manager,public,_=setup(data_dir)
    start=END-timedelta(minutes=3)
    public.change=lambda b:[r.__setitem__(6,'0') or r.__setitem__(7,0) for r in next(v for k,v in b['result'].items() if k!='last')]
    run=manager.run(pair='BTC-AUD',start=start,end=END)
    assert run['results'][0]['status']=='complete'
    stats,_,_=inventory(data_dir,'BTC-AUD','prices_1m',start,END,NOW)
    assert stats['explicit_source_zero_trade_candles']==3

def test_duplicate_invalid_conflicting_and_out_of_order_inventory(data_dir):
    manager,_,_=setup(data_dir)
    good=rows([END-timedelta(minutes=i) for i in (1,3,2)])
    bad=deepcopy(good[0]);bad['high']='1'
    conflict=deepcopy(good[1]);conflict['close']='100.5'
    manager.collector._binding()
    write_raw(manager,good+[deepcopy(good[0]),bad,conflict])
    stats,times,invalid=inventory(data_dir,'BTC-AUD','prices_1m',START,END,NOW)
    assert stats['records']==6 and stats['duplicate_records']==2
    assert stats['invalid_records']==1 and stats['conflicting_keys']==1
    assert stats['out_of_order_stored_rows']>0 and len(times)==1 and len(invalid)==2
    run=manager.run(pair='BTC-AUD',start=START,end=END)
    assert run['results'][0]['status']=='error'

def test_incomplete_stored_candle_excluded(data_dir):
    manager,_,_=setup(data_dir)
    record=rows([END])[0];record['is_complete']=False
    write_raw(manager,[record])
    stats,times,_=inventory(data_dir,'BTC-AUD','prices_1m',START,END+timedelta(minutes=1),NOW)
    assert not times and stats['uncommitted_records']==1

def test_schema_failure_reports_unknown(data_dir):
    manager,_,_=setup(data_dir)
    path=manager.collector.store.partition_path('BTC-AUD','prices_1m','2026-11');path.parent.mkdir(parents=True)
    pq.write_table(pa.table({'wrong':[1]}),path)
    report=coverage_report(data_dir,start=START,end=END,now=NOW,pair='BTC-AUD')
    assert report['pairs'][0]['data_readiness']=='Unknown'
    assert report['pairs'][0]['datasets']['prices_1m']['partition_errors']
    assert report['pairs'][0]['datasets']['prices_1m']['requested_window']['coverage_pct'] is None

def test_conflicting_historical_values_preserve_existing(data_dir):
    manager,public,_=setup(data_dir)
    manager.run(pair='BTC-AUD',start=START,end=END-timedelta(minutes=1))
    path=manager.collector.store.partition_path('BTC-AUD','prices_1m','2026-11');before=path.read_bytes()
    public.change=lambda b:next(v for k,v in b['result'].items() if k!='last')[0].__setitem__(4,'100.5')
    result=manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    assert result['code']=='conflicting_historical_values' and path.read_bytes()==before

def test_checkpoint_failure_restart_replays_pending(data_dir,monkeypatch):
    manager,public,clock=setup(data_dir)
    original=manager.collector._set_checkpoint
    def fail(asset,records):
        if records:raise OSError('checkpoint failed')
        return original(asset,records)
    monkeypatch.setattr(manager.collector,'_set_checkpoint',fail)
    assert manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]['status']=='error'
    assert len(manager.collector.store.read_all('BTC-AUD','prices_1m'))==3
    assert mod.ohlc._checkpoint(data_dir,'BTC-AUD') is None
    resumed,_,_=setup(data_dir,clock)
    result=resumed.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    assert result['checkpoint']['latest_observation_at']==mod.ohlc.sql_time(END-timedelta(minutes=1))
    assert not list((manager.collector.root/'pending').glob('*.json'))

def test_hard_interruption_and_recovery(data_dir,monkeypatch):
    manager,_,clock=setup(data_dir)
    original=manager.collector._set_checkpoint
    def stop(asset,records):
        if records:raise KeyboardInterrupt()
        return original(asset,records)
    monkeypatch.setattr(manager.collector,'_set_checkpoint',stop)
    assert manager.run(pair='BTC-AUD',start=START,end=END)['status']=='interrupted'
    resumed,_,_=setup(data_dir,clock)
    assert resumed.run(pair='BTC-AUD',start=START,end=END)['results'][0]['new_rows']==0

def test_all_aud_individual_failures_and_public_only(data_dir):
    manager,public,_=setup(data_dir);public.failure=TimeoutError('SECRET')
    run=manager.run(all_aud=True,start=START,end=END)
    assert run['selected_pairs']==['BTC-AUD','ETH-AUD']
    assert len(run['results'])==2 and all(r['status']=='error' for r in run['results'])
    assert 'SECRET' not in json.dumps(run)

def test_monthly_partitions_and_existing_history(data_dir):
    manager,public,clock=setup(data_dir)
    current=mod.ohlc.minute_start(clock())
    public.change=lambda b:b['result'].update({'BTC/AUD':[candle(t) for t in (current-timedelta(days=1),current-timedelta(minutes=1),current)]})
    result=manager.run(pair='BTC-AUD',start=current-timedelta(days=2),end=current)['results'][0]
    assert result['new_rows']==2
    assert manager.collector.store.months('BTC-AUD','prices_1m')==('2026-10','2026-11')

def test_empty_source_is_confirmed_unavailable_not_fabricated(data_dir):
    manager,public,_=setup(data_dir)
    public.change=lambda b:b['result'].update({'BTC/AUD':[]})
    result=manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    assert result['status']=='partial_unavailable' and result['new_rows']==0
    assert result['source_availability']['no_completed_history_as_of']
    count=len(public.calls)
    repeated=manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    assert repeated['request_skipped'] and len(public.calls)==count
    assert not manager.collector.store.read_all('BTC-AUD','prices_1m').num_rows

def test_source_out_of_order_is_reported_and_not_persisted(data_dir):
    manager,public,_=setup(data_dir)
    public.change=lambda b:b['result']['BTC/AUD'].__setitem__(slice(0,2),list(reversed(b['result']['BTC/AUD'][:2])))
    assert manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]['code']=='invalid_historical_response'
    report=coverage_report(data_dir,start=START,end=END,now=NOW,pair='BTC-AUD')
    assert report['pairs'][0]['datasets']['prices_1m']['source_audit']['out_of_order_source_responses']==1

def test_quote_and_book_counts_are_not_minute_bar_expectations(data_dir):
    book,_,clock=book_setup(data_dir)
    book.collect(all_aud=True)
    report=coverage_report(data_dir,start=START,end=END,now=clock(),all_aud=True)
    btc=report['pairs'][0]['datasets']
    assert btc['quotes_1m']['unique_timestamps']==1
    assert btc['orderbook']['complete_snapshots']==1 and btc['orderbook']['records']==4
    assert btc['quotes_1m']['sampling']['expected_observation_count'] is None

def test_offline_cli_readiness_no_writes_or_algorithm_changes(data_dir,monkeypatch,capsys):
    manager,_,clock=setup(data_dir);manager.run(pair='BTC-AUD',start=START,end=END)
    before={str(p):p.read_bytes() for p in data_dir.rglob('*') if p.is_file()}
    monkeypatch.setattr('kraken_quant.market_data.public_get',lambda *a,**k:(_ for _ in ()).throw(AssertionError()))
    assert mod.main(['coverage','--all-aud','--start',START.isoformat(),'--end',END.isoformat(),'--data-dir',str(data_dir)])==0
    data=json.loads(capsys.readouterr().out)
    assert all(p['data_readiness']=='Insufficient' for p in data['pairs'])
    assert before=={str(p):p.read_bytes() for p in data_dir.rglob('*') if p.is_file()}
    for name in ('calculate_spread_cost','calculate_book_execution','calculate_slippage','calculate_market_impact','calculate_total_cost'):
        with pytest.raises(NotImplementedError):getattr(CostAlgorithms(),name)(None)

@pytest.mark.parametrize('start,end',[(NOW,END),(END,END),(START.replace(tzinfo=None),END)])
def test_invalid_window_rejected(start,end):
    with pytest.raises(ValueError):minute_coverage([],start,end,NOW)


def test_partial_month_failure_replays_intent(data_dir,monkeypatch):
    manager,public,clock=setup(data_dir)
    current=mod.ohlc.minute_start(clock())
    public.change=lambda b:b['result'].update({'BTC/AUD':[candle(t) for t in (current-timedelta(days=1),current-timedelta(minutes=1),current)]})
    original=manager.collector.store._replace
    def fail(path,*args):
        if path.stem=='2026-11':raise OSError('disk interrupted')
        return original(path,*args)
    monkeypatch.setattr(manager.collector.store,'_replace',fail)
    result=manager.run(pair='BTC-AUD',start=current-timedelta(days=2),end=current)['results'][0]
    assert result['status']=='error' and manager.collector.store.months('BTC-AUD','prices_1m')==('2026-10',)
    assert mod.ohlc._checkpoint(data_dir,'BTC-AUD') is None
    resumed,_,_=setup(data_dir,clock)
    resumed.run(pair='BTC-AUD',start=current-timedelta(days=2),end=current)
    assert len(resumed.collector.store.read_all('BTC-AUD','prices_1m'))>=2
    assert mod.ohlc._checkpoint(data_dir,'BTC-AUD')['latest_observation_at']==mod.ohlc.sql_time(current-timedelta(minutes=1))

def test_positive_readiness_for_explicit_window(data_dir):
    manager,_,clock=setup(data_dir)
    start=END-timedelta(minutes=3)
    manager.run(pair='BTC-AUD',start=start,end=END)
    report=coverage_report(data_dir,start=start,end=END,now=clock(),pair='BTC-AUD')['pairs'][0]
    assert report['data_readiness']=='Sufficient'
    assert not report['datasets']['prices_1m']['ninety_days_available']
    assert report['datasets']['prices_1m']['calendar_days']==1

@pytest.mark.parametrize('change',[
 lambda b:b.update(error=['EService:Unavailable']),
 lambda b:b.update(result={'OTHER/AUD':[], 'last':0}),
 lambda b:b['result']['BTC/AUD'][0].__setitem__(1,'-1'),
 lambda b:b['result']['BTC/AUD'].__setitem__(1,deepcopy(b['result']['BTC/AUD'][0])[:4]+['100.5']+deepcopy(b['result']['BTC/AUD'][0])[5:]),
])
def test_invalid_api_observations_do_not_advance_checkpoint(data_dir,change):
    manager,public,_=setup(data_dir);public.change=change
    result=manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    assert result['status']=='error'
    assert mod.ohlc._checkpoint(data_dir,'BTC-AUD') is None

def test_rate_limit_deferral_all_pairs(data_dir):
    manager,public,_=setup(data_dir)
    public.change=lambda b:b.update(error=['EAPI:Rate limit exceeded'])
    result=manager.run(all_aud=True,start=START,end=END)
    assert len(public.calls)==1 and len(result['results'])==2
    assert result['results'][1]['code']=='deferred_after_rate_limit'

def test_since_overlap_and_nondefault_interval_rejected(data_dir):
    manager,public,_=setup(data_dir)
    start=END-timedelta(minutes=3)
    manager.provider.fetch('BTC-AUD',start,END)
    assert public.calls[0][1]['since']==int(start.timestamp())-60
    with pytest.raises(ValueError):manager.provider.fetch('BTC-AUD',START,END,interval_minutes=5)

def test_missing_db_does_not_initialize(tmp_path):
    missing=tmp_path/'missing'
    with pytest.raises(Exception):mod.HistoryManager(missing).run(pair='BTC-AUD',start=START,end=END)
    assert not missing.exists()

def test_checkpoint_ahead_cannot_regress(data_dir):
    manager,_,_=setup(data_dir)
    with connect_metadata(data_dir) as db:db.execute("INSERT INTO ingestion_state VALUES ('BTC-AUD','prices_1m',?,?)",(mod.ohlc.sql_time(END),mod.ohlc.sql_time(NOW)))
    result=manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    assert result['code']=='checkpoint_ahead_of_verified_storage'
    assert mod.ohlc._checkpoint(data_dir,'BTC-AUD')['latest_observation_at']==mod.ohlc.sql_time(END)

def test_backfill_cli_partial_exit_and_repair(data_dir,monkeypatch,capsys):
    manager,_,_=setup(data_dir)
    monkeypatch.setattr(mod,'HistoryManager',lambda directory:manager)
    args=['--pair','BTC-AUD','--start',START.isoformat(),'--end',END.isoformat(),'--data-dir',str(data_dir)]
    assert mod.main(['backfill',*args])==2
    assert json.loads(capsys.readouterr().out)['results'][0]['status']=='partial_unavailable'
    assert mod.main(['repair',*args])==2
    assert json.loads(capsys.readouterr().out)['results'][0]['request_skipped']


def test_source_receipt_and_cost_interface_preserved(data_dir):
    book,_,_=book_setup(data_dir);book._binding()
    manager,public,_=setup(data_dir)
    result=manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    for path in (data_dir/'ohlcv_collection/responses/BTC-AUD').glob('*.json'):
        raw=json.loads(path.read_text())
        assert raw['source']=='injected_test' and raw['params']['pair']=='XBTAUD'
        assert raw['params']['interval']==1
    from kraken_quant.cost_inputs import InputService
    from kraken_quant.cost_contracts import CostRequest
    from decimal import Decimal
    prepared=InputService(data_dir,clock=lambda:NOW).prepare(CostRequest('BTC-AUD','buy',Decimal('1000'),'taker',NOW,'reserved-v1'))
    assert prepared.inputs.candle is not None and prepared.inputs.candle.is_complete
    assert prepared.status=='inputs_incomplete'  # quote/book/fee data not manufactured.

def test_many_day_span_is_not_ninety_day_history(data_dir):
    manager,_,clock=setup(data_dir);manager.collector._binding()
    manager.collector.store.write('prices_1m',rows([END-timedelta(days=100),END-timedelta(minutes=1)]))
    report=coverage_report(data_dir,start=END-timedelta(days=90),end=END,now=clock(),pair='BTC-AUD')['pairs'][0]
    assert report['datasets']['prices_1m']['span_days']>90
    assert not report['datasets']['prices_1m']['ninety_days_available']
    assert report['data_readiness']=='Insufficient'

@pytest.mark.skipif(os.environ.get('KRAKEN_HISTORY_SMOKE')!='1',reason='Opt-in real public Kraken history integration')
def test_live_kraken_retention_and_persistence(tmp_path):
    from kraken_quant.metadata_store import initialize_metadata
    from kraken_quant.asset_catalogue import COLUMNS
    from kraken_quant.db_environment import load_config
    from datetime import datetime,timezone
    root=load_config().data_dir
    initialize_metadata(tmp_path)
    with connect_metadata(root,readonly=True) as source:
        records=source.execute('SELECT '+','.join(COLUMNS)+' FROM assets').fetchall()
    with connect_metadata(tmp_path) as dest:
        dest.executemany('INSERT INTO assets VALUES ('+','.join('?' for _ in COLUMNS)+')',records)
    (tmp_path/'catalogue_discovery').mkdir()
    (tmp_path/'catalogue_discovery/latest.json').write_bytes((root/'catalogue_discovery/latest.json').read_bytes())
    end=mod.ohlc.minute_start(datetime.now(timezone.utc));start=end-timedelta(days=90)
    manager=mod.HistoryManager(tmp_path)
    result=manager.run(pair='BTC-AUD',start=start,end=end)['results'][0]
    assert result['status']=='partial_unavailable'
    assert result['new_rows']>0 and result['coverage_after']['missing_minutes']>0
    assert result['source_availability']['floor_verified']
    assert result['source_availability']['additional_probe_retrieved_older'] is False
    assert all(r['is_complete'] for r in manager.collector.store.read_all('BTC-AUD','prices_1m').to_pylist())
    assert result['checkpoint']['latest_observation_at']==mod.ohlc.sql_time(end-timedelta(minutes=1))


def test_provider_records_respect_requested_end(data_dir):
    manager,_,_=setup(data_dir)
    fetched=manager.provider.fetch('BTC-AUD',START,END-timedelta(minutes=2))
    assert len(fetched.records)==1 and all(START<=r['timestamp']<END-timedelta(minutes=2) for r in fetched.records)

def test_probe_conflict_rejected_before_persistence(data_dir):
    manager,public,_=setup(data_dir)
    count=[0]
    def changed(b):
        count[0]+=1
        if count[0]==2:b['result']['BTC/AUD'][0][4]='100.5'
    public.change=changed
    result=manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    assert result['code']=='conflicting_source_probe_values'
    assert not manager.collector.store.read_all('BTC-AUD','prices_1m').num_rows

def test_unfinished_zero_placeholder_is_not_a_candle(data_dir):
    manager,public,_=setup(data_dir)
    public.change=lambda b:b['result'].update({'BTC/AUD':[[0,'0','0','0','0','0','0',0]]})
    result=manager.run(pair='BTC-AUD',start=START,end=END)['results'][0]
    assert result['new_rows']==0 and result['source_availability']['response_completed_counts']==[0,0]
    assert result['source_availability']['accessible_history_exhausted']
    assert not manager.collector.store.read_all('BTC-AUD','prices_1m').num_rows


def test_previously_available_source_becomes_empty(data_dir):
    manager,public,clock=setup(data_dir)
    manager.run(pair='BTC-AUD',start=START,end=END)
    clock.sleep(120)
    public.change=lambda b:b['result'].update({'BTC/AUD':[[0,'0','0','0','0','0','0',0]]})
    new_end=mod.ohlc.minute_start(clock())
    result=manager.run(pair='BTC-AUD',start=START,end=new_end,repair=True)['results'][0]
    assert result['http_requests']==2 and result['source_availability']['no_completed_history_as_of']
    assert result['source_availability']['oldest_accessible'] is None
    count=len(public.calls)
    manager.run(pair='BTC-AUD',start=START,end=new_end,repair=True)
    assert len(public.calls)==count
