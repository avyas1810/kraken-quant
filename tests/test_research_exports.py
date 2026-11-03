"""Read-only exact-data research access and immutable export verification."""
from datetime import timedelta
from decimal import Decimal
import csv,json,hashlib,subprocess,sys
from pathlib import Path
import pytest
pytest.importorskip('pyarrow')
from test_ohlcv_collector import data_dir,NOW
from test_eligibility_screening import env
from kraken_quant.research_data import ResearchData,ResearchDataError,load_prices
from kraken_quant.research_export import ResearchExporter,report,audit,populate,main
from kraken_quant.cost_storage import digest
from kraken_quant import ohlcv_collector as candles


def files(root):return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def prepared(env):
    root,clock,manager,_=env
    manager.persist(manager.evaluate(as_of=clock(),all_aud=True))
    reader=ResearchData(root,as_of=clock(),clock=clock)
    return root,clock,reader


def test_prices_decimal_utc_and_module_api(prepared):
    root,clock,reader=prepared;start=clock()-timedelta(hours=1);end=clock()
    data=reader.load_prices('BTC-AUD',start,end)
    assert len(data)==3 and data['open'].iloc[0]==Decimal('100.000000000000000001')
    assert str(data['timestamp'].dtype)=='datetime64[ns, UTC]' and str(data['trade_count'].dtype)=='Int64'
    assert data.attrs['status']=='verified' and len(data.attrs['sources'])==1
    assert len(load_prices('BTC-AUD',start,end,data_dir=root,as_of=end))==3


def test_quote_book_full_snapshot_precision_no_timestamp_rounding(prepared):
    _,clock,reader=prepared;start=NOW-timedelta(microseconds=1);end=NOW+timedelta(microseconds=1)
    quote=reader.load_quotes('BTC-AUD',start,end);book=reader.load_orderbooks('BTC-AUD',start,end)
    assert len(quote)==1 and len(book)==4 and set(book['side'])=={'bid','ask'}
    assert quote['timestamp'].iloc[0].second==30
    assert quote['bid_size'].iloc[0]==Decimal('2.000000000000000001')
    assert set(quote['source_id'])==set(book['source_id'])
    assert book.groupby('side')['level'].apply(list).to_dict()=={'ask':[1,2],'bid':[1,2]}


def test_bounded_queries_do_not_fill(prepared):
    _,clock,reader=prepared
    data=reader.load_prices('BTC-AUD',candles.minute_start(NOW)-timedelta(minutes=2),candles.minute_start(NOW)-timedelta(minutes=1))
    assert len(data)==1
    empty=reader.load_quotes('BTC-AUD',NOW-timedelta(hours=2),NOW-timedelta(hours=1))
    assert len(empty)==0 and empty.attrs['status']=='unavailable'
    with pytest.raises(ValueError):reader.load_prices('BTC-AUD','2026-11-01',clock())
    with pytest.raises(ValueError):reader.load_prices('BTC-AUD',clock(),clock()-timedelta(days=1))
    with pytest.raises(ValueError):reader.load_quotes('BTC-AUD',NOW,clock()+timedelta(days=1))
    with pytest.raises(ValueError):reader.load_prices('../BTC-AUD',NOW-timedelta(hours=1),clock())


def test_historical_metadata_no_status_reconstruction(prepared):
    root,clock,reader=prepared
    known=reader.load_market_metadata('BTC-AUD')
    assert len(known)==1 and known['kraken_pair_id'].iloc[0]=='XBTAUD'
    old=reader.load_market_metadata('BTC-AUD',as_of=NOW-timedelta(days=2))
    assert len(old)==0 and old.attrs['status']=='unknown'
    later=ResearchData(root,as_of=clock()+timedelta(seconds=1)).load_market_metadata('BTC-AUD')
    assert later['trading_status_as_of'].iloc[0] is None


def test_full_fee_tiers_and_unknowns_not_zero(prepared):
    _,clock,reader=prepared;fees=reader.load_fee_schedules('BTC-AUD')
    assert len(fees)>2 and set(fees['fee_applicability'])=={'unknown_account_applicability'}
    assert all(v is None for v in fees['account_maker_pct'])
    assert any(v is None for v in fees['maker_pct']) and any(v is None for v in fees['taker_pct'])
    assert all(isinstance(v,Decimal) for v in fees['maker_pct'] if v is not None)
    assert len(reader.load_fee_schedules('BTC-AUD',end=NOW-timedelta(seconds=1)))==0
    assert len(set(fees['source_sha256']))==1


def test_read_only_and_no_network(prepared,monkeypatch):
    root,clock,reader=prepared
    monkeypatch.setattr('kraken_quant.market_data._public_get',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network')))
    before=files(root)
    for dataset in ('prices_1m','quotes_1m','orderbook'):reader._load(dataset,'BTC-AUD',NOW-timedelta(hours=1),clock())
    reader.load_fee_schedules('BTC-AUD');reader.load_market_metadata('BTC-AUD');quality=reader.load_quality_report('BTC-AUD',NOW-timedelta(hours=1),clock())
    assert quality.datasets['prices_1m']['coverage']['coverage_pct']<100
    assert before==files(root)


def test_missing_provenance_fails_not_silently_dropped(prepared):
    root,clock,reader=prepared
    for path in (root/'ohlcv_collection/responses/BTC-AUD').glob('*.json'):path.unlink()
    with pytest.raises(ResearchDataError,match='provenance'):reader.load_prices('BTC-AUD',NOW-timedelta(hours=1),clock())
    quality=reader.load_quality_report('BTC-AUD',NOW-timedelta(hours=1),clock())
    assert quality.datasets['prices_1m']['query_verification_error']
    assert quality.datasets['prices_1m']['coverage']['coverage_pct'] is None


@pytest.mark.parametrize('problem',['incomplete','negative','duplicate'])
def test_invalid_records_fail_closed(prepared,problem):
    root,clock,reader=prepared
    import pyarrow as pa
    import pyarrow.parquet as pq
    from kraken_quant.storage_schema import dataset_schema
    values=reader.store.read_all('BTC-AUD','prices_1m').to_pylist()
    if problem=='incomplete':values[0]['is_complete']=False
    elif problem=='negative':values[0]['open']=Decimal('-1')
    else:values.append(values[0])
    path=reader.store.partition_path('BTC-AUD','prices_1m',values[0]['timestamp'].strftime('%Y-%m'))
    pq.write_table(pa.Table.from_pylist(values,schema=dataset_schema('prices_1m')),path)
    with pytest.raises(ResearchDataError):reader.load_prices('BTC-AUD',NOW-timedelta(hours=1),clock())


def test_export_all_files_deterministic_empty_eligible_and_verify(prepared):
    root,clock,reader=prepared;exporter=ResearchExporter(root)
    first=exporter.export(all_aud=True,as_of=clock());snapshot=files(root)
    again=exporter.export(all_aud=True,as_of=clock())
    assert first==again and files(root)==snapshot
    assert first['files']['eligible.csv']['rows']==0 and first['files']['prices_1m.csv']['rows']==6
    assert first['files']['orderbook_levels.csv']['rows']==8 and len(first['files'])==11
    assert exporter.verify(first['export_id'])['status']=='PASS'
    path=Path(first['path']);manifest=json.loads((path/'manifest.json').read_text())
    assert manifest['costs_1m']['calculated_records']==0 and manifest['null_representation']==r'\N'
    with (path/'prices_1m.csv').open(newline='') as handle:
        rows=list(csv.DictReader(handle));assert rows[0]['open']=='100.000000000000000001'
        assert rows[0]['timestamp'].endswith('Z') and rows[0]['source_id']
    with (path/'fee_schedules.csv').open(newline='') as handle:
        assert any(r['maker_pct']==r'\N' for r in csv.DictReader(handle))


def test_pair_range_export_and_immutable_conflict(prepared):
    root,clock,reader=prepared;exporter=ResearchExporter(root)
    saved=exporter.export(pair='BTC-AUD',start=candles.minute_start(NOW)-timedelta(minutes=2),end=candles.minute_start(NOW),as_of=clock())
    assert saved['files']['prices_1m.csv']['rows']==2
    path=Path(saved['path'])/'eligible.csv';path.write_text('user content')
    with pytest.raises(ValueError):exporter.export(pair='BTC-AUD',start=candles.minute_start(NOW)-timedelta(minutes=2),end=candles.minute_start(NOW),as_of=clock())
    assert path.read_text()=='user content'
    with pytest.raises(ValueError):exporter.verify(saved['export_id'])


def test_verify_detects_missing_original_source(prepared):
    root,clock,_=prepared;exporter=ResearchExporter(root);saved=exporter.export(pair='BTC-AUD',as_of=clock())
    path=next((root/'book_collection/responses/BTC-AUD').glob('*.json'));path.unlink()
    with pytest.raises((ValueError,FileNotFoundError)):exporter.verify(saved['export_id'])


def test_original_export_survives_legitimate_append(prepared):
    root,clock,reader=prepared;exporter=ResearchExporter(root);saved=exporter.export(pair='BTC-AUD',as_of=clock())
    from test_ohlcv_collector import collector,Public
    from test_book_collector import setup
    clock.sleep(120);collector(root,Public(clock),clock).collect(pair='BTC-AUD');setup(root,clock)[0].collect(pair='BTC-AUD')
    assert exporter.verify(saved['export_id'])['status']=='PASS'


def test_report_candidates_and_no_fake_eligibility(prepared):
    root,clock,reader=prepared;result=report(root,all_aud=True,as_of=clock())
    assert len(result['pairs'])==3
    btc=next(r for r in result['pairs'] if r['pair_id']=='BTC-AUD')
    assert btc['observed_candles']==3 and btc['quote_observations']==1 and btc['book_snapshots']==1
    assert not btc['ninety_days_available'] and btc['account_applicability']=='unknown'
    assert all(r['cost_algorithms']=='NOT_IMPLEMENTED' for r in result['pairs'])


def test_unsafe_export_id_and_source_paths(prepared):
    root,clock,_=prepared;exporter=ResearchExporter(root)
    with pytest.raises(ValueError):exporter.verify('../secret')
    with pytest.raises(ValueError):exporter._source(dict(source_id='.env',source_sha256='x'))
    with pytest.raises(ValueError):exporter._source(dict(source_id='ohlcv_collection/responses/../../.env',source_sha256='x'))


def test_cli_offline_export_report_verify(prepared,monkeypatch,capsys):
    root,clock,_=prepared
    monkeypatch.setattr('kraken_quant.market_data._public_get',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network')))
    assert main(['export','--all-aud','--as-of',clock().isoformat(),'--data-dir',str(root)])==0
    saved=json.loads(capsys.readouterr().out)
    assert main(['verify','--export-id',saved['export_id'],'--data-dir',str(root)])==0;capsys.readouterr()
    assert main(['report','--all-aud','--as-of',clock().isoformat(),'--data-dir',str(root)])==0;capsys.readouterr()


def test_audit_and_quickstart_are_read_only(prepared):
    root,clock,_=prepared;before=files(root)
    checked=audit(root,all_aud=True)
    assert checked['status']=='PASS' and checked['cost_rows']==0
    assert checked['integrity']=='ok' and len(checked['sqlite'])==5
    result=subprocess.run([sys.executable,'examples/research_data_quickstart.py','--data-dir',str(root)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert 'Expected algorithm boundary:' in result.stdout
    assert before==files(root)


def test_costs_anywhere_block_export(prepared):
    root,clock,reader=prepared
    record=dict(pair_id='ADA-USD',timestamp=NOW,side='buy',order_size_aud=Decimal('100'),fee_type='taker',fee_pct=None,half_spread_pct=None,slippage_pct=None,total_cost_pct=None,model_version='isolated-forbidden-row-fixture')
    reader.store.write('costs_1m',[record])
    with pytest.raises(ValueError,match='cost_records'):ResearchExporter(root).export(pair='BTC-AUD',as_of=clock())
    assert audit(root,pair='BTC-AUD')['status']=='FAIL'


def test_future_receipt_does_not_become_historical_quote(prepared):
    root,clock,_=prepared
    receipt=next((root/'book_collection/responses/BTC-AUD').glob('*.json'))
    doc=json.loads(receipt.read_text());doc['finished_at']=candles.sql_time(NOW+timedelta(seconds=2));receipt.write_text(json.dumps(doc))
    historical=ResearchData(root,as_of=NOW+timedelta(seconds=1))
    with pytest.raises(ResearchDataError,match='provenance'):
        historical.load_quotes('BTC-AUD',NOW-timedelta(microseconds=1),NOW+timedelta(microseconds=1))


def test_fee_namespace_and_private_source_payload(prepared):
    root,clock,reader=prepared
    doc=reader.audit.get('fees',reader.audit.versions('fees')[0])
    clock.sleep(1);doc['retrieved_at']=candles.sql_time(clock())
    doc['account']=dict(fees={'BTC-AUD':{'maker':'0.12','taker':'0.26'}},account_fingerprint='fixture_private_account_identifier',payload={'volume':'private_volume'})
    version=reader.audit.put('fees',doc)
    reader=ResearchData(root,as_of=clock(),clock=clock)
    fees=reader.load_fee_schedules('BTC-AUD');assert any(fees['account_specific'])
    export=ResearchExporter(root).export(pair='BTC-AUD',as_of=clock())
    text=''.join(p.read_text() for p in Path(export['path']).iterdir())
    assert 'fixture_private_account_identifier' not in text and 'private_volume' not in text
    doc['source_namespace']='wrong_namespace';reader.audit.put('fees',doc)
    with pytest.raises(ResearchDataError,match='namespace'):reader.load_fee_schedules('BTC-AUD')


def test_month_boundary_uses_existing_partitions(prepared):
    root,clock,_=prepared
    from test_ohlcv_collector import Public,collector,candle,response
    public=Public(clock);boundary=candles.minute_start(NOW)-timedelta(minutes=3)
    public.body=response(rows=[candle(boundary-timedelta(minutes=1)),candle(boundary),candle(candles.minute_start(clock()))],now=clock())
    collector(root,public,clock).collect(pair='BTC-AUD')
    reader=ResearchData(root,as_of=clock(),clock=clock)
    values=reader.load_prices('BTC-AUD',boundary-timedelta(minutes=1),boundary+timedelta(minutes=1))
    assert len(values)==2 and values['timestamp'].dt.month.to_list()==[10,11]
    assert values.attrs['partitions']==['2026-10','2026-11']


def test_failed_export_never_replaces_existing_directory(prepared,monkeypatch):
    from kraken_quant.research_export import CsvFile
    root,clock,_=prepared;exporter=ResearchExporter(root);saved=exporter.export(all_aud=True,as_of=clock())
    before=files(root/'research_exports');original=CsvFile.append
    def fail(self,rows):
        if self.path.name=='prices_1m.csv':raise OSError('isolated disk failure')
        return original(self,rows)
    monkeypatch.setattr(CsvFile,'append',fail)
    with pytest.raises(OSError):exporter.export(all_aud=True,as_of=clock())
    assert before==files(root/'research_exports')
    assert not list((root/'research_exports').glob('.building-*'))
    assert exporter.verify(saved['export_id'])['status']=='PASS'


def test_export_verify_rejects_changed_original_market_record(prepared):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from kraken_quant.storage_schema import dataset_schema
    root,clock,reader=prepared;exporter=ResearchExporter(root);saved=exporter.export(pair='BTC-AUD',as_of=clock())
    rows=reader.store.read_all('BTC-AUD','prices_1m').to_pylist();rows[0]['close']=Decimal('100.5')
    path=reader.store.partition_path('BTC-AUD','prices_1m',rows[0]['timestamp'].strftime('%Y-%m'))
    pq.write_table(pa.Table.from_pylist(rows,schema=dataset_schema('prices_1m')),path)
    with pytest.raises(ValueError):exporter.verify(saved['export_id'])


def test_cached_reader_rechecks_sources_and_does_not_share_mutable_refs(prepared):
    root,clock,reader=prepared;start=NOW-timedelta(hours=1)
    data=reader.load_prices('BTC-AUD',start,clock())
    original=data.attrs['sources'][0]['source_sha256'];data.attrs['sources'][0]['source_sha256']='caller mutation'
    assert reader.load_prices('BTC-AUD',start,clock()).attrs['sources'][0]['source_sha256']==original
    receipt=next((root/'ohlcv_collection/responses/BTC-AUD').glob('*.json'));receipt.unlink()
    with pytest.raises(ResearchDataError):reader.load_prices('BTC-AUD',start,clock())


def test_metadata_source_changes_detected_by_existing_reader(prepared):
    root,clock,reader=prepared
    meta=reader.load_market_metadata('BTC-AUD');path=root/meta['source_id'].iloc[0]
    doc=json.loads(path.read_text());doc['unexpected_change']='changed';path.write_text(json.dumps(doc))
    with pytest.raises(ResearchDataError,match='metadata_source_dependency_changed'):reader.load_market_metadata('BTC-AUD')


def test_populate_uses_one_bounded_public_pipeline_and_refreshes_all(prepared,monkeypatch):
    root,clock,_=prepared
    import kraken_quant.data_pipeline as pipeline
    import kraken_quant.research_export as module
    used=[]
    class Fake:
        def __init__(self,root,config):self.config=config;used.append(config)
        def dry_run(self):return dict(tasks=[dict(enabled=True,due=False),dict(enabled=True,due=True)])
        def run(self):used.append('run');return dict(last_cycle=dict(status='finished'))
    monkeypatch.setattr(pipeline,'Pipeline',Fake)
    monkeypatch.setattr(module,'report',lambda *a,**k:dict(pairs=[]))
    result=populate(root,all_aud=True)
    assert result['status']=='finished' and used.count('run')==1
    assert used[-2].max_requests_per_cycle==161 and not used[-2].enabled['eligibility']


def test_cli_rejects_ignored_temporal_arguments(prepared,capsys):
    root,clock,_=prepared
    assert main(['populate','--all-aud','--start',clock().isoformat(),'--data-dir',str(root)])==1
    assert main(['report','--all-aud','--start',clock().isoformat(),'--data-dir',str(root)])==1


def test_submicrosecond_query_boundaries_are_rejected_not_rounded(prepared):
    _,clock,reader=prepared
    with pytest.raises(ValueError,match='microsecond_precision'):
        reader.load_quotes('BTC-AUD','2026-11-01T00:03:30.000000001Z',clock())


def test_quality_report_is_typed_frozen_utc_object(prepared):
    from dataclasses import FrozenInstanceError
    from kraken_quant.research_data import QualityReport
    _,clock,reader=prepared
    quality=reader.load_quality_report('BTC-AUD',NOW-timedelta(hours=1),clock())
    assert isinstance(quality,QualityReport) and quality.as_of.utcoffset()==timedelta(0)
    with pytest.raises(FrozenInstanceError):quality.pair_id='ETH-AUD'
