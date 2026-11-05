"""Synthetic isolated import fixtures; never production market observations."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
import gzip,json
import pytest
pytest.importorskip('pyarrow')
from test_ohlcv_collector import data_dir,NOW
from kraken_quant import historical_population as h
from kraken_quant.parquet_store import MonthlyParquetStore
from kraken_quant.metadata_store import connect_metadata
from kraken_quant.historical_trades import aggregate,fetch_gap
from kraken_quant.historical_execution import sample_records,import_sample
from kraken_quant.research_data import ResearchData,ResearchDataError
from kraken_quant.historical_readiness import build_report
URL='https://assets.kraken.com/marketing/institutions/Kraken_OHLCVT_Full.zip'
END=datetime(2026,10,1,tzinfo=timezone.utc)
def line(stamp=1788220800,volume='1.000000000000000001',price='100'):
 return f'{stamp},{price},{price},{price},{price},{volume},2\n'.encode()
def run(root,raw,**kwargs):
 path=root/'XBTAUD_1.csv';path.write_bytes(raw)
 return h.import_csv(root,'BTC-AUD',path,source_url=URL,archive_sha256='a'*64,coverage_end=END,received_at=NOW,**kwargs)
def test_real_types_exact_sparse_no_fill(data_dir):
 docs=run(data_dir,line()+line(1788220920));rows=MonthlyParquetStore(data_dir).read_all('BTC-AUD','prices_1m').to_pylist()
 assert len(rows)==2 and rows[0]['volume_base']==Decimal('1.000000000000000001')
 assert rows[0]['timestamp'].tzinfo is not None and docs[0]['source_rows']==2
 assert len(h.source_records(data_dir,docs[0]))==2
 assert len(ResearchData(data_dir,as_of=NOW).load_prices('BTC-AUD','2026-09-01T00:00:00Z','2026-09-01T00:03:00Z'))==2
@pytest.mark.parametrize('raw',[b'bad',line(1788220801),line(price='-1'),line(volume='NaN')])
def test_bad_source_rejected(data_dir,raw):
 with pytest.raises((ValueError,ArithmeticError)):run(data_dir,raw)
def test_decimal_excess_quarantined_never_rounded(data_dir):
 docs=run(data_dir,line(volume='0.0015475200000000001')+line(1788220860))
 assert docs[0]['invalid_rows']==1 and docs[0]['source_rows']==2
 assert len(MonthlyParquetStore(data_dir).read_all('BTC-AUD','prices_1m'))==1
 assert list((data_dir/'historical_imports/invalid').glob('*.json'))
def test_idempotent_retry_and_checkpoint(data_dir):
 first=run(data_dir,line());before=(data_dir/'market_data/BTC-AUD/prices_1m/2026-09.parquet').read_bytes()
 second=run(data_dir,line());assert first==second
 assert before==(data_dir/'market_data/BTC-AUD/prices_1m/2026-09.parquet').read_bytes()
 with connect_metadata(data_dir,readonly=True) as db:assert db.execute("SELECT latest_observation_at FROM ingestion_state WHERE pair_id='BTC-AUD'").fetchone()[0].startswith('2026-09-01')
def test_conflict_existing_wins_with_evidence(data_dir):
 run(data_dir,line());docs=run(data_dir,line(price='99'))
 assert docs[0]['conflicting_rows']==1
 assert MonthlyParquetStore(data_dir).read_all('BTC-AUD','prices_1m')['close'][0].as_py()==100
 assert h.dependency(data_dir,docs[0]['conflict_path']).exists()
def test_interruption_before_receipt_replays_safely(data_dir,monkeypatch):
 original=h.immutable
 def fail(path,content):
  if 'receipts' in Path(path).parts:raise OSError('synthetic interruption')
  return original(path,content)
 monkeypatch.setattr(h,'immutable',fail)
 with pytest.raises(OSError):run(data_dir,line())
 with connect_metadata(data_dir,readonly=True) as db:assert db.execute('SELECT COUNT(*) FROM ingestion_state').fetchone()[0]==0
 monkeypatch.setattr(h,'immutable',original);run(data_dir,line())
 assert len(MonthlyParquetStore(data_dir).read_all('BTC-AUD','prices_1m'))==1
@pytest.mark.parametrize('name',['../outside','/outside','historical_imports/../../outside'])
def test_dependency_paths_fail(data_dir,name):
 with pytest.raises(ValueError):h.dependency(data_dir,name)
def test_source_corruption_fails_closed(data_dir):
 docs=run(data_dir,line());h.dependency(data_dir,docs[0]['raw_path']).write_bytes(b'changed')
 with pytest.raises(ValueError,match='checksum'):h.source_records(data_dir,docs[0])
 with pytest.raises(ResearchDataError,match='historical'):ResearchData(data_dir,as_of=NOW).load_prices('BTC-AUD','2026-09-01T00:00:00Z','2026-09-02T00:00:00Z')
def test_future_receipt_cannot_support_old_knowledge(data_dir):
 run(data_dir,line())
 with pytest.raises(ResearchDataError,match='provenance'):ResearchData(data_dir,as_of=NOW-timedelta(seconds=1)).load_prices('BTC-AUD','2026-09-01T00:00:00Z','2026-09-02T00:00:00Z')
def test_trade_id_exact_aggregation_and_unfinished_exclusion():
 page=dict(body=dict(result={'BTC/AUD':[['100','0.1','1788220801','b','l','',1],['101','0.2','1788220859','s','l','',2],['99','1','1788220860','b','l','',3]],'last':'1788220860000000000'}))
 rows=h.parse_csv(aggregate([page,page],'BTC/AUD',1788220800,1788220860),'BTC-AUD')
 assert len(rows)==1 and rows[0]['trade_count']==2 and rows[0]['volume_base']==Decimal('0.3') and rows[0]['close']==101
 page['body']['result']['BTC/AUD'].append(['90','0.1','1788220801','b','l','',1])
 with pytest.raises(ValueError,match='conflicting_trade'):aggregate([page],'BTC/AUD',1788220800,1788220860)
def test_month_boundaries_and_partition_load(data_dir):
 start=datetime(2026,8,31,23,55,tzinfo=timezone.utc)
 run(data_dir,b''.join(line(int((start+timedelta(minutes=i)).timestamp())) for i in range(10)))
 assert MonthlyParquetStore(data_dir).months('BTC-AUD','prices_1m')==('2026-08','2026-09')
 ident,report=build_report(data_dir,clock=lambda:NOW)
 reader=ResearchData(data_dir,as_of=NOW)
 parts=[reader.load_partition('BTC-AUD',s,ident) for s in ('training','validation','test')]
 assert list(map(len,parts))==[6,2,2] and parts[-1].attrs['untouched_out_of_sample']
 assert parts[0]['timestamp'].max()<parts[1]['timestamp'].min()<parts[-1]['timestamp'].min()
 market=next(m for m in report['markets'] if m['pair_id']=='BTC-AUD');assert market['prices_status']=='LIMITED'
def test_zero_volume_and_existing_checkpoint_preserved(data_dir):
 run(data_dir,line(volume='0'))
 with connect_metadata(data_dir) as db:db.execute("UPDATE ingestion_state SET latest_observation_at='2026-10-01T00:00:00.000000Z'")
 run(data_dir,line())
 with connect_metadata(data_dir,readonly=True) as db:assert db.execute('SELECT latest_observation_at FROM ingestion_state').fetchone()[0].startswith('2026-10-01')
def test_not_approved_source_or_identity(data_dir):
 with pytest.raises(ValueError):h.import_csv(data_dir,'BTC-AUD',data_dir/'ETHAUD_1.csv',source_url='https://example.org',archive_sha256='a'*64,coverage_end=END)


def synthetic_book_csv(pair='BTC/AUD',missing=False):
 import csv,io
 fields=['exchange','symbol','timestamp','local_timestamp']+[f'{side}[{n}].{field}' for n in range(5) for side in ('asks','bids') for field in ('price','amount')]
 out=io.StringIO();writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader()
 for micros in (1790812801000000,1790812802000000,1790812861000000):
  row=dict(exchange='kraken',symbol=pair,timestamp=micros-100,local_timestamp=micros)
  for n in range(5):
   for side in ('asks','bids'):
    row[f'{side}[{n}].price']='' if missing and n>1 else str(101+n if side=='asks' else 100-n)
    row[f'{side}[{n}].amount']='' if missing and n>1 else '1.000000000000000001'
  writer.writerow(row)
 return gzip.compress(out.getvalue().encode())
def test_real_snapshot_sampling_and_source_link(data_dir):
 raw=synthetic_book_csv();samples=sample_records(raw,'BTC-AUD')
 assert len(samples)==2 and len(samples[0][0])==10
 assert samples[0][1]['timestamp'].second==1 and samples[0][1]['bid_size']==Decimal('1.000000000000000001')
 path=data_dir/'sample.gz';path.write_bytes(raw);url='https://datasets.tardis.dev/v1/kraken/book_snapshot_5/2026/10/01/BTC-AUD.csv.gz'
 first=import_sample(data_dir,'BTC-AUD',path,source_url=url,received_at=NOW)
 second=import_sample(data_dir,'BTC-AUD',path,source_url=url,received_at=NOW)
 assert first==second
 reader=ResearchData(data_dir,as_of=NOW);quotes=reader.load_quotes('BTC-AUD','2026-10-01T00:00:00Z','2026-10-02T00:00:00Z');book=reader.load_orderbooks('BTC-AUD','2026-10-01T00:00:00Z','2026-10-02T00:00:00Z')
 assert len(quotes)==2 and len(book)==20 and set(quotes['source_id'])==set(book['source_id'])
def test_short_available_depth_not_fabricated():
 samples=sample_records(synthetic_book_csv(missing=True),'BTC-AUD')
 assert len(samples[0][0])==4 and {r['level'] for r in samples[0][0]}=={1,2}
def test_historical_execution_identity_rejected():
 with pytest.raises(ValueError,match='identity'):sample_records(synthetic_book_csv('ETH/AUD'),'BTC-AUD')
def test_trade_replay_bounded_public_and_idempotent(data_dir):
 start=datetime(2026,10,1,tzinfo=timezone.utc);end=start+timedelta(minutes=1);calls=[];sleeps=[]
 def transport(endpoint,params):
  calls.append((endpoint,params));return {'error':[],'result':{'BTC/AUD':[['100','0.1',str(int(start.timestamp())+1),'b','l','',1]],'last':str((int(start.timestamp())+1)*1000000000)}}
 result=fetch_gap(data_dir,'BTC-AUD',start=start,end=end,transport=transport,sleeper=sleeps.append)
 assert result['status']=='source_window_replayed' and calls[0][0]=='Trades' and calls[0][1]['pair']=='XBTAUD' and sleeps==[1.2]
 fetch_gap(data_dir,'BTC-AUD',start=start,end=end,transport=lambda *a:pytest.fail('network retry'),sleeper=sleeps.append)
 assert len(MonthlyParquetStore(data_dir).read_all('BTC-AUD','prices_1m'))==1

def test_public_split_zip_ranges_and_crc(tmp_path,monkeypatch):
 import io,zipfile
 from kraken_quant import official_trade_archive as module
 memory=io.BytesIO()
 with zipfile.ZipFile(memory,'w',compression=zipfile.ZIP_DEFLATED) as archive:archive.writestr('XBTAUD.csv','timestamp,price,volume\n1,100,1\n')
 content=memory.getvalue();size=(len(content)+13)//14;parts=[content[i*size:(i+1)*size] for i in range(14)];calls=[]
 # Ensure every virtual part has at least one byte for the discovery probes.
 assert all(parts)
 class Response(io.BytesIO):
  status=206
  def __init__(self,body,header):super().__init__(body);self.headers={'Content-Range':header}
 def fetch(request,timeout):
  assert not request.has_header('Authorization');n=int(request.full_url[-2:]);source=parts[n];bounds=request.headers['Range'].split('=')[1];left,right=map(int,bounds.split('-'));calls.append((n,left,right))
  return Response(source[left:right+1],f'bytes {left}-{right}/{len(source)}')
 monkeypatch.setattr(module.urllib.request,'urlopen',fetch)
 reader=module.PublicSplitArchive(tmp_path/'ranges')
 with zipfile.ZipFile(reader) as archive:assert archive.read('XBTAUD.csv').endswith(b'1,100,1\n')
 assert len(calls)>14
 cached=len(calls);reader.seek(0);reader.read(10);again=len(calls);reader.seek(0);reader.read(10);assert len(calls)==again and again>=cached

def test_archive_whole_checksum_and_required_members(data_dir):
 import zipfile
 manifest=dict(product='ohlcvt',schema_version=1,has_header=False,columns=['timestamp','open','high','low','close','volume','trades'],coverage=dict(end='2026-09-30'))
 archive=data_dir/'fixture.zip'
 with zipfile.ZipFile(archive,'w') as source:
  source.writestr('MANIFEST.json',json.dumps(manifest));source.writestr('XBTAUD_1.csv',line());source.writestr('ETHAUD_1.csv',line())
 with pytest.raises(ValueError,match='integrity'):h.import_archive(data_dir,archive,expected_sha256='0'*64,coverage_end=END)
 h.import_archive(data_dir,archive,expected_sha256=h.sha(archive.read_bytes()),coverage_end=END)
 assert len(MonthlyParquetStore(data_dir).read_all('ETH-AUD','prices_1m'))==1

def test_retry_missing_partition_fails_not_advances_checkpoint(data_dir):
 run(data_dir,line());(data_dir/'market_data/BTC-AUD/prices_1m/2026-09.parquet').unlink()
 with pytest.raises(ValueError,match='missing_or_changed'):run(data_dir,line())

def test_quality_evidence_idempotent_and_historical(data_dir):
 from kraken_quant.historical_readiness import publish_quality
 run(data_dir,line(volume='0.0015475200000000001')+line(1788220860))
 assert publish_quality(data_dir)==1 and publish_quality(data_dir)==1
 with connect_metadata(data_dir,readonly=True) as db:
  rows=db.execute("SELECT issue_type,resolution_status FROM data_quality_issues").fetchall()
 assert rows==[('historical_source_precision','open')]

def test_exact_trade_archive_precision_recovery(data_dir):
 from kraken_quant.official_trade_archive import recover_precision
 asset={'pair_id':'BTC-AUD','base_asset':'BTC'};area=data_dir/'historical_imports/official_trades';area.mkdir(parents=True)
 member=area/'XBTAUD.csv';raw=b'timestamp,price,volume,type,order_type,misc,trade_id\n1788220801.1234567,100.00000,0.00070000,b,l,,1\n1788220810.1234567,101.00000,0.00084752,s,l,,2\n'
 member.write_bytes(raw);(area/'XBTAUD.csv.receipt.json').write_text(json.dumps(dict(sha256=h.sha(raw),source_url='https://assets.kraken.com/marketing/institutions/Kraken_Trades_Full.zip')))
 source=data_dir/'XBTAUD_1.csv';source.write_bytes(line(volume='0.0015475200000000001'))
 r=recover_precision(data_dir,asset,source)
 row=MonthlyParquetStore(data_dir).read_all('BTC-AUD','prices_1m').to_pylist()[0]
 assert r['recovered']==r['required']==1 and row['volume_base']==Decimal('0.00154752') and row['close']==101 and row['trade_count']==2
 assert len(ResearchData(data_dir).load_prices('BTC-AUD','2026-09-01T00:00:00Z','2026-09-02T00:00:00Z'))==1

def test_historical_no_cost_or_execution_algorithms_added():
 from kraken_quant.cost_algorithms import CostAlgorithms
 obj=CostAlgorithms()
 for name in ('calculate_spread_cost','calculate_book_execution','calculate_slippage','calculate_market_impact','calculate_total_cost'):
  with pytest.raises(NotImplementedError):getattr(obj,name)(None)

def test_old_cached_short_page_does_not_claim_newer_coverage(data_dir):
 start=datetime(2026,10,1,tzinfo=timezone.utc);end=start+timedelta(minutes=1);since=str(int(start.timestamp())*1000000000)
 folder=data_dir/'historical_imports/trades/BTC-AUD';folder.mkdir(parents=True)
 body={'error':[],'result':{'BTC/AUD':[['100','0.1',str(int(start.timestamp())+1),'b','l','',1]],'last':str((int(start.timestamp())+1)*1000000000)}}
 (folder/(since+'.json')).write_bytes(h.encoded(dict(request_since=since,kraken_pair_id='XBTAUD',received_at=start,body=body)))
 calls=[]
 def transport(endpoint,params):calls.append(params);return body
 result=fetch_gap(data_dir,'BTC-AUD',start=start,end=end,transport=transport,sleeper=lambda _:None,prepare_only=True)
 assert len(calls)==1 and result['status']=='source_window_downloaded'
 assert any('-asof-' in d['path'] for d in result['dependencies'])

def test_public_trade_errors_have_bounded_retries(data_dir):
 calls=[];sleeps=[]
 def transport(endpoint,params):calls.append(endpoint);raise TimeoutError('synthetic')
 start=datetime(2026,10,1,tzinfo=timezone.utc)
 with pytest.raises(TimeoutError):fetch_gap(data_dir,'BTC-AUD',start=start,end=start+timedelta(minutes=1),transport=transport,sleeper=sleeps.append)
 assert calls==['Trades']*3 and sleeps==[1.2,2,4]

def test_price_validation_preserves_datetime_range_limit():
 import pyarrow as pa
 from kraken_quant.storage_schema import table_from_records,validate_table
 table=table_from_records('prices_1m',h.parse_csv(line(),'BTC-AUD'))
 future=pa.array([253402300800000000],type=pa.int64()).cast(pa.timestamp('us',tz='UTC'))
 changed=table.set_column(table.schema.get_field_index('timestamp'),table.schema.field('timestamp'),future)
 with pytest.raises(ValueError,match='datetime range'):validate_table('prices_1m',changed)

def test_partial_minute_trade_window_rejected(data_dir):
 start=datetime(2026,10,1,tzinfo=timezone.utc)
 with pytest.raises(ValueError,match='minute_aligned'):fetch_gap(data_dir,'BTC-AUD',start=start,end=start+timedelta(seconds=90),transport=lambda *args:pytest.fail('network'),sleeper=lambda _:None)
