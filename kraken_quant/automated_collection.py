"""Bounded public collection, official archive jobs and explicit research admission.

No financial eligibility, trading, or automatic changes to frozen research plans.
"""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid
import zipfile
import zlib

import pyarrow.compute as pc
import pyarrow.parquet as pq

from .asset_catalogue import discover, _atomic_json
from .cost_storage import payload
from .db_environment import load_config
from .historical_population import ALIASES, import_csv
from .metadata_store import connect_metadata
from .ohlcv_collector import OhlcvCollector, collection_universe, read_time, sql_time
from .operations import ProcessLock

QUOTES = ('AUD', 'USD', 'USDT', 'USDC', 'EUR')


def member_name(asset):
    return ALIASES.get(asset['base_asset'], asset['base_asset']) + asset['quote_asset'] + '_1.csv'


def choose_universe(assets, members, target=64, enrolled=()):
    if not 50 <= target <= 100:
        raise ValueError('target_must_be_50_to_100')
    aud = sorted((a for a in assets if a['quote_asset'] == 'AUD'), key=lambda a:a['pair_id'])
    # If fewer than 50 AUD markets even exist, fewer than 50 can be admitted.
    retained=aud+[a for a in assets if a['pair_id'] in enrolled and a['quote_asset']!='AUD']
    if len(retained)>100:raise ValueError('enrolled_universe_exceeds_explicit_limit')
    candidates = [a for a in assets if a['quote_asset'] != 'AUD' and a['pair_id'] not in enrolled and member_name(a) in members]
    # Source-byte availability ranking, NOT volume/liquidity/profitability screening.
    candidates.sort(key=lambda a:(-members[member_name(a)].file_size, a['pair_id']))
    return retained + candidates[:max(0,target-len(retained))]


def inventory(data_dir, assets):
    rows=[]
    with connect_metadata(data_dir, readonly=True) as db:
        saved=dict(db.execute("SELECT pair_id,latest_observation_at FROM ingestion_state WHERE dataset='prices_1m'"))
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
        foreign_keys=list(db.execute('PRAGMA foreign_key_check'))
    for asset in assets:
        first=last=None; count=0
        for path in sorted((Path(data_dir)/'market_data'/asset['pair_id']/'prices_1m').glob('*.parquet')):
            # Existing schema, physical key and partition validation, bounded by month.
            from .parquet_store import MonthlyParquetStore
            table=MonthlyParquetStore(data_dir).read(asset['pair_id'],'prices_1m',path.stem)
            if table.num_rows:
                edges=pc.min_max(table['timestamp']).as_py()
                first=min(first,edges['min']) if first else edges['min']
                last=max(last,edges['max']) if last else edges['max']
                count+=table.num_rows
        checkpoint=saved.get(asset['pair_id'])
        rows.append(dict(pair_id=asset['pair_id'],rows=count,first=first,last=last,checkpoint=checkpoint,
                         checkpoint_matches=(last==read_time(checkpoint) if checkpoint else last is None)))
    return dict(integrity=integrity,foreign_key_errors=len(foreign_keys),markets=rows)


def archive_jobs(data_dir, archive, assets, *, start_month, end_month, max_months=6, seconds=300, expected_sha256=None):
    """Checkpointed monthly imports; a finite budget always yields to incremental IO."""
    if max_months < 0 or seconds < 0:raise ValueError('nonnegative_archive_budget_required')
    if max_months == 0 or seconds == 0:return dict(completed_month_jobs=0,results=[],status='budget_disabled')
    root=Path(data_dir); begun=time.monotonic(); results=[]; used=0
    # Hash entire original source; ZIP member extraction also checks CRC on EOF.
    with Path(archive).open('rb') as handle: archive_hash=hashlib.file_digest(handle,'sha256').hexdigest()
    approved=set()
    known={}
    for path in (root/'historical_imports/receipts').glob('*.json'):
        doc=json.loads(path.read_text())
        if doc.get('source_kind')=='kraken_official_ohlcvt' and doc.get('source_url')=='https://assets.kraken.com/marketing/institutions/'+Path(archive).name:
            approved.add(doc['archive_sha256'])
        if doc.get('archive_sha256')==archive_hash:
            known.setdefault(doc['pair_id'],set()).add(doc['month'])
    expected_sha256=expected_sha256 or (next(iter(approved)) if len(approved)==1 else None)
    if archive_hash!=expected_sha256:raise ValueError('approved_archive_checksum_required_or_mismatch')
    with zipfile.ZipFile(archive) as source:
        manifest=json.loads(source.read('MANIFEST.json'))
        if manifest.get('product')!='ohlcvt' or manifest.get('schema_version')!=1:
            raise ValueError('unsupported_official_archive')
        end=datetime.fromisoformat(manifest['coverage']['end']+'T00:00:00+00:00')+timedelta(days=1)
        available=set(source.namelist())
        for asset in assets:
            name=member_name(asset)
            if name not in available:
                results.append(dict(pair_id=asset['pair_id'],status='official_member_unavailable'));continue
            if used>=max_months or time.monotonic()-begun>=seconds:break
            original=root/'historical_archives/extracted'/archive_hash/name
            original.parent.mkdir(parents=True,exist_ok=True)
            if not original.exists():
                temporary=original.with_suffix('.extracting')
                try:
                    with source.open(name) as inp, temporary.open('wb') as out:shutil.copyfileobj(inp,out,1024*1024)
                    temporary.replace(original)
                finally:temporary.unlink(missing_ok=True)
            checksum=0; size=0
            with original.open('rb') as handle:
                while chunk:=handle.read(1024*1024):
                    checksum=zlib.crc32(chunk,checksum);size+=len(chunk)
            info=source.getinfo(name)
            if size!=info.file_size or checksum & 0xffffffff != info.CRC:
                raise ValueError('original_member_crc_mismatch')
            # Import full, exact calendar months from a pinned complete original.
            pending=[month for month in months(start_month,end_month) if month not in known.get(asset['pair_id'],set())]
            todo=pending[:max_months-used]
            if not todo:continue
            docs=import_csv(root,asset['pair_id'],original,
                source_url='https://assets.kraken.com/marketing/institutions/'+Path(archive).name,
                archive_sha256=archive_hash,coverage_end=end,start_month=todo[0],end_month=todo[-1])
            used+=len(todo)
            if not docs:results.append(dict(pair_id=asset['pair_id'],months=todo,status='no_source_observations'))
            for doc in docs:
                results.append(dict(pair_id=asset['pair_id'],month=doc['month'],status='persisted',
                    inserted_rows=doc['inserted_rows'] if doc['month'] in todo else 0,conflicts=doc['conflicting_rows']))
    return dict(archive_sha256=archive_hash,completed_month_jobs=used,results=results,budget_seconds=seconds,
                elapsed_seconds=time.monotonic()-begun,history_end_exclusive=end)


def months(start,end):
    value=datetime.strptime(start,'%Y-%m')
    while value.strftime('%Y-%m')<=end:
        yield value.strftime('%Y-%m')
        value=datetime(value.year+1,1,1) if value.month==12 else datetime(value.year,value.month+1,1)


def research_audit(data_dir, assets, *, start, end, now=None, pair_limit=50, record_budget=5_000_000):
    """One joint admission, with explicit rejected-universe and proposal governance."""
    from dataclasses import replace
    from .research_data import ResearchData
    from .market_continuity import ContinuityRequirements
    from .partition_contracts import PartitionRequest,PHASES
    from .partition_algorithm import phase_cuts
    from .parquet_store import MonthlyParquetStore
    now=now or datetime.now(timezone.utc);reader=ResearchData(data_dir,as_of=now)
    request=PartitionRequest(assets=tuple(a['pair_id'] for a in assets),identifier='automated-archive-universe-v1',
        cutoff=now,start=start,end=end,window_mode='FIXED_WINDOW',frequency=timedelta(hours=1),
        minimum_observations=(1000,250,250),minimum_durations=(timedelta(days=30),timedelta(days=10),timedelta(days=10)),
        continuity=ContinuityRequirements(require_source_complete=True,allow_verified_no_trade=True),
        maximum_records=record_budget)
    print('M13 original universe',len(assets),flush=True)
    original=reader.generate_partition_plan(request)
    cuts=phase_cuts(request,start,end);store=MonthlyParquetStore(data_dir);prechecks={};candidates=[]
    for asset in assets:
        pair=asset['pair_id'];counts=[0,0,0]
        for month in store.months(pair,'prices_1m'):
            if month<start.strftime('%Y-%m') or month>end.strftime('%Y-%m'):continue
            table=store.read(pair,'prices_1m',month)
            for i in range(3):
                mask=pc.and_(pc.greater_equal(table['timestamp'],cuts[i]),pc.less(table['timestamp'],cuts[i+1]))
                counts[i]+=pc.sum(pc.cast(mask,'int64')).as_py() or 0
        failures=[dict(asset=pair,phase=PHASES[i],reason='insufficient_observations',available=counts[i],required=request.minimum_observations[i]) for i in range(3) if counts[i]<request.minimum_observations[i]]
        prechecks[pair]=dict(counts=counts,failures=failures)
        if not failures:candidates.append(pair)
    count_qualified=list(candidates)
    candidates=candidates[:pair_limit]
    capacity_excluded=set(count_qualified)-set(candidates)
    if original.ready:
        candidates=list(request.assets);capacity_excluded=set()
    proposed=original if original.ready else None
    if not original.ready and candidates:
        print('M13 explicit count-qualified proposal',len(candidates),flush=True)
        proposed=reader.generate_partition_plan(replace(request,assets=tuple(candidates),identifier='automated-archive-count-qualified-proposal-v1'))
    proposed_report=proposed.report if proposed else {}
    def summary(plan):
        doc=proposed_report if plan is proposed else plan.report if plan else {}
        return dict(status=plan.status,manifest_id=plan.manifest_id,failures=doc.get('failures',[]),window_selection=doc.get('window_selection',[])) if plan else dict(status='INSUFFICIENT_OBSERVATIONS',manifest_id=None)

    admitted=set(request.assets if original.ready else candidates) if proposed and proposed.ready else set()
    reports=[]
    for asset in assets:
        pair=asset['pair_id'];quality=[]
        if pair in admitted:
            quality=[part['assets'][pair]['prices_1m']['quality'] for group in proposed_report['groups'] for part in group['phases'].values()]
        if pair in capacity_excluded:prechecks[pair]['failures']=[dict(asset=pair,reason='explicit_joint_validation_capacity',pair_limit=pair_limit)]
        reports.append(dict(pair_id=pair,admissible=pair in admitted,continuity=quality,
            partition_status='READY' if pair in admitted else 'NOT_EVALUATED_CAPACITY' if pair in capacity_excluded else 'INSUFFICIENT_OBSERVATIONS' if prechecks[pair]['failures'] else proposed.status if proposed else 'SOURCE_UNAVAILABLE',
            manifest_id=proposed.manifest_id if pair in admitted else None,failures=prechecks[pair]['failures'] or (proposed_report.get('failures',[]) if proposed and not proposed.ready else []),
            evaluated_as_of=sql_time(now),phase_observations=prechecks[pair]['counts'],
            limitations='EVENT_TIME price study only; not dense-grid, receipt-time or execution readiness.'))
    return dict(markets=reports,original_universe=dict(assets=request.assets,**summary(original)),
        explicit_proposal=dict(assets=candidates,reuses_original=original.ready,count_qualified_assets=count_qualified,pair_limit=pair_limit,record_budget=record_budget,excluded=[dict(pair_id=p,failures=v['failures']) for p,v in prechecks.items() if v['failures']],**summary(proposed)))


def repair_jobs(data_dir, warnings, *, max_gaps=2, requests_per_gap=5):
    """Resume exact public trade pages; unknown intervals never become fake candles."""
    from .historical_trades import fetch_gap
    root=Path(data_dir); status_path=root/'automated_collection/gap-jobs.json'
    state=json.loads(status_path.read_text()) if status_path.exists() else {}
    results=[]
    pending=[]
    for item in warnings:
        pair=item['pair_id'];start=read_time(item['first']);end=read_time(item['last'])+timedelta(minutes=1)
        key=hashlib.sha256((pair+sql_time(start)+sql_time(end)).encode()).hexdigest()
        if state.get(key,{}).get('status')!='source_window_replayed':
            pending.append((state.get(key,{}).get('last_attempted_at',''),key,item,start,end))
    for _,key,item,start,end in sorted(pending,key=lambda v:(v[0],v[1])):
        pair=item['pair_id']
        if len(results)>=max_gaps:break
        try:
            result=fetch_gap(root,pair,start=start,end=end,max_requests=requests_per_gap,quote_currencies=QUOTES)
        except Exception as error:result=dict(pair_id=pair,status='error',code=getattr(error,'code',type(error).__name__))
        result.update(start=sql_time(start),end_exclusive=sql_time(end),last_attempted_at=sql_time(datetime.now(timezone.utc)))
        state[key]=result;results.append(result);_atomic_json(status_path,payload(state))
    return results


def remember_gaps(data_dir, checkpoints, collector):
    root=Path(data_dir);path=root/'automated_collection/gap-queue.json'
    queue=json.loads(path.read_text()) if path.exists() else {}
    for result in collector['results']:
        pair=result['pair_id'];saved=checkpoints.get(pair)
        if result['status']!='ok' or not saved:continue
        files=sorted((root/'ohlcv_collection/responses'/pair).glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        if not files:continue
        doc=json.loads(files[0].read_text())
        if doc['fetched_at']!=result['fetched_at']:raise ValueError('incremental_response_receipt_mismatch')
        rows=next(value for key,value in doc['body']['result'].items() if key!='last')[:-1]
        if not rows:continue
        left=read_time(saved)+timedelta(minutes=1)
        right=datetime.fromtimestamp(min(int(row[0]) for row in rows),timezone.utc)
        if left<right:
            warning=dict(pair_id=pair,first=sql_time(left),last=sql_time(right-timedelta(minutes=1)),
                classification='MISSING_OR_UNVERIFIED',source='older_than_actual_OHLC_response',
                response_receipt=files[0].relative_to(root).as_posix())
            key=hashlib.sha256((pair+sql_time(left)+sql_time(right)).encode()).hexdigest()
            queue.setdefault(key,warning)
    _atomic_json(path,queue)
    return list(queue.values())


def validate_configuration(config):
    bounds={'target_pairs':(50,100),'backfill_month_jobs':(0,12),'backfill_seconds':(0,900),
            'gap_jobs':(0,10),'gap_requests':(1,20),'research_pair_limit':(50,100),'research_record_budget':(1,8_000_000)}
    defaults=dict(target_pairs=64,backfill_month_jobs=6,backfill_seconds=300,gap_jobs=2,gap_requests=5,research_pair_limit=50,research_record_budget=5_000_000)
    for key,(low,high) in bounds.items():
        value=config.get(key,defaults[key])
        if type(value) is not int or not low<=value<=high:raise ValueError('invalid_bounded_'+key)
    digest=config.get('archive_sha256','')
    if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise ValueError('approved_archive_sha256_required')
    if type(config.get('validate_research',False)) is not bool:raise ValueError('boolean_research_flag_required')
    if read_time(config['research_start'])>=read_time(config['research_end']):raise ValueError('ordered_fixed_research_window_required')
    if not list(months(config['start_month'],config['end_month'])):raise ValueError('ordered_archive_months_required')


def run_cycle(data_dir, config):
    validate_configuration(config)
    root=Path(data_dir); home=root/'automated_collection';home.mkdir(exist_ok=True)
    lock=ProcessLock(home/'pipeline')
    from .data_pipeline import PipelineConfig,PublicPolicy
    from .market_data import PUBLIC_REQUEST_POLICY
    policy=PublicPolicy(home,PipelineConfig(max_cycle_seconds=3600,max_requests_per_cycle=1000),
        lambda:datetime.now(timezone.utc),time.monotonic,time.sleep)
    token=PUBLIC_REQUEST_POLICY.set(policy)
    ident=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    report=dict(run_id=ident,started_at=datetime.now(timezone.utc),status='running',errors=[])
    path=home/'runs'/(ident+'.json')
    def save():_atomic_json(path,payload(report));_atomic_json(home/'latest.json',payload(report))
    try:
        save()
        with connect_metadata(root,readonly=True):pass
        OhlcvCollector(root)._binding()
        report['catalogue']=discover(root)
        assets,audit,_=collection_universe(root,QUOTES)
        archive=Path(config['archive']);archive=archive if archive.is_absolute() else root/archive
        with zipfile.ZipFile(archive) as source:members={i.filename:i for i in source.infolist()}
        enrollment_path=home/'universe.json'
        enrolled=json.loads(enrollment_path.read_text()).get('pairs',[]) if enrollment_path.exists() else []
        selected=choose_universe(assets,members,config.get('target_pairs',64),enrolled)
        report['new_pairs']=[a['pair_id'] for a in selected if a['pair_id'] not in enrolled]
        _atomic_json(enrollment_path,dict(pairs=sorted(set(enrolled)|{a['pair_id'] for a in selected}),updated_at=sql_time(datetime.now(timezone.utc))))
        report.update(catalogue_run_id=audit,eligible_aud_pairs=sum(a['quote_asset']=='AUD' for a in assets),
            expanded_verified_candidates=len(assets),selected_pairs=[a['pair_id'] for a in selected])
        report['before']=inventory(root,selected);save()
        warnings_path=config.get('gap_warnings')
        warnings=json.loads(Path(warnings_path).read_text()) if warnings_path and Path(warnings_path).exists() else []
        queue_path=home/'gap-queue.json'
        if queue_path.exists():warnings+=list(json.loads(queue_path.read_text()).values())
        with (home/(ident+'-repair.log')).open('w',encoding='utf-8') as out,redirect_stdout(out):
            report['gap_repairs']=repair_jobs(root,warnings,max_gaps=config.get('gap_jobs',2),requests_per_gap=config.get('gap_requests',5))
        try:
            with (home/(ident+'-backfill.log')).open('w',encoding='utf-8') as out,redirect_stdout(out):
                report['backfill']=archive_jobs(root,archive,selected,start_month=config['start_month'],end_month=config['end_month'],
                    max_months=config.get('backfill_month_jobs',6),seconds=config.get('backfill_seconds',300),expected_sha256=config.get('archive_sha256'))
        except Exception as error:report['errors'].append(dict(task='backfill',code=type(error).__name__))
        save()
        with connect_metadata(root,readonly=True) as db:
            prior_checkpoints=dict(db.execute("SELECT pair_id,latest_observation_at FROM ingestion_state WHERE dataset='prices_1m'"))
        report['incremental']=OhlcvCollector(root).collect(all_aud=True,quote_currencies=QUOTES,pair_ids=report['selected_pairs'])
        report['known_historical_gaps']=remember_gaps(root,prior_checkpoints,report['incremental'])
        report['after']=inventory(root,selected);save()
        if config.get('validate_research',False):
            with (home/(ident+'-research.log')).open('w',encoding='utf-8') as out,redirect_stdout(out):
                admission=research_audit(root,selected,start=read_time(config['research_start']),end=read_time(config['research_end']),pair_limit=config.get('research_pair_limit',50),record_budget=config.get('research_record_budget',5_000_000))
            report['research']=admission.pop('markets');report['joint_research']=admission
            report['research_admissible_pairs']=sum(r['admissible'] for r in report['research'])
        report['status']='success' if not report['errors'] and all(r['status']=='ok' for r in report['incremental']['results']) and all(r['checkpoint_matches'] for r in report['after']['markets']) and report['after']['integrity']=='ok' and not report['after']['foreign_key_errors'] else 'partial'
    except Exception as error:report.update(status='failed');report['errors'].append(dict(code=type(error).__name__))
    finally:
        report['finished_at']=datetime.now(timezone.utc);report['public_requests']=policy.events
        save();PUBLIC_REQUEST_POLICY.reset(token);lock.close()
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('once','status','validate'))
    parser.add_argument('--config',default='examples/automated_collection.json');parser.add_argument('--root',default='.')
    args=parser.parse_args(argv);root=Path(args.root).resolve();data=load_config(root).data_dir
    config=json.loads((root/args.config).read_text())
    if args.command=='once':result=run_cycle(data,config)
    elif args.command=='status':result=json.loads((data/'automated_collection/latest.json').read_text())
    else:
        assets,_,_=collection_universe(data,QUOTES)
        latest=json.loads((data/'automated_collection/latest.json').read_text())
        selected=[a for a in assets if a['pair_id'] in latest['selected_pairs']]
        with (data/'automated_collection/research-validation.log').open('w',encoding='utf-8') as out,redirect_stdout(out):
            result=research_audit(data,selected,start=read_time(config['research_start']),end=read_time(config['research_end']),pair_limit=config.get('research_pair_limit',50),record_budget=config.get('research_record_budget',5_000_000))
        _atomic_json(data/'automated_collection/research.json',payload(result))
    print(json.dumps(payload(result),indent=2));return int(result.get('status') in ('partial','failed'))

if __name__=='__main__':raise SystemExit(main())
