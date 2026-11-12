"""Immutable partition identity pins and guarded, read-only phase access."""
import base64
import json
from pathlib import Path
import re
import zlib
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from .partition_contracts import ALGORITHM_VERSION
from .historical_population import encoded, sha
from .historical_readiness import row_hash
from .research_data import ResearchData, instant, key_text
from .storage_schema import dataset_schema
from .cost_storage import digest


def pin_frame(frame,dataset):
    records=frame[dataset_schema(dataset).names].to_dict('records')
    keys=sorted(key_text(dataset,row) for row in records)
    sources={row['source_id']:row['source_sha256'] for row in frame.to_dict('records')}
    return dict(rows=len(frame),observations=int(frame['timestamp'].nunique()),sha256=row_hash(records),
        keys_zlib_base64=base64.b64encode(zlib.compress(encoded(keys))).decode(),sources=sources)


def read_plan(data_dir,manifest_id):
    if not isinstance(manifest_id,str) or re.fullmatch('[a-f0-9]{64}',manifest_id) is None:
        raise ValueError('invalid_partition_plan_identifier')
    path=Path(data_dir)/'partition_plans/v1'/(manifest_id+'.json')
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('partition_plan_symlinks_prohibited')
    raw=path.read_bytes()
    if sha(raw)!=manifest_id:
        raise ValueError('partition_plan_hash_mismatch')
    doc=json.loads(raw)
    if doc.get('contract_version')!=1 or doc.get('algorithm_version') not in ('common-calendar-v1',ALGORITHM_VERSION) or doc.get('status')!='READY' or doc.get('executable') is not True:
        raise ValueError('validated_ready_partition_plan_required')
    if doc['algorithm_version']==ALGORITHM_VERSION:
        mode=doc['configuration']['window_mode']
        if mode not in ('FIXED_WINDOW','AUTO_WINDOW') or not doc.get('window_selection') or not doc.get('research_governance'):
            raise ValueError('partition_window_governance_missing')
        for audit in doc['window_selection']:
            selected=audit['selected_window'];original=audit['resolved_requested_window']
            if audit['window_mode']!=mode or selected is None or instant(selected['start'])<instant(original['start']) or instant(selected['end_exclusive'])>instant(original['end_exclusive']):
                raise ValueError('partition_selected_window_outside_request')
            if mode=='FIXED_WINDOW' and selected!=original:
                raise ValueError('fixed_partition_window_was_shifted')
    for group in doc['groups']:
        previous=None
        if doc['configuration']['selection_policy']=='COMMON_CALENDAR' and group['assets']!=doc['configuration']['assets']:
            raise ValueError('partition_joint_universe_mismatch')
        for phase in ('training','validation','test'):
            part=group['phases'][phase];start,end=instant(part['start']),instant(part['end_exclusive'])
            if start>=end or end>instant(doc['evaluation_cutoff']) or instant(part['context_start'])>start or previous is not None and previous>start:
                raise ValueError('partition_temporal_boundaries_invalid')
            previous=instant(part['label_support_end'])
    return doc


def duration(value):
    return timedelta(microseconds=value['microseconds'])


def continuity_config(config,minimum):
    from .market_continuity import ContinuityRequirements
    values=dict(config['continuity'])
    if values['minimum_coverage_pct'] is not None:values['minimum_coverage_pct']=Decimal(values['minimum_coverage_pct'])
    for key in ('maximum_missing_gap','maximum_staleness'):
        if values[key] is not None:values[key]=duration(values[key])
    values['minimum_observations']=max(minimum,values['minimum_observations'])
    return ContinuityRequirements(**values)


def verify_source(root,name,expected):
    path=Path(name);target=root/path
    if path.is_absolute() or '..' in path.parts or not target.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in (target,*target.parents)):
        raise ValueError('unsafe_partition_source_reference')
    if digest(json.loads(target.read_bytes()))!=expected:
        raise ValueError('partition_source_changed')


def unpack_keys(pin,maximum_records):
    if type(pin['rows']) is not int or not 0<=pin['rows']<=maximum_records:
        raise ValueError('partition_record_budget_invalid')
    decompressor=zlib.decompressobj()
    raw=decompressor.decompress(base64.b64decode(pin['keys_zlib_base64'],validate=True),pin['rows']*512+2)
    if not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
        raise ValueError('partition_key_payload_invalid')
    keys=json.loads(raw)
    if not isinstance(keys,list) or len(keys)!=pin['rows'] or any(not isinstance(k,str) for k in keys) or len(set(keys))!=len(keys):
        raise ValueError('partition_keys_invalid')
    return set(keys)


def load_plan_phase(reader,manifest_id,phase,asset,*,dataset='prices_1m',fold=0,purpose='evaluate',joint=True,context=False):
    return _load_plan_phase(reader,manifest_id,phase,asset,dataset=dataset,fold=fold,purpose=purpose,joint=joint,context=context,release=True)


def _load_plan_phase(reader,manifest_id,phase,asset,*,dataset='prices_1m',fold=0,purpose='evaluate',joint=True,context=False,release):
    from .partition_algorithm import canonical
    doc=read_plan(reader.data_dir,manifest_id);config=doc['configuration'];asset=canonical(asset)
    if phase not in ('training','validation','test') or dataset not in config['datasets'] or asset not in config['assets'] or type(fold) is not int or fold<0:
        raise ValueError('invalid_partition_phase_selection')
    if purpose not in ('fit','evaluate','features') or purpose=='fit' and phase!='training':
        raise ValueError('fitting_permitted_on_training_only')
    if type(joint) is not bool or type(context) is not bool:
        raise ValueError('explicit_boolean_access_scope_required')
    if config['selection_policy']=='ASSET_INDEPENDENT' and joint:
        raise ValueError('independent_partitions_require_separate_joint_temporal_validation')
    cutoff=instant(doc['evaluation_cutoff'])
    if cutoff>reader.as_of or instant(doc['created_at'])>instant(reader.clock()):
        raise ValueError('partition_plan_or_data_not_known_at_reader_cutoff')
    group=next((g for g in doc['groups'] if asset in g['assets'] and g['fold']==fold),None)
    if group is None:raise ValueError('unknown_partition_fold')
    part=group['phases'][phase];artifacts=part['assets'][asset][dataset]
    pin=artifacts['inputs']
    metadata=doc['metadata_sources'][asset]
    verify_source(reader.data_dir,metadata['source_id'],metadata['source_sha256'])
    coverage_sources=artifacts.get('coverage_sources',{})
    for name,expected in {**pin['sources'],**coverage_sources}.items():verify_source(reader.data_dir,name,expected)
    pinned=ResearchData(reader.data_dir,as_of=cutoff,clock=reader.clock)
    pinned.evidence.receipts=[(r,p) for r,p in pinned.evidence.receipts if p.relative_to(reader.data_dir).as_posix()==metadata['source_id']]
    pinned.source_selection=set(pin['sources']) | set(coverage_sources)
    pinned.record_selection={(dataset,asset):unpack_keys(pin,config['maximum_records'])}
    a,b=instant(part['start']),instant(part['end_exclusive'])
    if b-a<duration(config['minimum_durations'][('training','validation','test').index(phase)]):
        raise ValueError('partition_phase_duration_insufficient')
    reference=duration(config['reference_lookback']);lookback=duration(config['feature_lookback'])
    inputs=pinned._load(dataset,asset,a-reference-lookback,b,archive_first=True)
    if len(inputs)!=pin['rows'] or row_hash(inputs[dataset_schema(dataset).names].to_dict('records'))!=pin['sha256']:
        raise ValueError('partition_records_changed_or_missing')
    # Reuse the same M12 admission logic on pinned inputs, not a stored PASS flag.
    from .market_continuity import ContinuityRequest,analyze,_interval_end
    minimum=config['minimum_observations'][('training','validation','test').index(phase)]
    requirements=continuity_config(config,minimum)
    req=ContinuityRequest((asset,),a,b,duration(config['frequency']),(dataset,),config['data_policy'],requirements,reference,config['maximum_grid_cells'])
    from .continuity_sources import archive_windows
    windows,errors=archive_windows(pinned,asset,dataset,a-reference-lookback,b)
    _,quality=analyze(inputs.loc[inputs['timestamp']>=a-reference],req,asset,dataset,cutoff,windows,errors)
    if not quality['passed']:raise ValueError('partition_phase_no_longer_passes_admission')
    if lookback:
        context_req=replace(req,start=a-lookback,end=a,requirements=continuity_config(config,config['minimum_context_observations']))
        _,context_quality=analyze(inputs.loc[inputs['timestamp']<a],context_req,asset,dataset,cutoff,windows,errors)
        if not context_quality['passed']:raise ValueError('partition_feature_context_no_longer_passes_admission')
    start=part['context_start'] if context else part['start'];end=part['start'] if context else part['end_exclusive']
    result=inputs.loc[(inputs['timestamp']>=instant(start))&(inputs['timestamp']<instant(end))].copy()
    times=result['timestamp']+(timedelta(minutes=1) if dataset=='prices_1m' else timedelta(0))
    if requirements.require_received_by_reference:
        deadlines=[a if context else _interval_end(req,instant(s)) for s in result['timestamp']]
        result=result.loc[[max(instant(t),instant(r))<=d for t,r,d in zip(times,result['source_received_at'],deadlines)]]
    expected=artifacts['context' if context else 'samples']
    if len(result)!=expected['rows'] or row_hash(result[dataset_schema(dataset).names].to_dict('records'))!=expected['sha256']:
        raise ValueError('partition_phase_records_changed')
    from .partition_governance import freeze_plan,record_use,governance_status
    governed=doc['algorithm_version']==ALGORITHM_VERSION
    if governed and release and config['mode']=='research':freeze_plan(reader,manifest_id)
    if governed and release and phase=='test' and not context:
        record_use(reader,manifest_id,phase=phase,asset=asset,dataset=dataset,fold=fold,purpose=purpose)
    governance=governance_status(reader,manifest_id) if governed else dict(frozen=False,holdout_status='LEGACY_USE_HISTORY_UNKNOWN')
    result.attrs.update(partition_plan=manifest_id,phase=phase,fold=fold,purpose=purpose,
        context_only=context,start=start,end_exclusive=end,evaluation_cutoff=doc['evaluation_cutoff'],
        selection_policy=config['selection_policy'],untouched_out_of_sample=False,out_of_sample=phase=='test',
        window_mode=config.get('window_mode','LEGACY_AUTO_WINDOW'),research_window_frozen=governance['frozen'],holdout_status=governance['holdout_status'],
        out_of_sample_scope='this fold only; later walk-forward fits may use earlier observations',
        label_support_end=part['label_support_end'],model_fit_allowed=phase=='training' and purpose=='fit',
        fitting_scope='training only; arbitrary caller model code is not inspected',deployment_authorized=False)
    return result


def validate_plan(reader,manifest_id,*,joint=True):
    """Read-only whole-plan revalidation before a research/validation workflow."""
    doc=read_plan(reader.data_dir,manifest_id);checked=0
    from .partition_governance import governance_status
    governance=governance_status(reader,manifest_id)
    if doc['algorithm_version']==ALGORITHM_VERSION and governance['registered_manifest_id'] not in (None,manifest_id):
        raise ValueError('research_window_already_frozen_for_identifier')
    for group in doc['groups']:
        for phase in ('training','validation','test'):
            for asset in group['assets']:
                for dataset in doc['configuration']['datasets']:
                    _load_plan_phase(reader,manifest_id,phase,asset,dataset=dataset,fold=group['fold'],joint=joint,release=False)
                    checked+=1
    return dict(status='READY',manifest_id=manifest_id,phase_asset_datasets_verified=checked,
        research_window_frozen=governance['frozen'],holdout_status=governance['holdout_status'],
        registration_required_before_research_access=doc['algorithm_version']==ALGORITHM_VERSION and doc['configuration']['mode']=='research' and not governance['frozen'],deployment_authorized=False)
