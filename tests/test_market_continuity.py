"""Deterministic synthetic continuity fixtures, never production observations."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import zipfile

import pytest
pytest.importorskip('pyarrow')
import pandas as pd
from pandas.testing import assert_frame_equal
from kraken_quant.market_continuity import (
    ContinuityRequest, ContinuityRequirements, DataPolicy, SourceWindow,
    MarketDataContinuity, ContinuityRejected, analyze,
)
from kraken_quant.research_data import ResearchData
from test_ohlcv_collector import data_dir, NOW
from test_eligibility_screening import env

START = datetime(2026,9,1,tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
HASH = 'a'*64


def candles(minutes, pair='BTC-AUD'):
    return pd.DataFrame([dict(pair_id=pair,timestamp=START+v*MINUTE,
        close=Decimal('100.000000000000000001'),is_complete=True,
        source_id='fixture',source_sha256=HASH,source_received_at=START-timedelta(days=1)) for v in minutes],
        columns=('pair_id','timestamp','close','is_complete','source_id','source_sha256','source_received_at'))


def request(**changes):
    return replace(ContinuityRequest(('BTC-AUD',), START, START+10*MINUTE), **changes)


def evidence(**changes):
    return replace(SourceWindow(START, START+10*MINUTE, START-timedelta(days=1), 'source', HASH,
        complete=True,no_trade_semantics=True,domain_start=START,domain_end=START+10*MINUTE), **changes)


def inspect(frame, req=None, sources=()):
    return analyze(frame, req or request(), 'BTC-AUD', 'prices_1m', START+timedelta(days=1), sources)


def test_sparse_event_time_exact_no_fill():
    raw = candles([0,3,9]); original = raw.copy(deep=True)
    grid, report = inspect(raw)
    assert report['classifications'] == {'OBSERVED':3,'MISSING_OR_UNVERIFIED':7}
    assert report['coverage_pct'] == 30 and report['source_completeness_pct'] == 0
    assert grid.iloc[2].reference_close == Decimal('100.000000000000000001')
    assert not grid['executable'].any()
    assert_frame_equal(raw,original)


def test_no_trade_requires_complete_semantic_evidence():
    raw = candles([0,9]); src = evidence(observation_times=(START,START+9*MINUTE))
    grid, report = inspect(raw,sources=(src,))
    assert report['classifications'] == {'OBSERVED':2,'VERIFIED_NO_TRADE':8}
    assert report['source_completeness_pct'] == 100
    for src in (replace(src,complete=False),replace(src,no_trade_semantics=False)):
        assert inspect(raw,sources=(src,))[1]['classifications']['MISSING_OR_UNVERIFIED'] == 8


def test_source_row_missing_locally_is_not_no_trade():
    src=evidence(observation_times=(START+4*MINUTE,))
    grid,_=inspect(candles([0,9]),sources=(src,))
    assert grid.iloc[4].classification == 'MISSING_OR_UNVERIFIED'


def test_source_boundaries_and_partial_cells():
    src=evidence(start=START+2*MINUTE,end=START+8*MINUTE,domain_start=START+2*MINUTE,domain_end=START+8*MINUTE)
    grid,_=inspect(candles([]),sources=(src,))
    assert list(grid['classification']) == ['SOURCE_OUT_OF_COVERAGE']*2+['VERIFIED_NO_TRADE']*6+['SOURCE_OUT_OF_COVERAGE']*2
    partial=replace(src,start=START+timedelta(seconds=150))
    assert inspect(candles([]),sources=(partial,))[0].iloc[2].classification == 'MISSING_OR_UNVERIFIED'


def test_future_evidence_is_not_used():
    _,report=inspect(candles([]),sources=(evidence(known_at=START+timedelta(days=2)),))
    assert report['classifications'] == {'MISSING_OR_UNVERIFIED':10}


def test_adjacent_source_windows_cover_larger_grid_cell():
    req=request(frequency=10*MINUTE)
    left=evidence(end=START+5*MINUTE)
    right=evidence(start=START+5*MINUTE)
    grid,report=inspect(candles([]),req,(left,right))
    assert grid.iloc[0].classification == 'VERIFIED_NO_TRADE'
    assert report['source_completeness_pct'] == 100
    assert inspect(candles([]),req,(left,replace(right,no_trade_semantics=False)))[0].iloc[0].classification == 'MISSING_OR_UNVERIFIED'


def test_received_by_decision_rule_applies_to_source_evidence_too():
    req=request(requirements=ContinuityRequirements(require_received_by_reference=True))
    src=evidence(known_at=START+timedelta(hours=1))
    assert inspect(candles([]),req,(src,))[1]['classifications'] == {'MISSING_OR_UNVERIFIED':10}


def test_multihour_gap_and_explicit_threshold():
    req=request(end=START+timedelta(hours=5),requirements=ContinuityRequirements(maximum_missing_gap=timedelta(hours=1)))
    _,report=inspect(candles([0,299]),req)
    assert report['maximum_missing_gap_seconds'] == 298*60
    assert report['maximum_observation_gap_seconds'] == 299*60
    assert report['maximum_unobserved_interval_seconds'] == 298*60
    assert report['gap_distribution'] == {298*60:1}
    assert not report['passed'] and 'missing_gap_seconds' in report['failure_reasons'][0]


def test_gap_crossing_classifications_not_underreported():
    src=evidence(start=START+3*MINUTE,end=START+5*MINUTE)
    _,report=inspect(candles([0,9]),sources=(src,))
    assert report['maximum_unobserved_interval_seconds'] == 8*60


def test_candle_close_not_available_at_its_opening():
    grid,_=inspect(candles([0,3]))
    assert pd.isna(grid.iloc[0].last_observation_at)
    assert grid.iloc[1].last_observation_at == START
    assert grid.iloc[1].observation_available_at == START+MINUTE
    assert grid.iloc[3].last_observation_at == START
    assert grid.iloc[4].last_observation_at == START+3*MINUTE


def test_received_time_admission_and_out_of_order_receipts():
    raw=candles([-1,0,1]);raw.loc[0,'source_received_at']=START+8*MINUTE
    req=request(requirements=ContinuityRequirements(require_received_by_reference=True))
    grid,_=inspect(raw,req)
    assert grid.iloc[8].last_observation_at == START+MINUTE


def test_coarse_grid_does_not_hide_intracell_staleness():
    raw=candles([-1,8])
    req=request(frequency=10*MINUTE,requirements=ContinuityRequirements(maximum_staleness=5*MINUTE))
    _,report=inspect(raw,req)
    assert not report['passed']
    assert report['maximum_staleness_seconds'] == 10*60


def test_late_received_rows_do_not_satisfy_minimum_observations():
    raw=candles([0,1]);raw['source_received_at']=START+timedelta(hours=1)
    req=request(requirements=ContinuityRequirements(require_received_by_reference=True))
    _,report=inspect(raw,req)
    assert report['observation_count'] == 0 and not report['passed']


def test_asof_explicit_staleness_and_strict_grid():
    with pytest.raises(ValueError,match='explicit_staleness'):
        request(policy=DataPolicy.ASOF_REFERENCE)
    req=request(policy=DataPolicy.ASOF_REFERENCE,requirements=ContinuityRequirements(maximum_staleness=2*MINUTE))
    _,report=inspect(candles([-1,0,9]),req)
    assert not report['passed'] and any('staleness_seconds' in x for x in report['failure_reasons'])
    req=request(policy=DataPolicy.STRICT_GRID)
    assert not inspect(candles([0,9]),req)[1]['passed']
    assert inspect(candles(range(10)),req)[1]['passed']


def test_strict_source_no_trade_requires_explicit_opt_in():
    src=evidence(observation_times=(START,START+9*MINUTE))
    req=request(policy=DataPolicy.STRICT_GRID)
    assert not inspect(candles([0,9]),req,(src,))[1]['passed']
    req=replace(req,requirements=ContinuityRequirements(allow_verified_no_trade=True,require_source_complete=True))
    assert inspect(candles([0,9]),req,(src,))[1]['passed']


@pytest.mark.parametrize('changes',[
    {'assets':()}, {'assets':('BTC-AUD','BTC-AUD')}, {'assets':('../BTC-AUD',)},
    {'frequency':timedelta(0)}, {'frequency':timedelta(seconds=30)},
    {'datasets':('costs_1m',)}, {'maximum_grid_cells':1}, {'start':START.replace(tzinfo=None)},
    {'end':START}, {'reference_lookback':-MINUTE},
])
def test_invalid_requests(changes):
    with pytest.raises((ValueError,TypeError)):
        request(**changes)


@pytest.mark.parametrize('changes',[
    {'minimum_coverage_pct':Decimal('NaN')},{'minimum_coverage_pct':50.0},
    {'minimum_observations':-1},{'minimum_observations':True},
    {'maximum_staleness':-MINUTE},{'maximum_missing_gap':1},
])
def test_invalid_requirements(changes):
    with pytest.raises(ValueError):
        ContinuityRequirements(**changes)


def test_no_universal_density_threshold_and_configurable_sufficiency():
    assert inspect(candles([0]))[1]['passed']
    req=request(requirements=ContinuityRequirements(minimum_observations=2,minimum_coverage_pct=Decimal('80'),require_source_complete=True))
    report=inspect(candles([0]),req)[1]
    assert not report['passed'] and len(report['failure_reasons']) == 3


def test_future_or_incomplete_observations_rejected():
    raw=candles([10])
    with pytest.raises(ValueError,match='future'):
        inspect(raw)
    raw=candles([0]);raw.loc[0,'is_complete']=False
    with pytest.raises(ValueError,match='incomplete'):
        inspect(raw)
    raw=candles([0]);raw.loc[0,'source_received_at']=START+timedelta(days=2)
    with pytest.raises(ValueError,match='future'):
        inspect(raw)


def files(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def test_existing_research_interface_offline_repeats_and_preserves(env,monkeypatch):
    root,clock,_,_=env
    clock.sleep(60)  # Complete the minute containing the genuine fixture snapshot.
    reader=ResearchData(root,as_of=clock(),clock=clock)
    req=ContinuityRequest(('BTC-AUD',),clock().replace(second=0,microsecond=0)-timedelta(hours=1),clock().replace(second=0,microsecond=0),datasets=('prices_1m','quotes_1m','orderbook'))
    import kraken_quant.market_data as market
    monkeypatch.setattr(market,'public_get',lambda *a,**k:pytest.fail('network prohibited'))
    before=files(root)
    result=reader.load_for_research(req)
    again=reader.continuity(req)
    assert result.reports == again.reports and len(result.data) == 3
    assert len(result.observations[('BTC-AUD','orderbook')]) == 4
    assert result.reports[2]['observation_count'] == 1
    assert files(root) == before
    strict=replace(req,policy=DataPolicy.STRICT_GRID)
    with pytest.raises(ContinuityRejected,match='strict_grid') as err:
        reader.load_for_research(strict)
    assert not err.value.result.passed


def test_multiple_assets_independent_sources_and_missing_dataset(env):
    root,clock,_,_=env;reader=ResearchData(root,as_of=clock(),clock=clock)
    req=ContinuityRequest(('BTC-AUD','ETH-AUD'),clock().replace(second=0,microsecond=0)-timedelta(hours=1),clock().replace(second=0,microsecond=0))
    result=reader.continuity(req)
    assert len(result.reports) == 2
    assert {r['asset'] for r in result.reports} == {'BTC-AUD','ETH-AUD'}
    unknown=replace(req,assets=('UNKNOWN-AUD',))
    assert not reader.continuity(unknown).passed


def test_multiple_densities_do_not_share_source_assertions():
    req=request(assets=('BTC-AUD','ETH-AUD'))
    dense=analyze(candles(range(10)),req,'BTC-AUD','prices_1m',START+timedelta(days=1),(evidence(),))[1]
    sparse=analyze(candles([0,9],pair='ETH-AUD'),req,'ETH-AUD','prices_1m',START+timedelta(days=1))[1]
    assert dense['coverage_pct'] == 100 and sparse['coverage_pct'] == 20
    assert dense['source_completeness_pct'] == 100 and sparse['source_completeness_pct'] == 0
    assert sparse['classifications']['MISSING_OR_UNVERIFIED'] == 8


def test_reference_policy_separate_output_and_missing_type_rejection(env):
    root,clock,_,_=env;clock.sleep(60)
    reader=ResearchData(root,as_of=clock(),clock=clock)
    end=clock().replace(second=0,microsecond=0)
    req=ContinuityRequest(('BTC-AUD',),end-2*MINUTE,end,policy=DataPolicy.ASOF_REFERENCE,
        requirements=ContinuityRequirements(maximum_staleness=10*MINUTE),reference_lookback=10*MINUTE)
    result=reader.load_for_research(req)
    assert result.data is result.intervals
    assert len(result.data[('BTC-AUD','prices_1m')]) == 2
    assert not result.data[('BTC-AUD','prices_1m')]['executable'].any()
    assert 'observation_available_at' in result.observations[('BTC-AUD','prices_1m')]
    no_quotes=replace(req,start=end+MINUTE,end=end+2*MINUTE,datasets=('quotes_1m',))
    with pytest.raises(ValueError,match='knowledge_time'):
        reader.continuity(no_quotes)


def test_quotes_retain_actual_timestamps_and_no_executable_bid_ask():
    raw=candles([0]);raw['timestamp']=raw['timestamp']+timedelta(seconds=30)
    raw['bid']=Decimal('99.999999999999999999');raw['ask']=Decimal('100.000000000000000001')
    grid,report=analyze(raw,request(datasets=('quotes_1m',)),'BTC-AUD','quotes_1m',START+timedelta(days=1))
    assert grid.iloc[1]['last_observation_at'] == START+timedelta(seconds=30)
    assert grid.iloc[1]['reference_bid'] == Decimal('99.999999999999999999')
    assert not grid['executable'].any() and report['observation_count'] == 1


def test_missing_required_book_is_structured_rejection(data_dir):
    reader=ResearchData(data_dir,as_of=NOW)
    req=ContinuityRequest(('BTC-AUD',),START,START+MINUTE,datasets=('orderbook',))
    with pytest.raises(ContinuityRejected) as error:
        reader.load_for_research(req)
    assert 'observations:0<required:1' in error.value.result.reports[0]['failure_reasons']


def official(root,raw):
    from kraken_quant.historical_population import import_csv
    folder=root/'historical_archives/kraken_official';folder.mkdir(parents=True)
    archive=folder/'Kraken_OHLCVT_Full.zip'
    manifest=dict(product='ohlcvt',schema_version=1,has_header=False,columns=['timestamp','open','high','low','close','volume','trades'],coverage=dict(end='2026-09-30'))
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('MANIFEST.json',json.dumps(manifest));z.writestr('XBTAUD_1.csv',raw)
    archive_hash=hashlib.sha256(archive.read_bytes()).hexdigest()
    extracted=root/'historical_archives/extracted'/archive_hash;extracted.mkdir(parents=True)
    member=extracted/'XBTAUD_1.csv';member.write_bytes(raw)
    import_csv(root,'BTC-AUD',member,source_url='https://assets.kraken.com/marketing/institutions/Kraken_OHLCVT_Full.zip',archive_sha256=archive_hash,coverage_end=datetime(2026,10,1,tzinfo=timezone.utc),received_at=NOW)
    return member


def test_complete_archive_actual_slice_proof_and_tampering(data_dir):
    raw=b'1788220800,100,100,100,100,1,1\n1788220920,101,101,101,101,2,1\n'
    member=official(data_dir,raw)
    reader=ResearchData(data_dir,as_of=NOW)
    req=ContinuityRequest(('BTC-AUD',),START,START+3*MINUTE)
    before=files(data_dir);result=reader.continuity(req)
    assert result.reports[0]['classifications'] == {'OBSERVED':2,'VERIFIED_NO_TRADE':1}
    assert files(data_dir) == before
    member.write_bytes(raw.replace(b'100',b'999'))
    failed=reader.continuity(req)
    assert not failed.passed and 'VERIFIED_NO_TRADE' not in failed.reports[0]['classifications']


def test_missing_completeness_artifact_is_unknown_not_no_trade(data_dir):
    member=official(data_dir,b'1788220800,100,100,100,100,1,1\n1788220920,101,101,101,101,2,1\n')
    member.unlink()
    req=ContinuityRequest(('BTC-AUD',),START,START+3*MINUTE)
    result=ResearchData(data_dir,as_of=NOW).continuity(req)
    assert result.reports[0]['classifications']['MISSING_OR_UNVERIFIED'] == 1


def test_existing_m11_extraction_layout_reused_without_copy(data_dir):
    member=official(data_dir,b'1788220800,100,100,100,100,1,1\n1788220920,101,101,101,101,2,1\n')
    legacy=data_dir/'historical_archives/kraken_official/minute_csv';legacy.mkdir()
    member.rename(legacy/member.name)
    req=ContinuityRequest(('BTC-AUD',),START,START+3*MINUTE)
    before=files(data_dir)
    result=ResearchData(data_dir,as_of=NOW).continuity(req)
    assert result.reports[0]['classifications']['VERIFIED_NO_TRADE'] == 1
    assert files(data_dir) == before
