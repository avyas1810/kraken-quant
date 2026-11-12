"""Bounded, read-only Arrow access; raw access is not study admission.

Source verification uses the existing ResearchData contract in monthly units.
Replay uses projected Parquet scans and a heap of Arrow cursors, not pandas
frames for the asset universe. No spill files, cache or data copies are written.
"""
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from functools import lru_cache
import ctypes
import gc
import heapq
import hashlib
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from .research_data import bounds, ResearchDataError
from .storage_schema import dataset_schema, validate_pair_id, LOGICAL_KEYS


@lru_cache(maxsize=1)
def _windows_memory_api():
    # ctypes caches pointer types strongly. Defining this structure per sample
    # would otherwise accumulate a new cached type for every budget check.
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
            'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
    api.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    return Counters, kernel.GetCurrentProcess, api


def resident_bytes():
    """Actual resident working set and OS peak, not Python allocation counts."""
    import os
    if os.name == 'nt':
        Counters, current_process, api = _windows_memory_api()
        counters = Counters(); counters.cb = ctypes.sizeof(counters)
        if not api(current_process(), ctypes.byref(counters), counters.cb):
            raise OSError('working_set_measurement_failed')
        return counters.WorkingSetSize, counters.PeakWorkingSetSize
    values = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    return tuple(int(values[k].split()[0])*1024 for k in ('VmRSS', 'VmHWM'))


def utc_bound(value):
    # Date-only convenience is explicit UTC; naive datetime values remain invalid.
    if isinstance(value, str) and len(value) == 10:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class MarketObservation:
    pair_id: str
    timestamp: datetime
    available_at: datetime
    fields: tuple
    timing: str = 'retrospective_source_event_time_not_historical_receipt_time'


class MarketDataStream:
    """Context-managed RecordBatch iterator sorted by (timestamp, pair_id).

    Verification finishes before the first batch is released. Every selected
    monthly file is pinned by stat identity and rechecked while reading. A
    concurrent writer causes a clear retry error, never a mixed snapshot.
    Budget includes the process, including batches retained by the consumer.
    Source checks require all canonical fields, while replay projects columns.
    """
    def __init__(self, reader, pairs, start, end, *, columns=None, batch_size=10000,
                 memory_limit_mb=3072, dataset='prices_1m'):
        self.reader = reader
        self.start, self.end = bounds(utc_bound(start), utc_bound(end))
        if self.end > reader.as_of: raise ValueError('query_end_exceeds_knowledge_time')
        if isinstance(pairs,str): raise ValueError('asset_sequence_required')
        selected=tuple(pairs)
        if len(set(selected)) != len(selected): raise ValueError('duplicate_selected_assets')
        self.pairs = tuple(sorted(selected))
        if not self.pairs or len(self.pairs) > 128: raise ValueError('bounded_nonempty_asset_selection_required')
        for pair in self.pairs: validate_pair_id(pair)
        self.dataset = dataset; self.schema = dataset_schema(dataset)
        names = list(columns) if columns is not None else self.schema.names
        self.columns = list(dict.fromkeys([*LOGICAL_KEYS[dataset], *names]))
        if set(self.columns) - set(self.schema.names): raise ValueError('unknown_projection_column')
        if type(batch_size) is not int or not 1 <= batch_size <= 10000: raise ValueError('batch_size_must_be_1_to_10000')
        if type(memory_limit_mb) is not int or not 256 <= memory_limit_mb <= 4096: raise ValueError('memory_limit_mb_must_be_256_to_4096')
        self.batch_size = batch_size; self.limit = memory_limit_mb*1024**2
        self.files = {}; self.verification = []; self.closed = False; self._iterator = None
        self.admitted = False
        self.received_time_validation = False

    def _budget(self):
        if resident_bytes()[0] > self.limit:
            raise MemoryError('research_process_memory_budget_exceeded; close retained batches or reduce selection/batch size')

    @staticmethod
    def _identity(path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ino

    def _check(self, path, identity):
        if self._identity(path) != identity: raise ValueError('partition_changed_during_research_read; retry with a stable snapshot')

    def _prepare(self):
        for pair in self.pairs:
            from .historical_population import encoded
            fingerprint = hashlib.sha256(); count = 0
            _,error=self.reader._metadata(pair)
            if error: raise ResearchDataError('metadata_unknown_as_of',pair_id=pair,reason=error)
            files = []
            for month in self.reader.store.months(pair, self.dataset):
                a = datetime.fromisoformat(month+'-01').replace(tzinfo=timezone.utc)
                b = a.replace(year=a.year+1, month=1) if a.month == 12 else a.replace(month=a.month+1)
                if b <= self.start or a >= self.end: continue
                path = self.reader.store.partition_path(pair, self.dataset, month)
                identity = self._identity(path)
                physical = pq.ParquetFile(path, page_checksum_verification=True)
                if not physical.schema_arrow.equals(self.schema, check_metadata=True): raise ValueError('partition_schema_mismatch')
                # Conservative allowance for existing Python/Decimal provenance validation.
                if resident_bytes()[0] + physical.metadata.num_rows*8192 > self.limit:
                    raise MemoryError('monthly_source_verification_exceeds_budget; use a smaller date window or higher permitted budget')
                verified = self.reader._load(self.dataset, pair, max(a,self.start), min(b,self.end), archive_first=True)
                sources = sorted(set(zip(verified.get('source_id', []), verified.get('source_sha256', []))))
                if hasattr(self, '_expected'):
                    for row in verified[self.schema.names].to_dict('records'):
                        fingerprint.update(encoded(row)); fingerprint.update(b'\n'); count += 1
                self.verification.append(dict(pair_id=pair, month=month, rows=len(verified), sources=sources))
                del verified, physical
                self.reader._cache = None; self.reader._cache_key = None
                self.reader._parsed_sources.clear(); self.reader._historical_sources.clear()
                gc.collect(); self._check(path, identity); self._budget()
                files.append((path, identity))
            self.files[pair] = files
            if hasattr(self, '_expected'):
                expected = self._expected[pair]
                if count != expected['rows'] or fingerprint.hexdigest() != expected['sha256']:
                    raise ValueError('stream_partition_records_changed_or_missing')

    def _rows(self, pair):
        previous = None
        keys = ('timestamp','side','level') if self.dataset == 'orderbook' else ('timestamp',)
        scan_columns = list(dict.fromkeys([*self.columns, *keys]))
        for path, identity in self.files[pair]:
            self._check(path, identity)
            scanner = ds.dataset(path, format=ds.ParquetFileFormat(read_options=ds.ParquetReadOptions(),
                default_fragment_scan_options=ds.ParquetFragmentScanOptions(page_checksum_verification=True))).scanner(
                columns=scan_columns, filter=(ds.field('timestamp') >= self.start) & (ds.field('timestamp') < self.end),
                batch_size=self.batch_size, use_threads=False, batch_readahead=0, fragment_readahead=0)
            for batch in scanner.to_batches():
                self._check(path, identity); self._budget()
                for i in range(batch.num_rows):
                    row = {name: batch.column(batch.schema.get_field_index(name))[i].as_py() for name in scan_columns}
                    key = tuple(row[name] for name in keys)
                    if previous is not None and key <= previous: raise ValueError('duplicate_or_out_of_order_observation')
                    previous = key
                    yield row
            self._check(path, identity)

    def _run(self):
        cursors = []
        try:
            self._budget(); self._prepare()
            heap = []
            for pair in self.pairs:
                cursor = self._rows(pair); cursors.append(cursor)
                row = next(cursor, None)
                if row is not None: heapq.heappush(heap, (row['timestamp'], pair, row, cursor))
            rows = []; schema = pa.schema([self.schema.field(n) for n in self.columns],metadata=self.schema.metadata)
            while heap:
                _, pair, row, cursor = heapq.heappop(heap)
                rows.append({n: row[n] for n in self.columns})
                following = next(cursor, None)
                if following is not None: heapq.heappush(heap, (following['timestamp'], pair, following, cursor))
                if len(rows) == self.batch_size:
                    self._budget(); yield pa.RecordBatch.from_pylist(rows, schema=schema); rows.clear()
            if rows: yield pa.RecordBatch.from_pylist(rows, schema=schema)
        finally:
            for cursor in cursors: cursor.close()
            self.closed = True

    def __iter__(self):
        if self.closed or self._iterator is not None: raise ValueError('stream_is_single_use')
        self._iterator = self._run()
        return self._iterator

    def close(self):
        if self._iterator is not None: self._iterator.close()
        self.closed = True

    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    def events(self, *, allow_unadmitted=False):
        """Data-only replay cursor; a completed candle becomes known at close.

        This adapter is retrospective event-time replay, not a claim that an
        imported archive was received live. Decision-time receipt replay stays
        on M12's existing receipt-aware release API. No orders/fills are made.
        """
        if not self.admitted and not allow_unadmitted:
            raise ValueError('raw_stream_requires_study_admission_or_explicit_inspection')
        if self.received_time_validation:
            raise ValueError('use_m12_receipt_aware_release_for_decision_time_replay')
        if self.dataset=='prices_1m' and (self.end.second or self.end.microsecond):
            raise ValueError('candle_replay_end_must_align_to_completed_minute')
        for batch in self:
            for row in batch.to_pylist():
                at=row['timestamp']+(timedelta(minutes=1) if self.dataset=='prices_1m' else timedelta(0))
                yield MarketObservation(row['pair_id'],row['timestamp'],at,tuple(row.items()))


def partition_stream(reader, manifest_id, phase, *, fold=0, purpose='evaluate', **options):
    """Bounded joint replay after unchanged M13 per-asset/phase admission.

    Deliberately validates one asset at a time. Legacy independent plans cannot
    be combined here. Existing guarded loading is the authority for M12,
    received-time admission, context and holdout governance.
    """
    from .partition_manifest import read_plan, _load_plan_phase
    from .partition_governance import freeze_plan, record_use
    from .partition_contracts import ALGORITHM_VERSION
    from .research_data import ResearchData
    doc = read_plan(reader.data_dir, manifest_id)
    if doc['configuration']['selection_policy'] != 'COMMON_CALENDAR':
        raise ValueError('independent_partitions_require_separate_joint_temporal_validation')
    groups = [g for g in doc['groups'] if g['fold'] == fold]
    if len(groups) != 1: raise ValueError('common_calendar_group_required')
    group = groups[0]; part = group['phases'][phase]
    dataset = options.get('dataset', 'prices_1m'); expected = {}; sources = set()
    budget = options.get('memory_limit_mb', 3072)*1024**2
    for pair in group['assets']:
        artifact = part['assets'][pair][dataset]
        if resident_bytes()[0] + artifact['inputs']['rows']*16384 > budget:
            raise MemoryError('guarded_phase_validation_exceeds_budget; existing plan is preserved')
        checked = _load_plan_phase(reader, manifest_id, phase, pair, dataset=dataset,
            fold=fold, purpose=purpose, joint=True, release=False)
        expected[pair] = artifact['samples']; sources.update(artifact['inputs']['sources'])
        del checked; gc.collect()
    pinned = ResearchData(reader.data_dir, as_of=doc['evaluation_cutoff'], clock=reader.clock)
    pinned.source_selection = sources
    stream = MarketDataStream(pinned, group['assets'], part['start'], part['end_exclusive'], **options)
    stream._expected = expected
    stream.admitted = True
    stream.received_time_validation = doc['configuration']['continuity']['require_received_by_reference']
    # Registration precedes any research release; test access remains exposed.
    if doc['algorithm_version'] == ALGORITHM_VERSION:
        if doc['configuration']['mode'] == 'research': freeze_plan(reader, manifest_id)
        if phase == 'test': record_use(reader, manifest_id, phase=phase, fold=fold, purpose=purpose, access='bounded_joint_stream')
    return stream
