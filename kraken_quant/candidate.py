"""Hash-sealed candidate preregistration. Never releases the inspected locked test."""
from pathlib import Path
import hashlib
import json
import math
from datetime import datetime, timezone
import pandas as pd
from .data import canonical_json

REQUIREMENTS = {'minimum_sharpe': 0.5, 'maximum_drawdown': 0.15, 'daily_decisions': 30, 'order_lifecycles': 20, 'minimum_positive_assets': 3, 'minimum_neighbour_parameters_positive': 2, 'minimum_round_trips': 30, 'minimum_completion_fraction': 0.995, 'maximum_fault_events': 0, 'maximum_missing_days': 0, 'bootstrap_confidence': 0.95, 'minimum_elapsed_seconds': 29*86400, 'positive_net_and_benchmark_relative_return': True, 'nonnegative_doubled_spread_slippage_return': True, 'positive_bootstrap_mean_excess_lower_bound': True}

def freeze_candidate(path, specification):
    required = {'strategy', 'parameters', 'features', 'costs', 'risk_limits', 'input_hashes', 'code_revision', 'prospective_start', 'universe'}
    if not required <= specification.keys() or not specification['input_hashes'] or not specification['code_revision']:
        raise ValueError('Complete specification and input/code hashes required')
    if specification.get('evaluation_split', 'train') not in ('train', 'validation'):
        raise PermissionError('Inspected test cannot be an untouched candidate evaluation')
    if any(not isinstance(v,str) or len(v) != 64 or any(c not in '0123456789abcdef' for c in v) for v in specification['input_hashes'].values()):
        raise ValueError('SHA256 input identities required')
    prospective = pd.Timestamp(specification['prospective_start'])
    frozen_at = datetime.now(timezone.utc).isoformat()
    if prospective.tzinfo is None or prospective <= pd.Timestamp(frozen_at):
        raise ValueError('Prospective evaluation must start after preregistration')
    body = dict(specification, frozen_at=frozen_at, acceptance_rules=REQUIREMENTS, test_access='locked', protocol='Prospective observations after preregistration only; no retrospective tuning')
    payload = canonical_json(body)
    record = {'sha256': hashlib.sha256(payload).hexdigest(), 'specification': body}
    with Path(path).open('xb') as stream:
        stream.write(canonical_json(record))
    return record

def read_candidate(path):
    record = json.loads(Path(path).read_bytes())
    required = {'strategy','parameters','features','costs','risk_limits','input_hashes','code_revision','prospective_start','universe','frozen_at','test_access'}
    spec = record.get('specification',{})
    if not required <= spec.keys() or not spec['universe'] or not spec['input_hashes'] or spec['test_access'] != 'locked':
        raise ValueError('Incomplete frozen candidate')
    frozen, prospective = pd.Timestamp(spec['frozen_at']), pd.Timestamp(spec['prospective_start'])
    if frozen.tzinfo is None or prospective.tzinfo is None or prospective <= frozen:
        raise ValueError('Invalid preregistration chronology')
    if hashlib.sha256(canonical_json(record['specification'])).hexdigest() != record['sha256'] or record['specification']['acceptance_rules'] != REQUIREMENTS:
        raise ValueError('Frozen candidate altered')
    return record

def qualify_candidate(path, evidence):
    record = read_candidate(path)
    reasons = []
    if record['specification'].get('research_only') is True or 'SYNTHETIC' in record['specification'].get('data_kind','').upper(): reasons.append('Research-only or synthetic tutorial cannot qualify live')
    for key in ('actual_daily_decisions','simulated_order_lifecycles'):
        if isinstance(evidence.get(key),bool) or not isinstance(evidence.get(key),int) or evidence[key] < 0: reasons.append('Missing nonnegative integer '+key)
    for key in ('sharpe', 'max_drawdown'):
        if isinstance(evidence.get(key),bool) or not isinstance(evidence.get(key), (int,float)) or not math.isfinite(evidence[key]):
            reasons.append('Missing finite ' + key)
    if evidence.get('sharpe', -math.inf) < REQUIREMENTS['minimum_sharpe']: reasons.append('Sharpe below threshold')
    if evidence.get('max_drawdown', math.inf) > REQUIREMENTS['maximum_drawdown']: reasons.append('Drawdown above threshold')
    dates = evidence.get('decision_dates_utc', [])
    try:
        stamps = pd.DatetimeIndex(dates)
        valid_dates = len(stamps) >= 30 and str(stamps.tz) == 'UTC' and stamps.is_unique and stamps.is_monotonic_increasing and (stamps >= pd.Timestamp(record['specification']['prospective_start'])).all() and (stamps <= pd.Timestamp.now(tz='UTC')).all() and len(set(stamps.normalize())) == len(stamps)
    except (ValueError,TypeError):
        valid_dates = False
    if valid_dates and ((stamps.normalize()[1:] - stamps.normalize()[:-1]) != pd.Timedelta(days=1)).any(): valid_dates = False
    if valid_dates and (stamps[-1] - stamps[0]).total_seconds() < REQUIREMENTS['minimum_elapsed_seconds']: valid_dates = False
    if valid_dates and evidence.get('actual_daily_decisions') != len(stamps): reasons.append('Actual daily count disagrees with timestamp records')
    if not valid_dates: reasons.append('Missing unique actual daily decision timestamps after preregistration')
    lifecycle_ids = evidence.get('completed_lifecycle_ids', [])
    if not isinstance(lifecycle_ids,list) or any(not isinstance(x,str) or not x for x in lifecycle_ids) or len(set(lifecycle_ids)) != len(lifecycle_ids) or len(lifecycle_ids) < 20: reasons.append('Missing20 distinct completed lifecycle ids')
    if isinstance(lifecycle_ids,list) and evidence.get('simulated_order_lifecycles') != len(lifecycle_ids): reasons.append('Lifecycle count disagrees with completed ids')
    if evidence.get('max_drawdown', 0) < 0: reasons.append('Drawdown must be a nonnegative magnitude')
    if evidence.get('actual_daily_decisions', 0) < 30: reasons.append('Fewer than 30 actual daily decisions')
    if evidence.get('simulated_order_lifecycles', 0) < 20: reasons.append('Fewer than 20 order lifecycles')
    if evidence.get('candidate_sha256') != record['sha256']: reasons.append('Evidence candidate identity mismatch')
    if evidence.get('source') != 'prospective_public_feed': reasons.append('Replay is not prospective evidence')
    for key, limit, strict in [('net_return',0,True),('benchmark_relative_return',0,True),('positive_assets',3,False),('neighbour_parameters_positive',2,False),('round_trips',30,False),('completion_fraction',.995,False),('doubled_spread_slippage_return',0,False),('bootstrap_mean_excess_ci_low',0,True),('elapsed_seconds',29*86400,False)]:
        value = evidence.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or (value <= limit if strict else value < limit): reasons.append('Missing or failed ' + key)
    if isinstance(evidence.get('completion_fraction'), (int,float)) and evidence['completion_fraction'] > 1: reasons.append('Invalid completion_fraction')
    if isinstance(evidence.get('positive_assets'), (int,float)) and evidence['positive_assets'] > len(record['specification']['universe']): reasons.append('Positive assets exceed frozen universe')
    if evidence.get('bootstrap_confidence') != .95: reasons.append('95percent bootstrap confidence required')
    if evidence.get('fault_events') != 0 or evidence.get('missing_days') != 0: reasons.append('Fault or missing-day gate failed')
    if evidence.get('reliability_passed') is not True: reasons.append('Reliability/fault criteria not passed')
    if evidence.get('independent_review_passed') is not True: reasons.append('Independent review not passed')
    return {'candidate_sha256': record['sha256'], 'qualified_for_review': not reasons, 'live_authorized': False, 'reasons': reasons}
