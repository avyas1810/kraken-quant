"""Deterministic raw-input lookup and immutable manifests; no financial algorithms."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

from . import book_collector as books
from . import ohlcv_collector as candles
from .cost_contracts import (CostRequest, PreparationConfig, MarketMetadata, QuoteSnapshot,
    BookLevel, OrderBookSnapshot, CandleSnapshot, SourceReference, InputProvenance,
    CostModelInputs, PreparedInputs)
from .cost_storage import AuditStore, payload, digest
from .fee_data import fee_view
from .metadata_store import connect_metadata
from .storage_schema import LOGICAL_KEYS, table_from_records


def ordered(dataset,rows):
    return sorted(rows,key=lambda row:tuple(row[k] for k in LOGICAL_KEYS[dataset]))


class InputService:
    def __init__(self,data_dir,*,clock=lambda:datetime.now(timezone.utc)):
        self.data_dir=Path(data_dir)
        self.store=books.MonthlyParquetStore(data_dir)
        self.audit=AuditStore(data_dir)
        self.clock=clock
        self._parsed_sources={}

    def _namespace(self,folder):
        path=self.data_dir/folder/"source.json"
        return json.loads(path.read_text())["source"] if path.exists() else None

    def _metadata(self,request):
        candles.collection_universe(self.data_dir)  # Validate current catalogue identity/audit.
        with connect_metadata(self.data_dir,readonly=True) as db:
            row=db.execute("SELECT pair_id,kraken_pair_id,base_asset,quote_asset,status,last_seen,min_order_qty,min_order_value,qty_precision,price_precision FROM assets WHERE pair_id=?",(request.pair_id,)).fetchone()
        if row is None or row[3]!="AUD":
            raise ValueError("Request pair must exist in the AUD catalogue")
        value=dict(zip((f.name for f in fields(MarketMetadata)),row))
        value["last_seen"]=candles.read_time(value["last_seen"])
        for key in ("min_order_qty","min_order_value"):
            value[key]=Decimal(value[key]) if value[key] is not None else None
        return MarketMetadata(**value)

    def _reference(self,dataset,rows,metadata,as_of,response_id=None):
        folder="ohlcv_collection" if dataset=="prices_1m" else "book_collection"
        namespace=self._namespace(folder)
        if namespace is None:
            raise ValueError("missing_source_provenance")
        stamp=rows[0]["timestamp"]
        expected=digest(ordered(dataset,rows))
        paths=[self.data_dir/response_id] if response_id else sorted((self.data_dir/folder/"responses"/metadata.pair_id).glob("*.json"))
        for path in paths:
            receipt=json.loads(path.read_text())
            if receipt.get("source")!=namespace:
                continue
            try:
                cache_key=(digest(receipt),metadata.pair_id,metadata.kraken_pair_id)
                if dataset=="prices_1m":
                    received=candles.read_time(receipt["fetched_at"])
                    if received>as_of or receipt["params"]["pair"]!=metadata.kraken_pair_id:
                        continue
                    if cache_key not in self._parsed_sources:
                        self._parsed_sources[cache_key]=candles.parse_ohlc(receipt["body"],payload(metadata),received)
                    parsed=self._parsed_sources[cache_key]
                    if parsed["invalid_observations"]:
                        continue
                    source=[r for r in parsed["records"] if r["timestamp"]==stamp]
                    latency=None
                    semantics="UTC_minute_candle_open"
                else:
                    started=candles.read_time(receipt["started_at"])
                    received=candles.read_time(receipt["finished_at"])
                    if started!=stamp or received>as_of or receipt["kraken_pair_id"]!=metadata.kraken_pair_id:
                        continue
                    provenance=receipt["provenance"]
                    if cache_key not in self._parsed_sources:
                        self._parsed_sources[cache_key]=books.parse_depth(receipt["body"],payload(metadata),started,received,provenance["requested_depth"])
                    parsed=self._parsed_sources[cache_key]
                    if parsed["provenance"]!=provenance:
                        continue
                    source=parsed[dataset]
                    latency=Decimal(str(provenance["latency_seconds"]))
                    semantics=provenance["timestamp_semantics"]
                if digest(ordered(dataset,source))!=expected:
                    continue
                return SourceReference(dataset,metadata.pair_id,stamp,expected,str(path.relative_to(self.data_dir)).replace("\\","/"),digest(receipt),
                    "https://api.kraken.com/0/public/"+("OHLC" if dataset=="prices_1m" else "Depth"),latency,semantics)
            except (ValueError,KeyError,TypeError):
                continue
        raise ValueError("missing_or_inconsistent_source_provenance")

    def _fee(self,request,namespace,config):
        candidates=[]
        later=[]
        for version in self.audit.versions("fees"):
            doc=self.audit.get("fees",version)
            if doc["source_namespace"]!=namespace or request.pair_id not in doc["pair_fees"]:
                continue
            retrieved=candles.read_time(doc["retrieved_at"])
            if retrieved>self.clock():
                continue
            if doc["effective_at"] and candles.read_time(doc["effective_at"])>request.observation_time:
                continue
            target=candidates if retrieved<=request.observation_time else later
            account=doc.get("account")
            applicable=bool(account and account["fees"].get(request.pair_id,{}).get(request.execution_type) is not None)
            target.append((applicable,retrieved,version))
        assumption=False
        if not candidates:
            if not config.allow_present_day_fee_assumption or not later:
                return None,("historical_fee_unavailable" if later else "fee_schedule_missing"),()
            candidates=later;assumption=True
        _,retrieved,version=max(candidates)
        fee=fee_view(self.data_dir,version,request.pair_id,assumption=assumption)
        warnings=[]
        if assumption:
            warnings.append("present_day_fee_assumption_not_historical_applicability")
        elif (request.observation_time-retrieved).total_seconds()>config.max_fee_age_seconds:
            return replace(fee,verification_status="stale"),"stale_fee_information",()
        if fee.effective_at is None:
            warnings.append("fee_effective_timestamp_unknown")
        if not fee.account_specific:
            warnings.extend(("applicable_account_fee_tier_unknown","public_fee_category_not_assigned_to_pair"))
        else:
            rate=fee.account_maker_pct if request.execution_type=="maker" else fee.account_taker_pct
            if rate is None:
                warnings.append("requested_account_fee_unavailable")
        return fee,None,tuple(warnings)

    def prepare(self,request:CostRequest,config:PreparationConfig=PreparationConfig())->PreparedInputs:
        if request.observation_time>self.clock():
            raise ValueError("Future request observation time prohibited")
        metadata=self._metadata(request)
        namespace=self._namespace("book_collection")
        missing=[];warnings=[];refs=[]
        if metadata.last_seen>request.observation_time:
            provenance=InputProvenance(1,namespace or "unverified",None,None,(),config,())
            inputs=CostModelInputs(request,None,None,None,None,None,provenance)
            return PreparedInputs(inputs,"inputs_incomplete",("historical_market_metadata_unavailable",),())
        quote=book=candle=None
        if namespace is None:
            missing.append("missing_source_provenance")
        try:
            quote_rows=self.store.read_all(request.pair_id,"quotes_1m").to_pylist()
            book_rows=self.store.read_all(request.pair_id,"orderbook").to_pylist()
            eligible=sorted((r for r in quote_rows if r["timestamp"]<=request.observation_time),key=lambda r:r["timestamp"],reverse=True)
            selected=None
            rejected=set()
            for row in eligible:
                matching=[r for r in book_rows if r["timestamp"]==row["timestamp"]]
                if not matching:
                    rejected.add("quote_book_timestamp_mismatch" if book_rows else "missing_orderbook")
                    continue
                try:
                    books.validate_snapshot(matching)
                    qref=self._reference("quotes_1m",[row],metadata,request.observation_time)
                    bref=self._reference("orderbook",matching,metadata,request.observation_time)
                    if qref.response_id!=bref.response_id:
                        raise ValueError("quote_book_source_mismatch")
                    best={r["side"]:r for r in matching if r["level"]==1}
                    if (row["bid"]!=best["bid"]["price"] or row["ask"]!=best["ask"]["price"] or
                        row["bid_size"] is not None and row["bid_size"]!=best["bid"]["quantity"] or
                        row["ask_size"] is not None and row["ask_size"]!=best["ask"]["quantity"]):
                        raise ValueError("quote_book_values_mismatch")
                    selected=(row,matching,qref,bref)
                    break
                except ValueError as error:
                    code=error.code if isinstance(error,candles.CollectionError) else str(error)
                    rejected.add(code if code in ("quote_book_source_mismatch","quote_book_values_mismatch","missing_or_inconsistent_source_provenance","missing_source_provenance","missing_or_duplicate_book_levels","unordered_book_prices","crossed_book") else "invalid_book_snapshot_or_provenance")
                    continue
            if selected:
                row,matching,qref,bref=selected
                warnings.extend("rejected_newer_input:"+code for code in sorted(rejected))
                quote=QuoteSnapshot(kraken_pair_id=metadata.kraken_pair_id,source=qref,**row)
                provenance=json.loads((self.data_dir/bref.response_id).read_text())["provenance"]
                book=OrderBookSnapshot(metadata.pair_id,row["timestamp"],tuple(BookLevel(**{k:r[k] for k in ("side","level","price","quantity")}) for r in ordered("orderbook",matching)),
                    provenance["requested_depth"],provenance["actual_levels"]["bid"],provenance["actual_levels"]["ask"],provenance["snapshot"],tuple(provenance["level_update_epochs"]),bref)
                refs.extend((qref,bref))
                if (request.observation_time-row["timestamp"]).total_seconds()>config.max_market_age_seconds:
                    missing.append("stale_quote_and_orderbook")
            elif eligible:
                missing.extend(("missing_or_incomplete_corresponding_book","missing_or_inconsistent_source_provenance"))
                missing.extend(sorted(rejected))
            else:
                missing.extend(("missing_quote","missing_orderbook"))
        except Exception:
            missing.append("invalid_or_missing_market_records")
        try:
            rows=self.store.read_all(request.pair_id,"prices_1m").to_pylist()
            rows=sorted((r for r in rows if r["is_complete"] and r["timestamp"]+timedelta(minutes=1)<=request.observation_time),key=lambda r:r["timestamp"],reverse=True)
            for row in rows:
                try:
                    ref=self._reference("prices_1m",[row],metadata,request.observation_time)
                    if self._namespace("ohlcv_collection")!=namespace:
                        raise ValueError("Market-data source namespace mismatch")
                    candle=CandleSnapshot(source=ref,**row)
                    refs.append(ref)
                    break
                except ValueError:
                    continue
            if candle is None:
                missing.append("missing_completed_ohlcv_or_provenance")
            elif (request.observation_time-candle.timestamp).total_seconds()>config.max_candle_age_seconds:
                missing.append("stale_ohlcv")
        except Exception:
            missing.append("invalid_or_missing_ohlcv")
        fee,fee_error,fee_warnings=self._fee(request,namespace,config)
        if fee_error:
            missing.append(fee_error)
        warnings.extend(fee_warnings)
        if any(getattr(metadata,k) is None for k in ("min_order_qty","min_order_value","qty_precision","price_precision")):
            warnings.append("optional_exchange_constraints_unavailable")
        provenance=InputProvenance(1,namespace or "unverified",fee.version if fee else None,digest(metadata),tuple(refs),config,tuple(warnings))
        inputs=CostModelInputs(request,quote,book,fee,metadata,candle,provenance)
        return PreparedInputs(inputs,"inputs_incomplete" if missing else "inputs_ready",tuple(sorted(set(missing))),tuple(sorted(set(warnings))))

    def persist(self,prepared:PreparedInputs):
        inputs=prepared.inputs
        # Metadata is small and changes in-place; preserve its exact observed record.
        # Large market datasets remain references to logical records + source receipts.
        manifest=dict(contract_version=1,request=inputs.request,metadata=inputs.market_metadata,
            provenance=inputs.provenance,status=prepared.status,missing_inputs=prepared.missing_inputs,warnings=prepared.warnings)
        version=self.audit.put("inputs",manifest)
        if prepared.missing_inputs:
            with connect_metadata(self.data_dir) as db:
                for code in prepared.missing_inputs:
                    db.execute("INSERT OR IGNORE INTO data_quality_issues(issue_id,pair_id,dataset,observation_at,issue_type,description,detected_at,resolution_status) VALUES (?,?, 'costs_1m',?,?,?,?, 'open')",
                        (version+":"+code,inputs.request.pair_id,candles.sql_time(inputs.request.observation_time),code,"cost_infrastructure input validation: "+code,candles.sql_time(self.clock())))
        return version

    def load(self,version):
        """Resolve a pinned manifest, without selecting newer data or refreshing fees."""
        doc=self.audit.get("inputs",version)
        r=doc["request"]
        request=CostRequest(r["pair_id"],r["side"],Decimal(r["notional_aud"]),r["execution_type"],candles.read_time(r["observation_time"]),r["model_version"])
        m=doc["metadata"]
        if (digest(m) if m is not None else None)!=doc["provenance"]["metadata_hash"]:
            raise ValueError("missing_or_changed_metadata_dependency")
        if m is not None:
            m["last_seen"]=candles.read_time(m["last_seen"])
            for k in ("min_order_qty","min_order_value"):
                m[k]=Decimal(m[k]) if m[k] is not None else None
        metadata=MarketMetadata(**m) if m is not None else None
        quote=book=candle=None
        refs=[]
        for raw in doc["provenance"]["sources"]:
            raw["timestamp"]=candles.read_time(raw["timestamp"])
            raw["latency_seconds"]=Decimal(raw["latency_seconds"]) if raw["latency_seconds"] is not None else None
            ref=SourceReference(**raw)
            # Validate paths from manifests rather than trusting stored filesystem text.
            source=(self.data_dir/ref.response_id)
            if source.is_symlink() or not source.resolve().is_relative_to(self.data_dir.resolve()):
                raise ValueError("Invalid source dependency path")
            receipt=json.loads(source.read_text())
            if digest(receipt)!=ref.response_hash:
                raise ValueError("changed_source_dependency")
            rows=self.store.read(ref.pair_id,ref.dataset,ref.timestamp.strftime("%Y-%m")).to_pylist()
            rows=[r for r in rows if r["timestamp"]==ref.timestamp]
            if not rows or digest(ordered(ref.dataset,rows))!=ref.content_hash:
                raise ValueError("missing_or_changed_parquet_dependency")
            checked=self._reference(ref.dataset,rows,metadata,request.observation_time,ref.response_id)
            if checked.content_hash!=ref.content_hash or checked.response_hash!=ref.response_hash:
                raise ValueError("inconsistent_source_dependency")
            refs.append(ref)
            if ref.dataset=="quotes_1m":
                quote=QuoteSnapshot(kraken_pair_id=metadata.kraken_pair_id,source=ref,**rows[0])
            elif ref.dataset=="orderbook":
                p=receipt["provenance"]
                books.validate_snapshot(rows)
                book=OrderBookSnapshot(metadata.pair_id,ref.timestamp,tuple(BookLevel(**{k:r[k] for k in ("side","level","price","quantity")}) for r in ordered(ref.dataset,rows)),p["requested_depth"],p["actual_levels"]["bid"],p["actual_levels"]["ask"],p["snapshot"],tuple(p["level_update_epochs"]),ref)
            elif ref.dataset=="prices_1m":
                candle=CandleSnapshot(source=ref,**rows[0])
        p=doc["provenance"]
        fee=fee_view(self.data_dir,p["fee_version"],request.pair_id,assumption="present_day_fee_assumption_not_historical_applicability" in p["assumptions"]) if p["fee_version"] else None
        provenance=InputProvenance(1,p["source_namespace"],p["fee_version"],p["metadata_hash"],tuple(refs),PreparationConfig(**p["config"]),tuple(p["assumptions"]))
        return PreparedInputs(CostModelInputs(request,quote,book,fee,metadata,candle,provenance),doc["status"],tuple(doc["missing_inputs"]),tuple(doc["warnings"]))
