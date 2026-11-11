"""Exact official OHLCVT import, preserving existing records and immutable evidence."""
import csv,io,json,hashlib,os,tempfile
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal
from .cost_storage import payload
from .metadata_store import connect_metadata
from .parquet_store import MonthlyParquetStore
from .storage_schema import table_from_records,validate_pair_id
from .operations import ProcessLock
UTC=timezone.utc
ALIASES={'BTC':'XBT','DOGE':'XDG'}
def encoded(value):return json.dumps(payload(value),sort_keys=True,separators=(',',':')).encode()
def sha(value):return hashlib.sha256(value).hexdigest()
def immutable(path,content):
 path=Path(path)
 if any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('historical_symlinks_prohibited')
 path.parent.mkdir(parents=True,exist_ok=True)
 if path.exists():
  if path.read_bytes()!=content:raise ValueError('immutable_dependency_conflict')
  return
 fd,tmp=tempfile.mkstemp(dir=path.parent,suffix='.tmp')
 try:
  with os.fdopen(fd,'wb') as out:out.write(content);out.flush();os.fsync(out.fileno())
  os.replace(tmp,path)
 finally:Path(tmp).unlink(missing_ok=True)
def parse_csv(raw,pair,rejects=None):
 validate_pair_id(pair);rows=[]
 for v in csv.reader(io.StringIO(raw.decode('utf-8'))):
  if len(v)!=7:raise ValueError('invalid_official_csv_columns')
  seconds=int(v[0])
  if seconds%60:raise ValueError('unaligned_official_timestamp')
  values=list(map(Decimal,v[1:6]))
  def fits(value):
   digits=list(value.as_tuple().digits);exponent=value.as_tuple().exponent
   if not value.is_finite():raise ValueError('nonfinite_archive_decimal')
   while digits and digits[-1]==0:digits.pop();exponent+=1
   return exponent>=-18 and len(digits)+max(exponent,0)<=20+18
  if not all(fits(value) for value in values):
   if rejects is None:raise ValueError('source_decimal_exceeds_approved_schema')
   rejects.append(dict(timestamp=seconds,source=v,reason='source_decimal_exceeds_approved_schema'));continue
  rows.append(dict(pair_id=pair,timestamp=datetime.fromtimestamp(seconds,UTC),**dict(zip(('open','high','low','close','volume_base'),values)),trade_count=int(v[6]),is_complete=True))
 import pyarrow as pa
 from .storage_schema import dataset_schema,validate_table
 validate_table('prices_1m',pa.Table.from_pylist(rows,schema=dataset_schema('prices_1m')))
 return rows
def dependency(root,name):
 path=Path(name);target=Path(root)/path
 if path.is_absolute() or '..' in path.parts or not name.startswith('historical_imports/') or not target.resolve().is_relative_to(Path(root).resolve()) or any(p.is_symlink() for p in (target,*target.parents)):raise ValueError('unsafe_historical_dependency')
 return target
def source_records(root,doc,checked=None):
 raw=dependency(root,doc['raw_path']).read_bytes()
 if sha(raw)!=doc['raw_sha256']:raise ValueError('historical_raw_checksum_mismatch')
 checked={} if checked is None else checked
 for item in doc.get('dependencies',[]):
  key=(item['path'],item['sha256'])
  if key not in checked:
   with dependency(root,item['path']).open('rb') as handle:actual=hashlib.file_digest(handle,'sha256').hexdigest()
   if actual!=item['sha256']:raise ValueError('historical_source_dependency_changed')
   checked[key]=True
 rows=parse_csv(raw,doc['pair_id'],rejects=[])
 if len(rows)+doc.get('invalid_rows',0)!=doc['source_rows']:raise ValueError('historical_source_count_mismatch')
 return rows
def import_csv(data_dir,pair,path,*,source_url,archive_sha256,coverage_end,received_at=None, source_kind='kraken_official_ohlcvt', dependencies=(), start_month=None, end_month=None):
 """Commit one month at a time; existing validated rows win, conflicts retained.

 Receipts follow verified Parquet persistence. A restart reconciles the checkpoint.
 Official files contain traded intervals only; absent minutes are never fabricated.
 """
 root=Path(data_dir);path=Path(path);validate_pair_id(pair);received_at=received_at or datetime.now(UTC)
 if received_at.tzinfo is None or coverage_end.tzinfo is None or coverage_end>received_at.replace(second=0,microsecond=0):raise ValueError('invalid_source_time')
 if not (source_kind=='kraken_official_ohlcvt' and source_url.startswith('https://assets.kraken.com/marketing/institutions/') or source_kind=='kraken_public_trades_ohlcv' and source_url=='https://api.kraken.com/0/public/Trades' or source_kind=='kraken_official_trade_archive_ohlcv' and source_url=='https://assets.kraken.com/marketing/institutions/Kraken_Trades_Full.zip'):raise ValueError('approved_public_source_required')
 if len(archive_sha256)!=64 or any(c not in '0123456789abcdef' for c in archive_sha256):raise ValueError('archive_sha256_required')
 for item in dependencies:
  with dependency(root,item['path']).open('rb') as handle:actual=hashlib.file_digest(handle,'sha256').hexdigest()
  if actual!=item['sha256']:raise ValueError('historical_source_dependency_changed')
 with connect_metadata(root,readonly=True) as db:
  db.row_factory=__import__('sqlite3').Row
  asset=db.execute('SELECT * FROM assets WHERE pair_id=?',(pair,)).fetchone()
  if asset is None or asset['quote_asset'] not in ('AUD','USD','USDT','USDC','EUR'):raise ValueError('unknown_aud_market')
 if path.name!=ALIASES.get(asset['base_asset'],asset['base_asset'])+asset['quote_asset']+'_1.csv':raise ValueError('archive_member_identity_mismatch')
 store=MonthlyParquetStore(root);area=root/'historical_imports';area.mkdir(exist_ok=True);lock=ProcessLock(area/('import-'+pair));results=[]
 with path.open('rb') as handle:member_sha=hashlib.file_digest(handle,'sha256').hexdigest()
 def commit(month,lines):
  raw=b''.join(lines);invalid=[];rows=parse_csv(raw,pair,rejects=invalid);stamps=[r['timestamp'] for r in rows]
  if stamps!=sorted(stamps) or any(t>=coverage_end for t in stamps):raise ValueError('invalid_archive_window_or_order')
  old={r['timestamp']:r for r in store.read(pair,'prices_1m',month).to_pylist()};incoming={};conflicts=[];identical=0
  for row in rows:
   prior=incoming.get(row['timestamp'],old.get(row['timestamp']))
   if prior is not None:
    if prior!=row:conflicts.append(dict(existing=prior,incoming=row))
    else:identical+=1
   else:incoming[row['timestamp']]=row
  raw_id=sha(raw);raw_name='historical_imports/raw/'+raw_id+'.csv';immutable(root/raw_name,raw)
  body=dict(contract_version=1,source_kind=source_kind,pair_id=pair,kraken_pair_id=asset['kraken_pair_id'],member_name=path.name,member_sha256=member_sha,archive_sha256=archive_sha256,source_url=source_url,raw_path=raw_name,raw_sha256=raw_id,source_rows=len(lines),invalid_rows=len(invalid),month=month,coverage_end_exclusive=coverage_end,timestamp_semantics='UNIX seconds UTC candle opening; traded intervals only',conflict_policy='existing validated record wins; incoming retained as evidence')
  if dependencies:body['dependencies']=list(dependencies)
  if source_kind!='kraken_official_ohlcvt':body['source_artifact_sha256']=body.pop('archive_sha256')
  ident=sha(encoded(body));receipt=area/'receipts'/(ident+'.json')
  if receipt.exists():
   saved=json.loads(receipt.read_text())
   identity={k:v for k,v in saved.items() if k not in ('received_at','import_id','inserted_rows','identical_rows','conflicting_rows','conflict_path')}
   if identity!=json.loads(encoded(body)) or sha(dependency(root,saved['raw_path']).read_bytes())!=raw_id:raise ValueError('historical_retry_source_changed')
   accepted={r['timestamp']:r for r in rows}
   if saved.get('conflict_path'):
    for conflict in json.loads(dependency(root,saved['conflict_path']).read_text()):
     stamp=datetime.fromisoformat(conflict['existing']['timestamp'].replace('Z','+00:00'));accepted.pop(stamp,None)
   if any(old.get(k)!=v for k,v in accepted.items()):raise ValueError('historical_retry_missing_or_changed_records')
  else:
   if incoming:
    import pyarrow as pa
    from .storage_schema import dataset_schema
    store.write('prices_1m',pa.Table.from_pylist(list(incoming.values()),schema=dataset_schema('prices_1m')))
   actual={r['timestamp']:r for r in store.read(pair,'prices_1m',month).to_pylist()}
   if any(actual.get(k)!=v for k,v in incoming.items()):raise ValueError('historical_persistence_verification_failed')
   if conflicts:immutable(area/'conflicts'/(ident+'.json'),encoded(conflicts))
   if invalid:immutable(area/'invalid'/(ident+'.json'),encoded(invalid))
   body.update(received_at=received_at,import_id=ident,inserted_rows=len(incoming),identical_rows=identical,conflicting_rows=len(conflicts),conflict_path='historical_imports/conflicts/'+ident+'.json' if conflicts else None)
   immutable(receipt,encoded(body))
  # SQL follows verified persistence, never moves an existing checkpoint backwards.
  newest=max(stamps).strftime('%Y-%m-%dT%H:%M:%S.%fZ') if stamps else None;updated=datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
  with connect_metadata(root) as db:
   if newest:db.execute("INSERT INTO ingestion_state VALUES (?,?,?,?) ON CONFLICT(pair_id,dataset) DO UPDATE SET latest_observation_at=excluded.latest_observation_at,updated_at=excluded.updated_at WHERE excluded.latest_observation_at>ingestion_state.latest_observation_at",(pair,'prices_1m',newest,updated))
  results.append(json.loads(receipt.read_text()));print(pair,month,'source',len(rows),'new',len(incoming),'conflicts',len(conflicts),flush=True)
 lower=int(datetime.fromisoformat(start_month+'-01T00:00:00+00:00').timestamp()) if start_month else None
 upper=None
 if end_month:
  last=datetime.fromisoformat(end_month+'-01T00:00:00+00:00')
  upper=int((datetime(last.year+1,1,1,tzinfo=UTC) if last.month==12 else datetime(last.year,last.month+1,1,tzinfo=UTC)).timestamp())
 if lower is not None and upper is not None and lower>=upper:
  lock.close();raise ValueError('ordered_archive_month_range_required')
 try:
  lines=[];month=None
  with path.open('rb') as handle:
   for line in handle:
    stamp=int(line.split(b',',1)[0])
    if lower is not None and stamp<lower or upper is not None and stamp>=upper:continue
    value=datetime.fromtimestamp(stamp,UTC).strftime('%Y-%m')
    if month is not None and value!=month:commit(month,lines);lines=[]
    month=value;lines.append(line)
  if lines:commit(month,lines)
 finally:lock.close()
 return results

def references(data_dir,pair,start,end,as_of,selection=None):
 """Verify exact original CSV dependencies; never backdate acquisition knowledge."""
 from .cost_storage import digest
 result={};checked={}
 for path in sorted((Path(data_dir)/'historical_imports/receipts').glob('*.json')):
  doc=json.loads(path.read_text());name=path.relative_to(data_dir).as_posix()
  if doc['pair_id']!=pair or doc['month']<start.strftime('%Y-%m') or doc['month']>end.strftime('%Y-%m'):continue
  received=datetime.fromisoformat(doc['received_at'].replace('Z','+00:00'))
  if received>as_of or selection is not None and name not in selection:continue
  identity={k:v for k,v in doc.items() if k not in ('received_at','import_id','inserted_rows','identical_rows','conflicting_rows','conflict_path')}
  if sha(encoded(identity))!=doc['import_id'] or path.stem!=doc['import_id']:raise ValueError('historical_receipt_identity_mismatch')
  ref=dict(source_id=name,source_sha256=digest(doc),source_received_at=doc['received_at'],source_endpoint=doc['source_url'],timestamp_semantics=doc['timestamp_semantics'],source_namespace=doc['source_kind'])
  for row in source_records(data_dir,doc,checked):
   if start<=row['timestamp']<end:result.setdefault(row['timestamp'],[]).append((row,ref))
 return result

def import_archive(data_dir,archive,*,expected_sha256,coverage_end):
 """Verify actual archive bytes and CRCs, then extract only verified AUD members."""
 import zipfile,shutil
 from .ohlcv_collector import collection_universe
 archive=Path(archive)
 with archive.open('rb') as handle:actual=hashlib.file_digest(handle,'sha256').hexdigest()
 if actual!=expected_sha256:raise ValueError('archive_integrity_mismatch')
 results=[]
 with zipfile.ZipFile(archive) as source:
  manifest=json.loads(source.read('MANIFEST.json'))
  if manifest.get('product')!='ohlcvt' or manifest.get('schema_version')!=1 or manifest.get('has_header') is not False or manifest.get('columns')!=['timestamp','open','high','low','close','volume','trades']:raise ValueError('unsupported_official_archive')
  source_end=datetime.fromisoformat(manifest['coverage']['end']).replace(tzinfo=UTC)+__import__('datetime').timedelta(days=1)
  if coverage_end!=source_end:raise ValueError('archive_coverage_mismatch')
  folder=Path(data_dir)/'historical_archives/extracted'/actual;folder.mkdir(parents=True,exist_ok=True)
  for asset in collection_universe(data_dir)[0]:
   name=ALIASES.get(asset['base_asset'],asset['base_asset'])+asset['quote_asset']+'_1.csv'
   if name not in source.namelist():raise ValueError('verified_aud_archive_member_missing:'+asset['pair_id'])
   target=folder/name
   # Reading to EOF verifies the ZIP member CRC; no untrusted paths are extracted.
   with source.open(name) as member:
    fd,tmp=tempfile.mkstemp(dir=folder,suffix='.tmp')
    try:
     with os.fdopen(fd,'wb') as out:shutil.copyfileobj(member,out)
     content=Path(tmp).read_bytes();immutable(target,content)
    finally:Path(tmp).unlink(missing_ok=True)
   results.extend(import_csv(data_dir,asset['pair_id'],target,source_url='https://assets.kraken.com/marketing/institutions/'+archive.name,archive_sha256=actual,coverage_end=coverage_end))
 return results

def main(argv=None):
 import argparse
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('import-archive','trades','readiness'))
 parser.add_argument('--data-dir',default='data');parser.add_argument('--archive');parser.add_argument('--expected-sha256');parser.add_argument('--pair');parser.add_argument('--start');parser.add_argument('--end')
 args=parser.parse_args(argv)
 if args.command=='readiness':
  from .historical_readiness import build_report
  ident,body=build_report(args.data_dir);print(json.dumps(dict(manifest_id=ident,markets=[{k:m[k] for k in ('pair_id','candles','prices_status','execution_status')} for m in body['markets']]),indent=2));return
 end=datetime.fromisoformat(args.end.replace('Z','+00:00'))
 if args.command=='import-archive':result=import_archive(args.data_dir,args.archive,expected_sha256=args.expected_sha256,coverage_end=end)
 else:
  from .historical_trades import fetch_gap
  result=fetch_gap(args.data_dir,args.pair,start=datetime.fromisoformat(args.start.replace('Z','+00:00')),end=end)
 print(json.dumps(payload(result),indent=2))

if __name__=='__main__':main()
