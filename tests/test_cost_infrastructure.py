"""Structural cost-infrastructure tests; no mathematical cost algorithms."""
from dataclasses import FrozenInstanceError,replace
from datetime import timedelta,timezone
from decimal import Decimal
from copy import deepcopy
import ast
import inspect
import json
import os
from pathlib import Path
import pytest
pytest.importorskip("pyarrow")
from test_ohlcv_collector import data_dir,Clock,NOW,Public as CandlePublic,collector as candle_collector
from test_book_collector import setup as book_setup
from kraken_quant import cost_infrastructure as cli, cost_algorithms as algorithms
from kraken_quant.cost_contracts import *
from kraken_quant.cost_inputs import InputService
from kraken_quant.cost_storage import AuditStore,digest,payload
from kraken_quant.fee_data import *
from kraken_quant.metadata_store import connect_metadata
from kraken_quant.reconciliation import ReadOnlyKraken,REQUIRED_PERMISSIONS
from kraken_quant.storage_schema import dataset_schema

HTML='<html><div id="content-spot-crypto" aria-labelledby="spot-crypto"><table><tr><th>30-Day Volume (USD)</th><th>Maker</th><th>Taker</th></tr><tr><td>$0+</td><td>0.25%</td><td>0.40%</td></tr><tr><td>$100,000+</td><td>-0.01%</td><td>0.20%</td></tr></table></div><div aria-labelledby="stablecoin-pegged-token-fx-pairs"><table><tr><th>30-Day Volume (USD)</th><th>Maker</th><th>Taker</th></tr><tr><td>$0+</td><td>0.20%</td><td>0.20%</td></tr></table></div></html>'

class FeePublic:
    def __init__(self):self.calls=[];self.missing=False
    def __call__(self,endpoint,params):
        self.calls.append(endpoint)
        if endpoint=="FeeSchedule":return dict(url=PUBLIC_URL,html=HTML)
        assert endpoint=="AssetPairs"
        return dict(error=[],result={k:dict(fees=[] if self.missing else [["0","0.4"],["100000","0.2"]],fees_maker=[] if self.missing else [["0","0.25"],["100000","-0.01"]],fee_volume_currency="USD") for k in params["pair"].split(',')})

def fees(data_dir,clock,public=None,readonly=None):
    return refresh_fees(data_dir,transport=public or FeePublic(),readonly=readonly,clock=clock,sleeper=lambda s:None)

@pytest.fixture
def prepared_env(data_dir):
    clock=Clock()
    book,public,_=book_setup(data_dir,clock)
    book.collect(all_aud=True)
    candle_collector(data_dir,CandlePublic(clock),clock).collect(all_aud=True)
    clock.sleep(5)
    version=fees(data_dir,clock)
    service=InputService(data_dir,clock=clock)
    return data_dir,clock,service,version

def request(clock,**changes):
    return CostRequest(**dict(dict(pair_id="BTC-AUD",side="buy",notional_aud=Decimal("1000"),execution_type="taker",observation_time=clock(),model_version="reserved-v1"),**changes))

def test_public_schedule_preserves_every_tier_and_condition():
    tables=parse_public_page(dict(url=PUBLIC_URL,html=HTML))
    assert len(tables)==2 and len(tables[0].tiers)==2
    assert tables[0].tiers[1].maker_pct==Decimal("-0.01")
    assert tables[0].tiers[1].conditions[0]==("30-Day Volume (USD)","$100,000+")
    assert tables[0].tier_currency=="USD"

@pytest.mark.parametrize("doc",[{},dict(url="https://wrong",html=HTML),dict(url=PUBLIC_URL,html="no tables"),dict(url=PUBLIC_URL,html=HTML.replace('0.25%','NaN%')),dict(url=PUBLIC_URL,html=HTML.replace('<td>0.25%</td>',''))])
def test_invalid_public_document(doc):
    with pytest.raises(ValueError):parse_public_page(doc)

def test_api_tiers_and_empty_arrays(data_dir):
    assets=collection_universe(data_dir)[0];public=FeePublic()
    raw=public("AssetPairs",dict(pair=','.join(a['kraken_pair_id'] for a in assets)))
    parsed=parse_pair_fees(raw,assets)
    assert parsed['BTC-AUD']['maker'][1]==(Decimal('100000'),Decimal('-0.01'))
    public.missing=True
    parsed=parse_pair_fees(public("AssetPairs",dict(pair=','.join(a['kraken_pair_id'] for a in assets))),assets)
    assert parsed['BTC-AUD']['maker']==() and parsed['BTC-AUD']['taker']==()

@pytest.mark.parametrize("change",[
    lambda b:b.update(error=['bad']),lambda b:b.update(result={}),
    lambda b:b['result']['XBTAUD'].update(fees='bad'),
    lambda b:b['result']['XBTAUD'].update(fees=[[0,0.4]]),
    lambda b:b['result']['XBTAUD'].update(fees=[['-1','0.4']]),
    lambda b:b['result']['XBTAUD'].update(fees=[['0','0.4'],['0','0.3']]),
])
def test_invalid_api_fee_response(data_dir,change):
    assets=collection_universe(data_dir)[0];raw=FeePublic()('AssetPairs',dict(pair=','.join(a['kraken_pair_id'] for a in assets)))
    change(raw)
    with pytest.raises(ValueError):parse_pair_fees(raw,assets)

def test_version_retention_and_no_zero_default(data_dir):
    clock=Clock();public=FeePublic();public.missing=True
    first=fees(data_dir,clock,public)
    clock.sleep(5);second=fees(data_dir,clock,public)
    assert first!=second and len(AuditStore(data_dir).versions('fees'))==2
    view=fee_view(data_dir,first,'BTC-AUD')
    assert view.account_maker_pct is None and view.account_taker_pct is None
    assert not view.account_specific and view.applicable_tier is None
    assert len(view.public_tables)==2

def test_account_readonly_distinction_and_redaction(data_dir):
    calls=[]
    def transport(endpoint,params):
        calls.append(endpoint)
        if endpoint=='GetApiKeyInfo':return dict(error=[],result=dict(permissions=sorted(REQUIRED_PERMISSIONS)))
        assert endpoint=='TradeVolume'
        return dict(error=[],result=dict(currency='ZUSD',volume='PRIVATE_VOLUME_987654321',fees={k:dict(fee='0.37',tiervolume='0',nextvolume='100000') for k in ('XBTAUD','ETHAUD')},fees_maker={k:dict(fee='0.22') for k in ('XBTAUD','ETHAUD')}))
    adapter=ReadOnlyKraken(transport,'private-account-fingerprint',clock=Clock())
    version=fees(data_dir,Clock(),readonly=adapter)
    view=fee_view(data_dir,version,'BTC-AUD')
    assert view.account_specific and view.account_taker_pct==Decimal('0.37')
    assert set(calls)=={'GetApiKeyInfo','TradeVolume'}
    report=json.dumps(fee_report(data_dir))
    assert 'PRIVATE_VOLUME' not in report and 'private-account-fingerprint' not in report
    assert AuditStore(data_dir).get('fees',version)['account']['payload']['volume']=='PRIVATE_VOLUME_987654321'

@pytest.mark.parametrize('changes',[
 dict(notional_aud=Decimal('0')),dict(notional_aud=Decimal('-1')),dict(notional_aud=Decimal('NaN')),
 dict(notional_aud=Decimal('Infinity')),dict(notional_aud=1000.0),dict(side='short'),dict(execution_type='optimized'),
 dict(observation_time=NOW.replace(tzinfo=None)),dict(observation_time=NOW.astimezone(timezone(timedelta(hours=1)))),dict(model_version=''),dict(pair_id='../BTC-AUD')])
def test_invalid_request(changes):
    with pytest.raises(ValueError):request(Clock(),**changes)

def test_immutable_complete_bundle_metadata_and_exact_inputs(prepared_env):
    directory,clock,service,version=prepared_env
    prepared=service.prepare(request(clock,notional_aud=Decimal('1000.000000000000000001')))
    assert prepared.status=='inputs_ready',prepared.missing_inputs
    inputs=prepared.inputs
    assert inputs.quote.bid==Decimal('100.000000000000000001')
    assert len(inputs.order_book.levels)==4 and inputs.order_book.actual_bid_depth==2
    assert inputs.quote.source.response_id==inputs.order_book.source.response_id
    assert inputs.quote.timestamp==inputs.order_book.timestamp
    assert inputs.candle.is_complete and inputs.market_metadata.kraken_pair_id=='XBTAUD'
    assert inputs.fee_schedule.version==version
    with pytest.raises(FrozenInstanceError):inputs.quote.bid=Decimal('1')
    assert 'applicable_account_fee_tier_unknown' in prepared.warnings

@pytest.mark.parametrize('size',[Decimal('100'),Decimal('1000'),Decimal('5000')])
@pytest.mark.parametrize('side',['buy','sell'])
@pytest.mark.parametrize('execution',['maker','taker'])
def test_request_matrix(prepared_env,size,side,execution):
    _,clock,service,_=prepared_env
    assert service.prepare(request(clock,notional_aud=size,side=side,execution_type=execution)).status=='inputs_ready'

def test_manifest_reproducible_after_append_and_new_fee_version(prepared_env):
    directory,clock,service,version=prepared_env
    original=service.prepare(request(clock));manifest=service.persist(original)
    assert manifest==service.persist(service.prepare(request(clock)))
    clock.sleep(70)
    book,_,_=book_setup(directory,clock);book.collect(all_aud=True)
    fees(directory,clock)
    assert service.load(manifest)==original
    raw=AuditStore(directory).get('inputs',manifest)
    assert 'levels' not in json.dumps(raw) and 'bid_size' not in json.dumps(raw)

@pytest.mark.parametrize('dependency',['parquet','source','fee'])
def test_missing_dependencies_reported(prepared_env,dependency,capsys):
    directory,clock,service,_=prepared_env
    original=service.prepare(request(clock));manifest=service.persist(original)
    if dependency=='parquet':service.store.partition_path('BTC-AUD','orderbook','2026-11').unlink()
    elif dependency=='source':(directory/original.inputs.quote.source.response_id).unlink()
    else:AuditStore(directory).path('fees',original.inputs.fee_schedule.version).unlink()
    with pytest.raises((ValueError,FileNotFoundError)):service.load(manifest)
    assert cli.main(['inputs','load','--manifest',manifest,'--data-dir',str(directory)])==1
    assert json.loads(capsys.readouterr().out)['error'] in ('missing_referenced_dependency','missing_or_changed_parquet_dependency')

def test_future_exclusion_historical_fees_and_explicit_assumption(prepared_env):
    directory,clock,service,_=prepared_env
    old=request(clock,observation_time=NOW-timedelta(seconds=1))
    report=service.prepare(old)
    assert report.inputs.quote is None and report.status=='inputs_incomplete'
    historical=request(clock,observation_time=NOW+timedelta(seconds=4))
    report=service.prepare(historical)
    assert report.inputs.fee_schedule is None and 'historical_fee_unavailable' in report.missing_inputs
    report=service.prepare(historical,PreparationConfig(allow_present_day_fee_assumption=True))
    assert report.status=='inputs_ready' and report.inputs.fee_schedule.present_day_assumption
    assert 'present_day_fee_assumption_not_historical_applicability' in report.warnings
    with pytest.raises(ValueError):service.prepare(request(clock,observation_time=clock()+timedelta(seconds=1)))

def test_missing_market_and_stale_inputs(prepared_env):
    directory,clock,service,_=prepared_env
    with pytest.raises(ValueError):service.prepare(request(clock,pair_id='UNKNOWN-AUD'))
    clock.sleep(90000)
    prepared=service.prepare(request(clock))
    assert 'stale_fee_information' in prepared.missing_inputs
    assert 'stale_quote_and_orderbook' in prepared.missing_inputs
    assert 'stale_ohlcv' in prepared.missing_inputs

def test_source_consistency_rejected(prepared_env):
    directory,clock,service,_=prepared_env
    for path in (directory/'book_collection/responses/BTC-AUD').glob('*.json'):
        raw=json.loads(path.read_text());raw['kraken_pair_id']='WRONG';path.write_text(json.dumps(raw))
    prepared=service.prepare(request(clock))
    assert prepared.status=='inputs_incomplete' and prepared.inputs.quote is None
    assert 'missing_or_inconsistent_source_provenance' in prepared.missing_inputs

def test_missing_fee_and_quality_persistence(data_dir):
    clock=Clock();service=InputService(data_dir,clock=clock)
    prepared=service.prepare(request(clock))
    assert prepared.status=='inputs_incomplete' and 'fee_schedule_missing' in prepared.missing_inputs
    version=service.persist(prepared);service.persist(prepared)
    with connect_metadata(data_dir,readonly=True) as db:
        assert db.execute("SELECT count(*) FROM data_quality_issues WHERE dataset='costs_1m'").fetchone()[0]==len(prepared.missing_inputs)
        assert db.execute('SELECT count(*) FROM eligibility').fetchone()[0]==0
    assert not list((data_dir/'market_data').glob('*/costs_1m/*.parquet'))

@pytest.mark.parametrize('method',[n for n in vars(algorithms.CostAlgorithms) if n.startswith('calculate_')])
def test_all_placeholders_raise(method):
    with pytest.raises(NotImplementedError,match='after Milestone 10'):getattr(algorithms.CostAlgorithms(),method)(None)

def test_no_algorithm_logic_and_no_fabricated_results(prepared_env):
    tree=ast.parse(inspect.getsource(algorithms))
    methods=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef)]
    assert len(methods)==5 and all(len(n.body)==1 and isinstance(n.body[0],ast.Raise) for n in methods)
    _,clock,service,_=prepared_env;inputs=service.prepare(request(clock)).inputs
    result=CostResult(inputs.request,inputs.provenance)
    assert result.total_cost_pct is None and result.execution_price is None
    with pytest.raises(ValueError):replace(result,status='calculated')
    with pytest.raises(ValueError):replace(result,total_cost_pct=Decimal('0'))
    with pytest.raises(ValueError):replace(result,filled_quantity=Decimal('1'))
    assert dataset_schema('costs_1m').metadata[b'schema_version']==b'1'
    assert dataset_schema('costs_1m').names==['pair_id','timestamp','side','order_size_aud','fee_type','fee_pct','half_spread_pct','slippage_pct','total_cost_pct','model_version']

def test_readonly_cli_no_writes(prepared_env,capsys):
    directory,clock,service,_=prepared_env
    before={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    assert cli.main(['algorithms','status'])==0
    assert all(v=='NOT_IMPLEMENTED' for v in json.loads(capsys.readouterr().out).values())
    assert cli.main(['fees','report','--data-dir',str(directory)])==0
    capsys.readouterr()
    assert cli.main(['inputs','report','--all-aud','--data-dir',str(directory),'--observation-time',clock().isoformat()])==0
    assert len(json.loads(capsys.readouterr().out))==6
    assert before=={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}

def test_audit_tamper_and_invalid_version(data_dir):
    clock=Clock();version=fees(data_dir,clock);store=AuditStore(data_dir)
    with pytest.raises(ValueError):store.get('fees','../secret')
    path=store.path('fees',version);raw=json.loads(path.read_text());raw['retrieved_at']='tampered';path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):store.get('fees',version)


def test_received_after_request_not_available(prepared_env):
    directory,clock,service,_=prepared_env
    for path in (directory/'book_collection/responses/BTC-AUD').glob('*.json'):
        raw=json.loads(path.read_text());raw['finished_at']=(clock()+timedelta(seconds=1)).isoformat();path.write_text(json.dumps(raw))
    prepared=service.prepare(request(clock))
    assert prepared.inputs.quote is None and prepared.status=='inputs_incomplete'

def test_missing_book_levels_and_timestamp_mismatch(prepared_env):
    directory,clock,service,_=prepared_env
    path=service.store.partition_path('BTC-AUD','orderbook','2026-11')
    rows=service.store.read_all('BTC-AUD','orderbook').to_pylist();path.unlink()
    service.store.write('orderbook',[r for r in rows if r['side']=='bid'])
    prepared=service.prepare(request(clock))
    assert 'missing_or_duplicate_book_levels' in prepared.missing_inputs
    path.unlink()
    for r in rows:r['timestamp']-=timedelta(seconds=1)
    service.store.write('orderbook',rows)
    assert 'quote_book_timestamp_mismatch' in service.prepare(request(clock)).missing_inputs

def test_metadata_unknown_constraints_and_future_metadata(prepared_env):
    directory,clock,service,_=prepared_env
    prepared=service.prepare(request(clock))
    assert 'optional_exchange_constraints_unavailable' in prepared.warnings
    # Query-time metadata observed later cannot be silently used for a historical request.
    from kraken_quant.asset_catalogue import discover
    from test_ohlcv_collector import Catalogue
    discover(directory,transport=Catalogue(),clock=clock)
    historical=service.prepare(request(clock,observation_time=clock()-timedelta(seconds=1)))
    assert 'historical_market_metadata_unavailable' in historical.missing_inputs
    assert historical.inputs.market_metadata is None and historical.inputs.quote is None
    assert service.load(service.persist(historical))==historical

def test_persistence_failure_does_not_publish_partial_manifest(prepared_env,monkeypatch):
    directory,clock,service,_=prepared_env
    prepared=service.prepare(request(clock))
    import kraken_quant.cost_storage as storage
    def fail(*args):raise OSError('interrupted')
    monkeypatch.setattr(storage,'_atomic_json',fail)
    with pytest.raises(OSError):service.persist(prepared)
    assert not service.audit.versions('inputs')

def test_calculated_preparation_forbidden(prepared_env):
    _,clock,service,_=prepared_env
    prepared=service.prepare(request(clock))
    with pytest.raises(ValueError):replace(prepared,status='calculated')
    with pytest.raises(ValueError):replace(prepared,missing_inputs=('missing',))

def test_failed_readonly_adapter_is_explicit(data_dir):
    class Failed:
        def permissions(self):raise RuntimeError('private authentication error')
    version=fees(data_dir,Clock(),readonly=Failed())
    assert AuditStore(data_dir).get('fees',version)['account_status']=='unavailable_readonly_query_failed'
    assert fee_view(data_dir,version,'BTC-AUD').account_taker_pct is None

def test_stale_fee_report(data_dir):
    clock=Clock();fees(data_dir,clock)
    assert fee_report(data_dir,now=clock()+timedelta(days=2))[0]['verification_status']=='stale'

def test_cli_prepare_and_load(prepared_env,monkeypatch,capsys):
    directory,clock,service,_=prepared_env
    monkeypatch.setattr(cli,'InputService',lambda data_dir:service)
    args=['--data-dir',str(directory),'--observation-time',clock().isoformat()]
    assert cli.main(['inputs','prepare','--pair','BTC-AUD','--notional-aud','100',*args])==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='inputs_ready' and result['inputs']['request']['notional_aud']=='100'
    assert cli.main(['inputs','load','--manifest',result['manifest'],'--data-dir',str(directory)])==0
    assert json.loads(capsys.readouterr().out)['status']=='inputs_ready'

@pytest.mark.skipif(os.environ.get('KRAKEN_COST_SMOKE')!='1',reason='Opt-in official public fee and persisted real input integration')
def test_live_public_fee_and_existing_real_inputs(tmp_path):
    from kraken_quant.db_environment import load_config
    real=load_config().data_dir
    # Explicit integration uses existing catalogue and persisted raw observations,
    # and requests the official public page; it does not write market data.
    service=InputService(real)
    assets=collection_universe(real)[0]
    body=__import__('kraken_quant.market_data',fromlist=['public_get']).public_get('FeeSchedule',{})
    assert parse_public_page(body)
    rows=service.store.read_all('BTC-AUD','quotes_1m').to_pylist()
    assert rows
    timestamp=max(r['timestamp'] for r in rows)+timedelta(seconds=10)
    prepared=service.prepare(CostRequest('BTC-AUD','buy',Decimal('1000'),'taker',timestamp,'reserved-v1'),PreparationConfig(allow_present_day_fee_assumption=True))
    assert prepared.inputs.quote and prepared.inputs.order_book and prepared.inputs.fee_schedule


def test_invalid_newer_snapshot_fallback_is_reported(prepared_env):
    directory,clock,service,_=prepared_env
    original=service.prepare(request(clock));clock.sleep(70)
    collector,_,_=book_setup(directory,clock);collector.collect(all_aud=True)
    for path in (directory/'book_collection/responses/BTC-AUD').glob('*.json'):
        raw=json.loads(path.read_text())
        if read_time(raw['started_at'])>original.inputs.quote.timestamp:
            raw['kraken_pair_id']='WRONG';path.write_text(json.dumps(raw))
    prepared=service.prepare(request(clock))
    assert prepared.inputs.quote.timestamp==original.inputs.quote.timestamp
    assert any(w.startswith('rejected_newer_input:') for w in prepared.warnings)
