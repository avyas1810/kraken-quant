"""Offline point-in-time screening, immutable evidence, SQLite projections and CSV."""
import argparse
import csv
import hashlib
from datetime import datetime,timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import tempfile

from .asset_catalogue import _atomic_json
from .cost_storage import payload,digest
from .db_environment import load_config
from .eligibility_evidence import PointInTimeEvidence
from .eligibility_filters import (ScreeningPolicy,FilterContext,FilterResult,FILTERS,aggregate,filter_status)
from .metadata_store import connect_metadata
from .ohlcv_collector import read_time,sql_time
from .operations import ProcessLock
from .storage_schema import utc_timestamp,validate_pair_id

SQL_COLUMNS=('pair_id','screened_at','volume_24h_aud','avg_spread_pct','depth_aud','historical_days','completeness_pct','decision','rejection_reasons',
             'volume_24h_aud_status','avg_spread_pct_status','depth_aud_status','historical_days_status','completeness_pct_status')


def sql_projection(report,item):
    history=item['history'];known=bool(history and not history.get('error') and history.get('known_minutes'))
    completeness=history['coverage']['coverage_pct'] if known else None
    reasons=[r['filter_id']+':'+r['status']+':'+r['reason'] for r in item['filters'] if r['reason']]
    return (item['pair_id'],report['as_of'],None,None,None,history['whole_span_days'] if known else None,
        # Existing SQL stores at most 18 fractional digits. Decisions and sidecars
        # retain the original M7 measurement; this is representation only.
        format(Decimal(str(completeness)).quantize(Decimal('0.000000000000000001')),'f') if completeness is not None else None,
        'eligible' if item['overall']=='ELIGIBLE' else 'ineligible' if item['overall']=='REJECTED' else 'unknown',
        json.dumps(reasons,separators=(',',':')),'unavailable','unavailable','unavailable','known' if known else 'unknown','known' if completeness is not None else 'unknown')


class EligibilityManager:
    def __init__(self,data_dir,*,clock=lambda:datetime.now(timezone.utc)):
        self.data_dir=Path(data_dir);self.root=self.data_dir/'eligibility_screening';self.clock=clock

    def evaluate(self,*,as_of=None,policy=ScreeningPolicy(),pair=None,all_aud=False,_history_pins=None):
        as_of=utc_timestamp(as_of or self.clock())
        if as_of>utc_timestamp(self.clock()):raise ValueError('future_as_of_prohibited')
        if (pair is None)==(not all_aud):raise ValueError('select_pair_or_all_aud')
        if pair:validate_pair_id(pair)
        source=PointInTimeEvidence(self.data_dir,as_of,policy,history_pins=_history_pins)
        pairs=source.candidates() if all_aud else [pair]
        items=[]
        for name in pairs:
            metadata,error=source.metadata(name)
            history=source.history(name,metadata)
            inputs=source.raw_inputs(name,metadata)
            if error:inputs['issues'].append(error)
            context=FilterContext(as_of,policy,metadata,history,inputs)
            results=tuple(function(key,context) for key,function in FILTERS.items())
            dimensions,overall=aggregate(results)
            items.append(dict(pair_id=name,identity_known_as_of=metadata is not None,metadata=metadata,history=history,raw_inputs=inputs,
                operational_evidence=source.operational_evidence(name),
                filters=payload(results),dimensions=dimensions,overall=overall,
                main_blockers=[r.filter_id+':'+r.reason for r in results if r.status=='FAIL'],
                unresolved=[r.filter_id+':'+r.reason for r in results if r.status in ('UNKNOWN','NOT_IMPLEMENTED')]))
        return dict(contract_version=1,as_of=sql_time(as_of),policy=payload(policy),policy_hash=digest(policy),selection='all_aud_known_as_of' if all_aud else 'explicit_pair',
            universe_status='known' if pairs else 'UNKNOWN_no_point_in_time_universe_evidence',markets=items,
            summary={status:sum(r['overall']==status for r in items) for status in ('ELIGIBLE','REJECTED','PENDING_EVALUATION','INSUFFICIENT_DATA','UNKNOWN')})

    def _path(self,report_id):
        if len(report_id)!=64 or any(c not in '0123456789abcdef' for c in report_id):raise ValueError('invalid_report_identifier')
        path=self.root/'runs'/(report_id+'.json')
        if any(p.is_symlink() for p in (self.root,path.parent,path)):raise ValueError('screening_symlinks_prohibited')
        return path

    def _validate(self,report):
        ScreeningPolicy.from_dict(report['policy'])
        if report.get('contract_version')!=1 or report['policy_hash']!=digest(report['policy']):
            raise ValueError('screening_contract_or_policy_mismatch')
        for item in report['markets']:
            results=tuple(FilterResult(**{**f,'evidence':tuple(f['evidence'])}) for f in item['filters'])
            for f in results:
                if f.filter_id in ('F04','F05','F06','F10') and (f.status!='NOT_IMPLEMENTED' or f.observed_value is not None):
                    raise ValueError('reserved_filter_cannot_claim_calculation')
                if f.filter_id=='F09' and f.status not in ('UNKNOWN','NOT_IMPLEMENTED'):
                    raise ValueError('order_feasibility_calculation_reserved')
            dimensions,overall=aggregate(results)
            if dimensions!=item['dimensions'] or overall!=item['overall']:
                raise ValueError('screening_aggregation_mismatch')

    def persist(self,report):
        report=payload(report)
        self._validate(report)
        report_id=digest(report);path=self._path(report_id)
        self.root.mkdir(parents=True,exist_ok=True)
        lock=ProcessLock(self.root/'writer')
        try:
            with connect_metadata(self.data_dir) as db:
                db.execute('BEGIN IMMEDIATE')
                conflicts=[];rows=[];missing=[]
                for item in report['markets']:
                    if db.execute('SELECT 1 FROM assets WHERE pair_id=?',(item['pair_id'],)).fetchone() is None:
                        missing.append(item['pair_id']);continue
                    row=sql_projection(report,item)
                    old=db.execute('SELECT '+','.join(SQL_COLUMNS)+' FROM eligibility WHERE pair_id=? AND screened_at=?',row[:2]).fetchone()
                    if old is not None and tuple(old)!=row:conflicts.append(item['pair_id'])
                    rows.append((row,old is not None))
                envelope=dict(contract_version=1,report_id=report_id,evaluated_at=sql_time(self.clock()),evaluation=report,
                    sql_projection=dict(status='conflict' if conflicts else 'compatible',conflicting_pairs=conflicts,missing_asset_pairs=missing))
                if path.exists():
                    old=json.loads(path.read_text())
                    if old['evaluation']!=report:raise ValueError('immutable_screening_conflict')
                    envelope=old
                else:
                    _atomic_json(path,envelope)
                if not conflicts:
                    for row,exists in rows:
                        if not exists:db.execute('INSERT INTO eligibility ('+','.join(SQL_COLUMNS)+') VALUES ('+','.join('?' for _ in row)+')',row)
                # A conflicting policy/result is preserved in its sidecar, never
                # overwrites an existing as-of SQL result, and inserts no batch rows.
            return self.load(report_id)
        finally:
            lock.close()

    def load(self,report_id):
        envelope=json.loads(self._path(report_id).read_text())
        report=envelope['evaluation']
        if envelope.get('contract_version')!=1 or digest(report)!=report_id:
            raise ValueError('screening_evidence_corrupt_or_unsupported')
        self._validate(report)
        mismatches=[];missing=[]
        with connect_metadata(self.data_dir,readonly=True) as db:
            for item in report['markets']:
                row=sql_projection(report,item)
                actual=db.execute('SELECT '+','.join(SQL_COLUMNS)+' FROM eligibility WHERE pair_id=? AND screened_at=?',row[:2]).fetchone()
                if actual is None:missing.append(item['pair_id'])
                elif tuple(actual)!=row:mismatches.append(item['pair_id'])
        return {**envelope,'projection_verification':dict(mismatched_pairs=mismatches,missing_pairs=missing,status='verified' if not mismatches and not missing else 'conflict' if mismatches else 'sidecar_only_or_uncommitted')}

    def report(self,*,as_of=None,pair=None,all_aud=False,report_id=None):
        if report_id:return self.load(report_id)
        found=[]
        for path in (self.root/'runs').glob('*.json'):
            item=self.load(path.stem);evaluation=item['evaluation']
            if as_of and evaluation['as_of']!=sql_time(as_of):continue
            if pair and pair not in {r['pair_id'] for r in evaluation['markets']}:continue
            if all_aud and evaluation['selection']!='all_aud_known_as_of':continue
            if item['projection_verification']['status']=='verified':found.append(item)
        if not found:raise ValueError('no_matching_persisted_screening')
        return max(found,key=lambda v:(v['evaluation']['as_of'],v['evaluated_at'],v['report_id']))

    def verify_evidence(self,report_id):
        stored=self.load(report_id)['evaluation']
        try:
            all_aud=stored['selection']=='all_aud_known_as_of'
            regenerated=self.evaluate(as_of=read_time(stored['as_of']),policy=ScreeningPolicy.from_dict(stored['policy']),
                all_aud=all_aud,pair=None if all_aud else stored['markets'][0]['pair_id'],
                _history_pins={r['pair_id']:r['history'] for r in stored['markets']})
            # Legacy reports already encode their selected timestamps in complete
            # coverage intervals when all known records are inside that window.
            # Recheck that pinned selection, not later additions to old receipts.
            for original,current in zip(stored['markets'],regenerated['markets']):
                if 'selection_intervals' not in original['history']:
                    current['history'].pop('selection_intervals',None)
            actual=digest(regenerated)
            return dict(status='verified' if actual==report_id else 'dependency_changed_or_unavailable',regenerated_report_id=actual)
        except Exception:
            return dict(status='dependency_changed_or_unavailable')

    def export(self,report_id):
        envelope=self.load(report_id)
        if envelope['projection_verification']['status']!='verified':raise ValueError('cannot_export_unverified_sql_projection')
        if self.verify_evidence(report_id)['status']!='verified':raise ValueError('cannot_export_changed_or_missing_evidence')
        report=envelope['evaluation'];rows=[];details=[]
        for item in report['markets']:
            row=dict(report_id=report_id,as_of=report['as_of'],policy_hash=report['policy_hash'],pair_id=item['pair_id'],
                exchange=item['dimensions']['exchange'],data=item['dimensions']['data'],liquidity=item['dimensions']['liquidity'],execution=item['dimensions']['execution'],overall=item['overall'],
                reasons=';'.join(item['main_blockers']+item['unresolved']))
            rows.append((item,row))
            for f in item['filters']:
                details.append(dict(report_id=report_id,as_of=report['as_of'],pair_id=item['pair_id'],filter_id=f['filter_id'],status=f['status'],
                    observed_value=json.dumps(f['observed_value'],sort_keys=True),threshold=json.dumps(f['threshold'],sort_keys=True),evidence=json.dumps(f['evidence']),reason=f['reason']))
        fields=['report_id','as_of','policy_hash','pair_id','exchange','data','liquidity','execution','overall','reasons']
        groups={
            'all_verified_aud_spot.csv':[r for i,r in rows if i['metadata'] and i['metadata']['market_type']=='spot' and i['metadata']['quote_asset']=='AUD'],
            'exchange_eligible.csv':[r for i,r in rows if i['dimensions']['exchange']=='PASS'],
            'rejected.csv':[r for i,r in rows if i['overall']=='REJECTED'],
            'pending_mathematical_evaluation.csv':[r for i,r in rows if any(f['status']=='NOT_IMPLEMENTED' for f in i['filters'])],
            'insufficient_history.csv':[r for i,r in rows if any(f['filter_id'] in ('F07','F08') and f['status'] in ('FAIL','UNKNOWN') for f in i['filters'])],
            'eligible.csv':[r for i,r in rows if i['overall']=='ELIGIBLE'],
            'filter_evidence.csv':details}
        target=self.root/'exports'/report_id;target.mkdir(parents=True,exist_ok=True)
        if any(p.is_symlink() for p in (self.root,self.root/'exports',target)):
            raise ValueError('screening_symlinks_prohibited')
        exported={}
        for name,data in groups.items():
            headers=['report_id','as_of','pair_id','filter_id','status','observed_value','threshold','evidence','reason'] if name=='filter_evidence.csv' else fields
            buffer=io.StringIO(newline='');writer=csv.DictWriter(buffer,fieldnames=headers);writer.writeheader();writer.writerows(data)
            contents=buffer.getvalue().encode('utf-8');path=target/name
            if path.is_symlink():raise ValueError('screening_symlinks_prohibited')
            if path.exists():
                if path.read_bytes()!=contents:raise ValueError('immutable_csv_conflict')
            else:
                fd,temporary=tempfile.mkstemp(prefix='.export-',dir=target)
                try:
                    with os.fdopen(fd,'wb') as handle:handle.write(contents);handle.flush();os.fsync(handle.fileno())
                    os.replace(temporary,path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            exported[name]=dict(path=str(path),rows=len(data),sha256=hashlib.sha256(contents).hexdigest())
        return dict(report_id=report_id,exports=exported)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('evaluate','report','filters','export'))
    parser.add_argument('action',nargs='?',choices=('status',))
    group=parser.add_mutually_exclusive_group();group.add_argument('--pair');group.add_argument('--all-aud',action='store_true')
    parser.add_argument('--as-of',type=read_time);parser.add_argument('--policy');parser.add_argument('--report-id')
    parser.add_argument('--verify-evidence',action='store_true')
    parser.add_argument('--format',choices=('csv',),default='csv');parser.add_argument('--root',default='.');parser.add_argument('--data-dir')
    args=parser.parse_args(argv)
    try:
        if args.command=='filters':
            if args.action!='status':raise ValueError('filters_status_required')
            result=filter_status();code=0
        else:
            config=load_config(args.root,data_dir=args.data_dir);manager=EligibilityManager(config.data_dir)
            if args.command=='evaluate':
                policy=ScreeningPolicy.from_dict(json.loads(Path(args.policy).read_text(),parse_float=Decimal)) if args.policy else ScreeningPolicy()
                report=manager.evaluate(as_of=args.as_of,policy=policy,pair=args.pair,all_aud=args.all_aud)
                result=manager.persist(report)
                code=0 if result['projection_verification']['status']=='verified' and report['markets'] else 2
            else:
                result=manager.report(as_of=args.as_of,pair=args.pair,all_aud=args.all_aud or (args.command=='export' and not args.pair and not args.report_id),report_id=args.report_id)
                code=0 if result['projection_verification']['status']=='verified' else 2
                if args.verify_evidence:
                    result['evidence_verification']=manager.verify_evidence(result['report_id'])
                    if result['evidence_verification']['status']!='verified':code=2
                if args.command=='export':result=manager.export(result['report_id'])
        print(json.dumps(result,indent=2));return code
    except Exception as error:
        safe={'future_as_of_prohibited','select_pair_or_all_aud','invalid_report_identifier','no_matching_persisted_screening','cannot_export_unverified_sql_projection','cannot_export_changed_or_missing_evidence','immutable_csv_conflict','screening_evidence_corrupt_or_unsupported'}
        print(json.dumps({'error':str(error) if isinstance(error,ValueError) and str(error) in safe else 'screening_operation_failed; private details suppressed'}));return 1


if __name__=='__main__':raise SystemExit(main())
