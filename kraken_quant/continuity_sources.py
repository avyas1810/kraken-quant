"""Conservative adapters for existing, immutable source coverage evidence.

Import receipts alone are NOT completeness assertions. Official monthly CSV
evidence must match the entire pinned archive member, including its ZIP CRC.
REST OHLC responses and sampled books never prove absent candles mean no trades.
"""
import csv
import hashlib
import io
import json
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cost_storage import digest
from .historical_population import encoded, sha, dependency, source_records
from .research_data import instant


def archive_windows(reader, asset, dataset, start, end):
    from .market_continuity import SourceWindow
    if dataset != 'prices_1m':
        return (), []
    root = reader.data_dir
    receipts = []
    for path in sorted((root/'historical_imports/receipts').glob('*.json')):
        doc = json.loads(path.read_text())
        name = path.relative_to(root).as_posix()
        if reader.source_selection is not None and name not in reader.source_selection:
            continue
        if doc['pair_id'] != asset or doc['source_kind'] != 'kraken_official_ohlcvt' or instant(doc['received_at']) > reader.as_of:
            continue
        # Keep boundary evidence outside the window, but read only intersecting
        # monthly payloads. No entire-history DataFrame is materialized.
        receipts.append((path, doc))
    if not receipts:
        return (), []
    archive = root/'historical_archives/kraken_official/Kraken_OHLCVT_Full.zip'
    if not archive.exists():
        return (), []  # Missing completeness evidence is UNKNOWN, not no-trade.
    windows = []; checked_members = {}
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read('MANIFEST.json'))
        if manifest.get('product') != 'ohlcvt' or manifest.get('schema_version') != 1 or manifest.get('columns') != ['timestamp','open','high','low','close','volume','trades'] or manifest.get('has_header') is not False:
            raise ValueError('unsupported_official_archive_coverage_contract')
        source_end = instant(manifest['coverage']['end']+'T00:00:00Z')+timedelta(days=1)
        for path, doc in receipts:
            if doc['month'] < start.strftime('%Y-%m') or doc['month'] > end.strftime('%Y-%m'):
                continue
            identity = {k:v for k,v in doc.items() if k not in ('received_at','import_id','inserted_rows','identical_rows','conflicting_rows','conflict_path')}
            if sha(encoded(identity)) != path.stem or path.stem != doc['import_id']:
                raise ValueError('historical_receipt_identity_mismatch')
            if instant(doc['coverage_end_exclusive']) != source_end:
                raise ValueError('archive_coverage_end_mismatch')
            if doc['source_url'] != 'https://assets.kraken.com/marketing/institutions/Kraken_OHLCVT_Full.zip':
                continue
            member = doc['member_name']
            if '/' in member or '\\' in member or '..' in member:
                raise ValueError('unsafe_archive_member')
            extracted = root/'historical_archives/extracted'/doc['archive_sha256']/member
            if not extracted.exists():
                # M11's actual approved population retained its originals here;
                # both layouts require the identical pinned member hash + CRC.
                extracted = root/'historical_archives/kraken_official/minute_csv'/member
            if not extracted.exists():
                continue
            if any(p.is_symlink() for p in (extracted, *extracted.parents)) or not extracted.resolve().is_relative_to(root.resolve()):
                raise ValueError('unsafe_archive_completeness_dependency')
            member_key = (doc['archive_sha256'], member, doc['member_sha256'])
            if member_key not in checked_members:
                info = source.getinfo(member); checksum = 0; size = 0; hasher = hashlib.sha256()
                previous = None; first = None; monthly = {}
                with extracted.open('rb') as handle:
                    for line in handle:
                        size += len(line); checksum = zlib.crc32(line, checksum); hasher.update(line)
                        stamp = datetime.fromtimestamp(int(line.split(b',',1)[0]), timezone.utc)
                        if stamp.second or stamp.microsecond or previous is not None and stamp <= previous or stamp >= source_end:
                            raise ValueError('invalid_or_duplicate_archive_source_timestamp')
                        if first is None:
                            first = stamp
                        previous = stamp
                        month = stamp.strftime('%Y-%m')
                        if start.strftime('%Y-%m') <= month <= end.strftime('%Y-%m'):
                            monthly.setdefault(month, []).append(line)
                if size != info.file_size or checksum & 0xffffffff != info.CRC or hasher.hexdigest() != doc['member_sha256']:
                    raise ValueError('complete_archive_member_integrity_failed')
                checked_members[member_key] = first, monthly
            first, monthly = checked_members[member_key]
            raw = dependency(root, doc['raw_path']).read_bytes()
            source_records(root, doc)  # Exact existing schema/dependency checks.
            if raw != b''.join(monthly.get(doc['month'], [])):
                raise ValueError('incomplete_monthly_source_slice')
            if doc['conflicting_rows']:
                # Existing-vs-source disagreements must not certify continuity.
                return (), ['historical_source_conflicts_require_resolution']
            times = tuple(datetime.fromtimestamp(int(v[0]), timezone.utc) for v in csv.reader(io.StringIO(raw.decode())))
            left = datetime.fromisoformat(doc['month']+'-01T00:00:00+00:00')
            right = datetime(left.year+1,1,1,tzinfo=timezone.utc) if left.month == 12 else datetime(left.year,left.month+1,1,tzinfo=timezone.utc)
            left, right = max(left, first), min(right, source_end)
            if left < right:
                windows.append(SourceWindow(left, right, doc['received_at'], path.relative_to(root).as_posix(),
                    digest(doc), times, complete=True, no_trade_semantics=True, domain_start=first, domain_end=source_end))
    return tuple(windows), []
