"""Synthetic isolated calendar partitions, never strategies or production data."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import hashlib,json,zipfile

import pytest
pytest.importorskip('pyarrow')
from test_ohlcv_collector import data_dir,NOW
from kraken_quant.historical_population import import_csv
from kraken_quant.research_data import ResearchData
from kraken_quant.partition_contracts import PartitionRequest,RollingConfig,PHASES
from kraken_quant.partition_algorithm import PartitionAlgorithm
from kraken_quant.market_continuity import ContinuityRequirements,DataPolicy
from kraken_quant.parquet_store import MonthlyParquetStore
from kraken_quant.storage_schema import table_from_records

START=datetime(2026,9,1,tzinfo=timezone.utc)
HOUR=timedelta(hours=1)


@pytest.fixture
def make(data_dir):
    def build(offsets=None,missing=()):
        offsets=offsets or {'BTC-AUD':0,'ETH-AUD':48,'ADA-AUD':96}
        files={}
        for pair,offset in offsets.items():
            symbol={'BTC-AUD':'XBTAUD','ETH-AUD':'ETHAUD','ADA-AUD':'ADAAUD'}[pair]
            files[symbol+'_1.csv']=''.join(f'{int((START+i*HOUR).timestamp())},100,101,99,100,1.000000000000000001,2\n' for i in range(offset,240) if i not in missing).encode()
        folder=data_dir/'historical_archives/kraken_official';folder.mkdir(parents=True)
        archive=folder/'Kraken_OHLCVT_Full.zip'
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('MANIFEST.json',json.dumps(dict(product='ohlcvt',schema_version=1,has_header=False,columns=['timestamp','open','high','low','close','volume','trades'],coverage=dict(end='2026-09-30'))))
            for name,raw in files.items():z.writestr(name,raw)
        ident=hashlib.sha256(archive.read_bytes()).hexdigest()
        extracted=data_dir/'historical_archives/extracted'/ident;extracted.mkdir(parents=True)
        for pair in offsets:
            name={'BTC-AUD':'XBTAUD','ETH-AUD':'ETHAUD','ADA-AUD':'ADAAUD'}[pair]+'_1.csv'
            path=extracted/name;path.write_bytes(files[name])
            import_csv(data_dir,pair,path,source_url='https://assets.kraken.com/marketing/institutions/Kraken_OHLCVT_Full.zip',archive_sha256=ident,coverage_end=datetime(2026,10,1,tzinfo=timezone.utc),received_at=NOW)
        return ResearchData(data_dir,as_of=NOW,clock=lambda:NOW+timedelta(days=1))
    return build


def request(**changes):
    return replace(PartitionRequest(('BTC/AUD','ETH/AUD'),'synthetic-joint',NOW,frequency=HOUR,
        minimum_observations=(3,3,3),minimum_durations=(HOUR,HOUR,HOUR)),**changes)


def generate(reader,**changes):
    return reader.generate_partition_plan(request(**changes))


def only_group(plan):
    assert plan.ready,plan.report
    return plan.report['groups'][0]


def test_common_calendar_two_listing_dates_and_loads(make):
    reader=make();plan=generate(reader);group=only_group(plan)
    assert group['candidate_start'].startswith('2026-09-03T00:00:00')
    assert group['assets']==['BTC-AUD','ETH-AUD']
    for phase in PHASES:
        p=group['phases'][phase]
        frames=[reader.load_partition_phase(plan.manifest_id,phase,asset) for asset in group['assets']]
        assert all(f.attrs['start']==p['start'] and f.attrs['end_exclusive']==p['end_exclusive'] for f in frames)
        assert all(len(f)>=3 and not f.attrs['deployment_authorized'] for f in frames)
        assert frames[0]['close'].iloc[0]==Decimal('100.000000000000000000')
    phases=group['phases']
    assert phases['training']['end_exclusive']<=phases['validation']['start']
    assert phases['validation']['end_exclusive']<=phases['test']['start']


def test_three_dates_added_and_removed_recalculate(make):
    reader=make()
    two=only_group(generate(reader))
    three=only_group(generate(reader,assets=('BTC-AUD','ETH-AUD','ADA-AUD')))
    single=only_group(generate(reader,assets=('BTC-AUD',)))
    assert single['candidate_start']<two['candidate_start']<three['candidate_start']
    assert len({single['phases']['training']['end_exclusive'],two['phases']['training']['end_exclusive'],three['phases']['training']['end_exclusive']})==3


def test_new_asset_insufficient_history_has_no_executable_manifest(make):
    reader=make();before=set((reader.data_dir/'partition_plans').rglob('*.json'))
    plan=generate(reader,assets=('BTC-AUD','ADA-AUD'),minimum_durations=(180*HOUR,HOUR,HOUR))
    assert plan.status=='INSUFFICIENT_HISTORY' and plan.manifest_id is None
    assert not plan.report['executable'] and plan.report['phases']==[]
    failure=next(f for f in plan.report['failures'] if f['asset']=='ADA-AUD')
    assert failure['phase']=='training' and failure['required_seconds']>failure['available_seconds']
    assert set((reader.data_dir/'partition_plans').rglob('*.json'))==before


def test_long_calendar_but_too_few_observations(make):
    plan=generate(make(),minimum_observations=(1000,3,3))
    assert plan.status=='INSUFFICIENT_OBSERVATIONS'
    assert any(f['available_observations']<f['required_observations'] for f in plan.report['failures'])


def test_intermittent_history_uses_valid_common_run(make):
    reader=make(offsets={'BTC-AUD':0,'ETH-AUD':0},missing=range(48,150))
    plan=generate(reader,data_policy=DataPolicy.STRICT_GRID)
    group=only_group(plan)
    assert group['candidate_start'].startswith('2026-09-07T06:00:00')
    assert all(v['quality']['coverage_pct']==100 for p in group['phases'].values() for a in p['assets'].values() for v in a.values())


def test_sparse_early_event_does_not_hide_usable_recent_common_window(make):
    reader=make(offsets={'BTC-AUD':0,'ETH-AUD':0},missing=range(1,200))
    plan=generate(reader,minimum_observations=(5,5,5))
    group=only_group(plan)
    assert group['candidate_start'].startswith('2026-09-09T08:00:00')
    assert all(v['samples']['observations']>=5 for p in group['phases'].values() for a in p['assets'].values() for v in a.values())


def test_unacceptable_gaps_refuse_all_phases(make):
    reader=make(offsets={'BTC-AUD':0,'ETH-AUD':0},missing=range(1,240,2))
    plan=generate(reader,data_policy=DataPolicy.STRICT_GRID,maximum_candidates=200)
    assert not plan.ready and plan.manifest_id is None
    assert plan.status=='INSUFFICIENT_DATA_QUALITY'
    assert all(f['status'] in ('INSUFFICIENT_HISTORY','INSUFFICIENT_OBSERVATIONS','INSUFFICIENT_DATA_QUALITY') for f in plan.report['failures'])


def test_required_type_missing(make):
    plan=generate(make(),datasets=('prices_1m','orderbook'))
    assert plan.status=='SOURCE_UNAVAILABLE'
    assert plan.report['failures'][0]['details']['dataset']=='orderbook'


def test_cutoff_changes_calendar_and_excludes_future_events(make):
    reader=make();full=only_group(generate(reader))
    # Receipts remain known; start restricts the selected calendar instead.
    shorter=only_group(generate(reader,start=START+100*HOUR))
    assert shorter['phases']['training']['end_exclusive']!=full['phases']['training']['end_exclusive']
    future=generate(reader,cutoff=NOW+HOUR)
    assert future.status=='INVALID_CONFIGURATION'
    historical=generate(reader,cutoff=START+200*HOUR)
    assert historical.status=='SOURCE_UNAVAILABLE'  # late archive/metadata unavailable then


def test_newly_known_observations_change_cutoff_boundaries(data_dir):
    from test_ohlcv_collector import collector,Clock,Public
    clock=Clock();public=Public(clock);c=collector(data_dir,public,clock)
    c.collect(all_aud=True);old=clock()
    clock.sleep(180);c.collect(all_aud=True)
    reader=ResearchData(data_dir,as_of=clock(),clock=clock)
    req=PartitionRequest(('BTC-AUD','ETH-AUD'),'cutoff-test',old,
        proportions=(Decimal('.34'),Decimal('.34'),Decimal('.32')))
    first=only_group(reader.generate_partition_plan(req))
    second=only_group(reader.generate_partition_plan(replace(req,cutoff=clock())))
    assert first['phases']['training']['end_exclusive']<second['phases']['training']['end_exclusive']
    assert first['phases']['test']['end_exclusive']<second['phases']['test']['end_exclusive']


def test_independent_policy_requires_declaration_and_blocks_joint_load(make):
    reader=make()
    assert generate(reader,selection_policy='ASSET_INDEPENDENT').status=='INVALID_CONFIGURATION'
    plan=generate(reader,selection_policy='ASSET_INDEPENDENT',independent_experiments=True)
    assert plan.ready and len(plan.report['groups'])==2
    with pytest.raises(ValueError,match='joint_temporal'):
        reader.load_partition_phase(plan.manifest_id,'training','BTC-AUD')
    assert len(reader.load_partition_phase(plan.manifest_id,'training','BTC-AUD',joint=False))>0


def test_lookback_labels_purge_embargo_and_fit_guard(make):
    reader=make();plan=generate(reader,feature_lookback=2*HOUR,label_horizon=2*HOUR,purge=HOUR,embargo=HOUR)
    group=only_group(plan);p=group['phases']
    assert p['training']['label_support_end']<p['validation']['start']
    assert p['validation']['label_support_end']<p['test']['start']
    for phase in PHASES:
        samples=reader.load_partition_phase(plan.manifest_id,phase,'BTC-AUD')
        context=reader.load_partition_phase(plan.manifest_id,phase,'BTC-AUD',context=True,purpose='features')
        assert len(context)==2 and context['timestamp'].max()<samples['timestamp'].min()
        if phase!='training':
            with pytest.raises(ValueError,match='training_only'):
                reader.load_partition_phase(plan.manifest_id,phase,'BTC-AUD',purpose='fit')
    assert len(reader.load_partition_phase(plan.manifest_id,'training','BTC-AUD',purpose='fit'))>0


def test_guards_can_make_phase_insufficient(make):
    plan=generate(make(),purge=100*HOUR,embargo=100*HOUR,label_horizon=100*HOUR)
    assert plan.status=='INSUFFICIENT_HISTORY'


def test_rolling_folds_each_validated(make):
    reader=make();roll=RollingConfig((48*HOUR,24*HOUR,24*HOUR),24*HOUR,3)
    plan=generate(reader,rolling=roll)
    assert plan.ready and len(plan.report['groups'])==3
    for i,g in enumerate(plan.report['groups']):
        assert g['fold']==i
        assert len(reader.load_partition_phase(plan.manifest_id,'test','ETH-AUD',fold=i))==24
    assert generate(reader,rolling=replace(roll,folds=10)).status=='INSUFFICIENT_HISTORY'
    assert generate(reader,rolling=replace(roll,step=HOUR)).status=='INVALID_CONFIGURATION'


def test_manifest_reproducible_and_preserved_when_new_rows_arrive(make):
    reader=make();plan=generate(reader);path=reader.data_dir/'partition_plans/v1'/(plan.manifest_id+'.json')
    original=path.read_bytes();again=generate(reader)
    assert again.manifest_id==plan.manifest_id and path.read_bytes()==original
    expected=reader.load_partition_phase(plan.manifest_id,'training','BTC-AUD')
    # A later, previously absent event inside the old phase is not part of its pin.
    row=reader.store.read('BTC-AUD','prices_1m','2026-09').to_pylist()[49]
    row['timestamp']+=timedelta(minutes=1)
    reader.store.write('prices_1m',table_from_records('prices_1m',[row]))
    actual=reader.load_partition_phase(plan.manifest_id,'training','BTC-AUD')
    assert list(actual['timestamp'])==list(expected['timestamp'])
    assert path.read_bytes()==original
    path.write_bytes(original+b' ')
    with pytest.raises(ValueError,match='hash_mismatch'):
        reader.load_partition_phase(plan.manifest_id,'training','BTC-AUD')


@pytest.mark.parametrize('changes',[
    dict(assets=()),dict(assets=('BTC/AUD','BTC-AUD')),dict(identifier=''),dict(mode='live'),
    dict(proportions=(Decimal('.5'),Decimal('.2'),Decimal('.2'))),dict(proportions=(.6,.2,.2)),
    dict(minimum_observations=(0,1,1)),dict(minimum_durations=(timedelta(0),HOUR,HOUR)),
    dict(label_horizon=timedelta(minutes=1)),dict(frequency=timedelta(0)),
    dict(maximum_grid_cells=1),dict(maximum_records=1),dict(rolling=RollingConfig((HOUR,)*3,HOUR,33)),
])
def test_invalid_configuration_is_structured(make,changes):
    plan=generate(make(),**changes)
    assert plan.status=='INVALID_CONFIGURATION' and not plan.ready and plan.manifest_id is None


def test_production_validation_uses_same_service_and_never_authorizes_orders(make):
    reader=make();plan=generate(reader,mode='production_validation')
    assert plan.ready and plan.report['configuration']['mode']=='production_validation'
    frame=reader.load_partition_phase(plan.manifest_id,'validation','BTC/AUD')
    assert frame.attrs['deployment_authorized'] is False
    checked=reader.validate_partition_plan(plan.manifest_id)
    assert checked['status']=='READY' and checked['phase_asset_datasets_verified']==6
    assert generate(reader,mode='production_validation',cutoff=NOW+HOUR).status=='INVALID_CONFIGURATION'
    earlier=ResearchData(reader.data_dir,as_of=NOW-timedelta(seconds=1),clock=reader.clock)
    with pytest.raises(ValueError,match='cutoff'):
        earlier.load_partition_phase(plan.manifest_id,'training','BTC-AUD')


def test_source_change_and_missing_partition_fail_closed(make):
    reader=make();plan=generate(reader)
    source=next(iter(only_group(plan)['phases']['training']['assets']['BTC-AUD']['prices_1m']['samples']['sources']))
    path=reader.data_dir/source;path.write_bytes(path.read_bytes()+b' ')
    # JSON whitespace leaves semantic digest unchanged, but raw dependency remains
    # immutable where required. A material payload change always fails.
    doc=json.loads(path.read_text());doc['received_at']='2026-11-02T00:00:00.000000Z';path.write_text(json.dumps(doc))
    with pytest.raises(ValueError,match='source_changed'):
        reader.load_partition_phase(plan.manifest_id,'training','BTC-AUD')


def test_context_and_staleness_quality_are_revalidated_from_pinned_inputs(make):
    reader=make()
    plan=generate(reader,feature_lookback=2*HOUR,reference_lookback=2*HOUR,
        continuity=ContinuityRequirements(maximum_staleness=2*HOUR,require_source_complete=True))
    assert plan.ready,plan.report
    frame=reader.load_partition_phase(plan.manifest_id,'validation','ETH-AUD')
    assert len(frame)>0
    context=reader.load_partition_phase(plan.manifest_id,'validation','ETH-AUD',context=True)
    assert context['timestamp'].max()<frame['timestamp'].min()


def test_existing_m11_partition_interface_still_reads(make):
    reader=make()
    from kraken_quant.historical_population import immutable,encoded,sha
    from kraken_quant.historical_readiness import row_hash
    rows=reader.store.read('BTC-AUD','prices_1m','2026-09').to_pylist()
    parts={}
    for i,phase in enumerate(PHASES):
        selected=rows[i*3:i*3+3]
        parts[phase]=dict(start=selected[0]['timestamp'],end_exclusive=selected[-1]['timestamp']+timedelta(minutes=1),rows=3,sha256=row_hash(selected))
    raw=encoded(dict(contract_version=1,knowledge_time=NOW,markets=[dict(pair_id='BTC-AUD',prices_status='LIMITED',partitions=parts,sources=[])]))
    ident=sha(raw);immutable(reader.data_dir/'historical_imports/partitions'/(ident+'.json'),raw)
    for phase in PHASES:
        assert len(reader.load_partition('BTC-AUD',phase,ident))==3


def test_received_time_policy_cannot_backdate_archive_into_phases(make):
    plan=generate(make(),continuity=ContinuityRequirements(require_received_by_reference=True))
    assert plan.status=='INSUFFICIENT_OBSERVATIONS' and not plan.ready
    assert all(f['available_observations']==0 for f in plan.report['failures'])


def test_missing_pinned_data_fails_revalidation(make):
    reader=make();plan=generate(reader)
    reader.store.partition_path('ETH-AUD','prices_1m','2026-09').unlink()
    with pytest.raises(ValueError):
        reader.validate_partition_plan(plan.manifest_id)
