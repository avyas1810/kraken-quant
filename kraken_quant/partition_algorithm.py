"""Shared-calendar, source-pinned partitions with mandatory per-phase admission."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections import deque
import math
import pandas as pd

from .partition_contracts import PartitionRequest, PartitionPlan, RollingConfig, PHASES, ALGORITHM_VERSION
from .market_continuity import ContinuityRequest, ContinuityRequirements, DataPolicy, analyze
from .continuity_sources import archive_windows
from .research_data import ResearchData, ResearchDataError, instant
from .storage_schema import validate_pair_id
from .historical_population import encoded, immutable, sha
from .historical_readiness import row_hash

EPOCH = datetime(1970,1,1,tzinfo=timezone.utc)


def align(value, frequency, up=False):
    steps, remainder = divmod(value-EPOCH, frequency)
    return EPOCH+(steps+int(up and bool(remainder)))*frequency


def canonical(value):
    if not isinstance(value,str):
        raise ValueError('asset_identifiers_must_be_strings')
    value=value.replace('/','-')
    return validate_pair_id(value)


def configuration(request, reader):
    if not isinstance(request,PartitionRequest):
        raise ValueError('typed_partition_request_required')
    assets=tuple(canonical(p) for p in request.assets)
    if not assets or len(set(assets))!=len(assets):
        raise ValueError('distinct_selected_assets_required')
    if not isinstance(request.identifier,str) or not request.identifier.strip():
        raise ValueError('strategy_or_dataset_identifier_required')
    if request.mode not in ('research','production_validation'):
        raise ValueError('unsupported_validation_mode')
    cutoff=instant(request.cutoff)
    if cutoff>reader.as_of or cutoff>instant(reader.clock()):
        raise ValueError('future_evaluation_cutoff_prohibited')
    start=instant(request.start) if request.start is not None else None
    end=instant(request.end) if request.end is not None else cutoff
    if end>cutoff or start is not None and (start>=end or end-start>timedelta(days=3660)):
        raise ValueError('bounded_ordered_search_window_required')
    if not isinstance(request.frequency,timedelta) or request.frequency<=timedelta(0):
        raise ValueError('positive_data_frequency_required')
    if not request.datasets or len(set(request.datasets))!=len(request.datasets) or not set(request.datasets)<={'prices_1m','quotes_1m','orderbook'}:
        raise ValueError('supported_distinct_required_data_types_required')
    if 'prices_1m' in request.datasets and request.frequency%timedelta(minutes=1):
        raise ValueError('price_frequency_requires_whole_minutes')
    if len(request.proportions)!=3 or any(not isinstance(p,Decimal) or not p.is_finite() or p<=0 for p in request.proportions) or sum(request.proportions)!=1:
        raise ValueError('three_positive_exact_proportions_summing_to_one_required')
    if len(request.minimum_observations)!=3 or any(type(n) is not int or n<1 for n in request.minimum_observations):
        raise ValueError('positive_observation_minimum_for_every_phase_required')
    if len(request.minimum_durations)!=3 or any(not isinstance(d,timedelta) or d<=timedelta(0) for d in request.minimum_durations):
        raise ValueError('positive_calendar_duration_for_every_phase_required')
    if not isinstance(request.continuity,ContinuityRequirements):
        raise ValueError('typed_continuity_requirements_required')
    DataPolicy(request.data_policy)
    if request.data_policy==DataPolicy.ASOF_REFERENCE and request.continuity.maximum_staleness is None:
        raise ValueError('asof_policy_requires_explicit_staleness')
    if request.selection_policy not in ('COMMON_CALENDAR','ASSET_INDEPENDENT') or type(request.independent_experiments) is not bool:
        raise ValueError('explicit_partition_selection_policy_required')
    if request.selection_policy=='ASSET_INDEPENDENT' and not request.independent_experiments:
        raise ValueError('independent_model_fitting_declaration_required')
    for name in ('feature_lookback','reference_lookback','label_horizon','purge','embargo'):
        value=getattr(request,name)
        if not isinstance(value,timedelta) or value<timedelta(0) or value%request.frequency:
            raise ValueError(name+'_must_be_nonnegative_whole_frequency_cells')
    if type(request.minimum_context_observations) is not int or request.minimum_context_observations<1:
        raise ValueError('positive_feature_context_minimum_required')
    for name in ('maximum_grid_cells','maximum_records','maximum_candidates'):
        if type(getattr(request,name)) is not int or getattr(request,name)<1:
            raise ValueError('positive_bounded_work_budget_required')
    if request.rolling is not None:
        roll=request.rolling
        if not isinstance(roll,RollingConfig) or len(roll.durations)!=3 or any(not isinstance(d,timedelta) or d<=timedelta(0) or d%request.frequency for d in roll.durations) or not isinstance(roll.step,timedelta) or roll.step<=timedelta(0) or roll.step%request.frequency or type(roll.folds) is not int or not 1<=roll.folds<=32:
            raise ValueError('bounded_positive_aligned_rolling_configuration_required')
        if roll.step<roll.durations[2]:
            raise ValueError('rolling_out_of_sample_windows_must_not_overlap')
    return replace(request,assets=assets,datasets=tuple(request.datasets),cutoff=cutoff,start=start,end=end,data_policy=DataPolicy(request.data_policy))


def config_payload(request):
    def convert(value):
        if isinstance(value,timedelta):return dict(microseconds=value//timedelta(microseconds=1))
        if isinstance(value,dict):return {k:convert(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)):return [convert(v) for v in value]
        return value
    return convert(asdict(request))


def stale_intervals(frame,dataset,start,end,requirements):
    """Candidate boundaries only; M12 independently admits every final phase."""
    limit=requirements.maximum_staleness
    if limit is None:return []
    events={}
    for row in frame.to_dict('records'):
        stamp=instant(row['timestamp'])
        available=stamp+(timedelta(minutes=1) if dataset=='prices_1m' else timedelta(0))
        if requirements.require_received_by_reference:available=max(available,instant(row['source_received_at']))
        events[stamp]=available
    latest=None;cursor=start;bad=[]
    for stamp,available in sorted(events.items(),key=lambda item:(item[1],item[0])):
        if available>start:
            right=min(available,end)
            left=cursor if latest is None else max(cursor,latest+limit)
            if left<right:bad.append((left,right))
            cursor=right
        if available>end:break
        latest=max(latest,stamp) if latest is not None else stamp
    left=cursor if latest is None else max(cursor,latest+limit)
    if left<end:bad.append((left,end))
    return bad


class PartitionAlgorithm:
    def __init__(self,reader):
        self.reader=reader

    def generate_partition_plan(self,request):
        try:
            request=configuration(request,self.reader)
        except (ValueError,TypeError,AttributeError,OverflowError) as error:
            return self._reject('INVALID_CONFIGURATION',[dict(reason=str(error))])
        reader=ResearchData(self.reader.data_dir,as_of=request.cutoff,clock=self.reader.clock)
        try:
            return self._generate(reader,request)
        except ResearchDataError as error:
            return self._reject('SOURCE_UNAVAILABLE',[dict(reason=error.code,details=error.report)])
        except (ValueError,OSError,KeyError) as error:
            return self._reject('SOURCE_UNAVAILABLE',[dict(reason=str(error))])

    @staticmethod
    def _reject(status,reasons):
        return PartitionPlan(status,None,encoded(dict(contract_version=1,algorithm_version=ALGORITHM_VERSION,status=status,
            failures=reasons,executable=False,phases=[],note='No selected asset was dropped and no requirement was relaxed.')))

    def _index(self,reader,request):
        index={};metadata={}
        for asset in request.assets:
            meta=reader.load_market_metadata(asset)
            if meta.empty:raise ResearchDataError('metadata_unknown_as_of',pair_id=asset)
            metadata[asset]=dict(source_id=meta.iloc[0]['source_id'],source_sha256=meta.iloc[0]['source_sha256'])
            for dataset in request.datasets:
                first=last=None
                for month in reader.store.months(asset,dataset):
                    if month>request.cutoff.strftime('%Y-%m'):continue
                    table=reader.store.read(asset,dataset,month)
                    values=[t for t in table['timestamp'].to_pylist() if t+(timedelta(minutes=1) if dataset=='prices_1m' else timedelta(0))<=request.cutoff]
                    if values:first=min(first,min(values)) if first else min(values);last=max(last,max(values)) if last else max(values)
                if first is None:raise ResearchDataError('required_dataset_unavailable',pair_id=asset,dataset=dataset)
                index[(asset,dataset)]=(first,last)
        return index,metadata

    def _generate(self,reader,request):
        index,metadata=self._index(reader,request)
        groups=[request.assets] if request.selection_policy=='COMMON_CALENDAR' else [(p,) for p in request.assets]
        successful=[];all_failures=[];budget=0
        for assets in groups:
            left=max(index[(a,d)][0] for a in assets for d in request.datasets)
            left=align(max(left,request.start) if request.start else left,request.frequency,True)
            left+=request.feature_lookback+request.reference_lookback
            right=min(request.end,min(align(index[(a,d)][1],request.frequency)+request.frequency for a in assets for d in request.datasets))
            right=align(right,request.frequency)
            if left>=right:
                return self._reject('NO_COMMON_WINDOW',[dict(assets=assets,start=left,end=right,reason='Selected assets/data types have no common calendar interval after context requirements.')])
            if right-left>timedelta(days=3660):return self._reject('INVALID_CONFIGURATION',[dict(reason='Narrow the search window to at most 3660 days.')])
            cells=(right-left)//request.frequency
            budget+=cells*len(assets)*len(request.datasets)
            if budget>request.maximum_grid_cells:
                return self._reject('INVALID_CONFIGURATION',[dict(reason='Grid work budget exceeded; narrow start/cutoff or explicitly raise maximum_grid_cells.',required_cells=budget)])
            frames={};sources={};mask=[True]*cells;activity_gaps=[]
            load_start=left-request.feature_lookback-request.reference_lookback
            count=0
            for asset in assets:
                for dataset in request.datasets:
                    # Count selected physical records before materializing frames.
                    for month in reader.store.months(asset,dataset):
                        if not load_start.strftime('%Y-%m')<=month<=right.strftime('%Y-%m'):continue
                        count+=sum(load_start<=t<right for t in reader.store.read(asset,dataset,month)['timestamp'].to_pylist())
                    if count>request.maximum_records:
                        return self._reject('INVALID_CONFIGURATION',[dict(reason='Selected record budget exceeded; narrow the range or explicitly raise maximum_records.',records=count)])
                    key=(asset,dataset);frame=reader._load(dataset,asset,load_start,right)
                    frames[key]=frame
                    windows,errors=archive_windows(reader,asset,dataset,load_start,right)
                    if errors:return self._reject('SOURCE_UNAVAILABLE',[dict(asset=asset,dataset=dataset,reasons=errors)])
                    sources[key]=windows
                    req=ContinuityRequest(tuple(assets),left,right,request.frequency,(dataset,),request.data_policy,
                        replace(request.continuity,minimum_observations=0),request.reference_lookback,request.maximum_grid_cells)
                    grid,report=analyze(self._slice(frame,left-request.reference_lookback,right),req,asset,dataset,request.cutoff,windows)
                    activity_gaps.extend((g['start'],g['end']) for g in report['classified_gap_runs'])
                    allowed={'OBSERVED','VERIFIED_NO_TRADE'} if request.continuity.allow_verified_no_trade else {'OBSERVED'}
                    for i,row in enumerate(grid.to_dict('records')):
                        if request.data_policy==DataPolicy.STRICT_GRID and row['classification'] not in allowed or request.continuity.require_source_complete and not row['source_complete']:
                            mask[i]=False
                    bad=stale_intervals(frame,dataset,left,right,request.continuity)
                    limit=request.continuity.maximum_missing_gap
                    if limit is not None:
                        # Adjacent unknown classes are combined by M12's metric;
                        # combine them here too before deciding candidate cuts.
                        runs=[]
                        for row in grid.to_dict('records'):
                            if row['classification'] not in ('MISSING_OR_UNVERIFIED','SOURCE_OUT_OF_COVERAGE'):continue
                            a,b=row['interval_start'],row['interval_end']
                            if runs and runs[-1][1]==a:runs[-1]=(runs[-1][0],b)
                            else:runs.append((a,b))
                        bad.extend((a,b) for a,b in runs if b-a>limit)
                    for a,b in bad:
                        i=max(0,(a-left)//request.frequency);j=min(cells,math.ceil((b-left)/request.frequency))
                        for n in range(i,j):mask[n]=False
            candidates=[];begin=None
            for i,valid in enumerate(mask+[False]):
                if valid and begin is None:begin=i
                if not valid and begin is not None:candidates.append((left+begin*request.frequency,left+i*request.frequency));begin=None
            candidates.sort(key=lambda p:((p[1]-p[0]),p[1]),reverse=True)
            if not candidates:
                return self._reject('INSUFFICIENT_DATA_QUALITY',[dict(assets=assets,reason='No common source/continuity-valid calendar run.',start=left,end=right)])
            queue=deque(candidates[:request.maximum_candidates]);seen=set(queue)
            exhausted=len(candidates)>request.maximum_candidates
            gaps=sorted(set(activity_gaps),key=lambda p:((p[1]-p[0]),p[1]),reverse=True)
            chosen=None;all_failures=[];attempts=0
            while queue:
                a,b=queue.popleft();attempts+=1
                attempt,failures=self._attempt(reader,request,tuple(assets),a,b,frames,sources)
                if not failures:chosen=attempt;break
                all_failures.extend(failures)
                if any(f['status']=='INSUFFICIENT_HISTORY' for f in failures):continue
                # Intermittent EVENT_TIME data can be usable in a recent suffix
                # even when a distant isolated observation makes the full span
                # fail. Try source-derived gap boundaries; keep the SAME policy,
                # proportions, universe and thresholds for every child window.
                for x,y in gaps:
                    for child in ((y,b),(a,x)):
                        if not a<=child[0]<child[1]<=b or child in seen:continue
                        if len(seen)>=request.maximum_candidates:exhausted=True;continue
                        seen.add(child);queue.append(child)
            if chosen is None:
                if exhausted:return self._reject('INVALID_CONFIGURATION',[dict(reason='Candidate search budget exhausted without a valid plan; narrow the window or explicitly raise maximum_candidates.',attempts=attempts)])
                priority=('INSUFFICIENT_HISTORY','INSUFFICIENT_OBSERVATIONS','INSUFFICIENT_DATA_QUALITY')
                status=next(s for s in priority if any(f['status']==s for f in all_failures))
                if not all(mask) and status=='INSUFFICIENT_HISTORY':
                    status='INSUFFICIENT_DATA_QUALITY'
                return self._reject(status,all_failures)
            successful.extend(chosen)
        body=dict(contract_version=1,algorithm_version=ALGORITHM_VERSION,status='READY',executable=True,
            configuration=config_payload(request),evaluation_cutoff=request.cutoff,created_at=instant(reader.clock()),
            groups=successful,metadata_sources=metadata,
            guarantees=['Shared calendar boundaries within every common-calendar fold.','Every asset/data type/phase validated by M12.','No fitted user model or feature code is inspected.','A plan does not authorize live orders.'])
        raw=encoded(body);ident=sha(raw)
        immutable(reader.data_dir/'partition_plans/v1'/(ident+'.json'),raw)
        return PartitionPlan('READY',ident,raw)

    @staticmethod
    def _slice(frame,start,end):
        return frame.loc[(frame['timestamp']>=start)&(frame['timestamp']<end)].copy()

    def _attempt(self,reader,request,assets,start,end,frames,sources):
        from .partition_manifest import pin_frame
        if request.rolling:
            roll=request.rolling;span=sum(roll.durations,timedelta(0))
            windows=[(end-(roll.folds-1-i)*roll.step-span,end-(roll.folds-1-i)*roll.step) for i in range(roll.folds)]
            if windows[0][0]<start:return [],[dict(status='INSUFFICIENT_HISTORY',assets=assets,reason='Not enough calendar history for every requested rolling fold.',available_start=start,required_start=windows[0][0])]
        else:windows=[(start,end)]
        folds=[];failures=[]
        for fold,(left,right) in enumerate(windows):
            cells=(right-left)//request.frequency
            if request.rolling:
                cuts=[left,left+request.rolling.durations[0],left+sum(request.rolling.durations[:2],timedelta(0)),right]
            else:
                n=int(Decimal(cells)*request.proportions[0]);m=int(Decimal(cells)*request.proportions[1])
                cuts=[left,left+n*request.frequency,left+(n+m)*request.frequency,right]
            phases={}
            for i,phase in enumerate(PHASES):
                a=cuts[i]+(request.embargo if i else timedelta(0))
                b=cuts[i+1]-request.label_horizon-(request.purge if i<2 else timedelta(0))
                duration=max(b-a,timedelta(0));phase_assets={}
                for asset in assets:
                    datasets={}
                    for dataset in request.datasets:
                        key=(asset,dataset);minimum=max(request.minimum_observations[i],request.continuity.minimum_observations)
                        duration_failure=None
                        if duration<request.minimum_durations[i] or a>=b:
                            duration_failure=dict(status='INSUFFICIENT_HISTORY',asset=asset,dataset=dataset,phase=phase,fold=fold,
                                required_seconds=request.minimum_durations[i].total_seconds(),available_seconds=duration.total_seconds(),
                                required_observations=minimum,available_observations=0,reason='Calendar phase too short after lookback/label/purge/embargo constraints.')
                            failures.append(duration_failure)
                            if a>=b:continue
                        req=ContinuityRequest((asset,),a,b,request.frequency,(dataset,),request.data_policy,
                            replace(request.continuity,minimum_observations=minimum),request.reference_lookback,request.maximum_grid_cells)
                        grid,report=analyze(self._slice(frames[key],a-request.reference_lookback,b),req,asset,dataset,request.cutoff,sources[key])
                        if duration_failure is not None:duration_failure['available_observations']=report['observation_count']
                        if not report['passed']:
                            status='INSUFFICIENT_OBSERVATIONS' if report['observation_count']<minimum else 'INSUFFICIENT_DATA_QUALITY'
                            failures.append(dict(status=status,asset=asset,dataset=dataset,phase=phase,fold=fold,
                                required_observations=minimum,available_observations=report['observation_count'],
                                available_start=a,available_end_exclusive=b,available_seconds=duration.total_seconds(),reason=report['failure_reasons']))
                        selected=self._slice(frames[key],a,b)
                        if request.continuity.require_received_by_reference:
                            from .market_continuity import _interval_end
                            times=selected['timestamp']+(timedelta(minutes=1) if dataset=='prices_1m' else timedelta(0))
                            selected=selected.loc[[max(instant(t),instant(r))<=_interval_end(req,instant(s)) for t,r,s in zip(times,selected['source_received_at'],selected['timestamp'])]]
                        context=self._slice(frames[key],a-request.feature_lookback,a)
                        if not context.empty:
                            times=context['timestamp']+(timedelta(minutes=1) if dataset=='prices_1m' else timedelta(0))
                            context=context.loc[[max(instant(t),instant(r))<=a if request.continuity.require_received_by_reference else instant(t)<=a for t,r in zip(times,context['source_received_at'])]]
                        if request.feature_lookback:
                            context_req=replace(req,start=a-request.feature_lookback,end=a,requirements=replace(request.continuity,minimum_observations=request.minimum_context_observations))
                            _,context_report=analyze(self._slice(frames[key],context_req.start-request.reference_lookback,a),context_req,asset,dataset,request.cutoff,sources[key])
                            if not context_report['passed']:failures.append(dict(status='INSUFFICIENT_DATA_QUALITY',asset=asset,dataset=dataset,phase=phase,fold=fold,reason='Feature lookback context fails admission.',details=context_report['failure_reasons']))
                        inputs=self._slice(frames[key],a-request.feature_lookback-request.reference_lookback,b)
                        datasets[dataset]=dict(samples=pin_frame(selected,dataset),context=pin_frame(context,dataset),inputs=pin_frame(inputs,dataset),quality=report)
                    phase_assets[asset]=datasets
                phases[phase]=dict(start=a,end_exclusive=b,context_start=a-request.feature_lookback,
                    label_support_end=b+request.label_horizon,assets=phase_assets)
            folds.append(dict(assets=assets,fold=fold,candidate_start=start,candidate_end_exclusive=end,phases=phases))
        return folds,failures


def generate_partition_plan(request,*,reader=None):
    return PartitionAlgorithm(reader or ResearchData()).generate_partition_plan(request)
