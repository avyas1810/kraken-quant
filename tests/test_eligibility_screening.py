"""Structural screening tests. Financial formulas are deliberately absent."""
from copy import deepcopy
from dataclasses import replace,FrozenInstanceError
from datetime import timedelta
from decimal import Decimal
import ast,csv,inspect,json
from pathlib import Path
import pytest
pytest.importorskip('pyarrow')
from test_ohlcv_collector import data_dir,Clock,NOW,Catalogue,discover,collector as candle_collector,Public as CandlePublic
from test_book_collector import setup as book_setup
from test_cost_infrastructure import fees
from kraken_quant import eligibility_manager as cli
from kraken_quant.eligibility_filters import *
from kraken_quant.eligibility_evidence import PointInTimeEvidence,resolve_input_reference
from kraken_quant.metadata_store import connect_metadata
from kraken_quant.cost_storage import payload,digest
from kraken_quant.ohlcv_collector import sql_time,minute_start,read_time

@pytest.fixture
def env(data_dir):
    clock=Clock();book,_,_=book_setup(data_dir,clock);book.collect(all_aud=True)
    candle_collector(data_dir,CandlePublic(clock),clock).collect(all_aud=True)
    clock.sleep(5);fees(data_dir,clock)
    cat=Catalogue()
    for row in cat.pairs.values():row.update(ordermin='0.001',costmin='1',lot_decimals=8,pair_decimals=2)
    discover(data_dir,transport=cat,clock=clock)
    return data_dir,clock,cli.EligibilityManager(data_dir,clock=clock),cat

def context(as_of=NOW,**changes):
    metadata=dict(pair_id='BTC-AUD',kraken_pair_id='XBTAUD',status='online',market_type='spot',quote_asset='AUD',observed_at=sql_time(as_of),receipt_id='receipt.json',receipt_hash='hash',min_order_qty='0.001',min_order_value='1',qty_precision=8,price_precision=2)
    history=dict(known_minutes=129600,error=None,first=sql_time(minute_start(as_of)-timedelta(days=90)),last=sql_time(minute_start(as_of)-timedelta(minutes=1)),span_days=90,observed_minute_days=90,coverage=dict(coverage_pct=100),evidence=['price-receipt.json'])
    return FilterContext(**dict(dict(as_of=as_of,policy=ScreeningPolicy(),metadata=metadata,history=history,inputs=dict(evidence=['raw-reference'])),**changes))

def results(ctx):return tuple(fn(key,ctx) for key,fn in FILTERS.items())

def test_ten_registered_defaults_and_config():
    p=ScreeningPolicy();assert set(FILTERS)=={f'F{i:02d}' for i in range(1,11)}
    assert (p.min_volume_aud,p.max_spread_pct,p.depth_multiple,p.proposed_notional_aud,p.depth_band_pct,p.history_days,p.min_completeness_pct,p.max_cost_pct)==(Decimal('1000000'),Decimal('0.20'),Decimal('5'),Decimal('1000'),Decimal('0.5'),90,Decimal('99'),Decimal('0.50'))
    changed=ScreeningPolicy.from_dict(dict(min_volume_aud='2000000',max_spread_pct='0.1',history_days=30,min_completeness_pct='98',max_cost_pct='0.3'))
    assert changed.history_days==30 and changed.min_volume_aud==Decimal('2000000')

@pytest.mark.parametrize('values',[dict(min_volume_aud='0'),dict(max_spread_pct='NaN'),dict(max_cost_pct='101'),dict(proposed_notional_aud=-1),dict(depth_multiple='Infinity'),dict(history_days=0),dict(history_days=90.0),dict(market_max_age_seconds=0),dict(required_quote='USD'),dict(order_side='short'),dict(max_spread_pct=0.2)])
def test_invalid_policy(values):
    with pytest.raises((ValueError,TypeError)):ScreeningPolicy.from_dict(values)

@pytest.mark.parametrize('fid,key,value,expected',[
 ('F01','status','online','PASS'),('F01','status','offline','FAIL'),('F01','status','cancel_only','FAIL'),
 ('F02','market_type','spot','PASS'),('F02','market_type','future','FAIL'),('F02','market_type',None,'UNKNOWN'),
 ('F03','quote_asset','AUD','PASS'),('F03','quote_asset','USD','FAIL')])
def test_factual_filters(fid,key,value,expected):
    ctx=context();ctx.metadata[key]=value
    assert FILTERS[fid](fid,ctx).status==expected

def test_historical_and_stale_status_is_unknown():
    ctx=context();past=replace(ctx,as_of=NOW+timedelta(seconds=1))
    assert factual('F01',past).reason=='status_between_observations_unavailable'
    stale=replace(ctx,as_of=NOW+timedelta(days=2))
    assert factual('F01',stale).reason=='stale_metadata_status'
    assert factual('F02',stale).status=='PASS'
    future=replace(ctx,as_of=NOW-timedelta(seconds=1))
    assert factual('F01',future).reason=='future_metadata_rejected'
    assert reserved('F09',future).status=='UNKNOWN'

def test_f07_span_and_separate_completeness():
    ctx=context();assert history_filter('F07',ctx).status=='PASS'
    ctx.history['first']=sql_time(minute_start(NOW)-timedelta(days=2))
    assert history_filter('F07',ctx).status=='FAIL'
    ctx.history['coverage']['coverage_pct']=98.5
    assert history_filter('F08',ctx).status=='FAIL'
    assert history_filter('F08',replace(ctx,policy=replace(ctx.policy,min_completeness_pct=Decimal('98')))).status=='PASS'

@pytest.mark.parametrize('fid',['F04','F05','F06','F10'])
def test_financial_filters_unimplemented(fid):
    r=reserved(fid,context());assert r.status=='NOT_IMPLEMENTED' and r.observed_value is None

@pytest.mark.parametrize('method',['volume_24h_aud','average_spread','execution_side_depth','order_feasibility','execution_cost'])
def test_placeholder_exceptions(method):
    with pytest.raises(NotImplementedError,match='after Milestone 10'):getattr(ScreeningAlgorithms(),method)(context())

def test_f09_structural_only():
    ctx=context();r=reserved('F09',ctx)
    assert r.status=='NOT_IMPLEMENTED' and r.observed_value['min_order_value']=='1'
    assert r.threshold['proposed_notional_aud']==Decimal('1000')
    ctx.metadata['min_order_qty']=None
    assert reserved('F09',ctx).status=='UNKNOWN'
    assert reserved('F09',replace(ctx,metadata=None)).status=='UNKNOWN'

def test_missing_values_never_pass():
    ctx=context(metadata=None,history=None)
    assert not any(r.status=='PASS' for r in results(ctx))
    with pytest.raises(ValueError):FilterResult('F04','PASS',None,None,(),None)

def test_separate_dimensions_rejection_pending_and_eligibility():
    rs=results(context());d,overall=aggregate(rs)
    assert d==dict(exchange='PASS',data='PASS',liquidity='NOT_IMPLEMENTED',execution='NOT_IMPLEMENTED')
    assert overall=='PENDING_EVALUATION'
    rs=tuple(replace(r,status='FAIL',reason='bad') if r.filter_id=='F07' else r for r in rs)
    assert aggregate(rs)[1]=='REJECTED'
    rs=tuple(replace(r,status='PASS',observed_value='synthetic validated fact',evidence=('fixture',),reason=None) for r in rs)
    assert aggregate(rs)[1]=='ELIGIBLE'
    rs=tuple(replace(r,status='UNKNOWN') if r.filter_id=='F08' else r for r in rs)
    assert aggregate(rs)[1]=='INSUFFICIENT_DATA'
    with pytest.raises(ValueError):aggregate(rs[:-1])

def test_realistic_fixture_screening_and_sql(env):
    directory,clock,manager,_=env
    report=manager.evaluate(as_of=clock(),all_aud=True)
    assert len(report['markets'])==3
    btc=next(r for r in report['markets'] if r['pair_id']=='BTC-AUD')
    assert btc['dimensions']['exchange']=='PASS' and btc['overall']=='REJECTED'
    fs={f['filter_id']:f for f in btc['filters']}
    assert fs['F07']['status']=='FAIL' and fs['F08']['status']=='FAIL' and fs['F09']['status']=='NOT_IMPLEMENTED'
    stored=manager.persist(report);again=manager.persist(manager.evaluate(as_of=clock(),all_aud=True))
    assert stored==again and stored['projection_verification']['status']=='verified'
    with connect_metadata(directory,readonly=True) as db:
        assert db.execute('SELECT count(*) FROM eligibility').fetchone()[0]==3
        row=db.execute("SELECT screened_at,volume_24h_aud,volume_24h_aud_status,completeness_pct,decision FROM eligibility WHERE pair_id='BTC-AUD'").fetchone()
        assert row[0]==sql_time(clock()) and row[1] is None and row[2]=='unavailable' and Decimal(row[3])<99 and row[4]=='ineligible'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()

def test_future_backfill_metadata_and_books_do_not_change_past(env):
    directory,clock,manager,cat=env;asof=clock();first=manager.evaluate(as_of=asof,all_aud=True)
    clock.sleep(120)
    book,_,_=book_setup(directory,clock);book.collect(all_aud=True)
    candle_collector(directory,CandlePublic(clock),clock).collect(all_aud=True)
    cat.pairs['XBTAUD']['status']='offline'
    cat.native['NEW']=dict(aclass='currency',altname='NEW');cat.display['NEW']=dict(aclass='currency',altname='NEW')
    cat.pairs['NEWAUD']=dict(base='NEW',quote='ZAUD',aclass_base='currency',aclass_quote='currency',execution_venue='international',wsname='NEW/AUD',status='online')
    discover(directory,transport=cat,clock=clock)
    second=manager.evaluate(as_of=asof,all_aud=True)
    assert first==second and 'NEW-AUD' not in {r['pair_id'] for r in second['markets']}
    btc=next(r for r in second['markets'] if r['pair_id']=='BTC-AUD')
    assert btc['metadata']['status']=='online'

def test_later_collected_earlier_candle_excluded(env):
    directory,clock,manager,_=env;asof=clock();before=manager.evaluate(as_of=asof,pair='BTC-AUD')
    clock.sleep(10)
    from test_ohlcv_collector import candle,response
    public=CandlePublic(clock)
    public.body=response(rows=[candle(minute_start(asof)-timedelta(minutes=i)) for i in (10,3,2,1,0)],now=clock())
    candle_collector(directory,public,clock).collect(pair='BTC-AUD')
    after=manager.evaluate(as_of=asof,pair='BTC-AUD')
    assert before==after

def test_no_future_metadata_and_unknown_pair(env):
    directory,clock,manager,_=env
    unknown=manager.evaluate(as_of=clock(),pair='MISSING-AUD')
    assert unknown['markets'][0]['overall']=='UNKNOWN'
    stored=manager.persist(unknown)
    assert stored['projection_verification']['status']=='sidecar_only_or_uncommitted'
    earliest=NOW-timedelta(days=2)
    past=manager.evaluate(as_of=earliest,pair='BTC-AUD')['markets'][0]
    assert past['metadata'] is None and past['overall']=='UNKNOWN'
    assert manager.evaluate(as_of=earliest,all_aud=True)['universe_status'].startswith('UNKNOWN')
    with pytest.raises(ValueError):manager.evaluate(as_of=clock()+timedelta(seconds=1),all_aud=True)

def test_conflicting_policy_preserves_sql_and_sidecar(env):
    directory,clock,manager,_=env
    first=manager.persist(manager.evaluate(as_of=clock(),pair='BTC-AUD'))
    with connect_metadata(directory,readonly=True) as db:before=db.execute('SELECT * FROM eligibility').fetchall()
    different=manager.evaluate(as_of=clock(),pair='BTC-AUD',policy=replace(ScreeningPolicy(),min_completeness_pct=Decimal('0.001')))
    second=manager.persist(different)
    assert second['projection_verification']['status']=='conflict' and first['report_id']!=second['report_id']
    with connect_metadata(directory,readonly=True) as db:assert before==db.execute('SELECT * FROM eligibility').fetchall()
    assert manager.load(second['report_id'])['evaluation']==different
    with pytest.raises(ValueError):manager.export(second['report_id'])

def test_csv_exports_and_empty_eligible(env):
    directory,clock,manager,_=env
    saved=manager.persist(manager.evaluate(as_of=clock(),all_aud=True));out=manager.export(saved['report_id'])
    assert len(out['exports'])==7
    assert out['exports']['eligible.csv']['rows']==0
    assert out['exports']['rejected.csv']['rows']==3
    assert out['exports']['filter_evidence.csv']['rows']==30
    path=Path(out['exports']['filter_evidence.csv']['path'])
    with path.open(newline='',encoding='utf-8') as h:rows=list(csv.DictReader(h))
    assert rows[0]['as_of']==sql_time(clock()) and all(r['status']!='PASS' for r in rows if r['filter_id'] in ('F04','F05','F06','F09','F10'))
    assert manager.export(saved['report_id'])==out

def test_raw_inputs_provenance_and_full_book_resolution(env):
    directory,clock,manager,_=env
    item=manager.evaluate(as_of=clock(),pair='BTC-AUD')['markets'][0]
    refs=item['raw_inputs'];assert refs['aligned_quote_book']
    assert len(resolve_input_reference(directory,refs['orderbook'],clock()))==4
    assert resolve_input_reference(directory,refs['quote'],clock())[0]['bid']==Decimal('100.000000000000000001')
    assert refs['fee'] is not None and 'applicable_account_fee_tier_unknown' in refs['issues']

def test_missing_and_stale_inputs(env):
    directory,clock,manager,_=env
    clock.sleep(400)
    item=manager.evaluate(as_of=clock(),pair='BTC-AUD')['markets'][0]
    assert 'stale_quote' in item['raw_inputs']['issues'] and 'stale_orderbook' in item['raw_inputs']['issues']
    assert {f['filter_id']:f for f in item['filters']}['F01']['status']=='UNKNOWN'

def test_missing_price_provenance_unknown_not_zero(env):
    directory,clock,manager,_=env
    for path in (directory/'ohlcv_collection/responses/BTC-AUD').glob('*.json'):path.unlink()
    item=manager.evaluate(as_of=clock(),pair='BTC-AUD')['markets'][0]
    assert all(f['status']=='UNKNOWN' for f in item['filters'] if f['filter_id'] in ('F07','F08'))

def test_no_financial_algorithm_logic_or_forged_pass(env):
    import kraken_quant.eligibility_filters as filters
    tree=ast.parse(inspect.getsource(ScreeningAlgorithms))
    methods=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef)]
    assert len(methods)==5 and all(len(n.body)==1 and isinstance(n.body[0],ast.Raise) for n in methods)
    _,clock,manager,_=env;r=manager.evaluate(as_of=clock(),pair='BTC-AUD')
    f=next(f for f in r['markets'][0]['filters'] if f['filter_id']=='F04');f.update(status='PASS',observed_value='fake',evidence=['fake'])
    with pytest.raises(ValueError):manager.persist(r)
    from kraken_quant.cost_algorithms import CostAlgorithms
    for name in ('calculate_spread_cost','calculate_book_execution','calculate_slippage','calculate_market_impact','calculate_total_cost'):
        with pytest.raises(NotImplementedError):getattr(CostAlgorithms(),name)(None)

def test_offline_cli_evaluate_report_export(env,monkeypatch,capsys):
    directory,clock,manager,_=env
    monkeypatch.setattr(cli,'EligibilityManager',lambda data_dir:manager)
    monkeypatch.setattr('kraken_quant.market_data.public_get',lambda *a,**k:(_ for _ in ()).throw(AssertionError('no network')))
    args=['--data-dir',str(directory),'--as-of',clock().isoformat()]
    assert cli.main(['filters','status'])==0;assert len(json.loads(capsys.readouterr().out))==10
    assert cli.main(['evaluate','--all-aud',*args])==0
    saved=json.loads(capsys.readouterr().out)
    assert cli.main(['report','--all-aud',*args])==0
    assert json.loads(capsys.readouterr().out)['report_id']==saved['report_id']
    assert cli.main(['export','--report-id',saved['report_id'],'--data-dir',str(directory)])==0
    assert json.loads(capsys.readouterr().out)['exports']['eligible.csv']['rows']==0


def test_future_fee_and_quality_changes_do_not_leak(env):
    directory,clock,manager,_=env;asof=clock();before=manager.evaluate(as_of=asof,pair='BTC-AUD')
    clock.sleep(10);fees(directory,clock)
    with connect_metadata(directory) as db:
        db.execute("INSERT INTO data_quality_issues(issue_id,pair_id,dataset,observation_at,issue_type,description,detected_at,resolution_status) VALUES ('future','BTC-AUD','prices_1m',?,'future_issue','not known earlier',?,'open')",(sql_time(clock()),sql_time(clock())))
    assert before==manager.evaluate(as_of=asof,pair='BTC-AUD')

def test_future_only_fee_is_not_substituted(env):
    directory,clock,manager,_=env
    asof=clock()-timedelta(seconds=1)
    # Original fee was retrieved at the same current fixture instant.
    item=manager.evaluate(as_of=asof,pair='BTC-AUD')['markets'][0]
    assert item['raw_inputs']['fee'] is None
    assert 'historical_fee_unavailable' in item['raw_inputs']['issues']

def test_metadata_receipt_tampering_is_unknown(env):
    directory,clock,manager,_=env
    path=max((p for p in (directory/'catalogue_discovery').glob('*.json') if p.name!='latest.json'),key=lambda p:json.loads(p.read_text())['observed_at'])
    doc=json.loads(path.read_text())
    next(r for r in doc['records'] if r['pair_id']=='BTC-AUD')['status']='offline'
    path.write_text(json.dumps(doc))
    item=manager.evaluate(as_of=clock(),pair='BTC-AUD')['markets'][0]
    assert item['metadata'] is None and item['overall']=='UNKNOWN'

def test_sql_failure_leaves_explicit_uncommitted_sidecar_then_recovers(env,monkeypatch):
    directory,clock,manager,_=env;report=manager.evaluate(as_of=clock(),pair='BTC-AUD')
    original=cli.connect_metadata
    from contextlib import contextmanager
    @contextmanager
    def broken(data_dir,readonly=False):
        with original(data_dir,readonly=readonly) as db:
            class Proxy:
                def execute(self,sql,*args):
                    if sql.startswith('INSERT INTO eligibility'):raise OSError('disk failure')
                    return db.execute(sql,*args)
            yield Proxy()
    monkeypatch.setattr(cli,'connect_metadata',broken)
    with pytest.raises(OSError):manager.persist(report)
    report_id=digest(report)
    assert manager.load(report_id)['projection_verification']['status']=='sidecar_only_or_uncommitted'
    monkeypatch.setattr(cli,'connect_metadata',original)
    assert manager.persist(report)['projection_verification']['status']=='verified'

def test_new_as_of_preserves_prior_rows_and_report_is_readonly(env):
    directory,clock,manager,_=env
    first=manager.persist(manager.evaluate(as_of=clock(),pair='BTC-AUD'))
    clock.sleep(1);second=manager.persist(manager.evaluate(as_of=clock(),pair='BTC-AUD'))
    assert first['report_id']!=second['report_id']
    with connect_metadata(directory,readonly=True) as db:assert db.execute('SELECT count(*) FROM eligibility').fetchone()[0]==2
    before={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    assert manager.report(pair='BTC-AUD')['report_id']==second['report_id']
    assert before=={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}

def test_csv_default_selects_all_aud_even_after_pair_evaluation(env,monkeypatch,capsys):
    directory,clock,manager,_=env
    all_markets=manager.persist(manager.evaluate(as_of=clock(),all_aud=True))
    clock.sleep(1);manager.persist(manager.evaluate(as_of=clock(),pair='BTC-AUD'))
    monkeypatch.setattr(cli,'EligibilityManager',lambda path:manager)
    assert cli.main(['export','--format','csv','--data-dir',str(directory)])==0
    assert json.loads(capsys.readouterr().out)['report_id']==all_markets['report_id']

def test_no_network_or_market_mutation_during_evaluation(env,monkeypatch):
    directory,clock,manager,_=env
    monkeypatch.setattr('kraken_quant.market_data.public_get',lambda *a,**k:(_ for _ in ()).throw(AssertionError()))
    before={str(p):p.read_bytes() for p in (directory/'market_data').rglob('*') if p.is_file()}
    manager.persist(manager.evaluate(as_of=clock(),all_aud=True))
    assert before=={str(p):p.read_bytes() for p in (directory/'market_data').rglob('*') if p.is_file()}

def test_reserved_return_value_is_not_accepted(monkeypatch):
    monkeypatch.setattr(ScreeningAlgorithms,'volume_24h_aud',lambda self,c:Decimal('0'))
    with pytest.raises(RuntimeError):reserved('F04',context())

def test_csv_conflict_does_not_overwrite(env):
    _,clock,manager,_=env
    saved=manager.persist(manager.evaluate(as_of=clock(),all_aud=True));out=manager.export(saved['report_id'])
    path=Path(out['exports']['eligible.csv']['path']);path.write_text('user content')
    with pytest.raises(ValueError):manager.export(saved['report_id'])
    assert path.read_text()=='user content'


def test_evidence_reopen_detects_missing_dependency(env):
    directory,clock,manager,_=env
    saved=manager.persist(manager.evaluate(as_of=clock(),pair='BTC-AUD'))
    assert manager.verify_evidence(saved['report_id'])['status']=='verified'
    ref=saved['evaluation']['markets'][0]['metadata']['receipt_id']
    (directory/ref).unlink()
    assert manager.verify_evidence(saved['report_id'])['status']=='dependency_changed_or_unavailable'
    with pytest.raises(ValueError):manager.export(saved['report_id'])

def test_csv_hash_is_actual_file_hash(env):
    import hashlib
    _,clock,manager,_=env
    saved=manager.persist(manager.evaluate(as_of=clock(),all_aud=True))
    exported=manager.export(saved['report_id'])
    assert all(hashlib.sha256(Path(v['path']).read_bytes()).hexdigest()==v['sha256'] for v in exported['exports'].values())

def test_unknown_readiness_and_non_aud_rejection(env):
    _,clock,manager,_=env
    item=manager.evaluate(as_of=clock(),pair='ADA-USD')['markets'][0]
    assert next(f for f in item['filters'] if f['filter_id']=='F03')['status']=='FAIL'
    assert item['overall']=='REJECTED'
    ctx=context(history=None)
    assert aggregate(results(ctx))[1]=='INSUFFICIENT_DATA'

def test_f08_is_existing_coverage_measurement(env):
    directory,clock,manager,_=env
    item=manager.evaluate(as_of=clock(),pair='BTC-AUD')['markets'][0]
    from kraken_quant.history_coverage import minute_coverage
    h=item['history'];asof=clock();start=minute_start(asof)-timedelta(days=90)
    times=[minute_start(NOW)-timedelta(minutes=i) for i in (1,2,3)]
    assert h['coverage']==minute_coverage(times,start,minute_start(asof),asof)

def test_evaluation_does_not_write(env):
    directory,clock,manager,_=env
    before={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    manager.evaluate(as_of=clock(),all_aud=True)
    assert before=={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}


def test_corrupt_source_evidence_is_explicit_unknown(env):
    root,clock,manager,_=env
    history=next((root/'ohlcv_collection/responses/BTC-AUD').glob('*.json'))
    history.write_text('{broken')
    book=next((root/'book_collection/responses/BTC-AUD').glob('*.json'))
    book.write_text('{broken')
    item=manager.evaluate(pair='BTC-AUD')['markets'][0]
    assert item['history']['error']=='unreadable_history_provenance'
    assert {f['status'] for f in item['filters'] if f['filter_id'] in ('F07','F08')}=={'UNKNOWN'}
    assert 'unreadable_book_provenance' in item['raw_inputs']['issues']


def test_corrupt_source_namespace_is_unknown(env):
    root,clock,manager,_=env
    (root/'ohlcv_collection/source.json').write_text('{broken')
    item=manager.evaluate(pair='BTC-AUD')['markets'][0]
    assert item['history']['error']=='price_source_provenance_missing'


@pytest.mark.parametrize('legacy',[False,True])
def test_verify_pins_original_selection_after_later_persistence(env,legacy):
    root,clock,manager,_=env
    from test_ohlcv_collector import candle
    receipt=next((root/'ohlcv_collection/responses/BTC-AUD').glob('*.json'))
    doc=json.loads(receipt.read_text());result=doc['body']['result'];key=next(k for k in result if k!='last')
    stamp=minute_start(clock())-timedelta(minutes=10)
    result[key].insert(0,candle(stamp));receipt.write_text(json.dumps(doc))
    # The original response contained this completed row, but an earlier bounded
    # ingestion had not persisted it. This must not enlarge an immutable report.
    report=manager.evaluate(pair='BTC-AUD')
    assert report['markets'][0]['history']['known_minutes']==3
    if legacy:report['markets'][0]['history'].pop('selection_intervals')
    saved=manager.persist(report)
    from kraken_quant.ohlcv_collector import parse_ohlc
    record=next(r for r in parse_ohlc(doc['body'],report['markets'][0]['metadata'],read_time(doc['fetched_at']))['records'] if r['timestamp']==stamp)
    from kraken_quant.parquet_store import MonthlyParquetStore
    clock.sleep(10);MonthlyParquetStore(root).write('prices_1m',[record])
    assert manager.evaluate(as_of=read_time(report['as_of']),pair='BTC-AUD')['markets'][0]['history']['known_minutes']==4
    assert manager.verify_evidence(saved['report_id'])['status']=='verified'
    assert manager.load(saved['report_id'])['evaluation']==report
