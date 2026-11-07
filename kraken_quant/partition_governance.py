"""Immutable study registration and conservative holdout-use audit records.

These guards cover this API and stable research identifiers, not arbitrary Python
code or a caller disguising the same experiment with unrelated identifiers.
"""
import json
from pathlib import Path

from .cost_storage import digest
from .historical_population import encoded,immutable,sha
from .operations import ProcessLock
from .partition_contracts import PartitionPlan
from .research_data import instant


def root(data_dir):
    path=Path(data_dir)/'partition_plans/governance'
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('research_governance_symlinks_prohibited')
    return path


def read_record(path):
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('research_governance_symlinks_prohibited')
    doc=json.loads(path.read_bytes());body={k:v for k,v in doc.items() if k!='record_id'}
    if doc.get('record_id')!=sha(encoded(body)):
        raise ValueError('research_governance_record_changed')
    return doc


def write_record(path,body):
    body={**body,'record_id':sha(encoded(body))}
    immutable(path,encoded(body))
    return json.loads(encoded(body))


def frozen_request(reader,request,config):
    """Repeated identical requests reopen the frozen version, never reselect."""
    path=root(reader.data_dir)/'freezes'/(digest(request.identifier)+'.json')
    if not path.exists():return None
    record=read_record(path)
    if record['configuration_sha256']!=digest(config):
        raise ValueError('research_window_frozen_for_identifier; use the registered manifest, not performance-driven reselection')
    from .partition_manifest import read_plan
    doc=read_plan(reader.data_dir,record['manifest_id'])
    if instant(doc['created_at'])>instant(reader.clock()) or instant(record['frozen_at'])>instant(reader.clock()):
        raise ValueError('registered_research_plan_not_yet_known')
    if digest(doc['configuration'])!=record['configuration_sha256']:
        raise ValueError('research_freeze_configuration_mismatch')
    return PartitionPlan('READY',record['manifest_id'],encoded(doc))


def freeze_plan(reader,manifest_id):
    from .partition_manifest import read_plan
    doc=read_plan(reader.data_dir,manifest_id);config=doc['configuration']
    if config['mode']!='research':
        raise ValueError('research_freeze_requires_research_mode')
    if instant(doc['evaluation_cutoff'])>reader.as_of or instant(doc['created_at'])>instant(reader.clock()):
        raise ValueError('research_plan_not_known_at_reader_cutoff')
    area=root(reader.data_dir);area.mkdir(parents=True,exist_ok=True)
    name=digest(config['identifier']);path=area/'freezes'/(name+'.json')
    lock=ProcessLock(area/name)
    try:
        if path.exists():
            record=read_record(path)
            if record['manifest_id']!=manifest_id:
                raise ValueError('research_window_already_frozen_for_identifier')
            return record
        return write_record(path,dict(contract_version=1,identifier=config['identifier'],manifest_id=manifest_id,
            configuration_sha256=digest(config),frozen_at=instant(reader.clock()),
            phase_boundaries=[dict(assets=g['assets'],fold=g['fold'],phases={p:dict(start=v['start'],end_exclusive=v['end_exclusive']) for p,v in g['phases'].items()}) for g in doc['groups']],
            holdout_guarantee='NOT_CERTIFIED_UNTOUCHED',
            rule='No new selection under this research identifier after registration.'))
    finally:lock.close()


def record_use(reader,manifest_id,*,feedback=None,**access):
    if feedback is not None and (not isinstance(feedback,str) or not feedback.strip() or len(feedback)>2000):
        raise ValueError('nonempty_feedback_reason_required')
    from .partition_manifest import read_plan
    doc=read_plan(reader.data_dir,manifest_id)
    if doc['configuration']['mode']=='research':freeze_plan(reader,manifest_id)
    area=root(reader.data_dir);area.mkdir(parents=True,exist_ok=True)
    kind='feedback' if feedback is not None else 'exposures';path=area/kind/(manifest_id+'.json')
    lock=ProcessLock(area/manifest_id)
    try:
        if path.exists():return read_record(path)
        return write_record(path,dict(contract_version=1,manifest_id=manifest_id,
            recorded_at=instant(reader.clock()),status='RESULTS_INFLUENCED_DEVELOPMENT' if feedback is not None else 'EXPOSED_NOT_CERTIFIED_UNTOUCHED',
            feedback_reason=feedback,access=access))
    finally:lock.close()


def governance_status(reader,manifest_id):
    from .partition_manifest import read_plan
    doc=read_plan(reader.data_dir,manifest_id);area=root(reader.data_dir)
    freeze=area/'freezes'/(digest(doc['configuration']['identifier'])+'.json')
    registered=read_record(freeze) if freeze.exists() else None
    feedback=area/'feedback'/(manifest_id+'.json');exposure=area/'exposures'/(manifest_id+'.json')
    use=read_record(feedback) if feedback.exists() else read_record(exposure) if exposure.exists() else None
    return dict(manifest_id=manifest_id,frozen=bool(registered and registered['manifest_id']==manifest_id),
        registered_manifest_id=registered['manifest_id'] if registered else None,
        holdout_status=use['status'] if use else 'NO_USE_RECORDED_NOT_CERTIFIED_UNTOUCHED',
        untouched_holdout_certified=False,
        limitation='External data access, renamed studies and arbitrary strategy code are not inspected.')
