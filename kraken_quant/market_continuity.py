"""Read-only continuity and event-time admission, never a price-filling layer."""
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import Enum
import zipfile
import re

import pandas as pd

from .research_data import bounds, instant, ResearchDataError
from .storage_schema import validate_pair_id


class DataPolicy(str, Enum):
    EVENT_TIME = 'EVENT_TIME'
    STRICT_GRID = 'STRICT_GRID'
    ASOF_REFERENCE = 'ASOF_REFERENCE'


@dataclass(frozen=True)
class ContinuityRequirements:
    minimum_observations: int = 1
    minimum_coverage_pct: Decimal | None = None
    maximum_missing_gap: timedelta | None = None
    maximum_staleness: timedelta | None = None
    require_source_complete: bool = False
    allow_verified_no_trade: bool = False
    # Historical replay and information actually received at each decision are
    # distinct contracts. The latter is appropriate for future online admission.
    require_received_by_reference: bool = False

    def __post_init__(self):
        if type(self.minimum_observations) is not int or self.minimum_observations < 0:
            raise ValueError('nonnegative_integer_observation_requirement_required')
        value = self.minimum_coverage_pct
        if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 100):
            raise ValueError('explicit_decimal_coverage_requirement_required')
        for value in (self.maximum_missing_gap, self.maximum_staleness):
            if value is not None and (not isinstance(value, timedelta) or value < timedelta(0)):
                raise ValueError('nonnegative_duration_required')
        for value in (self.require_source_complete, self.allow_verified_no_trade, self.require_received_by_reference):
            if type(value) is not bool:
                raise ValueError('boolean_requirement_required')


@dataclass(frozen=True)
class ContinuityRequest:
    assets: tuple[str, ...]
    start: object
    end: object
    frequency: timedelta = timedelta(minutes=1)
    datasets: tuple[str, ...] = ('prices_1m',)
    policy: DataPolicy = DataPolicy.EVENT_TIME
    requirements: ContinuityRequirements = field(default_factory=ContinuityRequirements)
    reference_lookback: timedelta = timedelta(0)
    maximum_grid_cells: int = 2_000_000

    def __post_init__(self):
        start, end = bounds(self.start, self.end)
        object.__setattr__(self, 'start', start)
        object.__setattr__(self, 'end', end)
        object.__setattr__(self, 'policy', DataPolicy(self.policy))
        if not isinstance(self.assets, tuple) or not self.assets or len(set(self.assets)) != len(self.assets):
            raise ValueError('distinct_selected_assets_required')
        for asset in self.assets:
            validate_pair_id(asset)
        if not isinstance(self.datasets, tuple) or not self.datasets or len(set(self.datasets)) != len(self.datasets) or not set(self.datasets) <= {'prices_1m', 'quotes_1m', 'orderbook'}:
            raise ValueError('supported_distinct_data_types_required')
        if not isinstance(self.requirements, ContinuityRequirements):
            raise ValueError('typed_continuity_requirements_required')
        if not isinstance(self.frequency, timedelta) or self.frequency <= timedelta(0) or (end-start) % self.frequency:
            raise ValueError('positive_frequency_must_exactly_partition_window')
        if 'prices_1m' in self.datasets and (self.frequency % timedelta(minutes=1) or start.second or start.microsecond):
            raise ValueError('prices_require_whole_minute_grid')
        if not isinstance(self.reference_lookback, timedelta) or self.reference_lookback < timedelta(0) or self.reference_lookback > timedelta(days=3660):
            raise ValueError('bounded_nonnegative_reference_lookback_required')
        cells = (end-start)//self.frequency * len(self.assets) * len(self.datasets)
        if type(self.maximum_grid_cells) is not int or not 0 < cells <= self.maximum_grid_cells:
            raise ValueError('continuity_grid_budget_exceeded')
        if self.policy == DataPolicy.ASOF_REFERENCE and self.requirements.maximum_staleness is None:
            raise ValueError('asof_reference_requires_explicit_staleness_limit')


@dataclass(frozen=True)
class SourceWindow:
    """Validated coverage assertion supplied by a trusted source adapter.

    observation_times includes rejected source rows too: an absent local row
    whose timestamp exists in the source can NEVER become VERIFIED_NO_TRADE.
    A domain describes only this selected source, not global exchange history.
    """
    start: object
    end: object
    known_at: object
    source_id: str
    source_hash: str
    observation_times: tuple = ()
    complete: bool = False
    no_trade_semantics: bool = False
    domain_start: object | None = None
    domain_end: object | None = None

    def __post_init__(self):
        left, right = bounds(self.start, self.end)
        object.__setattr__(self, 'start', left)
        object.__setattr__(self, 'end', right)
        object.__setattr__(self, 'known_at', instant(self.known_at))
        object.__setattr__(self, 'observation_times', tuple(sorted(set(instant(t) for t in self.observation_times))))
        if not self.source_id or len(self.source_hash) != 64 or any(c not in '0123456789abcdef' for c in self.source_hash):
            raise ValueError('source_identity_and_sha256_required')
        if (self.domain_start is None) != (self.domain_end is None):
            raise ValueError('both_source_domain_bounds_required')
        if self.domain_start is not None:
            a, b = instant(self.domain_start), instant(self.domain_end)
            if a >= b:
                raise ValueError('ordered_source_domain_required')
            object.__setattr__(self, 'domain_start', a)
            object.__setattr__(self, 'domain_end', b)


@dataclass
class ContinuityResult:
    request: ContinuityRequest
    observations: dict
    intervals: dict
    reports: tuple[dict, ...]

    @property
    def passed(self):
        return all(row['passed'] for row in self.reports)

    @property
    def data(self):
        """Policy-selected output: references are never mixed into raw candles."""
        if self.request.policy == DataPolicy.ASOF_REFERENCE:
            return self.intervals
        if self.request.requirements.require_received_by_reference:
            return {key:frame.loc[frame['interval_admitted']].copy() for key,frame in self.observations.items()}
        return self.observations


class ContinuityRejected(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__('continuity_requirements_failed: ' + '; '.join(
            f"{r['asset']}/{r['dataset']}: {', '.join(r['failure_reasons'])}"
            for r in result.reports if not r['passed']))


def _runs(rows, frequency):
    runs = []
    for row in rows:
        if row['classification'] == 'OBSERVED':
            continue
        if runs and runs[-1]['end'] == row['interval_start'] and runs[-1]['classification'] == row['classification']:
            runs[-1]['end'] = row['interval_end']
        else:
            runs.append(dict(start=row['interval_start'], end=row['interval_end'], classification=row['classification']))
    return runs


def _covers(windows, start, end):
    cursor = start
    for source in sorted(windows, key=lambda s: s.start):
        if source.start > cursor:
            break
        cursor = max(cursor, source.end)
        if cursor >= end:
            return True
    return False


def _interval_end(request, stamp):
    """Deadline of the requested event-time cell, not the whole query window."""
    return request.start + ((stamp-request.start)//request.frequency+1)*request.frequency


def analyze(frame, request, asset, dataset, as_of, sources=(), source_errors=()):
    """Pure admission logic. Input frame is genuine observations, never modified."""
    as_of = instant(as_of)
    if request.end > as_of:
        raise ValueError('query_end_exceeds_knowledge_time')
    sources = tuple(s for s in sources if s.known_at <= as_of)
    groups = {}
    for row in frame.to_dict('records'):
        stamp = instant(row['timestamp'])
        if row['pair_id'] != asset or stamp >= request.end:
            raise ValueError('unexpected_asset_or_future_observation')
        event_available = stamp + timedelta(minutes=1) if dataset == 'prices_1m' else stamp
        available = event_available
        received = instant(row['source_received_at'])
        if received > as_of or available > as_of:
            raise ValueError('future_or_uncommitted_observation')
        if dataset == 'prices_1m' and row.get('is_complete') is not True:
            raise ValueError('incomplete_candle')
        if request.requirements.require_received_by_reference:
            available = max(available, received)
        groups.setdefault(stamp, dict(stamp=stamp, available=available, received=received,
            event_available=event_available, row=row))
    ordered = sorted(groups.values(), key=lambda v: (v['available'], v['stamp']))
    # Last source event known by decision time, not last response receipt.
    references = []; latest = None
    for item in ordered:
        if latest is None or item['stamp'] > latest['stamp']:
            latest = item
        references.append(latest)
    availability = [v['available'] for v in ordered]
    stamps = sorted(groups)
    source_in_window = [t for t in stamps if request.start <= t < request.end]
    in_window = [t for t in source_in_window if groups[t]['available'] <= _interval_end(request,t)]
    admitted = set(in_window)
    rows = []; point = request.start
    while point < request.end:
        end = point + request.frequency
        # A bar opening in a cell is observed only after its full minute ends.
        source_observed = stamps[bisect_left(stamps, point):bisect_left(stamps, end)]
        observed = [t for t in source_observed if t in admitted]
        visible = [s for s in sources if not request.requirements.require_received_by_reference or s.known_at <= end]
        covered = [s for s in visible if s.complete and s.start < end and s.end > point]
        source_complete = _covers(covered, point, end)
        no_trade_complete = _covers([s for s in covered if s.no_trade_semantics], point, end)
        expected_source = any(bisect_left(s.observation_times, end) > bisect_left(s.observation_times, point) for s in covered)
        if observed:
            classification = 'OBSERVED'
        elif no_trade_complete and not expected_source:
            classification = 'VERIFIED_NO_TRADE'
        elif visible and all(s.domain_start is not None and (end <= s.domain_start or point >= s.domain_end) for s in visible):
            classification = 'SOURCE_OUT_OF_COVERAGE'
        else:
            classification = 'MISSING_OR_UNVERIFIED'
        pos = bisect_right(availability, point)-1
        last = references[pos] if pos >= 0 else None
        last_row = last['row'] if last else {}
        stale = point-last['stamp'] if last else None
        rows.append(dict(interval_start=point, interval_end=end, classification=classification,
            source_complete=source_complete, coverage_evidence=tuple(s.source_id for s in covered),
            observation_count=len(observed), reference_time=point,
            source_event_observation_count=len(source_observed),
            last_observation_at=last['stamp'] if last else None,
            observation_available_at=last['available'] if last else None,
            staleness_seconds=stale.total_seconds() if stale is not None else None,
            reference_close=last_row.get('close'), reference_bid=last_row.get('bid'), reference_ask=last_row.get('ask'),
            reference_source_id=last_row.get('source_id'), reference_source_sha256=last_row.get('source_sha256'),
            reference_source_received_at=last_row.get('source_received_at'), executable=False))
        point = end
    counts = dict(Counter(r['classification'] for r in rows))
    runs = _runs(rows, request.frequency)
    unobserved_runs = []
    for row in rows:
        if row['classification'] == 'OBSERVED':
            continue
        if unobserved_runs and unobserved_runs[-1][1] == row['interval_start']:
            unobserved_runs[-1] = (unobserved_runs[-1][0], row['interval_end'])
        else:
            unobserved_runs.append((row['interval_start'], row['interval_end']))
    max_gap = max(((b-a).total_seconds() for a,b in unobserved_runs), default=0)
    missing_runs = []
    for row in rows:
        if row['classification'] not in ('MISSING_OR_UNVERIFIED', 'SOURCE_OUT_OF_COVERAGE'):
            continue
        if missing_runs and missing_runs[-1][1] == row['interval_start']:
            missing_runs[-1] = (missing_runs[-1][0], row['interval_end'])
        else:
            missing_runs.append((row['interval_start'], row['interval_end']))
    missing_gap = max(((b-a).total_seconds() for a,b in missing_runs), default=0)
    stale_values = [r['staleness_seconds'] for r in rows if r['staleness_seconds'] is not None]
    endpoint = bisect_right(availability, request.end)-1
    terminal = references[endpoint] if endpoint >= 0 else None
    if terminal:
        stale_values.append((request.end-terminal['stamp']).total_seconds())
    decision_stale = max(stale_values, default=None)
    # Conservative supremum between arrivals, not just ages at sampled grid
    # decisions. Otherwise a long gap hidden inside a coarse cell could pass.
    for i, item in enumerate(ordered):
        left = max(request.start, item['available'])
        right = min(request.end, ordered[i+1]['available'] if i+1 < len(ordered) else request.end)
        if left < right:
            stale_values.append((right-references[i]['stamp']).total_seconds())
    max_stale = max(stale_values, default=None)
    reference_missing = sum(r['last_observation_at'] is None for r in rows)
    observed_pct = 100 * counts.get('OBSERVED', 0) / len(rows)
    requirements = request.requirements; failures = list(source_errors)
    if len(in_window) < requirements.minimum_observations:
        failures.append(f'observations:{len(in_window)}<required:{requirements.minimum_observations}')
    if requirements.minimum_coverage_pct is not None and Decimal(counts.get('OBSERVED', 0))*100 < requirements.minimum_coverage_pct*len(rows):
        failures.append(f'observed_grid_coverage:{observed_pct}<required:{requirements.minimum_coverage_pct}')
    if requirements.maximum_missing_gap is not None and missing_gap > requirements.maximum_missing_gap.total_seconds():
        failures.append(f'missing_gap_seconds:{missing_gap}>limit:{requirements.maximum_missing_gap.total_seconds()}')
    if requirements.maximum_staleness is not None:
        if reference_missing:
            failures.append(f'reference_unavailable_at_grid_decisions:{reference_missing}; configure lookback or later start')
        if max_stale is not None and max_stale > requirements.maximum_staleness.total_seconds():
            failures.append(f'staleness_seconds:{max_stale}>limit:{requirements.maximum_staleness.total_seconds()}')
    if requirements.require_source_complete and not all(r['source_complete'] for r in rows):
        failures.append('complete_source_coverage_not_verified_for_every_interval')
    if request.policy == DataPolicy.STRICT_GRID:
        accepted = {'OBSERVED', 'VERIFIED_NO_TRADE'} if requirements.allow_verified_no_trade else {'OBSERVED'}
        rejected = sum(r['classification'] not in accepted for r in rows)
        if rejected:
            failures.append(f'strict_grid_unacceptable_intervals:{rejected}')
    report = dict(asset=asset, dataset=dataset, requested_start=request.start, requested_end_exclusive=request.end,
        available_start=in_window[0] if in_window else None, available_end=in_window[-1] if in_window else None,
        observation_count=len(in_window), physical_rows=sum(request.start <= instant(t) < request.end for t in frame['timestamp']),
        source_event_observation_count=len(source_in_window),
        source_event_start=source_in_window[0] if source_in_window else None,
        source_event_end=source_in_window[-1] if source_in_window else None,
        received_by_window_end_observation_count=sum(max(groups[t]['event_available'],groups[t]['received']) <= request.end for t in source_in_window),
        late_observation_count=sum(max(groups[t]['event_available'],groups[t]['received']) > _interval_end(request,t) for t in source_in_window),
        source_event_coverage_pct=100*sum(r['source_event_observation_count'] > 0 for r in rows)/len(rows),
        observation_count_semantics='available_by_original_interval_end' if requirements.require_received_by_reference else 'historical_event_time_replay',
        grid_intervals=len(rows), coverage_pct=observed_pct, classifications=counts,
        maximum_observation_gap_seconds=max(
            [(b-a).total_seconds() for a,b in zip(in_window,in_window[1:])]
            + [(in_window[0]-request.start).total_seconds(), (request.end-in_window[-1]).total_seconds()]
            if in_window else [(request.end-request.start).total_seconds()]),
        maximum_unobserved_interval_seconds=max_gap, maximum_missing_gap_seconds=missing_gap,
        maximum_staleness_seconds=max_stale, decisions_without_reference=reference_missing,
        maximum_decision_staleness_seconds=decision_stale,
        gap_distribution=dict(Counter((b-a).total_seconds() for a,b in unobserved_runs)),
        classified_gap_runs=tuple(runs),
        source_completeness_pct=100*sum(r['source_complete'] for r in rows)/len(rows),
        passed=not failures, failure_reasons=tuple(failures), policy=request.policy.value,
        frequency_seconds=request.frequency.total_seconds(), knowledge_cutoff=as_of,
        requirements=dict(minimum_observations=requirements.minimum_observations,
            minimum_coverage_pct=str(requirements.minimum_coverage_pct) if requirements.minimum_coverage_pct is not None else None,
            maximum_missing_gap_seconds=requirements.maximum_missing_gap.total_seconds() if requirements.maximum_missing_gap is not None else None,
            maximum_staleness_seconds=requirements.maximum_staleness.total_seconds() if requirements.maximum_staleness is not None else None,
            require_source_complete=requirements.require_source_complete,
            allow_verified_no_trade=requirements.allow_verified_no_trade,
            require_received_by_reference=requirements.require_received_by_reference),
        source_evidence=tuple(dict(source_id=s.source_id, sha256=s.source_hash, start=s.start, end=s.end, known_at=s.known_at) for s in sources),
        limitations=('Interval classifications describe [start,end); references use information available at interval START. '
            'Candle close is available no earlier than opening+1 minute. A reference is never executable. '
            'Historical replay does not establish as-published revisions or metadata.'))
    return pd.DataFrame(rows), report


class MarketDataContinuity:
    """Shared offline admission service for research and future validation callers."""
    def __init__(self, reader):
        self.reader = reader

    def evaluate(self, request):
        if request.end > self.reader.as_of:
            raise ValueError('query_end_exceeds_knowledge_time')
        from .continuity_sources import archive_windows
        observations = {}; grids = {}; reports = []
        for asset in request.assets:
            for dataset in request.datasets:
                key = (asset, dataset)
                errors = []
                try:
                    data = self.reader._load(dataset, asset, request.start-request.reference_lookback, request.end)
                    windows, errors = archive_windows(self.reader, asset, dataset, request.start, request.end)
                except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
                    code = error.code if isinstance(error, ResearchDataError) else str(error) if re.fullmatch('[a-z][a-z0-9_]{0,100}', str(error)) else 'source_validation_failed'
                    data = pd.DataFrame(columns=('pair_id','timestamp','source_received_at'))
                    windows = (); errors = [code]
                grid, report = analyze(data, request, asset, dataset, self.reader.as_of, windows, errors)
                selected = data.loc[(data['timestamp'] >= request.start) & (data['timestamp'] < request.end)].copy() if not data.empty else data.copy()
                if not selected.empty:
                    available = selected['timestamp'] + (timedelta(minutes=1) if dataset == 'prices_1m' else timedelta(0))
                    if request.requirements.require_received_by_reference:
                        available = pd.Series([max(instant(a), instant(b)) for a,b in zip(available,selected['source_received_at'])], index=selected.index)
                    selected['observation_available_at'] = available
                    if request.requirements.require_received_by_reference:
                        selected['decision_interval_end'] = [_interval_end(request,instant(t)) for t in selected['timestamp']]
                        selected['interval_admitted'] = available <= selected['decision_interval_end']
                    selected = selected.loc[available <= request.end]
                elif request.requirements.require_received_by_reference:
                    selected['interval_admitted'] = pd.Series(index=selected.index,dtype=bool)
                observations[key] = selected
                grids[key] = grid; reports.append(report)
        return ContinuityResult(request, observations, grids, tuple(reports))

    def release(self, request):
        result = self.evaluate(request)
        if not result.passed:
            raise ContinuityRejected(result)
        # EVENT_TIME/STRICT_GRID expose only original rows. Reference columns
        # stay in separate diagnostic grids and are labelled non-executable.
        return result
