"""Complete mocked catalogue discovery; no private API, orders or market ingestion."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3

import pytest

from kraken_quant import asset_catalogue as cat
from kraken_quant.metadata_store import connect_metadata, initialize_metadata

NOW = datetime(2026,11,1,tzinfo=timezone.utc)


def fixtures():
    native = {key: dict(aclass="currency", altname=alias) for key,alias in
              (("XXBT","XBT"),("XETH","ETH"),("XXDG","XDG"),("ZAUD","AUD"),("ZUSD","USD"),("XREP","REP"),("XNEW","XNEW"))}
    display = {key: dict(aclass="currency", altname=alias) for key,alias in
               (("BTC","XBT"),("ETH","ETH"),("DOGE","XDG"),("AUD","AUD"),("USD","USD"),("REP","REP"),("XNEW","XNEW"))}
    pairs = {}
    for key,base,quote in (("XXBTZAUD","XXBT","ZAUD"),("XETHZAUD","XETH","ZAUD"),("XXDGZUSD","XXDG","ZUSD"),("XREPZUSD","XREP","ZUSD"),("XNEWZUSD","XNEW","ZUSD")):
        pairs[key] = dict(base=base,quote=quote,aclass_base="currency",aclass_quote="currency",execution_venue="international",
            wsname=native[base]["altname"]+"/"+native[quote]["altname"],status="online",lot="unit5",lot_multiplier=1,
            ordermin="0.00010000",costmin="1.234567890123456789",lot_decimals=8,pair_decimals=2,tick_size="0.05",leverage_buy=[2,3])
    return pairs,native,display


class Public:
    def __init__(self):
        self.pairs,self.native,self.display=fixtures()
        self.extra={}
        self.calls=[]
    def __call__(self,endpoint,params):
        self.calls.append((endpoint,dict(params)))
        assert endpoint in ("AssetPairs","Assets")
        asset_class=params.get("aclass_base",params.get("aclass"))
        if endpoint=="AssetPairs": result=self.pairs if asset_class=="currency" else self.extra
        else: result=(self.display if params.get("assetVersion")==1 else self.native) if asset_class=="currency" else {}
        return {"error":[],"result":deepcopy(result)}


@pytest.fixture
def data_dir(tmp_path):
    initialize_metadata(tmp_path)
    return tmp_path


def discover(data_dir, public=None, now=NOW):
    return cat.discover(data_dir,transport=public or Public(),clock=lambda:now)


def parse(public=None):
    sources,native,display=cat.fetch_sources(public or Public())
    return cat.parse_sources(sources,native,display,NOW)


def test_complete_parsing_aliases_and_source_reconciliation():
    public=Public();result=parse(public)
    assert result["source_records"]==result["classified_spot"]==5
    assert result["unresolved_count"]==result["excluded_non_spot"]==0
    records={r["pair_id"]:r for r in result["records"]}
    assert set(records)=={"BTC-AUD","ETH-AUD","DOGE-USD","REP-USD","XNEW-USD"}
    assert records["BTC-AUD"]["kraken_pair_id"]=="XXBTZAUD"
    assert records["ETH-AUD"]["source_identity"]["base"]=="XETH"
    assert records["DOGE-USD"]["source_identity"]["base"]=="XXDG"
    assert len(public.calls)==6
    assert all("pair" not in params and "country_code" not in params for endpoint,params in public.calls)
    assert all(params["execution_venue"]==cat.VENUES for endpoint,params in public.calls if endpoint=="AssetPairs")


def test_nullable_and_decimal_constraints(data_dir):
    public=Public()
    for field in ("ordermin","costmin","lot_decimals","pair_decimals"): public.pairs["XETHZAUD"].pop(field)
    result=discover(data_dir,public)
    assert result["persisted"]==5
    with connect_metadata(data_dir) as db:
        assert db.execute("SELECT min_order_qty,min_order_value,qty_precision,price_precision FROM assets WHERE pair_id='ETH-AUD'").fetchone()==(None,None,None,None)
        assert db.execute("SELECT min_order_qty,min_order_value,qty_precision,price_precision FROM assets WHERE pair_id='BTC-AUD'").fetchone()==("0.00010000","1.234567890123456789",8,2)
    # 0.05 tick size was not invented as 10**-pair_decimals or stored as precision.
    receipt=json.loads((data_dir/"catalogue_discovery/latest.json").read_text())
    assert receipt["raw_sources"][0]["pairs"]["XXBTZAUD"]["tick_size"]=="0.05"


@pytest.mark.parametrize("field,value", [("ordermin",1.1),("ordermin","NaN"),("ordermin","-1"),("ordermin","1e-19"),
    ("costmin","1e20"),("costmin","1e1000000"),("lot_decimals",True),("lot_decimals",19),("pair_decimals",2.5)])
def test_invalid_optional_values_are_unresolved_not_defaulted(field,value):
    public=Public();public.pairs["XXBTZAUD"][field]=value
    result=parse(public)
    assert result["classified_spot"]==4 and result["unresolved_count"]==1
    assert result["unresolved"][0]["kraken_pair_id"]=="XXBTZAUD"


@pytest.mark.parametrize("field", ["base","quote","aclass_base","aclass_quote","execution_venue","wsname","status"])
def test_missing_mandatory_pair_metadata(field):
    public=Public();public.pairs["XXBTZAUD"].pop(field)
    result=parse(public)
    assert result["classified_spot"]+result["unresolved_count"]==result["source_records"]==5
    assert result["unresolved_count"]==1


def test_duplicate_canonical_identity_never_wins_arbitrarily():
    public=Public();public.pairs["duplicate"]=deepcopy(public.pairs["XXBTZAUD"])
    result=parse(public)
    assert result["classified_spot"]==4 and result["unresolved_count"]==2
    assert {r["reason"] for r in result["unresolved"]}=={"identifier_collision"}


def test_ambiguous_aliases_and_unknown_assets_are_unresolved():
    public=Public();public.display["OTHERBTC"]=dict(aclass="currency",altname="XBT")
    result=parse(public)
    assert result["unresolved"][0]["reason"]=="ambiguous_display_asset_identity"
    public=Public();public.native.pop("XXBT")
    assert parse(public)["unresolved"][0]["reason"]=="missing_native_asset_identity"
    public=Public();public.display["lowercase"]=public.display.pop("BTC")
    assert parse(public)["unresolved"][0]["reason"]=="unrepresentable_canonical_asset"


def test_nonspot_and_ambiguous_classes_and_venues_are_explicit():
    public=Public()
    public.pairs["future"]={**public.pairs["XXBTZAUD"],"instrument_type":"future"}
    public.pairs["unknown-kind"]={**public.pairs["XXBTZAUD"],"instrument_type":"unknown"}
    public.pairs["margin-venue"]={**public.pairs["XXBTZAUD"],"execution_venue":"bitnomial_exchange"}
    public.extra["tokenized"]={**public.pairs["XXBTZAUD"],"aclass_base":"tokenized_asset"}
    result=parse(public)
    assert (result["source_records"],result["classified_spot"],result["excluded_non_spot"],result["unresolved_count"])==(9,5,1,3)
    assert {r["reason"] for r in result["unresolved"]}=={"ambiguous_instrument_type","execution_venue_requires_resolution","asset_class_requires_resolution"}


@pytest.mark.parametrize("body", [None,{}, {"error":["error SECRET_SENTINEL"],"result":{}}, {"error":[],"result":[]}, {"error":[],"result":{}}])
def test_bad_response_is_mandatory_failure(data_dir,body):
    before=(data_dir/"metadata.sqlite").read_bytes()
    with pytest.raises(cat.DiscoveryError): discover(data_dir,lambda *args:body)
    assert (data_dir/"metadata.sqlite").read_bytes()==before


def test_malformed_record_is_not_dropped():
    public=Public();public.pairs["malformed"]=None
    result=parse(public)
    assert result["source_records"]==6 and result["unresolved_count"]==1
    assert result["unresolved"][0]["reason"]=="malformed_pair_record"


def test_first_repeat_status_update_and_absent_retention(data_dir):
    public=Public();first=discover(data_dir,public)
    assert first["inserted"]==5 and first["updated"]==0
    public.pairs["XXBTZAUD"]["status"]="post_only"
    public.pairs["XXBTZAUD"]["ordermin"]="0.001"
    public.pairs.pop("XETHZAUD")
    second=discover(data_dir,public,NOW+timedelta(days=1))
    assert (second["inserted"],second["updated"],second["retained_previous"],second["total_assets"])==(0,4,5,5)
    assert second["absent_retained"]==["ETH-AUD"]
    with connect_metadata(data_dir) as db:
        rows={r[0]:r[1:] for r in db.execute("SELECT pair_id,first_seen,last_seen,status FROM assets")}
        assert rows["BTC-AUD"][0]==first["observed_at"] and rows["BTC-AUD"][1]==second["observed_at"]
        assert rows["BTC-AUD"][2]=="post_only"
        assert rows["ETH-AUD"]==(first["observed_at"],first["observed_at"],"online")
    report=cat.catalogue_report(data_dir)
    assert report["aud_pairs"]==2 and report["observed_online_aud_pairs"]==0 and report["inactive_aud_pairs"]==1
    assert report["not_observed_latest"]==["ETH-AUD"]


def test_repeated_same_snapshot_is_idempotent(data_dir):
    discover(data_dir)
    before=(data_dir/"metadata.sqlite").read_bytes()
    result=discover(data_dir)
    assert result["inserted"]==0 and result["updated"]==0 and result["unchanged"]==5
    assert (data_dir/"metadata.sqlite").read_bytes()==before


def test_foreign_keys_and_history_preserved(data_dir):
    first=discover(data_dir)
    with connect_metadata(data_dir) as db:
        db.execute("INSERT INTO ingestion_state VALUES (?,?,?,?)",("BTC-AUD","prices_1m",None,first["observed_at"]))
        db.execute("INSERT INTO collection_status VALUES (?,?,?,?,?)",("BTC-AUD","unknown","idle",first["observed_at"],None))
        db.execute("INSERT INTO eligibility (pair_id,screened_at,decision,volume_24h_aud_status,avg_spread_pct_status,depth_aud_status,historical_days_status,completeness_pct_status) VALUES (?,?,?,?,?,?,?,?)",
                   ("BTC-AUD",first["observed_at"],"unknown","unknown","unknown","unknown","unknown","unknown"))
    discover(data_dir,now=NOW+timedelta(seconds=1))
    with connect_metadata(data_dir) as db:
        assert db.execute("SELECT count(*) FROM ingestion_state").fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM collection_status").fetchone()[0]==1
        assert db.execute("SELECT screened_at,decision FROM eligibility").fetchone()==(first["observed_at"],"unknown")
        assert db.execute("PRAGMA foreign_key_check").fetchall()==[]


def test_existing_identity_conflict_rolls_back_entire_update(data_dir):
    discover(data_dir)
    public=Public();public.pairs["XETHZAUD"]["status"]="post_only"
    public.pairs["new-native-key"]=public.pairs.pop("XXBTZAUD")
    before=(data_dir/"metadata.sqlite").read_bytes()
    with pytest.raises(cat.DiscoveryError,match="identity conflict"):
        discover(data_dir,public,NOW+timedelta(seconds=1))
    assert (data_dir/"metadata.sqlite").read_bytes()==before


def test_database_error_rolls_back_all_rows(data_dir,monkeypatch):
    real=cat._rows
    def fail_after_insert(db):
        rows=real(db)
        if rows: raise sqlite3.IntegrityError("synthetic commit-stage failure")
        return rows
    monkeypatch.setattr(cat,"_rows",fail_after_insert)
    with pytest.raises(sqlite3.IntegrityError): discover(data_dir)
    with connect_metadata(data_dir) as db: assert db.execute("SELECT count(*) FROM assets").fetchone()[0]==0


def test_receipt_failure_rolls_back_sql_and_late_failure_is_detected(data_dir,monkeypatch):
    real=cat._atomic_json
    def fail(path,content): raise OSError("synthetic disk failure")
    monkeypatch.setattr(cat,"_atomic_json",fail)
    with pytest.raises(OSError):discover(data_dir)
    with connect_metadata(data_dir) as db: assert db.execute("SELECT count(*) FROM assets").fetchone()[0]==0
    def fail_latest(path,content):
        if path.name=="latest.json": raise OSError("synthetic publication failure")
        return real(path,content)
    monkeypatch.setattr(cat,"_atomic_json",fail_latest)
    with pytest.raises(OSError):discover(data_dir)
    assert cat.catalogue_report(data_dir)["audit_status"]=="missing_or_stale"
    monkeypatch.setattr(cat,"_atomic_json",real)
    discover(data_dir,now=NOW+timedelta(seconds=1))
    assert cat.catalogue_report(data_dir)["audit_status"]=="current"


def test_regressing_clock_rejected(data_dir):
    discover(data_dir)
    before=(data_dir/"metadata.sqlite").read_bytes()
    with pytest.raises(cat.DiscoveryError,match="clock regressed"):discover(data_dir,now=NOW-timedelta(seconds=1))
    assert (data_dir/"metadata.sqlite").read_bytes()==before


def test_missing_incompatible_or_unrelated_db_not_initialized(tmp_path):
    public=Public()
    with pytest.raises(sqlite3.OperationalError):discover(tmp_path,public)
    assert not public.calls and not (tmp_path/"metadata.sqlite").exists()
    other=tmp_path/"research.sqlite";other.write_bytes(b"protected unrelated research")
    with sqlite3.connect(tmp_path/"metadata.sqlite") as db: db.execute("CREATE TABLE research (x INTEGER)")
    before=(tmp_path/"metadata.sqlite").read_bytes()
    with pytest.raises(ValueError):discover(tmp_path,public)
    assert (tmp_path/"metadata.sqlite").read_bytes()==before and other.read_bytes()==b"protected unrelated research"
    assert not public.calls


def test_reporting_is_db_backed_and_read_only(data_dir,monkeypatch):
    discover(data_dir)
    before=(data_dir/"metadata.sqlite").read_bytes()
    monkeypatch.setattr(cat,"fetch_sources",lambda *args:pytest.fail("Report cannot request API"))
    report=cat.catalogue_report(data_dir)
    assert len(report["all_spot_pairs"])==5 and report["aud_pairs"]==2
    assert report["stored_online_aud_pairs"]==report["observed_online_aud_pairs"]==2
    assert report["audit_status"]=="current" and report["requires_resolution"]==[]
    assert (data_dir/"metadata.sqlite").read_bytes()==before


def test_no_strategy_universe_or_credentials_dependency(data_dir,monkeypatch):
    from kraken_quant.config import CONFIG
    from kraken_quant import credentials
    monkeypatch.setattr(type(CONFIG),"universe",property(lambda self:pytest.fail("Research universe accessed")),raising=False)
    monkeypatch.setattr(credentials,"readonly_from_env",lambda *args,**kw:pytest.fail("No private credentials allowed"))
    public=Public();result=discover(data_dir,public)
    assert result["persisted"]==5 and {endpoint for endpoint,params in public.calls}=={"AssetPairs","Assets"}


def test_public_allowlist_accepts_assets_and_rejects_orders(monkeypatch):
    from kraken_quant import market_data
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return b'{"error":[],"result":{"XXBT":{}}}'
    calls=[]
    def opener(url,timeout):calls.append(url);return Response()
    monkeypatch.setattr(market_data,"urlopen",opener)
    assert market_data.public_get("Assets",{})["error"]==[]
    for endpoint in ("AddOrder","CancelOrder","Withdraw","Balance"):
        with pytest.raises(PermissionError):market_data.public_get(endpoint,{})
    assert len(calls)==1 and "/public/Assets" in calls[0]


def test_cli_discover_report_and_failure_exit_codes(data_dir,monkeypatch,capsys):
    real=cat.discover
    public=Public()
    monkeypatch.setattr(cat,"discover",lambda data_dir:real(data_dir,transport=public,clock=lambda:NOW))
    args=["--data-dir",str(data_dir),"--summary"]
    assert cat.main(["discover",*args])==0
    assert json.loads(capsys.readouterr().out)["persisted"]==5
    assert cat.main(["report",*args])==0
    assert json.loads(capsys.readouterr().out)["total_assets"]==5
    public.extra["unresolved"]={"aclass_base":"tokenized_asset"}
    assert cat.main(["discover",*args])==2
    assert json.loads(capsys.readouterr().out)["unresolved_count"]==1
    def failure(*args):raise OSError("SECRET_SENTINEL")
    monkeypatch.setattr(cat,"discover",failure)
    assert cat.main(["discover",*args])==1
    assert "SECRET_SENTINEL" not in capsys.readouterr().out


@pytest.mark.skipif(os.environ.get("KRAKEN_CATALOGUE_SMOKE")!="1",reason="Opt-in live public catalogue integration")
def test_real_public_discovery(tmp_path):
    initialize_metadata(tmp_path)
    result=cat.discover(tmp_path)
    assert result["source_records"]==result["classified_spot"]+result["excluded_non_spot"]+result["unresolved_count"]
    assert result["persisted"]==result["classified_spot"]>0
    report=cat.catalogue_report(tmp_path)
    assert report["total_assets"]==result["persisted"] and report["aud_pairs"]>0 and report["integrity_check"]=="ok"



def test_missing_asset_alias_is_not_invented():
    public=Public();public.native["XXBT"]["altname"]=""
    result=parse(public)
    assert result["unresolved_count"]==1
    assert result["unresolved"][0]["reason"]=="missing_native_asset_identity"



def test_transport_timeout_is_sanitized_and_does_not_change_db(data_dir):
    before=(data_dir/"metadata.sqlite").read_bytes()
    def timeout(*args):raise TimeoutError("SECRET_SENTINEL")
    with pytest.raises(cat.DiscoveryError,match="Public catalogue request failed") as error:
        discover(data_dir,timeout)
    assert "SECRET_SENTINEL" not in str(error.value)
    assert (data_dir/"metadata.sqlite").read_bytes()==before


def test_existing_native_pair_cannot_be_remapped(data_dir):
    discover(data_dir)
    public=Public();public.display["NEWBTC"]=public.display.pop("BTC")
    before=(data_dir/"metadata.sqlite").read_bytes()
    with pytest.raises(cat.DiscoveryError,match="identity conflict"):
        discover(data_dir,public,NOW+timedelta(seconds=1))
    assert (data_dir/"metadata.sqlite").read_bytes()==before
