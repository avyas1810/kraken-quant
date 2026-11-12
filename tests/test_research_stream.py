"""Bounded data replay; no strategies, returns or synthetic production data."""
from datetime import timedelta
import hashlib

import pytest
pytest.importorskip('pyarrow')
from test_research_exports import prepared
from test_eligibility_screening import env
from test_ohlcv_collector import data_dir, NOW
from test_partition_algorithm import make, request, START
from kraken_quant.historical_population import encoded
from kraken_quant.research_stream import resident_bytes


def collect(stream):
    with stream:
        return [row for batch in stream for row in batch.to_pylist()]


def test_exact_projected_sparse_read_and_no_mutation(prepared):
    root, clock, reader = prepared
    before = {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    a,b = clock()-timedelta(hours=1),clock()
    expected = reader.load_prices('BTC-AUD',a,b)[['pair_id','timestamp','close']].to_dict('records')
    stream = reader.load_market_data(['BTC-AUD'],a,b,columns=['close'],batch_size=1)
    assert collect(stream) == expected
    assert collect(reader.load_market_data(['BTC-AUD'],a,b,columns=['close'],batch_size=2)) == expected
    assert stream.closed and len(stream.verification) == 1
    assert before == {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_chronological_multi_asset_and_batch_independent(make):
    reader = make()
    def run(size):
        return collect(reader.load_market_data(['ETH-AUD','BTC-AUD'],START,START+timedelta(days=10),batch_size=size))
    small,large = run(1),run(99)
    assert small == large
    assert [(r['timestamp'],r['pair_id']) for r in small] == sorted((r['timestamp'],r['pair_id']) for r in small)
    assert len(small) == 240+192
    assert hashlib.sha256(encoded(small)).digest() == hashlib.sha256(encoded(large)).digest()


def test_budget_failure_and_cleanup(prepared,monkeypatch):
    _,clock,reader=prepared
    monkeypatch.setattr('kraken_quant.research_stream.resident_bytes',lambda:(5*1024**3,5*1024**3))
    stream=reader.load_market_data(['BTC-AUD'],clock()-timedelta(hours=1),clock())
    with pytest.raises(MemoryError,match='budget'):collect(stream)
    assert stream.closed


def test_future_unknown_columns_and_bounded_batch_rejected(prepared):
    _,clock,r=prepared
    for options in ({'batch_size':0},{'batch_size':10001},{'columns':['imaginary']},{'memory_limit_mb':5000}):
        with pytest.raises(ValueError):r.load_market_data(['BTC-AUD'],clock()-timedelta(hours=1),clock(),**options)
    with pytest.raises(ValueError):r.load_market_data(['BTC-AUD'],clock(),clock()+timedelta(hours=1))
    with pytest.raises(ValueError):r.load_market_data(['../BTC-AUD'],clock()-timedelta(hours=1),clock())


def test_changed_partition_fails_closed(prepared,monkeypatch):
    _,clock,r=prepared
    stream=r.load_market_data(['BTC-AUD'],clock()-timedelta(hours=1),clock())
    original=stream._prepare
    def prepare():
        original()
        path,_=stream.files['BTC-AUD'][0]
        import os
        stat=path.stat();os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000))
    monkeypatch.setattr(stream,'_prepare',prepare)
    with pytest.raises(ValueError,match='partition_changed'):collect(stream)
    assert stream.closed


def test_source_tampering_rejected(prepared):
    root,clock,r=prepared
    source=r.load_prices('BTC-AUD',clock()-timedelta(hours=1),clock()).iloc[0]['source_id']
    receipt=root/source
    receipt.write_text('{}')
    with pytest.raises(ValueError):collect(r.load_market_data(['BTC-AUD'],clock()-timedelta(hours=1),clock()))


def test_guarded_stream_equals_existing_phase(make):
    r=make();plan=r.generate_partition_plan(request())
    assert plan.ready
    expected=[]
    for pair in ('BTC-AUD','ETH-AUD'):
        expected.extend(r.load_partition_phase(plan.manifest_id,'training',pair)[['pair_id','timestamp','close']].to_dict('records'))
    expected.sort(key=lambda row:(row['timestamp'],row['pair_id']))
    actual=collect(r.stream_partition_phase(plan.manifest_id,'training',columns=['close'],batch_size=17))
    assert actual == expected


def test_os_memory_is_measured():
    current,peak=resident_bytes()
    assert 0 < current <= peak


def test_independent_assessment_is_read_only_not_authorization(make):
    from kraken_quant.partition_algorithm import PartitionAlgorithm
    r=make()
    before={str(p):p.read_bytes() for p in r.data_dir.rglob('*') if p.is_file()}
    report=PartitionAlgorithm(r).assess_partition_request(request())
    assert report['status']=='READY' and report['assessment_only']
    assert report['manifest_id'] is None and not report['executable']
    assert before=={str(p):p.read_bytes() for p in r.data_dir.rglob('*') if p.is_file()}


def test_early_close_releases_scanners(prepared):
    _,clock,r=prepared
    stream=r.load_market_data(['BTC-AUD'],clock()-timedelta(hours=1),clock(),batch_size=1)
    with stream:
        iterator=iter(stream)
        assert next(iterator).num_rows==1
    assert stream.closed
    with pytest.raises(StopIteration):next(iterator)


def test_guarded_stream_rejects_independent_joint_use(make):
    from dataclasses import replace
    r=make();plan=r.generate_partition_plan(replace(request(),selection_policy='ASSET_INDEPENDENT',independent_experiments=True))
    assert plan.ready
    with pytest.raises(ValueError,match='independent_partitions'):
        r.stream_partition_phase(plan.manifest_id,'training')


def test_replay_exposes_finalization_time_and_requires_explicit_scope(prepared):
    _,clock,r=prepared
    b=clock().replace(second=0,microsecond=0);a=b-timedelta(hours=1)
    with r.load_market_data(['BTC-AUD'],a,b) as stream:
        with pytest.raises(ValueError,match='study_admission'):next(stream.events())
    with r.load_market_data(['BTC-AUD'],a,b) as stream:
        events=list(stream.events(allow_unadmitted=True))
    assert len(events)==3
    assert all(e.available_at==e.timestamp+timedelta(minutes=1) for e in events)
    assert all(e.available_at<=b for e in events)
    assert all('retrospective' in e.timing for e in events)


def test_exclusive_month_end_does_not_validate_unrequested_future_source(make):
    import json
    r=make()
    future=r.data_dir/'historical_imports/receipts/unrequested.json'
    future.write_text(json.dumps(dict(pair_id='BTC-AUD',month='2026-10')))
    rows=collect(r.load_market_data(['BTC-AUD'],START,START+timedelta(days=30)))
    assert len(rows)==240


def test_book_projection_preserves_snapshot_logical_keys(prepared):
    _,_,r=prepared
    rows=collect(r.load_market_data(['BTC-AUD'],NOW-timedelta(microseconds=1),NOW+timedelta(microseconds=1),dataset='orderbook',columns=['price'],batch_size=2))
    assert len(rows)==4 and all(set(row)=={'pair_id','timestamp','side','level','price'} for row in rows)
    assert {(row['side'],row['level']) for row in rows}=={('bid',1),('bid',2),('ask',1),('ask',2)}
