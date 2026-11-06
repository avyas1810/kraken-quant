"""Public provider sample ingestion: original top-five Kraken snapshots only.

One first complete snapshot per observed UTC minute is retained with its actual
provider local_timestamp. Full original gzip is pinned; no historical filling.
"""
import csv,gzip,io,json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
from .historical_population import immutable,encoded,sha,dependency
from .storage_schema import table_from_records
from .parquet_store import MonthlyParquetStore
from .book_collector import validate_snapshot
from .metadata_store import connect_metadata
UTC=timezone.utc

def sample_records(raw,pair):
 rows=[];seen=set();symbol=pair.replace('-','/')
 with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
  with io.TextIOWrapper(compressed,encoding='utf-8') as source:
   header=next(csv.reader([source.readline()]))
   if header[:4]!=['exchange','symbol','timestamp','local_timestamp']:raise ValueError('historical_book_header_mismatch')
   for line in source:
    # Unselected events need identity/time checks, not 20 level-field objects.
    # Quoted metadata uses the normal CSV parser; accepted snapshots always do.
    prefix=line.split(',',4);parsed=None
    if len(prefix)<5:raise ValueError('historical_book_row_malformed')
    if any('"' in value for value in prefix[:4]):parsed=next(csv.reader([line]));prefix=parsed
    if prefix[0]!='kraken' or prefix[1]!=symbol:raise ValueError('historical_book_identity_mismatch')
    micros=int(prefix[3]);event=int(prefix[2])
    if micros<event or event<0:raise ValueError('historical_book_clock_error')
    minute=micros//60000000
    if minute in seen:continue
    parsed=parsed if parsed is not None else next(csv.reader([line]))
    if len(parsed)!=len(header):raise ValueError('historical_book_row_malformed')
    v=dict(zip(header,parsed))
    stamp=datetime(1970,1,1,tzinfo=UTC)+timedelta(microseconds=micros)
    snapshot=[]
    for side,prefix in (('ask','asks'),('bid','bids')):
     for level in range(5):
      price=v[f'{prefix}[{level}].price'];amount=v[f'{prefix}[{level}].amount']
      if price=='' and amount=='':break
      if not price or not amount:raise ValueError('partial_historical_level')
      snapshot.append(dict(pair_id=pair,timestamp=stamp,side=side,level=level+1,price=Decimal(price),quantity=Decimal(amount)))
    table_from_records('orderbook',snapshot);validate_snapshot(snapshot)
    bid=next(r for r in snapshot if r['side']=='bid');ask=next(r for r in snapshot if r['side']=='ask')
    rows.append((snapshot,dict(pair_id=pair,timestamp=stamp,bid=bid['price'],ask=ask['price'],bid_size=bid['quantity'],ask_size=ask['quantity'])))
    seen.add(minute)
 return rows

def import_sample(data_dir,pair,path,*,source_url,received_at=None):
 if source_url!=f'https://datasets.tardis.dev/v1/kraken/book_snapshot_5/2026/10/01/{pair}.csv.gz':raise ValueError('public_sample_source_required')
 root=Path(data_dir);raw=Path(path).read_bytes();content_id=sha(raw);name='historical_imports/execution_raw/'+content_id+'.csv.gz';immutable(root/name,raw)
 with connect_metadata(root,readonly=True) as db:
  if db.execute("SELECT 1 FROM assets WHERE pair_id=? AND quote_asset='AUD'",(pair,)).fetchone() is None:raise ValueError('unknown_aud_market')
 samples=sample_records(raw,pair);store=MonthlyParquetStore(root);received_at=received_at or datetime.now(UTC)
 body=dict(contract_version=1,pair_id=pair,source_kind='tardis_kraken_public_sample',source_url=source_url,raw_path=name,raw_sha256=content_id,policy='first complete top-five snapshot per provider-observed UTC minute; actual local_timestamp retained',timestamp_semantics='Tardis collector local observation microseconds UTC; exchange timestamp retained in original CSV',requested_depth=5,snapshots=len(samples),source_date='2026-10-01')
 ident=sha(encoded(body));receipt=root/'historical_imports/execution_receipts'/(ident+'.json')
 for dataset,records in (('quotes_1m',[q for _,q in samples]),('orderbook',[r for b,_ in samples for r in b])):
  if records:store.write(dataset,table_from_records(dataset,records))
  for month in {r['timestamp'].strftime('%Y-%m') for r in records}:
   actual=store.read(pair,dataset,month).to_pylist();keys={(r['timestamp'],r.get('side'),r.get('level')):r for r in actual}
   if any(keys.get((r['timestamp'],r.get('side'),r.get('level')))!=r for r in records if r['timestamp'].strftime('%Y-%m')==month):raise ValueError('historical_execution_persistence_failed')
  if records:
   latest=max(r['timestamp'] for r in records).strftime('%Y-%m-%dT%H:%M:%S.%fZ');updated=datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
   with connect_metadata(root) as db:db.execute("INSERT INTO ingestion_state VALUES (?,?,?,?) ON CONFLICT(pair_id,dataset) DO UPDATE SET latest_observation_at=excluded.latest_observation_at,updated_at=excluded.updated_at WHERE excluded.latest_observation_at>ingestion_state.latest_observation_at",(pair,dataset,latest,updated))
 if not receipt.exists():body.update(received_at=received_at,import_id=ident);immutable(receipt,encoded(body))
 return json.loads(receipt.read_text())

def references(data_dir,pair,dataset,start,end,as_of,selection=None,cache=None):
 from .cost_storage import digest
 result={}
 for path in sorted((Path(data_dir)/'historical_imports/execution_receipts').glob('*.json')):
  doc=json.loads(path.read_text());name=path.relative_to(data_dir).as_posix()
  if doc['pair_id']!=pair or selection is not None and name not in selection:continue
  if datetime.fromisoformat(doc['received_at'].replace('Z','+00:00'))>as_of:continue
  identity={k:v for k,v in doc.items() if k not in ('received_at','import_id')}
  if sha(encoded(identity))!=path.stem or doc['import_id']!=path.stem:raise ValueError('historical_execution_receipt_identity')
  raw=dependency(data_dir,doc['raw_path']).read_bytes()
  if sha(raw)!=doc['raw_sha256']:raise ValueError('historical_execution_raw_checksum')
  ref=dict(source_id=name,source_sha256=digest(doc),source_received_at=doc['received_at'],source_endpoint=doc['source_url'],timestamp_semantics=doc['timestamp_semantics'],source_namespace=doc['source_kind'])
  cache_key=(str(path),doc['raw_sha256'])
  samples=cache.get(cache_key) if cache is not None else None
  if samples is None:
   samples=sample_records(raw,pair)
   if cache is not None:
    cache[cache_key]=samples
    while len(cache)>32:cache.popitem(last=False)
  if cache is not None:cache.move_to_end(cache_key)
  for book,quote in samples:
   rows=book if dataset=='orderbook' else [quote]
   if start<=quote['timestamp']<end:result[quote['timestamp']]=[(rows,ref)]
 return result
