"""Bounded public REST L2 snapshots and best quotes; never historical reconstruction."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import time
import uuid

from .asset_catalogue import _atomic_json
from .db_environment import load_config
from .metadata_store import connect_metadata
from .ohlcv_collector import CollectionError, collection_universe, sql_time, read_time
from .operations import ProcessLock
from .parquet_store import MonthlyParquetStore, DuplicateKeyError
from .storage_schema import table_from_records, utc_timestamp, LOGICAL_KEYS

DATASETS = ("quotes_1m", "orderbook")


def encode(records):
    return [{k: sql_time(v) if k == "timestamp" else str(v) if isinstance(v, Decimal) else v
             for k, v in row.items()} for row in records]


def decode(records):
    return [{**row, "timestamp": read_time(row["timestamp"])} for row in records]


def validate_snapshot(records):
    """Validate each whole snapshot, including continuity and both book sides."""
    groups = {}
    for row in records:
        groups.setdefault((row["pair_id"], row["timestamp"]), []).append(row)
    for rows in groups.values():
        sides = {}
        for side in ("bid", "ask"):
            levels = sorted((r for r in rows if r["side"] == side), key=lambda r: r["level"])
            if not levels or [r["level"] for r in levels] != list(range(1, len(levels)+1)):
                raise CollectionError("missing_or_duplicate_book_levels")
            prices = [r["price"] for r in levels]
            if any((a <= b if side == "bid" else a >= b) for a, b in zip(prices, prices[1:])):
                raise CollectionError("unordered_book_prices")
            sides[side] = prices
        if sides["bid"][0] > sides["ask"][0]:
            raise CollectionError("crossed_book")
    return records


def parse_depth(body, asset, started, finished, depth=10, max_latency=5):
    started, finished = utc_timestamp(started), utc_timestamp(finished)
    if not 0 <= (finished-started).total_seconds() <= max_latency:
        raise CollectionError("stale_response_or_clock_regression")
    if not isinstance(body, dict) or body.get("error") != [] or not isinstance(body.get("result"), dict):
        raise CollectionError("exchange_error_or_bad_envelope")
    result = body["result"]
    if len(result) != 1 or not set(result) <= {asset["kraken_pair_id"], asset["base_asset"]+"/"+asset["quote_asset"]}:
        raise CollectionError("response_identity_mismatch")
    book = result[next(iter(result))]
    records, times, counts = [], [], {}
    try:
        for side, name in (("bid", "bids"), ("ask", "asks")):
            levels = book[name]
            if not isinstance(levels, list) or not 1 <= len(levels) <= depth:
                raise ValueError()
            counts[side] = len(levels)
            for index, level in enumerate(levels, 1):
                if not isinstance(level, list) or len(level) != 3:
                    raise ValueError()
                # Level time is a last-update time, not snapshot/event freshness.
                stamp = Decimal(str(level[2]))
                if isinstance(level[2], bool) or not stamp.is_finite() or stamp < 0 or stamp > Decimal(str(finished.timestamp()))+1:
                    raise ValueError()
                times.append(str(stamp))
                records.append(dict(pair_id=asset["pair_id"], timestamp=started, side=side,
                                    level=index, price=level[0], quantity=level[1]))
        records = table_from_records("orderbook", records).to_pylist()
        validate_snapshot(records)
    except CollectionError:
        raise
    except Exception:
        raise CollectionError("invalid_depth_snapshot") from None
    best = {r["side"]: r for r in records if r["level"] == 1}
    quote = dict(pair_id=asset["pair_id"], timestamp=started, bid=best["bid"]["price"],
                 ask=best["ask"]["price"], bid_size=best["bid"]["quantity"], ask_size=best["ask"]["quantity"])
    return {"quotes_1m": table_from_records("quotes_1m", [quote]).to_pylist(), "orderbook": records,
            "provenance": dict(timestamp_semantics="local_request_start_UTC", source="Kraken_REST_Depth",
                received_at=sql_time(finished), latency_seconds=(finished-started).total_seconds(),
                level_update_epochs=times, actual_levels=counts, requested_depth=depth,
                snapshot="complete_returned_L2_top_depth", exchange_snapshot_time_available=False)}


def checkpoint(data_dir, pair, dataset):
    with connect_metadata(data_dir, readonly=True) as db:
        row = db.execute("SELECT latest_observation_at,updated_at FROM ingestion_state WHERE pair_id=? AND dataset=?", (pair, dataset)).fetchone()
    return dict(latest_observation_at=row[0], updated_at=row[1]) if row else None


def stored(store, pair, dataset):
    rows = store.read_all(pair, dataset).to_pylist()
    if dataset == "orderbook":
        validate_snapshot(rows)
    return rows


class BookCollector:
    def __init__(self, data_dir, *, transport=None, clock=lambda: datetime.now(timezone.utc),
                 sleeper=time.sleep, monotonic=time.monotonic):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir/"book_collection"
        self.store = MonthlyParquetStore(data_dir)
        self.transport, self.clock, self.sleep, self.monotonic = transport, clock, sleeper, monotonic
        self.source = "kraken_public" if transport is None else "injected_test"
        self.last_request = None
        self.throttled = False

    def _binding(self):
        path = self.root/"source.json"
        expected = dict(version=1, source=self.source, datasets=list(DATASETS))
        if path.exists():
            if json.loads(path.read_text()) != expected:
                raise CollectionError("source_namespace_mismatch")
        else:
            if any(list(self.store.root.glob("*/"+d+"/*.parquet")) for d in DATASETS):
                raise CollectionError("existing_data_has_no_provenance_binding")
            _atomic_json(path, expected)

    def _request(self, asset, depth, deadline):
        if self.throttled:
            raise CollectionError("deferred_after_rate_limit")
        limiter = self.root/"last_request.json"
        delay = 0
        if self.last_request is not None:
            delay = 1.1-(self.monotonic()-self.last_request)
        elif limiter.exists():
            elapsed = (utc_timestamp(self.clock())-read_time(json.loads(limiter.read_text())["started_at"])).total_seconds()
            if elapsed < 0:
                raise CollectionError("request_clock_regressed")
            delay = 1.1-elapsed
        if self.monotonic()+max(0, delay) >= deadline:
            raise CollectionError("session_deadline")
        if delay > 0:
            self.sleep(delay)
        started = utc_timestamp(self.clock())
        _atomic_json(limiter, dict(started_at=sql_time(started)))
        self.last_request = self.monotonic()
        transport = self.transport
        if transport is None:
            from .market_data import public_get
            transport = public_get
        try:
            body = transport("Depth", dict(pair=asset["kraken_pair_id"], count=depth, assetVersion=1))
        except Exception as error:
            self.throttled = getattr(error, "code", None) == 429
            raise CollectionError("rate_limited" if self.throttled else "network_request_failed") from None
        if isinstance(body, dict) and body.get("error"):
            self.throttled = any("rate limit" in str(e).lower() or "throttl" in str(e).lower() for e in body["error"])
            raise CollectionError("rate_limited" if self.throttled else "exchange_error")
        finished = utc_timestamp(self.clock())
        parsed = parse_depth(body, asset, started, finished, depth)
        return parsed, dict(body=body, started_at=sql_time(started), finished_at=sql_time(finished),
                            kraken_pair_id=asset["kraken_pair_id"], source=self.source, provenance=parsed["provenance"])

    def _reconcile(self, pair, dataset):
        latest = None
        # Validate history one monthly partition at a time, never hold all history.
        for month in self.store.months(pair,dataset):
            rows = self.store.read(pair,dataset,month).to_pylist()
            if dataset == "orderbook":
                validate_snapshot(rows)
            candidate = max((r["timestamp"] for r in rows),default=None)
            if candidate is not None:
                latest = max(latest,candidate) if latest is not None else candidate
        saved = checkpoint(self.data_dir, pair, dataset)
        if saved and saved["latest_observation_at"] and (latest is None or read_time(saved["latest_observation_at"]) > latest):
            raise CollectionError("checkpoint_ahead_of_verified_storage")
        now = utc_timestamp(self.clock())
        if latest is not None:
            if latest > now or saved and read_time(saved["updated_at"]) > now:
                raise CollectionError("checkpoint_clock_regressed")
            with connect_metadata(self.data_dir) as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT latest_observation_at FROM ingestion_state WHERE pair_id=? AND dataset=?", (pair,dataset)).fetchone()
                if current and current[0] and read_time(current[0]) > latest:
                    raise CollectionError("checkpoint_would_regress")
                db.execute("INSERT INTO ingestion_state VALUES (?,?,?,?) ON CONFLICT(pair_id,dataset) DO UPDATE SET latest_observation_at=excluded.latest_observation_at,updated_at=excluded.updated_at",
                           (pair,dataset,sql_time(latest),sql_time(now)))
        return latest

    def _persist(self, intent):
        pair, dataset = intent["pair_id"], intent["dataset"]
        if intent["source"] != self.source or dataset not in DATASETS:
            raise CollectionError("pending_provenance_mismatch")
        rows = table_from_records(dataset, decode(intent["records"])).to_pylist()
        if not rows or any(r["pair_id"] != pair or r["timestamp"] > utc_timestamp(self.clock()) for r in rows):
            raise CollectionError("invalid_pending_observations")
        if dataset == "orderbook":
            validate_snapshot(rows)
        months = sorted({r["timestamp"].strftime("%Y-%m") for r in rows})
        old = [r for month in months for r in self.store.read(pair,dataset,month).to_pylist()]
        if dataset == "orderbook":
            validate_snapshot(old)
        keys = LOGICAL_KEYS[dataset]
        key = lambda r: tuple(r[k] for k in keys)
        previous = {key(r): r for r in old}
        if any(key(r) in previous and previous[key(r)] != r for r in rows):
            raise CollectionError("conflicting_logical_key")
        # A timestamp identifies one whole book, never an extensible level fragment.
        if dataset == "orderbook":
            for stamp in {r["timestamp"] for r in rows} & {r["timestamp"] for r in old}:
                if {key(r) for r in rows if r["timestamp"] == stamp} != {key(r) for r in old if r["timestamp"] == stamp}:
                    raise CollectionError("conflicting_snapshot_shape")
        self.store.write(dataset, rows)
        actual_rows = [r for month in months for r in self.store.read(pair,dataset,month).to_pylist()]
        if dataset == "orderbook":
            validate_snapshot(actual_rows)
        actual = {key(r): r for r in actual_rows}
        if any(actual.get(key(r)) != r for r in rows):
            raise CollectionError("persistence_verification_failed")
        self._reconcile(pair, dataset)
        return dict(new_rows=len(actual)-len(old), duplicate_rows=sum(key(r) in previous for r in rows))

    def _quality(self, pair, dataset, code, run_id):
        now = sql_time(self.clock())
        with connect_metadata(self.data_dir) as db:
            db.execute("INSERT OR IGNORE INTO data_quality_issues(issue_id,pair_id,dataset,observation_at,issue_type,description,detected_at,resolution_status) VALUES (?,?,?,?,?,?,?,'open')",
                       (run_id+":"+pair+":"+dataset+":"+code,pair,dataset,now,code,"book_collector: "+code,now))

    def _status(self, pair, failed):
        with connect_metadata(self.data_dir) as db:
            # Pair-wide table cannot encode per-dataset readiness. Preserve an existing
            # readiness decision; only initialize unknown and update operational state.
            db.execute("INSERT INTO collection_status VALUES (?,'unknown',?,?,?) ON CONFLICT(pair_id) DO UPDATE SET operational_status=excluded.operational_status,updated_at=excluded.updated_at,reason=excluded.reason WHERE collection_status.reason LIKE 'book_collector%' OR collection_status.operational_status IN ('unknown','idle')",
                       (pair,"error" if failed else "idle",sql_time(self.clock()),"book_collector latest attempt; no research eligibility decision"))

    def collect(self, *, pair=None, all_aud=False, dataset=None, duration_seconds=0,
                quote_interval=60, book_interval=60, depth=10):
        if dataset is not None and dataset not in DATASETS:
            raise CollectionError("invalid_dataset")
        if not 0 <= duration_seconds <= 3600 or not all(5 <= x <= 86400 for x in (quote_interval,book_interval)) or type(depth) is not int or not 1 <= depth <= 100:
            raise CollectionError("invalid_bounded_session_settings")
        assets,audit,unresolved = collection_universe(self.data_dir)
        if (pair is None) == (not all_aud):
            raise CollectionError("select_pair_or_all_aud")
        assets = assets if all_aud else [a for a in assets if a["pair_id"] == pair]
        if not assets:
            raise CollectionError("pair_not_approved_online_aud")
        selected = (dataset,) if dataset else DATASETS
        intervals = dict(quotes_1m=quote_interval, orderbook=book_interval)
        run = dict(run_id=uuid.uuid4().hex,status="running",source=self.source,catalogue_run_id=audit,
                   unresolved_unchanged=unresolved,selected_pairs=[a["pair_id"] for a in assets],datasets=list(selected),
                   intervals_seconds=intervals,depth=depth,duration_seconds=duration_seconds,started_at=sql_time(self.clock()),results=[])
        lock = ProcessLock(self.data_dir/"book-collector")
        run_path = self.root/"runs"/(run["run_id"]+".json")
        try:
            self._binding()
            _atomic_json(run_path,run)
            start = self.monotonic()
            deadline = start+duration_seconds if duration_seconds else float("inf")
            first = True
            while first or self.monotonic() < deadline:
                for asset in assets:
                    if not first and self.monotonic() >= deadline:
                        break
                    pair_id = asset["pair_id"]
                    due = []
                    failed = False
                    for name in selected:
                        try:
                            pending = self.root/"pending"/(pair_id+"_"+name+".json")
                            if pending.exists():
                                intent = json.loads(pending.read_text())
                                if intent["kraken_pair_id"] != asset["kraken_pair_id"] or intent["pair_id"] != pair_id or intent["dataset"] != name:
                                    raise CollectionError("pending_identity_mismatch")
                                self._persist(intent)
                                pending.unlink()
                            self._reconcile(pair_id,name)
                            saved = checkpoint(self.data_dir,pair_id,name)
                            age = (utc_timestamp(self.clock())-read_time(saved["latest_observation_at"])).total_seconds() if saved and saved["latest_observation_at"] else float("inf")
                            if age >= intervals[name]:
                                due.append(name)
                            elif first:
                                run["results"].append(dict(pair_id=pair_id,dataset=name,status="sampling_not_due"))
                        except Exception as error:
                            failed = True
                            code = error.code if isinstance(error,CollectionError) else "recovery_failed"
                            self._quality(pair_id,name,code,run["run_id"])
                            run["results"].append(dict(pair_id=pair_id,dataset=name,status="error",code=code))
                    if due:
                        try:
                            parsed,receipt = self._request(asset,depth,deadline)
                            _atomic_json(self.root/"responses"/pair_id/(uuid.uuid4().hex+".json"),receipt)
                            request_error = None
                        except Exception as error:
                            request_error = error.code if isinstance(error,CollectionError) else "request_failed"
                        for name in due:
                            try:
                                if request_error:
                                    raise CollectionError(request_error)
                                intent = dict(source=self.source,pair_id=pair_id,kraken_pair_id=asset["kraken_pair_id"],dataset=name,
                                              records=encode(parsed[name]),provenance=parsed["provenance"])
                                pending = self.root/"pending"/(pair_id+"_"+name+".json")
                                _atomic_json(pending,intent)
                                counts = self._persist(intent)
                                pending.unlink()
                                run["results"].append(dict(pair_id=pair_id,dataset=name,status="ok",timestamp=receipt["started_at"],
                                                          actual_levels=parsed["provenance"]["actual_levels"],**counts))
                            except Exception as error:
                                code = error.code if isinstance(error,CollectionError) else "conflicting_logical_key" if isinstance(error,DuplicateKeyError) else "persistence_failed"
                                failed = failed or code != "session_deadline"
                                if code != "session_deadline":
                                    self._quality(pair_id,name,code,run["run_id"])
                                run["results"].append(dict(pair_id=pair_id,dataset=name,status="deferred" if code == "session_deadline" else "error",code=code))
                    self._status(pair_id,failed)
                    _atomic_json(run_path,run)
                first = False
                if not duration_seconds or self.throttled:
                    break
                remaining = deadline-self.monotonic()
                if remaining > 0:
                    self.sleep(min(1,remaining))
            run["status"] = "finished"
            for asset in assets:
                for name in selected:
                    observed = sorted(read_time(r["timestamp"]) for r in run["results"] if r["pair_id"] == asset["pair_id"] and r["dataset"] == name and r["status"] == "ok")
                    bounds = [read_time(run["started_at"])]+observed+[utc_timestamp(self.clock())]
                    missed = sum(max(0,int((b-a).total_seconds()/intervals[name])-1) for a,b in zip(bounds,bounds[1:]))
                    if missed:
                        self._quality(asset["pair_id"],name,"unresolved_sampling_intervals",run["run_id"])
        except KeyboardInterrupt:
            run["status"] = "interrupted"
        finally:
            run["finished_at"] = sql_time(self.clock())
            completed = {(r["pair_id"],r["dataset"]) for r in run["results"] if r["status"] in ("ok","sampling_not_due")}
            run["uncollected_pair_datasets"] = [dict(pair_id=a["pair_id"],dataset=d) for a in assets for d in selected if (a["pair_id"],d) not in completed]
            try:
                if run_path.exists():
                    _atomic_json(run_path,run)
            finally:
                lock.close()
        return run


def report(data_dir, *, now=None):
    now = utc_timestamp(now or datetime.now(timezone.utc))
    assets,audit,unresolved = collection_universe(data_dir)
    eligible = {a["pair_id"] for a in assets}
    with connect_metadata(data_dir,readonly=True) as db:
        historical = [r[0] for r in db.execute("SELECT DISTINCT pair_id FROM ingestion_state WHERE dataset IN ('quotes_1m','orderbook')")]
        issues = [dict(zip(("issue_id","pair_id","dataset","issue_type","resolution_status"),r)) for r in db.execute("SELECT issue_id,pair_id,dataset,issue_type,resolution_status FROM data_quality_issues WHERE dataset IN ('quotes_1m','orderbook')")]
        statuses = {r[0]:dict(readiness=r[1],operational_status=r[2],reason=r[3]) for r in db.execute("SELECT pair_id,readiness,operational_status,reason FROM collection_status")}
    results = []
    store = MonthlyParquetStore(data_dir)
    runs = sorted((json.loads(path.read_text()) for path in (Path(data_dir)/"book_collection/runs").glob("*.json")), key=lambda r:r["started_at"])
    for pair in sorted(eligible | set(historical)):
        for dataset in DATASETS:
            try:
                rows = stored(store,pair,dataset)
                times = sorted({r["timestamp"] for r in rows})
                saved = checkpoint(data_dir,pair,dataset)
                latest = sql_time(times[-1]) if times else None
                status = "matches" if saved and latest == saved["latest_observation_at"] else "empty" if not saved and not times else "mismatch"
                intervals = [(b-a).total_seconds() for a,b in zip(times,times[1:])]
                # Session receipts define expected sampling; offline time is not a promised session.
                gaps = []
                period = None
                for run in runs:
                    if pair not in run["selected_pairs"] or dataset not in run["datasets"]:
                        continue
                    period = run["intervals_seconds"][dataset]
                    begin = read_time(run["started_at"])
                    # A crashed journal is not evidence of continued operation.
                    observed = [read_time(r["timestamp"]) for r in run["results"] if r.get("timestamp")]
                    end = read_time(run["finished_at"]) if run.get("finished_at") else max(observed,default=begin)
                    sampled = [t for t in times if begin <= t <= end]
                    boundaries = [begin]+sampled+[end]
                    missing = sum(max(0,int((b-a).total_seconds()/period)-1) for a,b in zip(boundaries,boundaries[1:]))
                    if missing:
                        gaps.append(dict(run_id=run["run_id"],unresolved_sampling_intervals=missing))
                own = [i for i in issues if i["pair_id"] == pair and i["dataset"] == dataset]
                codes = [i["issue_type"] for i in own]
                results.append(dict(pair_id=pair,dataset=dataset,eligible=pair in eligible,observations=len(times),level_rows=len(rows) if dataset == "orderbook" else None,
                    earliest=sql_time(times[0]) if times else None,latest=latest,mean_interval_seconds=sum(intervals)/len(intervals) if intervals else None,
                    age_seconds=(now-times[-1]).total_seconds() if times else None,source_event_freshness="unavailable; local observation freshness only",
                    checkpoint=saved,checkpoint_status=status,session_sampling_gaps=gaps,issues=own,
                    configured_interval_seconds=period,
                    stale_local_observation=bool(times and period and (now-times[-1]).total_seconds() > period+5),
                    invalid_attempts=sum(c in ("invalid_depth_snapshot","unordered_book_prices","crossed_book","missing_or_duplicate_book_levels","stale_response_or_clock_regression") for c in codes),
                    conflicting_attempts=sum("conflict" in c for c in codes),
                    source_errors=sum(c in ("rate_limited","deferred_after_rate_limit","network_request_failed","exchange_error","exchange_error_or_bad_envelope","response_identity_mismatch") for c in codes),
                    duplicate_records=0,readiness=statuses.get(pair,{}),pending_recovery=(Path(data_dir)/"book_collection/pending"/(pair+"_"+dataset+".json")).exists()))
            except Exception:
                results.append(dict(pair_id=pair,dataset=dataset,checkpoint_status="storage_validation_failed"))
    binding = Path(data_dir)/"book_collection/source.json"
    source = json.loads(binding.read_text())["source"] if binding.exists() else "unverified_or_empty"
    return dict(source=source,catalogue_run_id=audit,unresolved_unchanged=unresolved,pairs=results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("collect","report"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--pair")
    group.add_argument("--all-aud",action="store_true")
    parser.add_argument("--dataset",choices=DATASETS)
    parser.add_argument("--duration-seconds",type=float,default=0)
    parser.add_argument("--quote-interval",type=float,default=60)
    parser.add_argument("--book-interval",type=float,default=60)
    parser.add_argument("--depth",type=int,default=10)
    parser.add_argument("--root",default=".")
    parser.add_argument("--data-dir")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.root,data_dir=args.data_dir)
        if args.command == "report":
            if args.pair or args.all_aud or args.dataset:
                raise CollectionError("report_is_offline_without_selection")
            result = report(config.data_dir)
            code = int(any(r["checkpoint_status"] not in ("matches","empty") for r in result["pairs"]))
        else:
            result = BookCollector(config.data_dir).collect(pair=args.pair,all_aud=args.all_aud,dataset=args.dataset,
                duration_seconds=args.duration_seconds,quote_interval=args.quote_interval,book_interval=args.book_interval,depth=args.depth)
            # Reaching a bounded session's deadline is normal once every requested
            # pair/dataset is accounted for; genuine errors still fail the command.
            code = int(result["status"] != "finished" or bool(result["uncollected_pair_datasets"]) or any(r["status"] == "error" for r in result["results"]))
        print(json.dumps(result,indent=2))
        return code
    except Exception as error:
        print(json.dumps(dict(error=error.code if isinstance(error,CollectionError) else "operation_failed_private_details_suppressed")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
