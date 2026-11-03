"""Official fee facts and full source retention. No tier/execution optimization."""
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser
import time

from .cost_contracts import FeeTier, FeeTable, FeeSchedule
from .cost_storage import AuditStore, payload
from .ohlcv_collector import collection_universe, read_time, sql_time
from .storage_schema import decimal_value, utc_timestamp

PUBLIC_URL = "https://www.kraken.com/features/fee-schedule"
PRIVATE_URL = "https://api.kraken.com/0/private/TradeVolume"


class FeePageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack,self.tables = [],[]
        self.table,self.row,self.cell = None,None,None
        self.category = None

    def handle_starttag(self,tag,attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.table=[]
            self.category=next((a["aria-labelledby"] for _,a in reversed(self.stack) if "aria-labelledby" in a),None)
        if self.table is not None and tag == "tr":
            self.row=[]
        if self.row is not None and tag in ("th","td"):
            self.cell=[]
        if tag not in ("img","br","hr","meta","link","input","source","wbr"):
            self.stack.append((tag,attrs))

    def handle_endtag(self,tag):
        if tag in ("th","td") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell=None
        if tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row=None
        if tag == "table" and self.table is not None:
            self.tables.append((self.category,self.table))
            self.table=None
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0] == tag:
                self.stack=self.stack[:index]
                break

    def handle_data(self,text):
        if self.cell is not None:
            self.cell.append(text)


def parse_public_page(document):
    if not isinstance(document,dict) or document.get("url") != PUBLIC_URL or not isinstance(document.get("html"),str):
        raise ValueError("Official fee document missing")
    parser=FeePageParser()
    parser.feed(document["html"])
    parsed=[]
    for category,rows in parser.tables:
        if not rows:
            continue
        headers=rows[0]
        maker=next((i for i,h in enumerate(headers) if h.lower() in ("maker","maker (%)","spot maker (%)")),None)
        taker=next((i for i,h in enumerate(headers) if h.lower() in ("taker","taker (%)","spot taker (%)")),None)
        if maker is None and taker is None:
            continue  # Original document preserves non-maker/taker products too.
        if maker is None or taker is None or not category or len(rows)<2:
            raise ValueError("Unrecognized fee-table structure")
        tiers=[]
        for row in rows[1:]:
            if len(row)!=len(headers) or not row[maker].endswith('%') or not row[taker].endswith('%'):
                raise ValueError("Malformed official fee tier")
            tiers.append(FeeTier(row[0],tuple(zip(headers,row)),decimal_value(row[maker][:-1].strip()),decimal_value(row[taker][:-1].strip())))
        parsed.append(FeeTable(category,"USD" if any("(USD)" in h for h in headers) else None,tuple(tiers)))
    if not parsed or not any(t.category == "spot-crypto" for t in parsed):
        raise ValueError("Official spot schedule not found; refusing guessed fallback")
    return tuple(parsed)


def parse_pair_fees(body,assets):
    if not isinstance(body,dict) or body.get("error") != [] or not isinstance(body.get("result"),dict):
        raise ValueError("Invalid public fee response")
    result=body["result"]
    parsed={}
    recognized=set()
    for asset in assets:
        native=asset["kraken_pair_id"]
        keys={native,asset["base_asset"]+"/"+asset["quote_asset"]}&set(result)
        if len(keys)!=1:
            raise ValueError("Missing or ambiguous fee pair")
        key=next(iter(keys));recognized.add(key);row=result[key]
        if not isinstance(row,dict):
            raise ValueError("Malformed pair fee record")
        schedules=[]
        for field in ("fees_maker","fees"):
            tiers=row.get(field)
            if tiers is None:
                schedules.append(None)
                continue
            if not isinstance(tiers,list):
                raise ValueError("Malformed API fee tiers")
            values=[]
            for tier in tiers:
                if not isinstance(tier,list) or len(tier)!=2:
                    raise ValueError("Malformed API fee tier")
                threshold,fee=map(decimal_value,tier)
                if threshold<0:
                    raise ValueError("Negative volume threshold")
                values.append((threshold,fee))
            if len({v[0] for v in values})!=len(values) or values!=sorted(values):
                raise ValueError("Ambiguous API volume tiers")
            schedules.append(tuple(values))
        parsed[asset["pair_id"]]=dict(native=native,tier_currency=row.get("fee_volume_currency"),
                                       maker=schedules[0],taker=schedules[1])
    if recognized!=set(result):
        raise ValueError("Unexpected fee pair identities")
    return parsed


def parse_account_fees(result,assets):
    if not isinstance(result,dict):
        raise ValueError("Invalid private fee response")
    allowed={a["kraken_pair_id"] for a in assets}|{a["base_asset"]+"/"+a["quote_asset"] for a in assets}
    for key in ("fees","fees_maker"):
        if not isinstance(result.get(key,{}),dict) or set(result.get(key,{}))-allowed:
            raise ValueError("Unresolved account fee identity")
    fees={}
    for asset in assets:
        native=asset["kraken_pair_id"]
        row={}
        for key,label in (("fees","taker"),("fees_maker","maker")):
            candidates={native,asset["base_asset"]+"/"+asset["quote_asset"]}&set(result.get(key,{}))
            if len(candidates)>1:
                raise ValueError("Ambiguous account fee identity")
            item=result.get(key,{}).get(next(iter(candidates))) if candidates else None
            row[label]=decimal_value(item["fee"]) if item and item.get("fee") is not None else None
        fees[asset["pair_id"]]=row
    return fees


def refresh_fees(data_dir,*,transport=None,readonly=None,clock=lambda:datetime.now(timezone.utc),sleeper=time.sleep,source_namespace=None,account_unavailability=None):
    assets,audit,unresolved=collection_universe(data_dir)
    if transport is None:
        from .market_data import public_get
        transport=lambda endpoint,params:public_get(endpoint,params,exact_numbers=True)
        namespace="kraken_public"
    else:
        namespace="injected_test"
    if source_namespace is not None and source_namespace!=namespace:
        raise ValueError("Fee transport provenance mismatch")
    started=utc_timestamp(clock())
    pairs=transport("AssetPairs",dict(pair=",".join(a["kraken_pair_id"] for a in assets),assetVersion=1,info="fees"))
    parsed_pairs=parse_pair_fees(pairs,assets)
    sleeper(1.1)
    page=transport("FeeSchedule",{})
    tables=parse_public_page(page)
    account=None
    account_status=account_unavailability or "unavailable_readonly_credentials"
    if readonly is not None:
        try:
            readonly.permissions()
            # Reuse guarded read-only adapter; never access its trading transport.
            result=readonly._read("TradeVolume",{"pair":",".join(a["kraken_pair_id"] for a in assets)})
            account=dict(payload=result,fees={},account_fingerprint=readonly.account_fingerprint)
            try:
                account["fees"]=parse_account_fees(result,assets)
                account_status="verified_readonly" if any(v is not None for row in account["fees"].values() for v in row.values()) else "unavailable_account_fee_fields"
            except ValueError:
                account_status="unresolved_account_fee_metadata"
        except Exception:
            account_status="unavailable_readonly_query_failed"
    retrieved=utc_timestamp(clock())
    if retrieved<started:
        raise ValueError("Fee retrieval clock regressed")
    document=dict(contract_version=1,source_namespace=namespace,exchange="Kraken",catalogue_run_id=audit,
        retrieved_at=sql_time(retrieved),effective_at=None,source_url=PUBLIC_URL,
        public_api_endpoint="https://api.kraken.com/0/public/AssetPairs",public_api_payload=pairs,
        pair_fees=parsed_pairs,public_document=page,public_tables=tables,
        account_endpoint=PRIVATE_URL,account_status=account_status,account=account,
        unresolved_unchanged=unresolved)
    return AuditStore(data_dir).put("fees",document)


def fee_view(data_dir,version,pair_id,*,assumption=False):
    doc=AuditStore(data_dir).get("fees",version)
    pair=doc["pair_fees"][pair_id]
    tables=tuple(FeeTable(t["category"],t["tier_currency"],tuple(FeeTier(r["label"],tuple(tuple(v) for v in r["conditions"]),None if r["maker_pct"] is None else Decimal(r["maker_pct"]),None if r["taker_pct"] is None else Decimal(r["taker_pct"])) for r in t["tiers"])) for t in doc["public_tables"])
    # Preserve pair-specific API schedules without merging unlike thresholds or
    # selecting a maker/taker rate. The complete raw arrays remain in the document.
    extra=[]
    for key in ("maker","taker"):
        values=pair[key]
        if values:
            extra.append(FeeTable("AssetPairs:"+key,pair["tier_currency"],tuple(FeeTier(str(v[0]),(("volume_threshold",str(v[0])),),Decimal(v[1]) if key=="maker" else None,Decimal(v[1]) if key=="taker" else None) for v in values)))
    account=doc["account"]["fees"].get(pair_id,{}) if doc.get("account") else {}
    maker=Decimal(account["maker"]) if account.get("maker") is not None else None
    taker=Decimal(account["taker"]) if account.get("taker") is not None else None
    return FeeSchedule(version,pair_id,pair["native"],"Kraken",doc["source_url"],read_time(doc["retrieved_at"]),
                       read_time(doc["effective_at"]) if doc["effective_at"] else None,"assumed" if assumption else "verified",
                       tables+tuple(extra),maker is not None or taker is not None,maker,taker,None,None,assumption,
                       PRIVATE_URL if maker is not None or taker is not None else None)


def fee_report(data_dir,*,now=None,max_age_seconds=86400):
    result=[]
    store=AuditStore(data_dir)
    for version in store.versions("fees"):
        doc=store.get("fees",version)
        result.append(dict(version=version,source=doc["source_url"],source_namespace=doc["source_namespace"],retrieved_at=doc["retrieved_at"],effective_at=doc["effective_at"],
            account_status=doc["account_status"],pairs=sorted(doc["pair_fees"]),tables=doc["public_tables"],
            verification_status="stale" if ((now or datetime.now(timezone.utc))-read_time(doc["retrieved_at"])).total_seconds()>max_age_seconds else "verified",
            verification="source verified; account applicability not inferred"))
    return result
