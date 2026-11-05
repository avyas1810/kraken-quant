"""Evidence-backed AUD history inventory and chronological price partitions.

Readiness is scoped: sparse traded-minute price studies differ from dense-minute
studies and historical execution studies. This does not approve M8 eligibility.
"""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import hashlib,json
from .historical_population import immutable,encoded,sha
from .parquet_store import MonthlyParquetStore
from .ohlcv_collector import collection_universe
from .metadata_store import connect_metadata
UTC=timezone.utc

def row_hash(rows):
 h=hashlib.sha256()
 for row in rows:h.update(encoded(row));h.update(b'\n')
 return h.hexdigest()

def intervals(times):
 if not times:return [],0
 gaps=[];longest=run=1
 for left,right in zip(times,times[1:]):
  missing=int((right-left).total_seconds()//60)-1
  if missing>0:gaps.append(dict(start=(left+timedelta(minutes=1)).isoformat(),end_exclusive=right.isoformat(),minutes=missing));run=1
  else:run+=1
  longest=max(longest,run)
 return gaps,longest

def build_report(data_dir,*,minimum_days=365,clock=lambda:datetime.now(UTC)):
 if type(minimum_days) is not int or minimum_days<365:raise ValueError('minimum_one_year_required')
 root=Path(data_dir);store=MonthlyParquetStore(root);markets=[];now=clock()
 for asset in collection_universe(root)[0]:
  pair=asset['pair_id'];prices=[];monthly=[];verified=set();source_errors=[];checked={}
  for month in store.months(pair,'prices_1m'):
   rows=store.read(pair,'prices_1m',month).to_pylist();prices.extend(rows)
   monthly.append(dict(month=month,rows=len(rows),sha256=row_hash(rows)))
  prices.sort(key=lambda r:r['timestamp']);physical={r['timestamp']:r for r in prices};times=[r['timestamp'] for r in prices];gaps,longest=intervals(times)
  expected=int((times[-1]-times[0]).total_seconds()//60)+1 if times else 0
  days=(times[-1]-times[0]).total_seconds()/86400 if times else 0
  receipts=[];invalid=conflicts=0;source_kinds=set();archive_end=None;invalid_times=set();trade_windows=[]
  for path in (root/'historical_imports/receipts').glob('*.json'):
   doc=json.loads(path.read_text())
   if doc['pair_id']!=pair:continue
   try:
    from .historical_population import source_records
    identity={k:v for k,v in doc.items() if k not in ('received_at','import_id','inserted_rows','identical_rows','conflicting_rows','conflict_path')}
    if sha(encoded(identity))!=path.stem or doc['import_id']!=path.stem:raise ValueError('receipt_identity_changed')
    for row in source_records(root,doc,checked):
     if physical.get(row['timestamp'])==row:verified.add(row['timestamp'])
   except Exception:source_errors.append(path.relative_to(root).as_posix())
   invalid+=doc.get('invalid_rows',0);conflicts+=doc['conflicting_rows'];
   invalid_path=root/'historical_imports/invalid'/(doc['import_id']+'.json')
   if invalid_path.exists():invalid_times.update(datetime.fromtimestamp(v['timestamp'],UTC) for v in json.loads(invalid_path.read_text()))
   if doc['source_kind']=='kraken_public_trades_ohlcv' and doc.get('dependencies'):
    first=json.loads((root/doc['dependencies'][0]['path']).read_text())
    trade_windows.append(dict(start=datetime.fromtimestamp(int(first['request_since'])//1000000000,UTC),end_exclusive=doc['coverage_end_exclusive']))
   source_kinds.add(doc['source_kind']);receipts.append(dict(path=path.relative_to(root).as_posix(),sha256=sha(path.read_bytes())))
   if doc['source_kind']=='kraken_official_ohlcvt':archive_end=doc['coverage_end_exclusive']
  snapshots={}
  for ds in ('quotes_1m','orderbook'):
   count=0;timestamps=set();first=last=None
   for month in store.months(pair,ds):
    table=store.read(pair,ds,month);count+=len(table)
    for t in table['timestamp'].to_pylist():timestamps.add(t);first=min(first,t) if first else t;last=max(last,t) if last else t
   snapshots[ds]=dict(rows=count,observations=len(timestamps),first=first,last=last,continuous_history=False)
  fee_paths=[]
  for path in (root/'cost_infrastructure/fees').glob('*.json'):
   doc=json.loads(path.read_text())
   if pair in doc.get('pair_fees',{}):fee_paths.append(dict(path=path.relative_to(root).as_posix(),retrieved_at=doc['retrieved_at'],effective_at=doc.get('effective_at'),account_applicability='unknown'))
  # Conservative: retained source precision failures/conflicts block an unqualified
  # ready claim. Calendar-minute density is reported independently of sparse files.
  unresolved_invalid=len(invalid_times-set(times))
  source_windows=[(w['start'],datetime.fromisoformat(w['end_exclusive'].replace('Z','+00:00'))) for w in trade_windows]
  from .ohlcv_collector import parse_ohlc,read_time
  for response in (root/'ohlcv_collection/responses'/pair).glob('*.json'):
   try:
    doc=json.loads(response.read_text());parsed=parse_ohlc(doc['body'],asset,read_time(doc['fetched_at']))
    if parsed['records'] and not parsed['invalid_observations']:
     matched=False
     for row in parsed['records']:
      if physical.get(row['timestamp'])==row:verified.add(row['timestamp']);matched=True
     if matched:receipts.append(dict(path=response.relative_to(root).as_posix(),sha256=sha(response.read_bytes())))
     stamps=[r['timestamp'] for r in parsed['records']];source_windows.append((min(stamps),max(stamps)+timedelta(minutes=1)))
   except (ValueError,KeyError,TypeError):pass
  cursor=datetime.fromisoformat(archive_end.replace('Z','+00:00')) if archive_end else None
  for left,right in sorted(source_windows):
   if cursor is not None and left<=cursor:cursor=max(cursor,right)
  gap_closed=bool(cursor and times and cursor>=times[-1]+timedelta(minutes=1))
  gap_classes={}
  from bisect import bisect_left
  missing_invalid=sorted(invalid_times-set(times))
  for gap in gaps:
   left=datetime.fromisoformat(gap['start']);right=datetime.fromisoformat(gap['end_exclusive'])
   pos=bisect_left(missing_invalid,left)
   if pos<len(missing_invalid) and missing_invalid[pos]<right:kind='contains_unresolved_invalid_source'
   elif archive_end and right<=datetime.fromisoformat(archive_end.replace('Z','+00:00')):kind='source_declared_no_trade_official_archive'
   elif any(w['start']<=left and datetime.fromisoformat(w['end_exclusive'].replace('Z','+00:00'))>=right for w in trade_windows):kind='no_trades_in_verified_public_trade_replay'
   else:kind='missing_or_mixed_source_windows_unresolved'
   gap['classification']=kind;gap_classes[kind]=gap_classes.get(kind,0)+gap['minutes']
  sufficient=days>=minimum_days and bool(receipts) and not unresolved_invalid and not conflicts and gap_closed and not source_errors and len(verified)==len(prices)
  price_status='PRICE_RESEARCH_READY' if sufficient else ('LIMITED' if prices else 'NOT_READY')
  n=len(prices);a=n*3//5;b=a+n//5;partitions={}
  if n>=5:
   for name,section in (('training',prices[:a]),('validation',prices[a:b]),('test',prices[b:])):
    partitions[name]=dict(start=section[0]['timestamp'],end_exclusive=section[-1]['timestamp']+timedelta(minutes=1),rows=len(section),sha256=row_hash(section),untouched_out_of_sample=name=='test')
  markets.append(dict(pair_id=pair,kraken_pair_id=asset['kraken_pair_id'],first=times[0] if times else None,last=times[-1] if times else None,candles=n,observed_span_days=days,expected_calendar_minutes=expected,calendar_density_pct=100*n/expected if expected else None,coverage_window_start=times[0] if times else None,coverage_window_end_exclusive=times[-1]+timedelta(minutes=1) if times else None,history_start_basis='first genuine archived trade; formal listing timestamp not reconstructed',source_coverage_through=cursor,missing_interval_classifications=gap_classes,missing_calendar_minutes=expected-n,longest_continuous_minutes=longest,missing_intervals=gaps,source_integrity_status='verified' if not source_errors and len(verified)==len(prices) else 'incomplete_or_unverified',provenance_verified_candles=len(verified),unverified_candles=n-len(verified),source_verification_errors=source_errors,invalid_source_rows=invalid,unresolved_invalid_minutes=unresolved_invalid,verified_trade_windows=trade_windows,archive_to_recent_gap_closed=gap_closed,conflicting_source_rows=conflicts,duplicates_in_storage=n-len(set(times)),source_kinds=sorted(source_kinds),sources=receipts,archive_end_exclusive=archive_end,monthly=monthly,prices_status=price_status,execution_status='LIMITED' if snapshots['orderbook']['observations'] else 'NOT_READY',quotes=snapshots['quotes_1m'],orderbooks=snapshots['orderbook'],fee_sources=fee_paths,historical_fee_applicability='unknown',partitions=partitions,limitations=['Sparse traded-minute candles; calendar holes are not filled.','Archive-declared absent traded intervals differ from unexplained/unavailable history.','Retrospective archive vintage; historical status/fee applicability not reconstructed.','Sampled historical quotes/books are not continuous execution history.']+(['Source decimals exceed approved precision; rows quarantined, never rounded.'] if invalid else [])+(['Existing-source conflicts retained; no silent overwrites.'] if conflicts else [])))
 with connect_metadata(root,readonly=True) as db:
  integrity=db.execute('PRAGMA integrity_check').fetchall();foreign=db.execute('PRAGMA foreign_key_check').fetchall()
 body=dict(contract_version=1,created_at=now,minimum_price_span_days=minimum_days,partition_policy='chronological actual-observation counts: floor(60%), floor(20%), remainder test; no shuffle, no overlap',knowledge_time=now,markets=markets,sqlite_integrity=[r[0] for r in integrity],foreign_key_issues=foreign,scope='Price-only retrospective traded-minute studies; dense-minute and point-in-time execution studies require additional evidence. No strategy or performance evaluated.')
 compact=dict(contract_version=1,knowledge_time=body['knowledge_time'],partition_policy=body['partition_policy'],markets=[{k:market[k] for k in ('pair_id','prices_status','partitions','sources')} for market in markets])
 compact_bytes=encoded(compact);partition_id=sha(compact_bytes)
 immutable(root/'historical_imports/partitions'/(partition_id+'.json'),compact_bytes)
 body['partition_manifest_id']=partition_id
 content=encoded(body);ident=sha(content);immutable(root/'historical_imports/readiness'/(ident+'.json'),content)
 return ident,body

def partition_contract(data_dir,manifest_id):
 """Read a small split contract; stream-check a linked large coverage report."""
 import re
 root=Path(data_dir)/'historical_imports'
 small=root/'partitions'/(manifest_id+'.json')
 if not small.exists():
  report=root/'readiness'/(manifest_id+'.json')
  with report.open('rb') as source:
   if hashlib.file_digest(source,'sha256').hexdigest()!=manifest_id:raise ValueError('partition_manifest_changed')
   source.seek(max(0,report.stat().st_size-1048576));tail=source.read()
  references=re.findall(rb'"partition_manifest_id":"([a-f0-9]{64})"',tail)
  if len(references)!=1:raise ValueError('compact_partition_reference_missing')
  manifest_id=references[0].decode();small=root/'partitions'/(manifest_id+'.json')
 raw=small.read_bytes()
 if sha(raw)!=manifest_id:raise ValueError('partition_manifest_changed')
 doc=json.loads(raw)
 if doc.get('contract_version')!=1:raise ValueError('unsupported_partition_contract')
 return doc


def load_partition(reader,pair,split,manifest_id):
 if split not in ('training','validation','test') or len(manifest_id)!=64 or any(c not in '0123456789abcdef' for c in manifest_id):raise ValueError('invalid_partition_selection')
 doc=partition_contract(reader.data_dir,manifest_id)
 if reader.as_of<datetime.fromisoformat(doc['knowledge_time'].replace('Z','+00:00')):raise ValueError('partition_manifest_not_known_as_of')
 market=next((m for m in doc['markets'] if m['pair_id']==pair),None)
 if market is None:raise ValueError('pair_missing_from_manifest')
 part=market['partitions'][split]
 for source in market['sources']:
  if sha((reader.data_dir/source['path']).read_bytes())!=source['sha256']:raise ValueError('partition_source_changed')
 from .research_data import ResearchData
 pinned=ResearchData(reader.data_dir,as_of=doc['knowledge_time'],clock=reader.clock)
 data=pinned.load_prices(pair,part['start'],part['end_exclusive'])
 fields=reader.store.read(pair,'prices_1m',part['start'][:7]).schema.names
 if len(data)!=part['rows'] or row_hash(data[fields].to_dict('records'))!=part['sha256']:raise ValueError('partition_records_changed')
 data.attrs.update(partition=split,partition_manifest=manifest_id,untouched_out_of_sample=split=='test',readiness=market['prices_status'])
 return data

def publish_quality(data_dir):
 """Persist immutable-source problems without overwriting earlier quality history."""
 import calendar
 root=Path(data_dir);count=0
 for path in (root/'historical_imports/receipts').glob('*.json'):
  doc=json.loads(path.read_text());year,month=map(int,doc['month'].split('-'))
  start=datetime(year,month,1,tzinfo=UTC);end=datetime(year+1,1,1,tzinfo=UTC) if month==12 else datetime(year,month+1,1,tzinfo=UTC)
  for key,kind in (('invalid_rows','historical_source_precision'),('conflicting_rows','historical_source_conflict')):
   if not doc.get(key):continue
   ident=sha(encoded(dict(import_id=doc['import_id'],kind=kind)))
   description=json.dumps(dict(receipt=path.relative_to(root).as_posix(),source_rows_affected=doc[key],handling='original evidence retained; no rounding or overwrite; consult readiness for exact-trade recovery'),sort_keys=True)
   with connect_metadata(root) as db:db.execute('INSERT INTO data_quality_issues VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(issue_id) DO NOTHING',(ident,doc['pair_id'],'prices_1m',None,start.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),end.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),kind,description,doc['received_at'],'open'))
   count+=1
 return count
