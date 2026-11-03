"""Bounded historical provider contract. Only Kraken REST is approved here."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import uuid

from . import ohlcv_collector as ohlc
from .asset_catalogue import _atomic_json
from .operations import ProcessLock
from .history_coverage import window,availability


@dataclass(frozen=True)
class HistoricalBatch:
    records: tuple[dict,...]
    fetched_at: datetime
    last_marker: int
    response_id: str
    uncommitted_rows: int


@dataclass(frozen=True)
class HistoricalFetch:
    pair_id: str
    batches: tuple[HistoricalBatch,...]
    evidence: dict
    start: datetime
    end_exclusive: datetime

    @property
    def records(self)->tuple[dict,...]:
        """Requested-window records; raw batches retain overlap for source audits."""
        unique={}
        for batch in self.batches:
            for row in batch.records:
                stamp=row["timestamp"]
                if self.start<=stamp<self.end_exclusive:
                    if stamp in unique and unique[stamp]!=row:
                        raise ohlc.CollectionError("conflicting_source_probe_values")
                    unique[stamp]=row
        return tuple(unique[t] for t in sorted(unique))


class HistoricalDataProvider:
    def fetch(self,pair_id:str,start_time:datetime,end_time:datetime,interval_minutes:int=1)->HistoricalFetch:
        raise NotImplementedError("An approved historical provider implementation is required")


class KrakenOHLCProvider(HistoricalDataProvider):
    def __init__(self,data_dir,**collector_options):
        self.collector=ohlc.OhlcvCollector(data_dir,**collector_options)
        self.data_dir=Path(data_dir)

    def fetch(self,pair_id,start_time,end_time,interval_minutes=1):
        # Standalone fetch also uses the existing collector-wide lock and pacing.
        assets=ohlc.collection_universe(self.data_dir)[0]
        asset=next((a for a in assets if a["pair_id"]==pair_id),None)
        if asset is None:
            raise ValueError("Unknown verified AUD market")
        lock=ProcessLock(self.data_dir/"ohlcv-collector")
        try:
            self.collector._binding()
            return self._fetch_locked(asset,start_time,end_time,interval_minutes)
        finally:
            lock.close()

    def _one(self,asset,since):
        params=dict(pair=asset["kraken_pair_id"],interval=1,assetVersion=1,since=since)
        body=self.collector._request(params)
        fetched=ohlc.utc_timestamp(self.collector.clock())
        path=self.collector.root/"responses"/asset["pair_id"]/(uuid.uuid4().hex+".json")
        _atomic_json(path,dict(source=self.collector.source,params=params,fetched_at=ohlc.sql_time(fetched),body=body))
        parsed=ohlc.parse_ohlc(body,asset,fetched)
        if parsed["invalid_observations"]:
            raise ohlc.CollectionError("invalid_historical_response",invalid_observations=parsed["invalid_observations"])
        return HistoricalBatch(tuple(parsed["records"]),fetched,parsed["last_marker"],str(path.relative_to(self.data_dir)).replace("\\","/"),parsed["uncommitted_rows"])

    def _fetch_locked(self,asset,start,end,interval_minutes=1):
        start,end=window(start,end)
        if interval_minutes!=1:
            raise ValueError("Only approved prices_1m interval is supported")
        cached=availability(self.data_dir,asset["pair_id"])
        if cached and (cached.get("source_namespace")!=self.collector.source or cached.get("kraken_pair_id")!=asset["kraken_pair_id"]):
            raise ValueError("Cached source evidence identity mismatch")
        if cached and cached.get("no_completed_history_as_of") and end<=ohlc.read_time(cached["no_completed_history_as_of"]):
            return HistoricalFetch(asset["pair_id"],(),{**cached,"request_skipped":"no_completed_history_returned_at_confirmed_probe"},start,end)
        if cached and cached.get("floor_verified") and end<=ohlc.read_time(cached["oldest_accessible"]):
            return HistoricalFetch(asset["pair_id"],(),{**cached,"request_skipped":"requested_period_precedes_verified_rest_floor"},start,end)
        # Overlap one minute because Kraken's since cursor can be exclusive.
        first=self._one(asset,max(0,int(start.timestamp())-60))
        batches=[first]
        oldest=min((r["timestamp"] for r in first.records),default=None)
        newest=max((r["timestamp"] for r in first.records),default=None)
        floor=ohlc.read_time(cached["oldest_accessible"]) if cached and cached.get("floor_verified") else None
        verified=bool(floor)
        older=None
        # Verify truncation once, not an unlimited historical pagination loop.
        empty_verified=False
        if oldest is None or (start<oldest and floor is None):
            probe=self._one(asset,0)
            batches.append(probe)
            probe_oldest=min((r["timestamp"] for r in probe.records),default=None)
            older=probe_oldest is not None and (oldest is None or probe_oldest<oldest)
            if probe_oldest is not None:
                initially_empty=oldest is None
                oldest=min(oldest,probe_oldest) if oldest is not None else probe_oldest
                newest=max(newest,max(r["timestamp"] for r in probe.records)) if newest else max(r["timestamp"] for r in probe.records)
                if not older or initially_empty:
                    floor=oldest;verified=True
            elif oldest is None:
                empty_verified=True
                floor=None;verified=False
        elif floor is not None and oldest is not None and start<=floor and oldest>=floor:
            floor=oldest  # Rolling REST retention moved forward; never infer a floor from a narrow since.
        evidence=dict(source="kraken_public_OHLC",source_namespace=self.collector.source,pair_id=asset["pair_id"],kraken_pair_id=asset["kraken_pair_id"],
            query_start=ohlc.sql_time(start),query_end_exclusive=ohlc.sql_time(end),
            observed_at=ohlc.sql_time(self.collector.clock()),oldest_accessible=ohlc.sql_time(floor or oldest) if oldest or floor else None,
            newest_completed=ohlc.sql_time(newest) if newest else None,floor_verified=verified,
            requested_start_available=oldest is not None and oldest<=start,
            requested_start_observed=any(r["timestamp"]==start for b in batches for r in b.records),
            accessible_history_exhausted=bool(empty_verified or verified and floor and start<floor),
            additional_probe_retrieved_older=older,
            older_pagination="not_supported_by_Kraken_REST_contract",response_ids=[b.response_id for b in batches],
            response_completed_counts=[len(b.records) for b in batches],uncommitted_rows_excluded=sum(b.uncommitted_rows for b in batches),
            limitation="approximately 720 recent entries; since is an incremental cursor, not unlimited historical pagination")
        evidence["oldest_returned_in_fetch"]=ohlc.sql_time(oldest) if oldest else None
        evidence["no_completed_history_as_of"]=ohlc.sql_time(ohlc.minute_start(batches[-1].fetched_at)) if empty_verified else None
        evidence["status"]="no_completed_candles_returned" if empty_verified else "available_recent_window"
        evidence["floor_evidence_response_ids"]=(cached.get("floor_evidence_response_ids",cached["response_ids"]) if cached and cached.get("floor_verified") else [b.response_id for b in batches])
        if older:
            evidence["unexpected_source_behavior"]="older_probe_returned_earlier_data; bounded verification only, no completeness claim"
        _atomic_json(self.data_dir/"history/availability"/(asset["pair_id"]+".json"),evidence)
        return HistoricalFetch(asset["pair_id"],tuple(batches),evidence,start,end)
