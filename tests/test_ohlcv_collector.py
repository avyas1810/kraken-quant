"""Synthetic OHLCV tests use isolated, explicitly injected data namespaces."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3

import pytest
pytest.importorskip("pyarrow")

from kraken_quant import ohlcv_collector as mod
from kraken_quant.asset_catalogue import discover, catalogue_report
from kraken_quant.metadata_store import initialize_metadata, connect_metadata
from kraken_quant.parquet_store import MonthlyParquetStore

NOW=datetime(2026,11,1,0,3,30,tzinfo=timezone.utc)


class Clock:
    def __init__(self,now=NOW):self.now=now;self.elapsed=0;self.sleeps=[]
    def __call__(self):return self.now
    def monotonic(self):return self.elapsed
    def sleep(self,seconds):
        self.sleeps.append(seconds);self.elapsed+=seconds;self.now+=timedelta(seconds=seconds)


class Catalogue:
    def __init__(self):
        self.pairs={}
        self.native={name:{"aclass":"currency","altname":alias} for name,alias in
                     (("XXBT","XBT"),("XETH","ETH"),("ADA","ADA"),("ZAUD","AUD"),("ZUSD","USD"))}
        self.display={name:{"aclass":"currency","altname":alias} for name,alias in
                      (("BTC","XBT"),("ETH","ETH"),("ADA","ADA"),("AUD","AUD"),("USD","USD"))}
        for name,base,quote,status in (("XBTAUD","XXBT","ZAUD","online"),("ETHAUD","XETH","ZAUD","online"),
                                      ("ADAUSD","ADA","ZUSD","online"),("ADAAUD","ADA","ZAUD","post_only")):
            self.pairs[name]=dict(base=base,quote=quote,aclass_base="currency",aclass_quote="currency",execution_venue="international",
                                  wsname=self.native[base]["altname"]+"/"+self.native[quote]["altname"],status=status)
    def __call__(self,endpoint,params):
        assert endpoint in ("AssetPairs","Assets")
        kind=params.get("aclass_base",params.get("aclass"))
        if kind=="tokenized_asset":
            result={"UNRESOLVED":dict(aclass_base="tokenized_asset")} if endpoint=="AssetPairs" else {}
        elif endpoint=="AssetPairs":result=self.pairs
        else:result=self.display if params.get("assetVersion")==1 else self.native
        return {"error":[],"result":deepcopy(result)}


@pytest.fixture
def data_dir(tmp_path):
    initialize_metadata(tmp_path)
    discover(tmp_path,transport=Catalogue(),clock=lambda:NOW-timedelta(days=1))
    return tmp_path


def candle(stamp,**changes):
    row=[int(stamp.timestamp()),"100.000000000000000001","102","99","101","100.5","1.250000000000000001",2]
    indices=dict(open=1,high=2,low=3,close=4,vwap=5,volume=6,count=7,timestamp=0)
    for key,value in changes.items():row[indices[key]]=value
    return row


def response(rows=None,key="BTC/AUD",now=NOW,last=None):
    current=mod.minute_start(now)
    rows=rows if rows is not None else [candle(current-timedelta(minutes=i)) for i in (3,2,1,0)]
    return {"error":[],"result":{key:rows,"last":int((current-mod.MINUTE).timestamp()) if last is None else last}}


class Public:
    def __init__(self,clock=None):self.clock=clock or Clock();self.calls=[];self.body=None;self.failure=None
    def __call__(self,endpoint,params):
        self.calls.append((endpoint,dict(params),self.clock.monotonic()))
        assert endpoint=="OHLC" and params["interval"]==1 and params["assetVersion"]==1
        if self.failure:raise self.failure
        if self.body is not None:return deepcopy(self.body)
        key={"XBTAUD":"BTC/AUD","ETHAUD":"ETH/AUD"}[params["pair"]]
        return response(key=key,now=self.clock())


def collector(data_dir,public=None,clock=None):
    clock=clock or (public.clock if public else Clock())
    public=public or Public(clock)
    return mod.OhlcvCollector(data_dir,transport=public,clock=clock,sleeper=clock.sleep,monotonic=clock.monotonic)


def one(run):return run["results"][0]


def test_universe_uses_sql_and_consistent_positive_spot_audit(data_dir,monkeypatch):
    from kraken_quant.config import CONFIG
    monkeypatch.setattr(type(CONFIG),"universe",property(lambda self:pytest.fail("Research universe accessed")),raising=False)
    assets,run_id,unresolved=mod.collection_universe(data_dir)
    assert {a["pair_id"] for a in assets}=={"BTC-AUD","ETH-AUD"} and unresolved==1
    with connect_metadata(data_dir) as db:db.execute("UPDATE assets SET status='offline' WHERE pair_id='BTC-AUD'")
    with pytest.raises(mod.CollectionError,match="catalogue_audit_missing_or_stale"):mod.collection_universe(data_dir)


def test_unconfirmed_provenance_and_unknown_pair_rejected(data_dir):
    public=Public()
    with pytest.raises(mod.CollectionError,match="pair_not_online"):collector(data_dir,public).collect(pair="ADA-AUD")
    assert not public.calls
    path=data_dir/"catalogue_discovery/latest.json"
    receipt=json.loads(path.read_text());receipt["records"][0]["source_identity"]["execution_venue"]="unknown"
    # Find and change the positive BTC proof regardless of source ordering.
    for row in receipt["records"]:
        if row["pair_id"]=="BTC-AUD":row["source_identity"]["execution_venue"]="unknown"
    path.write_text(json.dumps(receipt))
    with pytest.raises(mod.CollectionError,match="spot_identity_not_confirmed"):collector(data_dir,public).collect(pair="BTC-AUD")
    assert not public.calls


def test_exact_fields_final_candle_and_native_request(data_dir):
    public=Public();run=collector(data_dir,public).collect(pair="BTC-AUD")
    result=one(run)
    assert result["status"]=="ok" and result["uncommitted_rows"]==1 and result["new_rows"]==3
    assert public.calls[0][1]=={"pair":"XBTAUD","interval":1,"assetVersion":1}
    rows=MonthlyParquetStore(data_dir).read_all("BTC-AUD","prices_1m").to_pylist()
    assert len(rows)==3 and all(r["is_complete"] and r["timestamp"]<mod.minute_start(NOW) for r in rows)
    assert str(rows[0]["open"])=="100.000000000000000001" and str(rows[0]["volume_base"])=="1.250000000000000001"
    assert rows[0]["timestamp"].tzinfo is not None and rows[0]["trade_count"]==2
    report=mod.coverage_report(data_dir,now=public.clock())
    btc=next(r for r in report["pairs"] if r["pair_id"]=="BTC-AUD")
    assert btc["checkpoint_status"]=="matches" and btc["stored_rows"]==3 and btc["missing_minutes"]==0


def test_repeat_and_marker_cannot_skip_unpersisted_minutes(data_dir):
    public=Public();first=collector(data_dir,public).collect(pair="BTC-AUD")
    state=data_dir/"ohlcv_collection/pairs/BTC-AUD.json"
    stored=json.loads(state.read_text())
    # Even a saved marker ahead of the checkpoint cannot advance the request beyond storage.
    stored["last_success"]["last_marker"]=int(NOW.timestamp())
    state.write_text(json.dumps(stored))
    second=collector(data_dir,public).collect(pair="BTC-AUD")
    assert one(second)["new_rows"]==0 and one(second)["overlap_duplicates"]==3
    assert public.calls[-1][1]["since"]==int((mod.minute_start(NOW)-mod.MINUTE).timestamp())
    assert MonthlyParquetStore(data_dir).read_all("BTC-AUD","prices_1m").num_rows==3


def test_all_aud_accounts_each_pair_and_paces_requests(data_dir):
    public=Public();run=collector(data_dir,public).collect(all_aud=True)
    assert run["attempted_pairs"]==run["http_requests"]==2
    assert {r["pair_id"] for r in run["results"]}=={"BTC-AUD","ETH-AUD"}
    assert all(r["status"]=="ok" for r in run["results"])
    assert public.calls[1][2]-public.calls[0][2]>=1.1
    assert run["unresolved_catalogue_records_unchanged"]==1


@pytest.mark.parametrize("bad", [None,{}, {"error":["exchange error"],"result":{}},
    {"error":[],"result":{"ETH/AUD":[],"last":1}},
    {"error":[],"result":{"BTC/AUD":"bad","last":1}},
    {"error":[],"result":{"BTC/AUD":[],"last":-1}}])
def test_invalid_response_or_missing_pair(data_dir,bad):
    class Bad:
        def __call__(self,*args):return deepcopy(bad)
    result=one(collector(data_dir,Bad(),Clock()).collect(pair="BTC-AUD"))
    assert result["status"]=="error"
    assert MonthlyParquetStore(data_dir).read_all("BTC-AUD","prices_1m").num_rows==0
    with connect_metadata(data_dir) as db:assert db.execute("SELECT * FROM ingestion_state").fetchall()==[]


@pytest.mark.parametrize("changes", [dict(open="0"),dict(high="98"),dict(low="103"),dict(volume="-1"),
    dict(count=-1),dict(count=2.5),dict(open=100.1),dict(close=None),dict(timestamp=int(NOW.timestamp())),
    dict(timestamp=int((mod.minute_start(NOW)+mod.MINUTE).timestamp()))])
def test_invalid_candles_rejected_and_reported(data_dir,changes):
    public=Public();rows=response()["result"]["BTC/AUD"]
    rows[1]=candle(mod.minute_start(NOW)-timedelta(minutes=2),**changes)
    public.body=response(rows)
    result=one(collector(data_dir,public).collect(pair="BTC-AUD"))
    assert result["status"]=="error" and result["invalid_observations_count"]>=1
    report=next(r for r in mod.coverage_report(data_dir,now=public.clock())["pairs"] if r["pair_id"]=="BTC-AUD")
    assert report["stored_rows"]==0 and report["invalid_observations_last_attempt"]>=1
    assert report["checkpoint"] is None


def test_identical_and_conflicting_response_duplicates(data_dir):
    public=Public();rows=response()["result"]["BTC/AUD"]
    public.body=response([rows[0],rows[0],*rows[1:]])
    result=one(collector(data_dir,public).collect(pair="BTC-AUD"))
    assert result["status"]=="ok" and result["identical_response_duplicates"]==1
    public.body=response([rows[0],candle(mod.minute_start(NOW)-timedelta(minutes=3),close="100"),*rows[1:]])
    result=one(collector(data_dir,public).collect(pair="BTC-AUD"))
    assert result["status"]=="error" and result["invalid_observations_count"]==1
    assert MonthlyParquetStore(data_dir).read_all("BTC-AUD","prices_1m").num_rows==3


def test_existing_conflict_preserves_partition_and_checkpoint(data_dir):
    public=Public();collector(data_dir,public).collect(pair="BTC-AUD")
    path=data_dir/"market_data/BTC-AUD/prices_1m/2026-11.parquet";before=path.read_bytes()
    with connect_metadata(data_dir,readonly=True) as db:before_checkpoint=db.execute("SELECT latest_observation_at FROM ingestion_state WHERE pair_id='BTC-AUD'").fetchone()[0]
    rows=response()["result"]["BTC/AUD"];rows[1][4]="100"
    public.body=response(rows)
    result=one(collector(data_dir,public).collect(pair="BTC-AUD"))
    assert result["status"]=="error" and result["code"]=="conflicting_stored_candle"
    assert path.read_bytes()==before
    with connect_metadata(data_dir,readonly=True) as db:assert db.execute("SELECT latest_observation_at FROM ingestion_state WHERE pair_id='BTC-AUD'").fetchone()[0]==before_checkpoint


def test_gap_zero_trade_and_nullable_count_are_distinct(data_dir):
    public=Public();current=mod.minute_start(NOW)
    zero=candle(current-timedelta(minutes=3),volume="0",count=0)
    nullable=candle(current-mod.MINUTE);nullable.pop()
    public.body=response([zero,nullable,candle(current)])
    assert one(collector(data_dir,public).collect(pair="BTC-AUD"))["status"]=="ok"
    report=next(r for r in mod.coverage_report(data_dir,now=public.clock())["pairs"] if r["pair_id"]=="BTC-AUD")
    assert (report["stored_rows"],report["expected_minutes"],report["missing_minutes"])==(2,3,1)
    assert report["explicit_zero_trade_candles"]==report["trade_count_unavailable"]==1
    assert report["gaps"][0]["classification"]=="unresolved_no_trade_or_missing_observation"


def test_empty_and_unfinished_only_never_advance_checkpoint(data_dir):
    public=Public();public.body=response([])
    assert one(collector(data_dir,public).collect(pair="BTC-AUD"))["status"]=="no_data"
    public.body=response([candle(mod.minute_start(NOW)-timedelta(minutes=10))])
    result=one(collector(data_dir,public).collect(pair="BTC-AUD"))
    assert result["status"]=="no_data" and result["uncommitted_rows"]==1
    with connect_metadata(data_dir) as db:assert db.execute("SELECT * FROM ingestion_state").fetchall()==[]


def test_checkpoint_failure_replays_after_parquet_write(data_dir,monkeypatch):
    public=Public();original=mod.OhlcvCollector._set_checkpoint
    def failure(self,asset,records):
        if records:raise sqlite3.OperationalError("synthetic checkpoint failure")
        return original(self,asset,records)
    monkeypatch.setattr(mod.OhlcvCollector,"_set_checkpoint",failure)
    assert one(collector(data_dir,public).collect(pair="BTC-AUD"))["status"]=="error"
    assert MonthlyParquetStore(data_dir).read_all("BTC-AUD","prices_1m").num_rows==3
    with connect_metadata(data_dir) as db:assert db.execute("SELECT * FROM ingestion_state").fetchall()==[]
    assert (data_dir/"ohlcv_collection/pending/BTC-AUD.json").exists()
    monkeypatch.setattr(mod.OhlcvCollector,"_set_checkpoint",original)
    result=one(collector(data_dir,public).collect(pair="BTC-AUD"))
    assert result["status"]=="ok" and result["recovered_pending"]
    assert not (data_dir/"ohlcv_collection/pending/BTC-AUD.json").exists()
    assert next(r for r in mod.coverage_report(data_dir,now=public.clock())["pairs"] if r["pair_id"]=="BTC-AUD")["checkpoint_status"]=="matches"


def test_partial_month_write_recovery(data_dir,monkeypatch):
    clock=Clock(datetime(2026,11,1,0,1,30,tzinfo=timezone.utc));public=Public(clock)
    stamps=[datetime(2026,10,31,23,58,tzinfo=timezone.utc),datetime(2026,10,31,23,59,tzinfo=timezone.utc),datetime(2026,11,1,tzinfo=timezone.utc),datetime(2026,11,1,0,1,tzinfo=timezone.utc)]
    public.body=response([candle(stamp) for stamp in stamps],now=clock())
    original=MonthlyParquetStore._replace
    def fail_nov(self,path,table,pair,dataset,month):
        if month=="2026-11":raise OSError("synthetic second-month failure")
        return original(self,path,table,pair,dataset,month)
    monkeypatch.setattr(MonthlyParquetStore,"_replace",fail_nov)
    assert one(collector(data_dir,public).collect(pair="BTC-AUD"))["status"]=="error"
    with connect_metadata(data_dir) as db:assert db.execute("SELECT * FROM ingestion_state").fetchall()==[]
    assert MonthlyParquetStore(data_dir).read_all("BTC-AUD","prices_1m").num_rows==2
    monkeypatch.setattr(MonthlyParquetStore,"_replace",original)
    assert one(collector(data_dir,public).collect(pair="BTC-AUD"))["status"]=="ok"
    assert MonthlyParquetStore(data_dir).months("BTC-AUD","prices_1m")==("2026-10","2026-11")
    assert MonthlyParquetStore(data_dir).read_all("BTC-AUD","prices_1m").num_rows==3


def test_checkpoint_ahead_of_storage_is_not_moved_backwards(data_dir):
    stamp=mod.sql_time(mod.minute_start(NOW)-mod.MINUTE)
    with connect_metadata(data_dir) as db:db.execute("INSERT INTO ingestion_state VALUES (?,?,?,?)",("BTC-AUD","prices_1m",stamp,mod.sql_time(NOW)))
    public=Public();result=one(collector(data_dir,public).collect(pair="BTC-AUD"))
    assert result["code"]=="checkpoint_ahead_of_verified_storage" and not public.calls
    with connect_metadata(data_dir) as db:assert db.execute("SELECT latest_observation_at FROM ingestion_state").fetchone()[0]==stamp


def test_network_failure_all_pairs_accounted_and_no_private_calls(data_dir):
    public=Public();public.failure=TimeoutError("SECRET_SENTINEL")
    run=collector(data_dir,public).collect(all_aud=True)
    assert run["attempted_pairs"]==2 and len(public.calls)==2
    assert all(r["code"]=="network_request_failed" for r in run["results"])
    assert "SECRET_SENTINEL" not in json.dumps(run)
    assert all(call[0]=="OHLC" for call in public.calls)


def test_rate_limit_defers_remaining_pairs_explicitly(data_dir):
    public=Public();public.body={"error":["EAPI:Rate limit exceeded"]}
    run=collector(data_dir,public).collect(all_aud=True)
    assert run["attempted_pairs"]==2 and run["http_requests"]==len(public.calls)==1
    assert [r["code"] for r in run["results"]]==["rate_limited","deferred_after_rate_limit"]


def test_real_and_injected_namespaces_cannot_mix(data_dir):
    collector(data_dir).collect(pair="BTC-AUD")
    with pytest.raises(mod.CollectionError,match="cannot_mix"):
        mod.OhlcvCollector(data_dir).collect(pair="BTC-AUD")


def test_report_has_no_api_or_writes_and_keeps_offline_history(data_dir,monkeypatch):
    public=Public();collector(data_dir,public).collect(pair="BTC-AUD")
    before=(data_dir/"metadata.sqlite").read_bytes()
    monkeypatch.setattr(mod.OhlcvCollector,"_request",lambda *args:pytest.fail("Report cannot request API"))
    report=mod.coverage_report(data_dir,now=public.clock())
    assert report["eligible_pair_count"]==2 and (data_dir/"metadata.sqlite").read_bytes()==before
    catalogue=Catalogue();catalogue.pairs["XBTAUD"]["status"]="post_only"
    discover(data_dir,transport=catalogue,clock=lambda:NOW)
    report=mod.coverage_report(data_dir,now=public.clock())
    btc=next(r for r in report["pairs"] if r["pair_id"]=="BTC-AUD")
    assert not btc["eligible_for_collection"] and btc["stored_rows"]==3 and report["eligible_pair_count"]==1


def test_missing_db_does_not_initialize_or_request(tmp_path):
    public=Public()
    with pytest.raises(sqlite3.OperationalError):collector(tmp_path,public).collect(all_aud=True)
    assert not (tmp_path/"metadata.sqlite").exists() and not public.calls


def test_cli_collect_report_and_errors(data_dir,monkeypatch,capsys):
    real=mod.OhlcvCollector;public=Public();clock=public.clock
    monkeypatch.setattr(mod,"OhlcvCollector",lambda data_dir:real(data_dir,transport=public,clock=clock,sleeper=clock.sleep,monotonic=clock.monotonic))
    args=["--data-dir",str(data_dir)]
    assert mod.main(["collect","--pair","BTC-AUD",*args])==0
    assert json.loads(capsys.readouterr().out)["attempted_pairs"]==1
    assert mod.main(["report",*args])==0
    assert json.loads(capsys.readouterr().out)["pair_count"]==2
    assert mod.main(["collect","--pair","NOT-AUD",*args])==1
    assert "pair_not_online" in capsys.readouterr().out


@pytest.mark.skipif(os.environ.get("KRAKEN_OHLCV_SMOKE")!="1",reason="Opt-in actual public OHLCV integration")
def test_live_btc_aud(tmp_path):
    from kraken_quant.db_environment import load_config
    from kraken_quant.asset_catalogue import COLUMNS
    real_dir=load_config().data_dir
    initialize_metadata(tmp_path)
    with connect_metadata(real_dir,readonly=True) as source:
        rows=source.execute("SELECT "+",".join(COLUMNS)+" FROM assets").fetchall()
    with connect_metadata(tmp_path) as destination:
        destination.executemany("INSERT INTO assets VALUES ("+",".join("?" for _ in COLUMNS)+")",rows)
    (tmp_path/"catalogue_discovery").mkdir()
    (tmp_path/"catalogue_discovery/latest.json").write_bytes((real_dir/"catalogue_discovery/latest.json").read_bytes())
    run=mod.OhlcvCollector(tmp_path).collect(pair="BTC-AUD")
    assert one(run)["status"]=="ok"
    rows=MonthlyParquetStore(tmp_path).read_all("BTC-AUD","prices_1m").to_pylist()
    assert rows and all(row["is_complete"] for row in rows)
    assert next(r for r in mod.coverage_report(tmp_path)["pairs"] if r["pair_id"]=="BTC-AUD")["checkpoint_status"]=="matches"



def test_catalogue_status_proof_mismatch_rejected(data_dir):
    path=data_dir/"catalogue_discovery/latest.json"
    receipt=json.loads(path.read_text())
    for row in receipt["records"]:
        if row["pair_id"]=="BTC-AUD":row["status"]="offline"
    path.write_text(json.dumps(receipt))
    with pytest.raises(mod.CollectionError,match="spot_identity_not_confirmed"):
        collector(data_dir).collect(pair="BTC-AUD")


def test_hard_interruption_keeps_replayable_intent_and_run_journal(data_dir,monkeypatch):
    public=Public();original=mod.OhlcvCollector._set_checkpoint
    def interrupt(self,asset,records):
        if records:raise KeyboardInterrupt("synthetic interruption after Parquet")
        return original(self,asset,records)
    monkeypatch.setattr(mod.OhlcvCollector,"_set_checkpoint",interrupt)
    with pytest.raises(KeyboardInterrupt):collector(data_dir,public).collect(pair="BTC-AUD")
    assert (data_dir/"ohlcv_collection/pending/BTC-AUD.json").is_file()
    journals=list((data_dir/"ohlcv_collection/runs").glob("*.json"))
    assert json.loads(journals[0].read_text())["status"]=="running"
    with connect_metadata(data_dir) as db:assert db.execute("SELECT * FROM ingestion_state").fetchall()==[]
    monkeypatch.setattr(mod.OhlcvCollector,"_set_checkpoint",original)
    result=one(collector(data_dir,public).collect(pair="BTC-AUD"))
    assert result["status"]=="ok" and result["recovered_pending"]
    assert MonthlyParquetStore(data_dir).read_all("BTC-AUD","prices_1m").num_rows==3



def test_unresolved_record_count_is_preserved_without_reclassification(data_dir):
    path=data_dir/"catalogue_discovery/latest.json"
    receipt=json.loads(path.read_text())
    receipt["unresolved"].append(deepcopy(receipt["unresolved"][0]))
    path.write_text(json.dumps(receipt))
    assets,audit_id,count=mod.collection_universe(data_dir)
    assert count==2 and {a["pair_id"] for a in assets}=={"BTC-AUD","ETH-AUD"}

def test_explicit_quote_scope_preserves_aud_default(data_dir):
    assert {a['pair_id'] for a in mod.collection_universe(data_dir)[0]}=={'BTC-AUD','ETH-AUD'}
    expanded=mod.collection_universe(data_dir,('AUD','USD'))[0]
    assert {a['pair_id'] for a in expanded}=={'BTC-AUD','ETH-AUD','ADA-USD'}
    assert next(a for a in expanded if a['pair_id']=='ADA-USD')['kraken_pair_id']=='ADAUSD'


def test_explicit_selected_pairs_fail_closed(data_dir):
    with pytest.raises(mod.CollectionError,match='unverified_requested_market'):
        mod.OhlcvCollector(data_dir).collect(all_aud=True,pair_ids=('UNKNOWN-USD',),quote_currencies=('AUD','USD'))


def test_trade_resume_counts_new_requests_not_cached_pages(data_dir):
    from kraken_quant.historical_trades import fetch_gap
    start=NOW.replace(second=0,microsecond=0)-timedelta(minutes=5)
    end=start+timedelta(minutes=3)
    calls=[]
    def transport(endpoint,params):
        calls.append(params['since'])
        if len(calls)==1:
            rows=[['100','1',str(int(start.timestamp())+i/10),'b','m','',i] for i in range(1000)]
            last=str(int(start.timestamp()*1000000000)+100000000000)
        else:
            rows=[['100','1',str(int(end.timestamp())),'b','m','',1001]]
            last=str(int(end.timestamp()*1000000000))
        return {'error':[],'result':{'BTC/AUD':rows,'last':last}}
    options=dict(start=start,end=end,max_requests=1,transport=transport,sleeper=lambda _:None,prepare_only=True)
    first=fetch_gap(data_dir,'BTC-AUD',**options)
    assert first['status']=='incomplete_request_budget_or_source_exhausted'
    second=fetch_gap(data_dir,'BTC-AUD',**options)
    assert second['status']=='source_window_downloaded'
    assert second['pages']==2 and second['requests']==1 and len(calls)==2

def test_fast_collection_does_not_decode_whole_history(data_dir,monkeypatch):
    def forbidden(*a,**k):raise AssertionError('incremental collection must not call read_all')
    monkeypatch.setattr(MonthlyParquetStore,'read_all',forbidden)
    assert one(collector(data_dir,Public()).collect(pair='BTC-AUD'))['status']=='ok'


def test_expired_policy_is_not_reported_as_a_network_request(data_dir):
    from kraken_quant.data_pipeline import PublicPolicy,PipelineConfig
    from kraken_quant.market_data import PUBLIC_REQUEST_POLICY
    clock=Clock();policy=PublicPolicy(data_dir/'policy',PipelineConfig(max_requests_per_cycle=1),clock,clock.monotonic,clock.sleep)
    policy.request('Assets',{},lambda:{'error':[],'result':{}})
    token=PUBLIC_REQUEST_POLICY.set(policy)
    try:
        client=mod.OhlcvCollector(data_dir,clock=clock)
        with pytest.raises(mod.CollectionError,match='cycle_request_budget_exhausted') as caught:
            client._request({'pair':'XBTAUD','interval':1})
        assert not caught.value.details['http_requested'] and client.request_count==0
    finally:PUBLIC_REQUEST_POLICY.reset(token)
