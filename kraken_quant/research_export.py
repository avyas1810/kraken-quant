"""Offline verified research exports; populate alone uses the public pipeline."""
import argparse
import csv
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import hashlib
from itertools import groupby
import json
import os
from pathlib import Path
import re
import tempfile

import pandas as pd
from .asset_catalogue import _atomic_json
from .cost_storage import AuditStore,digest,payload
from .db_environment import load_config
from .eligibility_manager import EligibilityManager
from .metadata_store import connect_metadata
from .operations import ProcessLock
from .research_data import ResearchData,ResearchDataError,instant,bounds,key_text,SOURCE_COLUMNS
from .storage_schema import dataset_schema,LOGICAL_KEYS
from . import ohlcv_collector as candles

DATA_FILES={'prices_1m.csv':'prices_1m','quotes_1m.csv':'quotes_1m','orderbook_levels.csv':'orderbook'}
CSV_FILES=set(DATA_FILES)|{'assets.csv','fee_schedules.csv','all_markets.csv','eligible.csv','rejected.csv','pending_evaluation.csv','data_quality.csv','coverage.csv'}
NULL=r'\N'


def sha(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(1048576),b''):result.update(block)
    return result.hexdigest()


def cell(value):
    if value is None or value is pd.NA or value is pd.NaT:return NULL
    if isinstance(value,(datetime,pd.Timestamp)):return candles.sql_time(value.to_pydatetime() if isinstance(value,pd.Timestamp) else value)
    if isinstance(value,bool):return str(value).lower()
    if isinstance(value,Decimal):return str(value)
    if isinstance(value,(list,tuple,dict)):return json.dumps(payload(value),sort_keys=True,separators=(',',':'))
    if isinstance(value,float) and pd.isna(value):raise ValueError('unexpected_float_nan')
    return str(value)


class CsvFile:
    def __init__(self,path,columns):
        self.path=Path(path);self.columns=list(columns);self.rows=0
        self.handle=self.path.open('w',encoding='utf-8',newline='')
        self.writer=csv.writer(self.handle,lineterminator='\n');self.writer.writerow(self.columns)
    def append(self,rows):
        for row in rows:self.writer.writerow([cell(row.get(k)) for k in self.columns]);self.rows+=1
    def finish(self):
        self.handle.flush();os.fsync(self.handle.fileno());self.handle.close()
        return dict(rows=self.rows,sha256=sha(self.path),columns=self.columns)


def selected_pairs(reader,pair=None,all_aud=False):
    if (pair is None)==(not all_aud):raise ValueError('select_pair_or_all_aud')
    pairs=reader.pairs()
    if pair and pair not in pairs:raise ValueError('pair_not_verified_aud_spot')
    return [pair] if pair else pairs


def screening(reader,pairs):
    manager=EligibilityManager(reader.data_dir);runs=[]
    for path in (reader.data_dir/'eligibility_screening/runs').glob('*.json'):
        run=manager.load(path.stem)
        if run['evaluation']['as_of']<=candles.sql_time(reader.as_of) and run['evaluation']['selection']=='all_aud_known_as_of' and run['projection_verification']['status']=='verified':runs.append(run)
    if not runs:return [],None
    latest=max(runs,key=lambda r:(r['evaluation']['as_of'],r['evaluated_at'],r['report_id']))
    if manager.verify_evidence(latest['report_id'])['status']!='verified':raise ResearchDataError('screening_evidence_unavailable')
    return [m for m in latest['evaluation']['markets'] if m['pair_id'] in pairs],latest


def report(data_dir,*,pair=None,all_aud=False,as_of=None):
    reader=ResearchData(data_dir,as_of=as_of);pairs=selected_pairs(reader,pair,all_aud)
    from .data_pipeline import PipelineConfig
    health=PipelineConfig()
    end=candles.minute_start(reader.as_of);start=end-timedelta(days=90);rows=[]
    for name in pairs:
        quality=reader.load_quality_report(name,start,end);stats=quality.datasets;prices=stats['prices_1m'];cov=prices['coverage']
        fees=reader.load_fee_schedules(name);full={};verification_errors=[]
        for dataset in DATA_FILES.values():
            first=stats[dataset]['first']
            try:
                data=reader._load(dataset,name,first if first and instant(first)<reader.as_of else start,reader.as_of)
                full[dataset]=data
            except ResearchDataError as error:
                full[dataset]=None;verification_errors.append(dataset+':'+error.code)
        price=full['prices_1m']
        snapshots={}
        for key in ('quotes_1m','orderbook'):
            values=full[key]
            latest=values['timestamp'].max().to_pydatetime() if values is not None and len(values) else None
            age=(reader.as_of-latest).total_seconds() if latest else None
            snapshots[key]=dict(latest=candles.sql_time(latest) if latest else None,age_seconds=age,status='unavailable' if age is None else 'delayed' if age>health.book_health_seconds else 'recent')
        first_price=price['timestamp'].min().to_pydatetime() if price is not None and len(price) else None
        last_price=price['timestamp'].max().to_pydatetime() if price is not None and len(price) else None
        price_age=(reader.as_of-last_price-candles.MINUTE).total_seconds() if last_price else None
        fee_age=(reader.as_of-fees['retrieved_at'].max().to_pydatetime()).total_seconds() if len(fees) else None
        rows.append(dict(pair_id=name,first_price=candles.sql_time(first_price) if first_price else None,last_price=candles.sql_time(last_price) if last_price else None,observed_candles=len(price) if price is not None else None,historical_span_days=(last_price-first_price+candles.MINUTE).total_seconds()/86400 if first_price else None,
            coverage_start=candles.sql_time(start),coverage_end_exclusive=candles.sql_time(end),completeness_pct=cov['coverage_pct'],missing_minutes=cov['missing_minutes'],longest_continuous_minutes=cov['longest_continuous_minutes'],
            missing_intervals=cov['missing_intervals'],quote_observations=len(full['quotes_1m']) if full['quotes_1m'] is not None else None,book_snapshots=full['orderbook']['timestamp'].nunique() if full['orderbook'] is not None else None,book_level_rows=len(full['orderbook']) if full['orderbook'] is not None else None,verification_errors=verification_errors,
            fee_source_available=not fees.empty,account_applicability='account_rates_available_tier_unknown' if not fees.empty and any(fees['account_specific']) else 'unknown',
            price_age_seconds=price_age,price_freshness='unavailable' if price_age is None else 'delayed' if price_age>health.ohlcv_health_seconds else 'recent',
            snapshot_freshness=snapshots,fee_source_age_seconds=fee_age,fee_source_status='unavailable' if fee_age is None else 'stale' if fee_age>health.cadences['fees'] else 'retrieved_effective_date_unknown',fee_applicability='No effective historical date or account tier inferred',
            cost_algorithms='NOT_IMPLEMENTED',ninety_days_available=cov['missing_minutes']==0 and not prices['integrity_unknown'] and not prices['invalid_records'],
            usability='Raw-data inspection and user-designed short-window experiments only; sparse snapshots do not support historical execution-cost studies; no calculated costs',
            historical_limit='REST recent ~720 entries; uncollected older history unavailable without approved archive/source'))
    return dict(contract_version=1,as_of=candles.sql_time(reader.as_of),pairs=rows,financial_algorithms='NOT_IMPLEMENTED')


def cost_inventory(reader):
    rows=0;partitions=0;errors=[]
    for folder in reader.store.root.glob('*/costs_1m'):
        try:
            for month in reader.store.months(folder.parent.name,'costs_1m'):
                rows+=reader.store.read(folder.parent.name,'costs_1m',month).num_rows;partitions+=1
        except Exception:errors.append('invalid_cost_partition')
    return dict(rows=rows,partitions=partitions,errors=errors)


def audit(data_dir,*,pair=None,all_aud=False):
    from .data_pipeline import validate,PipelineConfig
    from .cost_inputs import InputService
    from .cost_algorithms import CostAlgorithms
    from .eligibility_filters import ScreeningAlgorithms
    now=datetime.now(timezone.utc);reader=ResearchData(data_dir,as_of=now);pairs=selected_pairs(reader,pair,all_aud)
    base=validate(data_dir,replace(PipelineConfig(),pair=pair,all_aud=all_aud),now)
    errors=list(base['errors']);tables={}
    with connect_metadata(data_dir,readonly=True) as db:
        for name in ('assets','eligibility','ingestion_state','collection_status','data_quality_issues'):
            tables[name]=dict(rows=db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0],columns=[r[1] for r in db.execute('PRAGMA table_info('+name+')')])
    cost_status=cost_inventory(reader);costs=cost_status['rows'];verified_rows={}
    errors.extend(dict(code=c) for c in cost_status['errors'])
    for name in pairs:
        for dataset in ('prices_1m','quotes_1m','orderbook','costs_1m'):
            try:
                records=reader.store.read_all(name,dataset).to_pylist()
                if dataset=='costs_1m':continue
                if records:
                    start=min(r['timestamp'] for r in records);end=reader.as_of
                    frame=reader._load(dataset,name,start,end)
                    if len(frame)!=len(records):errors.append(dict(pair_id=name,dataset=dataset,code='selected_record_count_mismatch'))
                    verified_rows[name+'|'+dataset]=len(frame)
                else:verified_rows[name+'|'+dataset]=0
            except Exception as error:errors.append(dict(pair_id=name,dataset=dataset,code=error.code if isinstance(error,ResearchDataError) else 'storage_validation_failed'))
    if costs:errors.append(dict(code='unexpected_cost_records',rows=costs))
    manifests=[];service=InputService(data_dir)
    for version in reader.audit.versions('inputs'):
        try:service.load(version);manifests.append(version)
        except Exception:errors.append(dict(code='cost_input_manifest_unavailable',version=version))
    screens=[];manager=EligibilityManager(data_dir)
    for path in (Path(data_dir)/'eligibility_screening/runs').glob('*.json'):
        try:
            ok=manager.verify_evidence(path.stem)['status']=='verified' and manager.load(path.stem)['projection_verification']['status']=='verified'
            if not ok:raise ValueError()
            screens.append(path.stem)
        except Exception:errors.append(dict(code='screening_manifest_unavailable',version=path.stem))
    for cls,names in ((CostAlgorithms,('calculate_spread_cost','calculate_book_execution','calculate_slippage','calculate_market_impact','calculate_total_cost')),
                      (ScreeningAlgorithms,('volume_24h_aud','average_spread','execution_side_depth','order_feasibility','execution_cost'))):
        for method in names:
            try:getattr(cls(),method)(None);errors.append(dict(code='reserved_algorithm_implemented',method=method))
            except NotImplementedError:pass
            except Exception:errors.append(dict(code='unexpected_algorithm_placeholder_behavior',method=method))
    return dict(status='PASS' if not errors and base['integrity']=='ok' and not base['foreign_key_violations'] else 'FAIL',checked_at=candles.sql_time(now),sqlite=tables,integrity=base['integrity'],foreign_key_violations=base['foreign_key_violations'],
        errors=errors,verified_market_rows=verified_rows,cost_rows=costs,cost_status='NOT_IMPLEMENTED_no_calculated_records',m6_manifests=manifests,m8_reports=screens,coverage=base['coverage'])


class ResearchExporter:
    def __init__(self,data_dir):self.data_dir=Path(data_dir);self.root=self.data_dir/'research_exports'
    def path(self,export_id):
        if not isinstance(export_id,str) or not re.fullmatch('[a-f0-9]{64}',export_id):raise ValueError('invalid_export_id')
        target=self.root/export_id
        if self.root.is_symlink() or target.is_symlink():raise ValueError('export_symlinks_prohibited')
        return target
    def _source(self,source):
        name=source['source_id'];path=Path(name)
        allowed=('catalogue_discovery/','ohlcv_collection/responses/','book_collection/responses/','cost_infrastructure/fees/','eligibility_screening/runs/')
        if path.is_absolute() or '..' in path.parts or not name.startswith(allowed):raise ValueError('invalid_source_path')
        target=self.data_dir/path
        if not target.resolve().is_relative_to(self.data_dir.resolve()) or target.is_symlink():raise ValueError('invalid_source_path')
        actual=sha(target) if source.get('hash_kind')=='file_sha256' else digest(json.loads(target.read_text()))
        if actual!=source['source_sha256']:raise ValueError('source_dependency_changed')
    def export(self,*,pair=None,all_aud=False,start=None,end=None,as_of=None):
        reader=ResearchData(self.data_dir,as_of=as_of);pairs=selected_pairs(reader,pair,all_aud)
        if start is None:
            first=[]
            for name in pairs:
                for dataset in DATA_FILES.values():
                    months=reader.store.months(name,dataset)
                    if months:
                        values=reader.store.read(name,dataset,months[0]).to_pylist()
                        if values:first.append(min(r['timestamp'] for r in values))
            start=min(first) if first else candles.minute_start(reader.as_of)-timedelta(days=90)
        start,end=bounds(start,end or reader.as_of)
        if end>reader.as_of:raise ValueError('query_end_exceeds_knowledge_time')
        saved_screen,screen=screening(reader,pairs)
        available=report(self.data_dir,pair=pair,all_aud=all_aud,as_of=reader.as_of)
        self.path('0'*64);self.root.mkdir(parents=True,exist_ok=True)
        stage=Path(tempfile.mkdtemp(prefix='.building-',dir=self.root));writers={};sources={};warnings=[];units={}
        def source(row):
            if row.get('source_id'):
                item={k:row[k] for k in ('source_id','source_sha256')}
                if item['source_id'] in sources and sources[item['source_id']]!=item:raise ValueError('conflicting_source_dependency')
                sources[item['source_id']]=item
        def write(name,data,columns=None):
            if name not in writers:writers[name]=CsvFile(stage/name,columns or data.columns)
            writers[name].append(data.to_dict('records') if isinstance(data,pd.DataFrame) else data)
        try:
            for name in pairs:
                assets=reader.load_market_metadata(name);write('assets.csv',assets)
                for row in assets.to_dict('records'):source(row)
                for filename,dataset in DATA_FILES.items():
                    data=reader._load(dataset,name,start,end);write(filename,data);units[dataset]=data.attrs['units']
                    for ref in data.attrs['sources']:source(ref)
                    warnings.extend(data.attrs['source_warnings'])
                fees=reader.load_fee_schedules(name);write('fee_schedules.csv',fees)
                for row in fees.to_dict('records'):source(row)
            market_columns=['pair_id','screening_report_id','screening_as_of','overall','exchange','data','liquidity','execution','reasons','filter_evidence_json']
            original={m['pair_id']:m for m in saved_screen};market=[]
            for name in pairs:
                item=original.get(name)
                market.append(dict(pair_id=name,screening_report_id=screen['report_id'] if screen else None,screening_as_of=screen['evaluation']['as_of'] if screen else None,
                    overall=item['overall'] if item else 'UNKNOWN',**(item['dimensions'] if item else dict(exchange='UNKNOWN',data='UNKNOWN',liquidity='UNKNOWN',execution='UNKNOWN')),
                    reasons=item['main_blockers']+item['unresolved'] if item else ['no_verified_screening_run_known_as_of'],filter_evidence_json=item['filters'] if item else None))
            write('all_markets.csv',market,market_columns)
            write('eligible.csv',[m for m in market if m['overall']=='ELIGIBLE'],market_columns)
            write('rejected.csv',[m for m in market if m['overall']=='REJECTED'],market_columns)
            pending=[m for m in market if m['overall']=='UNKNOWN' or any(f['status']=='NOT_IMPLEMENTED' for f in m['filter_evidence_json'] or [])]
            write('pending_evaluation.csv',pending,market_columns)
            if screen:
                path=self.data_dir/'eligibility_screening/runs'/(screen['report_id']+'.json')
                sources[str(path.relative_to(self.data_dir)).replace('\\','/')]=dict(source_id=str(path.relative_to(self.data_dir)).replace('\\','/'),source_sha256=sha(path),hash_kind='file_sha256')
            quality=[]
            for name in pairs:quality.extend(reader.quality_issues(name,start,end))
            qcols=['issue_id','pair_id','dataset','observation_at','period_start','period_end','issue_type','description','detected_at','resolution_status_current','resolution_as_of']
            write('data_quality.csv',quality,qcols)
            coverage_columns=list(available['pairs'][0]) if available['pairs'] else ['pair_id']
            write('coverage.csv',available['pairs'],coverage_columns)
            files={name:writer.finish() for name,writer in writers.items()}
            if set(files)!=CSV_FILES:raise ValueError('incomplete_export_files')
            for ref in sources.values():self._source(ref)
            cost_status=cost_inventory(reader)
            if cost_status['rows'] or cost_status['errors']:raise ValueError('unexpected_or_invalid_cost_records')
            body=dict(contract_version=1,pairs=pairs,start=candles.sql_time(start),end_exclusive=candles.sql_time(end),as_of=candles.sql_time(reader.as_of),
                files=files,sources=sorted(sources.values(),key=lambda r:r['source_id']),null_representation=NULL,encoding='UTF-8',timestamps='UTC ISO8601, microseconds; [start,end)',
                units=units,fee_units='Percentage points; full source tiers; account applicability explicit, no tier selected',
                screening_report_id=screen['report_id'] if screen else None,screening_as_of=screen['evaluation']['as_of'] if screen else None,
                screening_semantics='Original M8 decisions, not new screening. Pending mathematical evaluation may overlap rejected.',
                costs_1m=dict(rows=0,algorithm_status='NOT_IMPLEMENTED',calculated_records=0),source_warnings=warnings,availability=available['pairs'],
                selection='Immutable selected rows; later legitimate appends do not invalidate original export. No global SQLite/Parquet snapshot atomicity claim.')
            export_id=digest(body);manifest=dict(export_id=export_id,**body);_atomic_json(stage/'manifest.json',manifest)
            target=self.path(export_id);lock=ProcessLock(self.root/'writer')
            try:
                if target.exists():
                    if json.loads((target/'manifest.json').read_text())!=manifest or any(sha(target/n)!=v['sha256'] for n,v in files.items()):raise ValueError('immutable_export_conflict')
                else:os.replace(stage,target)
            finally:lock.close()
            return dict(export_id=export_id,path=str(target),files=files,as_of=manifest['as_of'])
        finally:
            for writer in writers.values():
                if not writer.handle.closed:writer.handle.close()
            # Only this call's flat, newly-created staging directory is cleaned.
            if stage.exists():
                if stage.resolve().parent!=self.root.resolve() or not stage.name.startswith('.building-'):raise ValueError('unsafe_staging_cleanup')
                for child in stage.iterdir():
                    if child.is_dir():raise ValueError('unexpected_staging_directory')
                    child.unlink()
                stage.rmdir()
    def verify(self,export_id):
        target=self.path(export_id);manifest=json.loads((target/'manifest.json').read_text());body={k:v for k,v in manifest.items() if k!='export_id'}
        if digest(body)!=export_id or manifest['export_id']!=export_id or manifest.get('contract_version')!=1:raise ValueError('export_manifest_corrupt')
        if set(manifest['files'])!=CSV_FILES or {p.name for p in target.iterdir()}!=CSV_FILES|{'manifest.json'}:raise ValueError('unexpected_export_files')
        for name,info in manifest['files'].items():
            path=target/name
            if path.is_symlink() or sha(path)!=info['sha256']:raise ValueError('export_file_corrupt')
            with path.open(encoding='utf-8',newline='') as handle:
                rows=csv.reader(handle);header=next(rows)
                if header!=info['columns'] or sum(1 for _ in rows)!=info['rows']:raise ValueError('export_row_count_or_schema_mismatch')
        for ref in manifest['sources']:self._source(ref)
        reader=ResearchData(self.data_dir,as_of=manifest['as_of']);reader.source_selection={r['source_id'] for r in manifest['sources']}
        for filename,dataset in DATA_FILES.items():
            with (target/filename).open(encoding='utf-8',newline='') as handle:
                for pair,group in groupby(csv.DictReader(handle),lambda r:r['pair_id']):
                    original=list(group);keys={key_text(dataset,r) for r in original}
                    if len(keys)!=len(original):raise ValueError('duplicate_export_logical_key')
                    reader.record_selection={(dataset,pair):keys}
                    current=reader._load(dataset,pair,manifest['start'],manifest['end_exclusive']).to_dict('records')
                    actual={key_text(dataset,r):{k:cell(v) for k,v in r.items()} for r in current}
                    if any(actual.get(key_text(dataset,r))!=r for r in original):raise ValueError('missing_or_changed_parquet_dependency')
        return dict(export_id=export_id,status='PASS',files=len(manifest['files']),rows={n:v['rows'] for n,v in manifest['files'].items()},sources_verified=len(manifest['sources']),costs_1m=manifest['costs_1m'])


def populate(data_dir,*,pair=None,all_aud=False):
    from .data_pipeline import Pipeline,PipelineConfig
    before=report(data_dir,pair=pair,all_aud=all_aud)
    # A deliberate bounded configuration change makes every enabled task due;
    # never edit scheduler state or bypass its writer/recovery locks.
    config=replace(PipelineConfig(),pair=pair,all_aud=all_aud,max_requests_per_cycle=160)
    pipeline=Pipeline(data_dir,config)
    if not all(t['due'] for t in pipeline.dry_run()['tasks'] if t['enabled']):
        config=replace(config,max_requests_per_cycle=161)
    result=Pipeline(data_dir,config).run()
    after=report(data_dir,pair=pair,all_aud=all_aud)
    return dict(status=result['last_cycle']['status'],before=before,after=after,pipeline=result)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=('populate','validate','export','report','verify'))
    group=p.add_mutually_exclusive_group();group.add_argument('--pair');group.add_argument('--all-aud',action='store_true')
    p.add_argument('--start');p.add_argument('--end');p.add_argument('--as-of');p.add_argument('--export-id');p.add_argument('--format',choices=('csv',),default='csv');p.add_argument('--root',default='.');p.add_argument('--data-dir')
    args=p.parse_args(argv)
    try:
        if args.command in ('populate','validate','verify') and any((args.start,args.end,args.as_of)):raise ValueError('temporal_options_only_for_export_or_report')
        if args.command=='report' and (args.start or args.end):raise ValueError('report_uses_explicit_90_day_window')
        directory=load_config(args.root,data_dir=args.data_dir).data_dir;selection=dict(pair=args.pair,all_aud=args.all_aud)
        if args.command=='verify':result=ResearchExporter(directory).verify(args.export_id)
        elif args.command=='populate':result=populate(directory,**selection)
        elif args.command=='validate':result=audit(directory,**selection)
        elif args.command=='report':result=report(directory,as_of=args.as_of,**selection)
        else:result=ResearchExporter(directory).export(start=args.start,end=args.end,as_of=args.as_of,**selection)
        print(json.dumps(payload(result),indent=2));return int(result.get('status') in ('FAIL','partial','error','interrupted'))
    except Exception as error:
        print(json.dumps(dict(error=error.code if isinstance(error,ResearchDataError) else 'research_operation_failed; details suppressed')));return 1


if __name__=='__main__':raise SystemExit(main())
