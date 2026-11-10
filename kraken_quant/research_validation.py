"""Bounded, immutable validation snapshots for a fixed historical data request."""
from datetime import datetime,timedelta,timezone
import hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
from .cost_storage import payload
from .historical_population import encoded,immutable,sha,dependency
from .metadata_store import connect_metadata
from .operations import ProcessLock
from .asset_catalogue import _atomic_json

POLICY_KEYS=('start_month','research_start','research_end','research_pair_limit','research_record_budget','research_cutoff','archive_sha256')

def file_sha(path):
    hasher=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):hasher.update(chunk)
    return hasher.hexdigest()


def fingerprint(root,assets,config,extra_paths=()):
    root=Path(root).resolve();files=set();roster=[];pairs={a['pair_id'] for a in assets}
    from .ohlcv_collector import read_time
    start=config.get('start_month',config['research_start'][:7]);end=(read_time(config['research_end'])-timedelta(microseconds=1)).strftime('%Y-%m')
    def add(path):
        path=Path(path)
        if not path.is_absolute():path=root/path
        if '..' in path.parts or not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in (path,*path.parents)):
            raise ValueError('unsafe_validation_dependency')
        if path.relative_to(root).parts[0] not in ('market_data','historical_imports','historical_archives','catalogue_discovery','partition_plans'):
            raise ValueError('unsupported_validation_dependency')
        files.add(path)
    for asset in assets:
        for path in sorted((root/'market_data'/asset['pair_id']/'prices_1m').glob('*.parquet')):
            if path.stem<=end:
                roster.append(path.relative_to(root).as_posix());add(path)
        import pyarrow.parquet as pq
        later=any(pq.ParquetFile(path).metadata.num_rows>0 for path in (root/'market_data'/asset['pair_id']/'prices_1m').glob('*.parquet') if path.stem>end)
        roster.append(asset['pair_id']+':post_window_present='+str(later))
    for path in sorted((root/'historical_imports/receipts').glob('*.json')):
        doc=json.loads(path.read_text())
        if doc['pair_id'] not in pairs or not start<=doc['month']<=end:continue
        add(path);add(dependency(root,doc['raw_path']))
        for item in doc.get('dependencies',[]):add(dependency(root,item['path']))
        if doc.get('archive_sha256'):
            name=doc['member_name']
            if '/' in name or '\\' in name or '..' in name:raise ValueError('unsafe_archive_member')
            source=root/'historical_archives/extracted'/doc['archive_sha256']/name
            if not source.exists():source=root/'historical_archives/kraken_official/minute_csv'/name
            add(source)
    for path in sorted((root/'historical_imports/trade_windows').glob('*.json')):
        doc=json.loads(path.read_text())
        if doc['pair_id'] not in pairs or read_time(doc['start'])>=read_time(config['research_end']) or read_time(doc['end'])<=read_time(config['research_start']):continue
        add(path)
        for item in doc['dependencies']:add(dependency(root,item['path']))
    for name in extra_paths:add(name)
    archive=Path(config['archive']);archive=archive if archive.is_absolute() else root/archive
    archive_hash=file_sha(archive)
    if archive_hash!=config['archive_sha256']:raise ValueError('approved_archive_checksum_mismatch')
    identities=[{k:v for k,v in a.items() if k not in ('first_seen','last_seen')} for a in assets]
    with connect_metadata(root,readonly=True) as db:
        issues=[list(row) for row in db.execute('SELECT * FROM data_quality_issues ORDER BY issue_id') if row[1] is None or row[1] in pairs]
    document=dict(version=1,policy={k:config.get(k) for k in POLICY_KEYS},identities=identities,roster=roster,
        files={p.relative_to(root).as_posix():file_sha(p) for p in sorted(files)},archive_sha256=archive_hash,issues=issues,
        code={p.name:file_sha(p) for p in sorted(Path(__file__).parent.glob('*.py'))})
    return sha(encoded(document))


def cache_result(root,assets,config,result,before):
    root=Path(root);after=fingerprint(root,assets,config)
    if before!=after:raise ValueError('validation_inputs_changed_during_run')
    ready=result.get('explicit_proposal',{}).get('status')=='READY'
    if not ready:return result
    from .partition_manifest import read_plan
    ident=result['explicit_proposal']['manifest_id'];plan=read_plan(root,ident)
    extra=['partition_plans/v1/'+ident+'.json']+[v['source_id'] for v in plan['metadata_sources'].values()]
    full=fingerprint(root,assets,config,extra)
    doc=dict(version=1,fingerprint=full,extra_paths=extra,result=result)
    blob=encoded(doc);ident=sha(blob)
    immutable(root/'automated_collection/research_cache'/(ident+'.json'),blob)
    _atomic_json(root/'automated_collection/research-cache.json',dict(cache_id=ident))
    return result


def cached_result(root,assets,config):
    root=Path(root);pointer=root/'automated_collection/research-cache.json'
    if not pointer.exists():return None
    try:
        ident=json.loads(pointer.read_text())['cache_id']
        if len(ident)!=64 or any(c not in '0123456789abcdef' for c in ident):return None
        raw=(root/'automated_collection/research_cache'/(ident+'.json')).read_bytes()
        if sha(raw)!=ident:return None
        doc=json.loads(raw)
        if doc.get('version')!=1:return None
        from .partition_manifest import read_plan
        proposal=doc['result']['explicit_proposal'];plan=read_plan(root,proposal['manifest_id'])
        expected={'partition_plans/v1/'+proposal['manifest_id']+'.json',*[v['source_id'] for v in plan['metadata_sources'].values()]}
        if not expected<=set(doc['extra_paths']) or plan['configuration']['selection_policy']!='COMMON_CALENDAR' or plan['configuration']['window_mode']!='FIXED_WINDOW' or plan['configuration']['assets']!=proposal['assets']:return None
        if {r['pair_id'] for r in doc['result']['markets'] if r['admissible']}!=set(proposal['assets']):return None
        if doc['fingerprint']!=fingerprint(root,assets,config,doc['extra_paths']):return None
        result=doc['result'];result['validation_reused']=True
        result['inputs_verified_at']=datetime.now(timezone.utc).isoformat()
        # evaluated_as_of remains the immutable snapshot cutoff, never today's time.
        return result
    except (OSError,ValueError,KeyError,TypeError):return None


def bounded_validation(root,assets,config,*,clock=lambda:datetime.now(timezone.utc),sleep=time.sleep):
    root=Path(root);cached=cached_result(root,assets,config)
    if cached is not None:return cached
    home=root/'automated_collection';ident=uuid.uuid4().hex
    request=home/(ident+'-validation-request.json');result_path=home/(ident+'-validation-result.json')
    public_config={k:config[k] for k in (*POLICY_KEYS,'archive') if k in config}
    _atomic_json(request,payload(dict(data_dir=str(root.resolve()),assets=assets,config=public_config,result_path=str(result_path.resolve()))))
    environment=dict(os.environ);environment['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
    deadline=clock()+timedelta(seconds=config.get('research_validation_seconds',1800))
    with (home/(ident+'-validation-worker.log')).open('w',encoding='utf-8') as log:
        process=subprocess.Popen([sys.executable,'-m','kraken_quant.research_validation',str(request.resolve())],cwd=Path(__file__).resolve().parents[1],env=environment,stdout=log,stderr=log)
        while process.poll() is None:
            if clock()>=deadline:
                process.kill();process.wait();break
            sleep(1)
    if result_path.exists():return json.loads(result_path.read_text())
    return dict(markets=[dict(pair_id=a['pair_id'],admissible=False,partition_status='PENDING_VALIDATION',failures=[dict(reason='bounded_validation_incomplete')]) for a in assets],
                original_universe=dict(status='PENDING_VALIDATION'),explicit_proposal=dict(status='PENDING_VALIDATION'),validation_pending=True)


def worker(path):
    request=json.loads(Path(path).read_text());root=Path(request['data_dir']);assets=request['assets'];config=request['config']
    lock=ProcessLock(root/'automated_collection/research-validation')
    try:
        before=fingerprint(root,assets,config)
        from .automated_collection import research_audit
        from .ohlcv_collector import read_time
        result=research_audit(root,assets,start=read_time(config['research_start']),end=read_time(config['research_end']),
            now=read_time(config['research_cutoff']) if config.get('research_cutoff') else None,
            pair_limit=config.get('research_pair_limit',50),record_budget=config.get('research_record_budget',5_000_000))
        result=cache_result(root,assets,config,result,before)
        _atomic_json(Path(request['result_path']),payload(result))
    finally:lock.close()

if __name__=='__main__':worker(sys.argv[1])
