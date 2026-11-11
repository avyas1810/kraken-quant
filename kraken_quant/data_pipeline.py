"""Small local public-data scheduler. No credentials, trading or financial models."""
import argparse
from dataclasses import dataclass,field,asdict
from datetime import datetime,timedelta,timezone
import json
import math
from pathlib import Path
import time
import uuid
from urllib.error import HTTPError,URLError

from . import asset_catalogue as catalogue,ohlcv_collector as ohlc,book_collector as books
from .asset_catalogue import _atomic_json
from .db_environment import load_config
from .metadata_store import connect_metadata
from .operations import ProcessLock
from .market_data import PUBLIC_REQUEST_POLICY

TASKS=('metadata','ohlcv','books','fees','coverage','eligibility')
DEFAULT_CADENCES=dict(metadata=86400,ohlcv=60,books=60,fees=86400,coverage=86400,eligibility=86400)


@dataclass(frozen=True)
class PipelineConfig:
    cadences:dict=field(default_factory=lambda:dict(DEFAULT_CADENCES))
    enabled:dict=field(default_factory=lambda:{t:t!='eligibility' for t in TASKS})
    pair:str|None=None
    all_aud:bool=True
    public_interval_seconds:float=1.1
    request_attempts:int=2
    backoff_seconds:float=5
    max_backoff_seconds:float=60
    max_requests_per_cycle:int=150
    max_cycle_seconds:float=600
    max_cycle_reports:int=1000
    delay_threshold_seconds:float=30
    ohlcv_health_seconds:float=180
    book_health_seconds:float=180
    metadata_max_age_seconds:float=86400
    history_days:int=90
    book_depth:int=10
    repair_on_start:bool=True

    def __post_init__(self):
        if set(self.cadences)!=set(TASKS) or set(self.enabled)!=set(TASKS):raise ValueError('all_task_keys_required')
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<60 or v>31536000 for v in self.cadences.values()):raise ValueError('invalid_cadence')
        if any(type(v) is not bool for v in self.enabled.values()):raise ValueError('invalid_enabled')
        for name in ('public_interval_seconds','backoff_seconds','max_backoff_seconds','max_cycle_seconds','delay_threshold_seconds','ohlcv_health_seconds','book_health_seconds','metadata_max_age_seconds'):
            v=getattr(self,name)
            if type(v) not in (int,float) or not math.isfinite(v) or v<=0:raise ValueError('invalid_operational_limit')
        if self.public_interval_seconds<1.1 or not self.backoff_seconds<=self.max_backoff_seconds<=300 or self.max_cycle_seconds>3600:raise ValueError('unsafe_request_limits')
        for name,low,high in (('request_attempts',1,3),('max_requests_per_cycle',1,1000),('max_cycle_reports',1,10000),('history_days',1,3660),('book_depth',1,100)):
            if type(getattr(self,name)) is not int or not low<=getattr(self,name)<=high:raise ValueError('invalid_bounded_limit')
        if type(self.repair_on_start) is not bool or type(self.all_aud) is not bool or self.all_aud==bool(self.pair):raise ValueError('select_pair_or_all_aud')
        if self.pair:
            from .storage_schema import validate_pair_id
            validate_pair_id(self.pair)

    @classmethod
    def read(cls,path=None):
        value=json.loads(Path(path).read_text()) if path else {}
        for key,default in (('cadences',DEFAULT_CADENCES),('enabled',{t:t!='eligibility' for t in TASKS})):
            value[key]={**default,**value.get(key,{})}
        return cls(**value)


class PipelineFailure(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


class PublicPolicy:
    """Shared start-slot pacing across processes; per-process bounded retry budgets."""
    def __init__(self,root,config,clock,monotonic,sleep):
        self.root=Path(root);self.config=config;self.clock=clock;self.monotonic=monotonic;self.sleep=sleep
        self.last=None;self.cooldown_until=0;self.events=[];self.total_requests=0;self.reset()

    def reset(self):
        self.events=[];self.total_requests=0;self.deadline=self.monotonic()+self.config.max_cycle_seconds;self.wall_deadline=self.clock()+timedelta(seconds=self.config.max_cycle_seconds)

    def pause(self,seconds):
        if self.clock()+timedelta(seconds=seconds)>self.wall_deadline or self.monotonic()+seconds>self.deadline:raise PipelineFailure('cycle_request_budget_exhausted')
        self.sleep(seconds)

    def _pacing_lock(self):
        while True:
            if self.monotonic()>=self.deadline or self.clock()>=self.wall_deadline:raise PipelineFailure('cycle_request_budget_exhausted')
            self.root.mkdir(parents=True,exist_ok=True)
            try:return ProcessLock(self.root/'public-request-pacing')
            except PermissionError as error:
                if error.filename is not None:raise PipelineFailure('public_pacing_unavailable') from None
                self.pause(.05)

    def _reserve_start(self,limiter):
        lock=self._pacing_lock()
        try:
            prior=json.loads(limiter.read_text()) if limiter.exists() else None
            if prior and prior.get('cooldown_until') and self.clock()<ohlc.read_time(prior['cooldown_until']):raise PipelineFailure('cycle_deferred_after_throttle')
            elapsed=(self.clock()-ohlc.read_time(prior['started_at'])).total_seconds() if prior else float('inf')
            if elapsed<0:raise PipelineFailure('request_clock_regressed')
            self.pause(max(0,self.config.public_interval_seconds-elapsed))
            started=self.clock();self.last=self.monotonic();self.total_requests+=1
            _atomic_json(limiter,dict(started_at=ohlc.sql_time(started)))
            return started
        finally:lock.close()

    def request(self,endpoint,params,fetch):
        if endpoint not in ('OHLC','Depth','AssetPairs','Assets','FeeSchedule','Trades'):raise PipelineFailure('public_endpoint_not_allowed')
        for attempt in range(self.config.request_attempts):
            if self.monotonic()<self.cooldown_until:raise PipelineFailure('cycle_deferred_after_throttle')
            if self.total_requests>=self.config.max_requests_per_cycle or (self.monotonic()>=self.deadline or self.clock()>=self.wall_deadline):raise PipelineFailure('cycle_request_budget_exhausted')
            limiter=self.root/'last_public_request.json'
            started=self._reserve_start(limiter)
            event=dict(endpoint=endpoint,pair=params.get('pair'),attempt=attempt+1,request_started_at=ohlc.sql_time(started),status='interrupted')
            received=False
            retry=False;wait=self.config.backoff_seconds*(2**attempt)
            try:
                result=fetch();received=True
                errors=result.get('error',[]) if isinstance(result,dict) else []
                throttled=any('rate limit' in str(v).lower() or 'throttl' in str(v).lower() for v in errors)
                retry=throttled or any('service:unavailable' in str(v).lower() for v in errors)
                event['status']='rate_limited' if throttled else 'exchange_error' if errors else 'ok'
                if not retry:return result
            except (HTTPError,URLError,TimeoutError,ConnectionError,OSError) as error:
                received=isinstance(error,HTTPError)
                code=getattr(error,'code',None)
                retry=code is None or code==429 or code>=500
                event['status']='rate_limited' if code==429 else 'network_error'
                if isinstance(error,HTTPError) and error.headers:
                    try:wait=max(wait,float(error.headers.get('Retry-After','0')))
                    except ValueError:wait=self.config.max_backoff_seconds+1
                if not retry:raise PipelineFailure('public_request_rejected') from None
            except (ValueError,TypeError):
                event['status']='invalid_public_response'
                raise PipelineFailure('invalid_public_response') from None
            finally:
                event['request_finished_at']=ohlc.sql_time(self.clock())
                event['response_received_at']=event['request_finished_at'] if received else None
                self.events.append(event)
                _atomic_json(self.root/'current-public-requests.json',self.events)
            if event['status']=='rate_limited' and (attempt+1==self.config.request_attempts or wait>self.config.max_backoff_seconds):
                cooldown=max(wait,self.config.max_backoff_seconds)
                self.cooldown_until=self.monotonic()+cooldown
                lock=self._pacing_lock()
                try:
                    prior=json.loads(limiter.read_text())
                    until=self.clock()+timedelta(seconds=cooldown)
                    if prior.get('cooldown_until'):until=max(until,ohlc.read_time(prior['cooldown_until']))
                    _atomic_json(limiter,dict(started_at=prior['started_at'],cooldown_until=ohlc.sql_time(until)))
                finally:lock.close()
            if not retry or attempt+1==self.config.request_attempts:raise PipelineFailure(event['status'])
            if wait>self.config.max_backoff_seconds:raise PipelineFailure('retry_after_exceeds_bound')
            self.pause(wait)


def error_code(error):
    # Never persist raw exception text or request headers.
    if isinstance(error,(PipelineFailure,ohlc.CollectionError)):return error.code
    return 'lock_unavailable' if isinstance(error,PermissionError) else 'operation_failed'


class ExistingTasks:
    def __init__(self,data_dir,config,clock,monotonic,sleep):
        self.data_dir=Path(data_dir);self.config=config;self.clock=clock;self.monotonic=monotonic;self.sleep=sleep
        state_path=self.data_dir/'collection_pipeline/state.json'
        prior=json.loads(state_path.read_text()) if state_path.exists() else {}
        last=prior.get('tasks',{}).get('ohlcv',{}).get('last_success_at')
        self.first_ohlcv=not last or (clock()-ohlc.read_time(last)).total_seconds()>2*config.cadences['ohlcv']

    def selection(self):return dict(pair=self.config.pair,all_aud=self.config.all_aud)

    def assets(self):
        assets,audit,_=ohlc.collection_universe(self.data_dir)
        assets=[a for a in assets if self.config.all_aud or a['pair_id']==self.config.pair]
        if not assets:raise PipelineFailure('no_verified_selected_markets')
        age=(self.clock()-ohlc.read_time(assets[0]['last_seen'])).total_seconds()
        if not 0<=age<=self.config.metadata_max_age_seconds:raise PipelineFailure('catalogue_refresh_required')
        return assets

    def execute(self,task,recovery=False):
        opts=dict(clock=self.clock,sleeper=self.sleep,monotonic=self.monotonic)
        if task=='metadata':
            result=catalogue.discover(self.data_dir,clock=self.clock)
            return dict(status='ok',result=result)
        if task=='ohlcv':
            self.assets()
            result=ohlc.OhlcvCollector(self.data_dir,**opts).collect(**self.selection())
            repair=None
            if self.config.repair_on_start and (self.first_ohlcv or recovery):
                from .history_manager import HistoryManager
                end=ohlc.minute_start(self.clock())
                repair=HistoryManager(self.data_dir,**opts).run(start=end-timedelta(minutes=720),end=end,repair=True,**self.selection())
            if repair and repair['status']=='interrupted':raise KeyboardInterrupt
            self.first_ohlcv=False
            failures=[r for run in (result,repair) if run for r in run['results'] if r['status']=='error']
            return dict(status='partial' if failures else 'ok',result=result,recent_repair=repair,
                errors=[dict(pair_id=r['pair_id'],code=r.get('code','collector_error')) for r in failures],
                history_limitation='repair only recent 720-minute window; older REST gaps remain unavailable, never retried as old-date backfill')
        if task=='books':
            self.assets()
            result=books.BookCollector(self.data_dir,**opts).collect(**self.selection(),duration_seconds=0,quote_interval=self.config.cadences['books'],book_interval=self.config.cadences['books'],depth=self.config.book_depth)
            if result['status']=='interrupted':raise KeyboardInterrupt
            failures=[r for r in result['results'] if r['status'] in ('error','deferred')]
            return dict(status='partial' if failures or result['uncollected_pair_datasets'] else 'ok',result=result,
                errors=[dict(pair_id=r['pair_id'],code=r.get('code','collector_error')) for r in failures])
        if task=='fees':
            self.assets()
            from .fee_data import refresh_fees
            version=refresh_fees(self.data_dir,clock=self.clock,sleeper=self.sleep,readonly=None,account_unavailability='not_requested_pipeline_public_only')
            return dict(status='ok',version=version,account_specific='disabled_public_only')
        if task=='coverage':return validate(self.data_dir,self.config,self.clock())
        if task=='eligibility':
            from .eligibility_manager import EligibilityManager
            manager=EligibilityManager(self.data_dir,clock=self.clock)
            stored=manager.persist(manager.evaluate(**self.selection()))
            return dict(status='ok' if stored['projection_verification']['status']=='verified' else 'error',report_id=stored['report_id'],summary=stored['evaluation']['summary'])
        raise PipelineFailure('unknown_task')


def validate(data_dir,config,now):
    """Existing read-only coverage and exact receipt-backed evidence checks."""
    from .history_coverage import coverage_report
    from .eligibility_evidence import PointInTimeEvidence
    from .eligibility_filters import ScreeningPolicy
    end=ohlc.minute_start(now)
    report=coverage_report(data_dir,start=end-timedelta(days=config.history_days),end=end,now=now,pair=config.pair,all_aud=config.all_aud)
    source=PointInTimeEvidence(data_dir,now,ScreeningPolicy(history_days=config.history_days))
    errors=[]
    for pair in report['pairs']:
        name=pair['pair_id'];metadata,problem=source.metadata(name)
        history=source.history(name,metadata);raw=source.raw_inputs(name,metadata)
        pair['provenance']=dict(metadata_error=problem,history_error=history.get('error'),raw_input_issues=raw['issues'],quote_book_aligned=raw.get('aligned_quote_book'))
        for dataset,stats in pair['datasets'].items():
            check=stats.get('checkpoint_status',stats.get('sampling',{}).get('checkpoint_status'))
            if stats['integrity_unknown'] or stats['invalid_records'] or stats['duplicate_records'] or stats.get('uncommitted_records') or stats.get('out_of_order_stored_rows') or stats.get('source_audit',{}).get('malformed_source_receipts') or check not in ('matches','empty'):
                errors.append(dict(pair_id=name,dataset=dataset,code='storage_integrity_or_checkpoint_failure'))
        if problem or history.get('error'):errors.append(dict(pair_id=name,code=problem or history['error']))
        if 'unreadable_book_provenance' in raw['issues']:errors.append(dict(pair_id=name,code='unreadable_book_provenance'))
        if not raw.get('aligned_quote_book'):errors.append(dict(pair_id=name,code='missing_or_unaligned_quote_book_provenance'))
    with connect_metadata(data_dir,readonly=True) as db:
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0];fk=len(db.execute('PRAGMA foreign_key_check').fetchall())
    return dict(status='ok' if not errors and integrity=='ok' and not fk else 'error',generated_at=ohlc.sql_time(now),integrity=integrity,foreign_key_violations=fk,errors=errors,coverage=report)


class Pipeline:
    def __init__(self,data_dir,config=PipelineConfig(),*,clock=lambda:datetime.now(timezone.utc),monotonic=time.monotonic,sleep=time.sleep,executor=None):
        self.data_dir=Path(data_dir);self.root=self.data_dir/'collection_pipeline';self.config=config
        self.clock=clock;self.monotonic=monotonic;self.sleep=sleep
        with connect_metadata(data_dir,readonly=True):pass
        self.backend=executor or ExistingTasks(data_dir,config,clock,monotonic,sleep).execute
        self.state=self._read_state();self.due={};self.stopping=False
        self.policy=PublicPolicy(self.root,config,clock,monotonic,sleep)
        now=clock();mono=monotonic()
        for task in TASKS:
            saved=self.state['tasks'].get(task,{}) if self.state.get('configuration')==asdict(config) else {}
            target=ohlc.read_time(saved['next_due_at']) if saved.get('next_due_at') else now
            self.due[task]=mono+max(0,(target-now).total_seconds())

    def _read_state(self):
        path=self.root/'state.json'
        value=json.loads(path.read_text()) if path.exists() else dict(version=1,tasks={},active_cycle=None)
        if value.get('version')!=1:raise PipelineFailure('unsupported_pipeline_state')
        return value

    def dry_run(self):
        now=self.clock();mono=self.monotonic()
        return dict(mode='dry-run',at=ohlc.sql_time(now),scope=dict(pair=self.config.pair,all_aud=self.config.all_aud),
            tasks=[dict(task=t,enabled=self.config.enabled[t],due=self.config.enabled[t] and self.due[t]<=mono,next_due_at=ohlc.sql_time(now+timedelta(seconds=self.due[t]-mono))) for t in TASKS],
            interrupted_cycle=self.state.get('active_cycle'),config=asdict(self.config))

    def _save(self):_atomic_json(self.root/'state.json',self.state)

    def _cycle(self):
        self.policy.reset();start=self.monotonic();now=self.clock()
        due=[t for t in TASKS if self.config.enabled[t] and self.due[t]<=start]
        recovery=self.state.get('active_cycle') is not None
        run=dict(version=1,run_id=uuid.uuid4().hex,status='running',started_at=ohlc.sql_time(now),recovered_interrupted_cycle=self.state.get('active_cycle'),config=asdict(self.config),tasks=[])
        path=self.root/'cycles'/(run['run_id']+'.json')
        self.state['active_cycle']=run['run_id'];self._save();_atomic_json(path,run)
        failed_metadata=False;token=PUBLIC_REQUEST_POLICY.set(self.policy)
        try:
            for task in due:
                actual=self.clock();began=self.monotonic();scheduled=actual+timedelta(seconds=self.due[task]-began)
                old=self.state['tasks'].get(task,{})
                entry=dict(task=task,scheduled_at=ohlc.sql_time(scheduled),started_at=ohlc.sql_time(actual),delay_seconds=max(0,began-self.due[task]))
                entry['excessive_delay']=entry['delay_seconds']>self.config.delay_threshold_seconds
                self.state['tasks'][task]={**old,'last_attempt_at':entry['started_at'],'status':'running'}
                self._save();_atomic_json(path,run)
                try:
                    if failed_metadata and task in ('ohlcv','books','fees','eligibility'):raise PipelineFailure('blocked_by_metadata_refresh_failure')
                    outcome=self.backend(task,recovery=recovery)
                    if not isinstance(outcome,dict) or outcome.get('status') not in ('ok','partial','error'):raise PipelineFailure('invalid_task_result')
                    entry.update(outcome)
                except Exception as error:entry.update(status='error',error=error_code(error))
                entry.update(finished_at=ohlc.sql_time(self.clock()),duration_seconds=self.monotonic()-began)
                failed_metadata=failed_metadata or task=='metadata' and entry['status']!='ok'
                period=self.config.cadences[task]
                self.due[task]+=max(1,math.floor((self.monotonic()-self.due[task])/period)+1)*period
                saved=self.state['tasks'][task]
                saved.update(status=entry['status'],last_error=entry.get('error') or (entry.get('errors') or [None])[0],errors=entry.get('errors',[]),last_attempt_finished_at=entry['finished_at'],next_due_at=ohlc.sql_time(self.clock()+timedelta(seconds=self.due[task]-self.monotonic())))
                if entry['status']=='ok':saved['last_success_at']=entry['finished_at']
                run['tasks'].append(entry);self._save();_atomic_json(path,run)
            run['status']='partial' if any(t['status']!='ok' for t in run['tasks']) else 'finished'
        except KeyboardInterrupt:
            self.stopping=True;run['status']='interrupted'
        finally:
            PUBLIC_REQUEST_POLICY.reset(token)
            run.update(finished_at=ohlc.sql_time(self.clock()),duration_seconds=self.monotonic()-start,requests=self.policy.events,
                success_count=sum(t['status']=='ok' for t in run['tasks']),failure_count=sum(t['status']!='ok' for t in run['tasks']))
            _atomic_json(path,run)
            if run['status']!='interrupted':self.state['active_cycle']=None
            self.state['last_cycle']=run['run_id'];self._save()
            try:run['health']=self.status()['pairs']
            except Exception:run['health_error']='offline_health_report_failed'
            run['duration_seconds']=self.monotonic()-start
            _atomic_json(path,run)
            _atomic_json(self.root/'latest.json',run)
            # Only pipeline-owned cycle journals are bounded; raw market data is retained.
            paths=sorted((self.root/'cycles').glob('*.json'),key=lambda p:p.stat().st_mtime)
            for old in paths[:-self.config.max_cycle_reports]:old.unlink()
        return run

    def run(self,*,continuous=False,max_cycles=None,max_seconds=None):
        if self.root.is_symlink() or (self.root/'cycles').is_symlink():raise PipelineFailure('pipeline_output_symlink_prohibited')
        self.root.mkdir(parents=True,exist_ok=True)
        lock=ProcessLock(self.root/'pipeline');results=[];count=0;start=self.monotonic()
        try:
            # Reload under lock: an instance constructed earlier must not replay stale state.
            self.state=self._read_state()
            changed=self.state.get('configuration')!=asdict(self.config)
            previous=self.state.get('configuration',{})
            if previous and (previous.get('pair'),previous.get('all_aud'))!=(self.config.pair,self.config.all_aud):
                self.state['tasks']={}  # Prior scope remains in immutable cycle journals, never applied to a new scope.
            self.state['configuration']=asdict(self.config)
            now=self.clock();mono=self.monotonic()
            for task in TASKS:
                target=self.state['tasks'].get(task,{}).get('next_due_at')
                self.due[task]=mono if changed else mono+max(0,(ohlc.read_time(target)-now).total_seconds()) if target else mono
            while not self.stopping:
                result=self._cycle();results=[result];count+=1
                if not continuous or max_cycles and count>=max_cycles:break
                if max_seconds is not None and self.monotonic()-start>=max_seconds:break
                deadlines=[self.due[t] for t in TASKS if self.config.enabled[t]]
                if not deadlines:break
                while self.monotonic()<min(deadlines):
                    wait=min(1,min(deadlines)-self.monotonic())
                    if max_seconds is not None:wait=min(wait,max(0,max_seconds-(self.monotonic()-start)))
                    if wait<=0:break
                    self.sleep(wait)
                if max_seconds is not None and self.monotonic()-start>=max_seconds:break
        except KeyboardInterrupt:self.stopping=True
        finally:lock.close()
        return dict(status='stopped' if self.stopping else 'finished',cycles=count,last_cycle=results[-1] if results else None)

    def status(self):
        now=self.clock();prices=ohlc.coverage_report(self.data_dir,now=now);book=books.report(self.data_dir,now=now)
        by_book={(r['pair_id'],r['dataset']):r for r in book['pairs']};pairs=[]
        state=self._read_state()
        for row in prices['pairs']:
            pair=row['pair_id']
            if not self.config.all_aud and pair!=self.config.pair:continue
            quote=by_book.get((pair,'quotes_1m'),{});depth=by_book.get((pair,'orderbook'),{})
            latest=row.get('latest_candle');age=(now-ohlc.read_time(latest)).total_seconds() if latest else None
            errors=list(row.get('collection_errors_last_attempt',[]))
            for task,setting in state['tasks'].items():
                if setting.get('status') in ('error','partial','running'):
                    own=[r['code'] for r in setting.get('errors',[]) if r.get('pair_id')==pair]
                    errors.extend(own or ([task+':'+str(setting.get('last_error') or setting['status'])] if not setting.get('errors') else []))
            for name,r in (('quotes_1m',quote),('orderbook',depth)):
                if r.get('checkpoint_status') not in ('matches','empty'):errors.append(name+':checkpoint_or_storage_failure')
            if any(r.get('age_seconds',0) is not None and r.get('age_seconds',0)<0 for r in (quote,depth)):errors.append('future_snapshot_timestamp')
            health='failing' if errors or row['checkpoint_status'] not in ('matches','empty') else 'unavailable' if age is None or depth.get('age_seconds') is None or quote.get('age_seconds') is None else 'delayed' if age>self.config.ohlcv_health_seconds or max(depth['age_seconds'],quote['age_seconds'])>self.config.book_health_seconds else 'healthy'
            pairs.append(dict(pair_id=pair,last_ohlcv=latest,last_quote=quote.get('latest'),last_book=depth.get('latest'),ohlcv_age_seconds=age,book_age_seconds=depth.get('age_seconds'),quote_age_seconds=quote.get('age_seconds'),health=health,last_errors=errors,
                stored_rows=row.get('stored_rows'),quote_observations=quote.get('observations'),book_snapshots=depth.get('observations'),missing_minutes=row.get('missing_minutes'),historical_coverage_days=row.get('historical_coverage_days'),
                checkpoint=row.get('checkpoint'),checkpoint_status=row.get('checkpoint_status'),quote_checkpoint_status=quote.get('checkpoint_status'),book_checkpoint_status=depth.get('checkpoint_status'),
                timing='OHLCV exchange candle-open; quote/book local snapshot observation, not certified exchange event time'))
        state=self._read_state()
        latest_path=self.root/'latest.json';latest=json.loads(latest_path.read_text()) if latest_path.exists() else None
        return dict(at=ohlc.sql_time(now),tasks=state['tasks'],interrupted_cycle=state.get('active_cycle'),pairs=pairs,last_cycle=latest,
            history_limitation='Kraken OHLC only most recent ~720 entries; newly accumulated data does not imply 90-day completeness')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('once','run','status','dry-run','validate'))
    group=parser.add_mutually_exclusive_group();group.add_argument('--pair');group.add_argument('--all-aud',action='store_true')
    parser.add_argument('--config');parser.add_argument('--root',default='.');parser.add_argument('--data-dir')
    parser.add_argument('--max-cycles',type=int);parser.add_argument('--duration-seconds',type=float)
    args=parser.parse_args(argv)
    try:
        from dataclasses import replace
        config=PipelineConfig.read(args.config)
        if args.pair or args.all_aud:config=replace(config,pair=args.pair,all_aud=args.all_aud)
        for v in (args.max_cycles,args.duration_seconds):
            if v is not None and (not math.isfinite(v) or v<=0):raise PipelineFailure('invalid_session_bound')
        directory=load_config(args.root,data_dir=args.data_dir).data_dir
        pipeline=Pipeline(directory,config)
        if args.mode=='dry-run':result=pipeline.dry_run()
        elif args.mode=='status':result=pipeline.status()
        elif args.mode=='validate':result=validate(directory,config,pipeline.clock())
        else:result=pipeline.run(continuous=args.mode=='run',max_cycles=args.max_cycles,max_seconds=args.duration_seconds)
        print(json.dumps(result,indent=2))
        return int(result.get('status') in ('error','partial') or bool(result.get('last_cycle') and result['last_cycle']['status'] not in ('finished',)))
    except Exception as error:
        print(json.dumps(dict(error=error_code(error))));return 1


if __name__=='__main__':raise SystemExit(main())
