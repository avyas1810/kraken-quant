"""Synthetic public depth tests use isolated data directories."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
import pytest
pytest.importorskip("pyarrow")
from test_ohlcv_collector import data_dir, Clock, NOW
from kraken_quant import book_collector as mod
from kraken_quant.metadata_store import connect_metadata

ASSET=dict(pair_id="BTC-AUD",kraken_pair_id="XBTAUD",base_asset="BTC",quote_asset="AUD")

def body(key="BTC/AUD"):
    return {"error":[],"result":{key:{"bids":[["100.000000000000000001","2.000000000000000001",1],["99","3",1]],"asks":[["101","4",1],["102","5",1]]}}}

class Public:
    def __init__(self,clock):self.clock=clock;self.calls=[];self.failure=None;self.change=None
    def __call__(self,endpoint,params):
        assert endpoint=="Depth" and params["assetVersion"]==1
        self.calls.append((endpoint,params,self.clock.monotonic()))
        if self.failure:raise self.failure
        result=body({"XBTAUD":"BTC/AUD","ETHAUD":"ETH/AUD"}[params["pair"]])
        if self.change:self.change(result)
        return result

def setup(data_dir,clock=None):
    clock=clock or Clock(); public=Public(clock)
    return mod.BookCollector(data_dir,transport=public,clock=clock,sleeper=clock.sleep,monotonic=clock.monotonic),public,clock

def test_fields_decimals_timestamps_and_native_mapping(data_dir):
    c,p,clock=setup(data_dir)
    run=c.collect(pair="BTC-AUD")
    assert all(r["status"]=="ok" for r in run["results"])
    assert p.calls[0][1]["pair"]=="XBTAUD"
    quote=c.store.read_all("BTC-AUD","quotes_1m").to_pylist()[0]
    assert quote["bid"]==Decimal("100.000000000000000001")
    assert quote["bid_size"]==Decimal("2.000000000000000001")
    assert quote["timestamp"]==NOW and quote["timestamp"].second==30
    report=mod.report(data_dir,now=clock())
    assert len(report["pairs"])==4
    assert all(r["checkpoint_status"] in ("matches","empty") for r in report["pairs"])

def test_universe_and_invalid_pair(data_dir):
    c,p,_=setup(data_dir)
    run=c.collect(all_aud=True)
    assert run["selected_pairs"]==["BTC-AUD","ETH-AUD"]
    assert run["unresolved_unchanged"]==1
    with pytest.raises(mod.CollectionError):c.collect(pair="ADA-AUD")
    assert len(p.calls)==2

@pytest.mark.parametrize("change",[
    lambda b:b.update(error=["bad"]),
    lambda b:b.update(result={}),
    lambda b:b.update(result={"OTHER/AUD":next(iter(b["result"].values()))}),
    lambda b:b["result"]["BTC/AUD"].update(bids=[]),
    lambda b:b["result"]["BTC/AUD"].update(asks=[]),
    lambda b:b["result"]["BTC/AUD"]["bids"][0].__setitem__(0,"0"),
    lambda b:b["result"]["BTC/AUD"]["bids"][0].__setitem__(1,"-1"),
    lambda b:b["result"]["BTC/AUD"]["bids"][0].__setitem__(0,"105"),
    lambda b:b["result"]["BTC/AUD"]["bids"].reverse(),
    lambda b:b["result"]["BTC/AUD"]["asks"].reverse(),
    lambda b:b["result"]["BTC/AUD"]["asks"][0].pop(),
    lambda b:b["result"]["BTC/AUD"]["asks"][0].__setitem__(2,"999999999999"),
    lambda b:b["result"]["BTC/AUD"]["asks"][0].__setitem__(0,101.0),
])
def test_invalid_snapshot(change):
    b=body();change(b)
    with pytest.raises(mod.CollectionError):mod.parse_depth(b,ASSET,NOW,NOW)

@pytest.mark.parametrize("seconds",[-1,6])
def test_stale_response(seconds):
    with pytest.raises(mod.CollectionError):mod.parse_depth(body(),ASSET,NOW,NOW+timedelta(seconds=seconds))

def test_nullable_quote_sizes(data_dir):
    c,_,_=setup(data_dir)
    row=mod.parse_depth(body(),ASSET,NOW,NOW)["quotes_1m"][0]
    row.update(bid_size=None,ask_size=None)
    c.store.write("quotes_1m",[row])
    assert c.store.read_all("BTC-AUD","quotes_1m").to_pylist()[0]["bid_size"] is None

def intent(dataset="orderbook",now=NOW):
    parsed=mod.parse_depth(body(),ASSET,now,now)
    return dict(source="injected_test",pair_id="BTC-AUD",kraken_pair_id="XBTAUD",dataset=dataset,records=mod.encode(parsed[dataset]))

def test_idempotent_and_conflicting_retries(data_dir):
    c,_,_=setup(data_dir)
    i=intent();assert c._persist(i)["new_rows"]==4
    assert c._persist(i)==dict(new_rows=0,duplicate_rows=4)
    bad=deepcopy(i);bad["records"][0]["quantity"]="9"
    with pytest.raises(mod.CollectionError):c._persist(bad)
    assert len(c.store.read_all("BTC-AUD","orderbook"))==4
    bad=deepcopy(i);bad["records"]=bad["records"][:1]+bad["records"][2:]
    with pytest.raises(mod.CollectionError):c._persist(bad)

def test_checkpoint_failure_and_restart(data_dir,monkeypatch):
    c,p,clock=setup(data_dir)
    original=c._reconcile
    def fail(pair,dataset):
        if c.store.read_all(pair,dataset).num_rows:raise OSError("private error")
        return original(pair,dataset)
    monkeypatch.setattr(c,"_reconcile",fail)
    run=c.collect(pair="BTC-AUD",dataset="orderbook")
    assert run["results"][0]["status"]=="error"
    assert mod.checkpoint(data_dir,"BTC-AUD","orderbook") is None
    assert list((c.root/"pending").glob("*.json"))
    resumed,_,_=setup(data_dir,clock)
    resumed.collect(pair="BTC-AUD",dataset="orderbook")
    assert mod.checkpoint(data_dir,"BTC-AUD","orderbook")["latest_observation_at"]==mod.sql_time(NOW)
    assert not list((c.root/"pending").glob("*.json"))

def test_failure_before_write(data_dir,monkeypatch):
    c,_,_=setup(data_dir)
    monkeypatch.setattr(c.store,"write",lambda *a: (_ for _ in ()).throw(OSError()))
    run=c.collect(pair="BTC-AUD")
    assert all(r["status"]=="error" for r in run["results"])
    assert mod.checkpoint(data_dir,"BTC-AUD","quotes_1m") is None

def test_months_multiple_snapshots_and_independent_checkpoints(data_dir):
    clock=Clock(NOW+timedelta(days=1));c,_,_=setup(data_dir,clock)
    for t in (NOW-timedelta(days=1),NOW):c._persist(intent(now=t))
    c._persist(intent("quotes_1m",now=NOW-timedelta(days=1)))
    assert len(c.store.months("BTC-AUD","orderbook"))==2
    assert len({r["timestamp"] for r in c.store.read_all("BTC-AUD","orderbook").to_pylist()})==2
    assert mod.checkpoint(data_dir,"BTC-AUD","orderbook")!=mod.checkpoint(data_dir,"BTC-AUD","quotes_1m")

def test_network_errors_quality_and_rate_limit(data_dir):
    c,p,clock=setup(data_dir);p.failure=TimeoutError("SECRET")
    run=c.collect(all_aud=True)
    assert len(run["results"])==4 and "SECRET" not in json.dumps(run)
    with connect_metadata(data_dir,readonly=True) as db:
        assert db.execute("SELECT count(*) FROM data_quality_issues").fetchone()[0]==4
        assert db.execute("SELECT count(*) FROM eligibility").fetchone()[0]==0
    assert p.calls[1][2]-p.calls[0][2]>=1.1
    p.failure=None;p.change=lambda b:b.update(error=["EAPI:Rate limit exceeded"])
    c,p,clock=setup(data_dir,clock);p.change=lambda b:b.update(error=["EAPI:Rate limit exceeded"])
    run=c.collect(all_aud=True)
    assert len(p.calls)==1 and len(run["results"])==4

def test_sampling_restart_and_bounded_shutdown(data_dir):
    c,p,clock=setup(data_dir)
    run=c.collect(pair="BTC-AUD",duration_seconds=12,quote_interval=5,book_interval=10)
    assert run["status"]=="finished" and clock.elapsed==12
    assert len(c.store.read_all("BTC-AUD","quotes_1m"))==3
    assert len({r["timestamp"] for r in c.store.read_all("BTC-AUD","orderbook").to_pylist()})==2
    resumed,p2,_=setup(data_dir,clock)
    resumed.collect(pair="BTC-AUD",quote_interval=5,book_interval=10)
    assert not p2.calls

def test_keyboard_interrupt_recovery(data_dir,monkeypatch):
    c,_,clock=setup(data_dir);original=c._reconcile
    def interrupt(pair,dataset):
        if c.store.read_all(pair,dataset).num_rows:raise KeyboardInterrupt()
        return original(pair,dataset)
    monkeypatch.setattr(c,"_reconcile",interrupt)
    assert c.collect(pair="BTC-AUD",dataset="orderbook")["status"]=="interrupted"
    resumed,_,_=setup(data_dir,clock)
    resumed.collect(pair="BTC-AUD",dataset="orderbook")
    assert mod.checkpoint(data_dir,"BTC-AUD","orderbook")

def test_offline_report_preserves_readiness_and_open_issues(data_dir,monkeypatch):
    c,p,clock=setup(data_dir)
    with connect_metadata(data_dir) as db:db.execute("INSERT INTO collection_status VALUES ('BTC-AUD','not_ready','paused',?,'prior')",(mod.sql_time(NOW),))
    c.collect(pair="BTC-AUD")
    def no_network(*a):raise AssertionError()
    monkeypatch.setattr("kraken_quant.market_data.public_get",no_network)
    report=mod.report(data_dir,now=clock())
    assert report["pairs"][0]["readiness"]["readiness"]=="not_ready"

def test_ahead_checkpoint_rejected(data_dir):
    c,_,_=setup(data_dir)
    with connect_metadata(data_dir) as db:db.execute("INSERT INTO ingestion_state VALUES ('BTC-AUD','quotes_1m',?,?)",(mod.sql_time(NOW),mod.sql_time(NOW)))
    assert c.collect(pair="BTC-AUD",dataset="quotes_1m")["results"][0]["code"]=="checkpoint_ahead_of_verified_storage"

def test_source_binding_and_missing_database(data_dir,tmp_path):
    c,_,_=setup(data_dir);c.collect(pair="BTC-AUD")
    with pytest.raises(mod.CollectionError):mod.BookCollector(data_dir)._binding()
    missing=tmp_path/"missing"
    with pytest.raises(Exception):mod.BookCollector(missing).collect(pair="BTC-AUD")
    assert not missing.exists()

@pytest.mark.parametrize("kwargs",[dict(depth=0),dict(depth=101),dict(duration_seconds=3601),dict(quote_interval=1),dict(dataset="prices_1m")])
def test_invalid_settings(data_dir,kwargs):
    c,_,_=setup(data_dir)
    with pytest.raises(mod.CollectionError):c.collect(pair="BTC-AUD",**kwargs)

def test_cli_report(data_dir,capsys):
    assert mod.main(["report","--data-dir",str(data_dir)])==0
    assert json.loads(capsys.readouterr().out)["unresolved_unchanged"]==1


def test_snapshot_shapes_and_duplicate_levels(data_dir):
    c,_,_=setup(data_dir)
    i=intent();c._persist(i)
    bad=deepcopy(i);bad["records"].append(deepcopy(bad["records"][0]))
    with pytest.raises(mod.CollectionError):c._persist(bad)
    bad=deepcopy(i);bad["records"][1]["timestamp"]=mod.sql_time(NOW-timedelta(seconds=1))
    with pytest.raises(mod.CollectionError):c._persist(bad)

def test_atomic_partition_failure_preserves_existing_data(data_dir,monkeypatch):
    c,_,clock=setup(data_dir);c._persist(intent())
    path=c.store.partition_path("BTC-AUD","orderbook","2026-11")
    before=path.read_bytes();saved=mod.checkpoint(data_dir,"BTC-AUD","orderbook")
    clock.sleep(10)
    monkeypatch.setattr(c.store,"_replace",lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):c._persist(intent(now=clock()))
    assert path.read_bytes()==before and mod.checkpoint(data_dir,"BTC-AUD","orderbook")==saved

def test_pending_identity_rejected(data_dir):
    c,_,_=setup(data_dir);c._binding()
    i=intent();i["kraken_pair_id"]="WRONG"
    mod._atomic_json(c.root/"pending/BTC-AUD_orderbook.json",i)
    run=c.collect(pair="BTC-AUD",dataset="orderbook")
    assert run["results"][0]["code"]=="pending_identity_mismatch"
    assert not c.store.read_all("BTC-AUD","orderbook").num_rows

def test_cli_collection(data_dir,monkeypatch,capsys):
    real=mod.BookCollector;c,p,clock=setup(data_dir)
    monkeypatch.setattr(mod,"BookCollector",lambda directory:c)
    assert mod.main(["collect","--pair","BTC-AUD","--dataset","quotes_1m","--data-dir",str(data_dir)])==0
    assert json.loads(capsys.readouterr().out)["results"][0]["dataset"]=="quotes_1m"

def test_session_gap_reporting_and_crashed_journal(data_dir):
    c,_,clock=setup(data_dir);c.collect(pair="BTC-AUD")
    path=next((c.root/"runs").glob("*.json"));run=json.loads(path.read_text())
    run["finished_at"]=mod.sql_time(NOW+timedelta(seconds=200));run["intervals_seconds"]["quotes_1m"]=60
    mod._atomic_json(path,run)
    quote=next(r for r in mod.report(data_dir,now=NOW+timedelta(days=1))["pairs"] if r["pair_id"]=="BTC-AUD" and r["dataset"]=="quotes_1m")
    assert quote["session_sampling_gaps"][0]["unresolved_sampling_intervals"]==2
    del run["finished_at"];run["status"]="running";mod._atomic_json(path,run)
    quote=next(r for r in mod.report(data_dir,now=NOW+timedelta(days=1))["pairs"] if r["pair_id"]=="BTC-AUD" and r["dataset"]=="quotes_1m")
    assert not quote["session_sampling_gaps"]

import os
@pytest.mark.skipif(os.environ.get("KRAKEN_BOOK_SMOKE")!="1",reason="Opt-in real public depth integration")
def test_live_btc_depth(tmp_path):
    from kraken_quant.asset_catalogue import COLUMNS
    from kraken_quant.metadata_store import initialize_metadata
    real_dir=mod.load_config().data_dir
    initialize_metadata(tmp_path)
    with connect_metadata(real_dir,readonly=True) as source:
        rows=source.execute("SELECT "+",".join(COLUMNS)+" FROM assets").fetchall()
    with connect_metadata(tmp_path) as dest:
        dest.executemany("INSERT INTO assets VALUES ("+",".join("?" for _ in COLUMNS)+")",rows)
    (tmp_path/"catalogue_discovery").mkdir()
    (tmp_path/"catalogue_discovery/latest.json").write_bytes((real_dir/"catalogue_discovery/latest.json").read_bytes())
    run=mod.BookCollector(tmp_path).collect(pair="BTC-AUD")
    assert all(r["status"]=="ok" for r in run["results"])
    assert all(r["checkpoint_status"]=="matches" for r in mod.report(tmp_path)["pairs"] if r["pair_id"]=="BTC-AUD")


def test_cli_normal_deadline_deferral_and_uncollected(data_dir,monkeypatch,capsys):
    class Fake:
        def collect(self,**kwargs):return dict(status="finished",uncollected_pair_datasets=[],results=[dict(status="deferred",code="session_deadline")])
    monkeypatch.setattr(mod,"BookCollector",lambda directory:Fake())
    assert mod.main(["collect","--all-aud","--data-dir",str(data_dir)])==0
    capsys.readouterr()
    class Partial:
        def collect(self,**kwargs):return dict(status="finished",uncollected_pair_datasets=[dict(pair_id="ETH-AUD",dataset="orderbook")],results=[])
    monkeypatch.setattr(mod,"BookCollector",lambda directory:Partial())
    assert mod.main(["collect","--all-aud","--data-dir",str(data_dir)])==1
