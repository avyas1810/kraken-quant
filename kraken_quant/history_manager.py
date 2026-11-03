"""Research-window coverage, bounded public backfill and safe OHLCV gap repair."""
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import uuid

from . import ohlcv_collector as ohlc
from .asset_catalogue import _atomic_json
from .db_environment import load_config
from .history_coverage import (window,inventory,minute_coverage,coverage_report,availability)
from .history_provider import KrakenOHLCProvider
from .metadata_store import connect_metadata
from .operations import ProcessLock


class HistoryManager:
    def __init__(self,data_dir,**collector_options):
        self.data_dir=Path(data_dir)
        self.provider=KrakenOHLCProvider(data_dir,**collector_options)
        self.collector=self.provider.collector

    def _recover(self,asset):
        path=self.collector.root/"pending"/(asset["pair_id"]+".json")
        if path.exists():
            self.collector._persist_intent(asset,json.loads(path.read_text()))
            path.unlink()
        rows=ohlc._stored(self.collector.store,asset["pair_id"],self.collector.clock())
        self.collector._set_checkpoint(asset,rows)

    def _coverage(self,pair,start,end):
        stats,times,bad=inventory(self.data_dir,pair,"prices_1m",start,end,self.collector.clock())
        if stats["integrity_unknown"] or stats["invalid_records"] or stats["duplicate_records"] or stats["uncommitted_records"]:
            raise ohlc.CollectionError("local_history_invalid; repair_does_not_overwrite_corrupt_partitions")
        return minute_coverage(times,start,end,self.collector.clock(),invalid_times=bad,source=availability(self.data_dir,pair)),times

    def _record_quality(self,asset,result):
        now=ohlc.sql_time(self.collector.clock())
        coverage=result.get("coverage_after")
        if not coverage:
            return
        with connect_metadata(self.data_dir) as db:
            for gap in coverage["missing_intervals"]:
                identity=hashlib.sha256(json.dumps([asset["pair_id"],gap],sort_keys=True).encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO data_quality_issues(issue_id,pair_id,dataset,period_start,period_end,issue_type,description,detected_at,resolution_status) VALUES (?,?,'prices_1m',?,?,?,?,?,'open')",
                    (identity,asset["pair_id"],gap["start"],ohlc.sql_time(ohlc.read_time(gap["end_exclusive"])-ohlc.MINUTE),gap["classification"],"history_manager: local gap; source classification at recorded probe",now))

    def run(self,*,start,end,pair=None,all_aud=False,repair=False):
        start,end=window(start,end)
        assets,audit,unresolved=ohlc.collection_universe(self.data_dir)
        if (pair is None)==(not all_aud):
            raise ValueError("Select verified pair or all AUD")
        assets=assets if all_aud else [a for a in assets if a["pair_id"]==pair]
        if not assets:
            raise ValueError("Pair is not a verified online AUD market")
        if end>ohlc.minute_start(self.collector.clock()):
            raise ValueError("Collection end must exclude uncommitted/future minutes")
        run=dict(run_id=uuid.uuid4().hex,status="running",operation="repair" if repair else "backfill",catalogue_run_id=audit,
            source_namespace=self.collector.source,unresolved_unchanged=unresolved,requested_start=ohlc.sql_time(start),requested_end_exclusive=ohlc.sql_time(end),
            started_at=ohlc.sql_time(self.collector.clock()),selected_pairs=[a["pair_id"] for a in assets],results=[])
        path=self.data_dir/"history/runs"/(run["run_id"]+".json")
        lock=ProcessLock(self.data_dir/"ohlcv-collector")
        try:
            self.collector._binding()
            _atomic_json(path,run)
            for asset in assets:
                requests_before=self.collector.request_count
                try:
                    self._recover(asset)
                    before,times=self._coverage(asset["pair_id"],start,end)
                    missing=before["missing_intervals"]
                    fetchable=[g for g in missing if g["classification"]!="unavailable_from_kraken_rest_at_probe"]
                    new=overlap=0
                    evidence=availability(self.data_dir,asset["pair_id"])
                    if fetchable:
                        since=ohlc.read_time(fetchable[0]["start"]) if repair else start
                        if evidence and evidence.get("floor_verified"):
                            since=max(since,ohlc.read_time(evidence["oldest_accessible"]))
                        fetched=self.provider._fetch_locked(asset,since,end)
                        evidence=fetched.evidence
                        fetched.records  # Validate cross-response conflicts before any new write.
                        for batch in fetched.batches:
                            records=[r for r in batch.records if start<=r["timestamp"]<end]
                            # Validate all returned overlaps before selecting only missing
                            # rows for repair; completed-source revisions never overwrite.
                            months={r["timestamp"].strftime("%Y-%m") for r in records}
                            previous={r["timestamp"]:r for month in months for r in self.collector.store.read(asset["pair_id"],"prices_1m",month).to_pylist()}
                            if any(r["timestamp"] in previous and r!=previous[r["timestamp"]] for r in records):
                                raise ohlc.CollectionError("conflicting_historical_values")
                            if repair:
                                overlap+=sum(r["timestamp"] in previous for r in records)
                                records=[r for r in records if r["timestamp"] not in previous]
                            if not records:
                                continue
                            intent=dict(source=self.collector.source,pair_id=asset["pair_id"],kraken_pair_id=asset["kraken_pair_id"],interval=1,
                                records=ohlc._json_records(records),last_marker=batch.last_marker,fetched_at=ohlc.sql_time(batch.fetched_at))
                            pending=self.collector.root/"pending"/(asset["pair_id"]+".json")
                            _atomic_json(pending,intent)
                            checkpoint,added,duplicates=self.collector._persist_intent(asset,intent)
                            new+=added;overlap+=duplicates
                            self.collector._save_state(asset,dict(status="ok",checkpoint=checkpoint["latest_observation_at"],last_marker=batch.last_marker,fetched_at=ohlc.sql_time(batch.fetched_at)))
                            pending.unlink()
                    after,_=self._coverage(asset["pair_id"],start,end)
                    result=dict(pair_id=asset["pair_id"],status="complete" if not after["missing_minutes"] else "partial_unavailable" if after["unavailable_at_probe_minutes"] else "partial_unresolved",new_rows=new,identical_overlap_rows=overlap,
                        requested_interval_fully_retrieved=after["missing_minutes"]==0,
                        requested_start_available_from_source=False if evidence and (evidence.get("no_completed_history_as_of") or evidence.get("floor_verified") and start<ohlc.read_time(evidence["oldest_accessible"])) else None,
                        coverage_before=before,coverage_after=after,source_availability=evidence,
                        request_skipped=not bool(fetchable),http_requests=self.collector.request_count-requests_before,
                        checkpoint=ohlc._checkpoint(self.data_dir,asset["pair_id"]))
                    self._record_quality(asset,result)
                except Exception as error:
                    result=dict(pair_id=asset["pair_id"],status="error",code=error.code if isinstance(error,ohlc.CollectionError) else "history_request_or_persistence_failed",
                                http_requests=self.collector.request_count-requests_before)
                run["results"].append(result)
                _atomic_json(path,run)
            run["status"]="finished"
        except KeyboardInterrupt:
            run["status"]="interrupted"
        finally:
            run["finished_at"]=ohlc.sql_time(self.collector.clock())
            try:
                if path.exists():
                    _atomic_json(path,run)
            finally:
                lock.close()
        return run


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("coverage","gaps","backfill","repair"))
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pair")
    group.add_argument("--all-aud",action="store_true")
    parser.add_argument("--days",type=int,default=90)
    parser.add_argument("--start",type=ohlc.read_time)
    parser.add_argument("--end",type=ohlc.read_time)
    parser.add_argument("--source",choices=("kraken",),default="kraken")
    parser.add_argument("--root",default=".")
    parser.add_argument("--data-dir")
    args=parser.parse_args(argv)
    try:
        now=datetime.now(timezone.utc)
        end=args.end or ohlc.minute_start(now)
        if not 1<=args.days<=3660:
            raise ValueError("Bounded positive days required")
        start=args.start or end-timedelta(days=args.days)
        window(start,end)
        config=load_config(args.root,data_dir=args.data_dir)
        if args.command in ("coverage","gaps"):
            result=coverage_report(config.data_dir,start=start,end=end,now=now,pair=args.pair,all_aud=args.all_aud)
            code=int(any(r["data_readiness"]=="Unknown" for r in result["pairs"]))
        else:
            result=HistoryManager(config.data_dir).run(start=start,end=end,pair=args.pair,all_aud=args.all_aud,repair=args.command=="repair")
            code=1 if result["status"]!="finished" or any(r["status"]=="error" for r in result["results"]) else 2 if any(r["status"]!="complete" for r in result["results"]) else 0
        print(json.dumps(result,indent=2))
        return code
    except Exception as error:
        print(json.dumps({"error":error.code if isinstance(error,ohlc.CollectionError) else "history_operation_failed; private details suppressed"}))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
