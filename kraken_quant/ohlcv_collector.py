"""Bounded, public Kraken 1-minute OHLCV collection into existing storage."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
import uuid

from .asset_catalogue import catalogue_report, _atomic_json
from .db_environment import load_config
from .metadata_store import connect_metadata
from .operations import ProcessLock
from .parquet_store import MonthlyParquetStore, DuplicateKeyError
from .storage_schema import table_from_records, utc_timestamp

MINUTE = timedelta(minutes=1)
DATASET = "prices_1m"


class CollectionError(ValueError):
    def __init__(self, code, **details):
        self.code, self.details = code, details
        super().__init__(code)


def sql_time(value):
    return utc_timestamp(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def read_time(value):
    return utc_timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")))


def minute_start(value):
    return utc_timestamp(value).replace(second=0, microsecond=0)


def collection_universe(data_dir):
    """SQL selection is accepted only with consistent, positive spot provenance."""
    report = catalogue_report(data_dir)
    if report["audit_status"] != "current" or report["integrity_check"] != "ok":
        raise CollectionError("catalogue_audit_missing_or_stale")
    receipt = json.loads((Path(data_dir) / "catalogue_discovery/latest.json").read_text(encoding="utf-8"))
    if receipt.get("observed_at") != report["latest_discovery"]["observed_at"]:
        raise CollectionError("catalogue_changed_during_selection")
    accepted = {row["pair_id"]: row for row in receipt["records"]}
    unresolved = {row["kraken_pair_id"] for row in receipt["unresolved"]}
    selected = report["online_aud_spot_pairs"]
    for asset in selected:
        proof = accepted.get(asset["pair_id"], {})
        identity = proof.get("source_identity", {})
        if (asset["quote_asset"] != "AUD" or asset["status"] != "online"
                or proof.get("kraken_pair_id") != asset["kraken_pair_id"]
                or proof.get("base_asset") != asset["base_asset"] or proof.get("quote_asset") != "AUD"
                or proof.get("status") != "online" or proof.get("last_seen") != asset["last_seen"]
                or identity.get("requested_asset_class") != "currency"
                or identity.get("aclass_base") != "currency" or identity.get("aclass_quote") != "currency"
                or identity.get("execution_venue") != "international"
                or asset["kraken_pair_id"] in unresolved):
            raise CollectionError("spot_identity_not_confirmed")
    return selected, receipt["run_id"], len(receipt["unresolved"])


def _integer(value):
    if isinstance(value, bool) or not (type(value) is int or isinstance(value, str) and value.isascii() and value.isdigit()):
        raise ValueError("Integer required")
    value = int(value)
    if value < 0:
        raise ValueError("Nonnegative integer required")
    return value


def parse_ohlc(body, asset, now):
    """Discard the last array entry unconditionally; validate the entire prefix."""
    now = utc_timestamp(now)
    if not isinstance(body, dict) or body.get("error") != [] or not isinstance(body.get("result"), dict):
        raise CollectionError("exchange_api_error_or_bad_envelope")
    result = body["result"]
    keys = set(result) - {"last"}
    expected = asset["base_asset"] + "/" + asset["quote_asset"]
    if len(keys) != 1 or not keys <= {expected, asset["kraken_pair_id"]}:
        raise CollectionError("response_pair_identity_mismatch")
    try:
        marker = _integer(result["last"])
        if marker > int(now.timestamp()):
            raise ValueError()
    except (KeyError, ValueError, TypeError, OverflowError):
        raise CollectionError("invalid_last_marker") from None
    rows = result[next(iter(keys))]
    if not isinstance(rows, list):
        raise CollectionError("malformed_ohlc_rows")
    accepted, invalid, duplicates, times = {}, [], 0, []
    for index, raw in enumerate(rows):
        stamp = None
        try:
            if not isinstance(raw, list) or len(raw) not in (7, 8):
                raise ValueError()
            stamp = datetime.fromtimestamp(_integer(raw[0]), timezone.utc)
            if stamp.second or stamp.microsecond:
                raise ValueError()
            times.append(stamp)
            if index == len(rows) - 1:
                if stamp > minute_start(now):
                    raise ValueError()
                continue  # Always uncommitted, even if its timestamp looks old.
            if stamp > minute_start(now) - MINUTE:
                raise ValueError()
            count = None if len(raw) == 7 or raw[7] is None else _integer(raw[7])
            record = dict(pair_id=asset["pair_id"], timestamp=stamp, open=raw[1], high=raw[2], low=raw[3],
                          close=raw[4], volume_base=raw[6], trade_count=count, is_complete=True)
            record = table_from_records(DATASET, [record]).to_pylist()[0]
            if stamp in accepted:
                if accepted[stamp] != record:
                    raise CollectionError("conflicting_response_candle")
                duplicates += 1
            else:
                accepted[stamp] = record
        except Exception as error:
            invalid.append({"row_index": index, "observation_at": sql_time(stamp) if stamp is not None else None,
                            "code": error.code if isinstance(error, CollectionError) else "invalid_ohlcv_observation"})
    if times != sorted(times):
        invalid.append({"row_index": None, "observation_at": None, "code": "unordered_ohlc_response"})
    if len(times) > 1 and times[-1] in times[:-1]:
        invalid.append({"row_index": len(rows)-1, "observation_at": sql_time(times[-1]), "code": "unfinished_timestamp_overlap"})
    return dict(records=[accepted[key] for key in sorted(accepted)], response_rows=len(rows),
                uncommitted_rows=1 if rows else 0, identical_response_duplicates=duplicates,
                invalid_observations=invalid, last_marker=marker)


def _json_records(records):
    return [{key: sql_time(value) if key == "timestamp" else str(value) if key in
             ("open", "high", "low", "close", "volume_base") else value for key,value in row.items()} for row in records]


def _decode_records(records):
    return [{**row, "timestamp": read_time(row["timestamp"])} for row in records]


def _checkpoint(data_dir, pair_id):
    with connect_metadata(data_dir, readonly=True) as db:
        row = db.execute("SELECT latest_observation_at,updated_at FROM ingestion_state WHERE pair_id=? AND dataset=?", (pair_id, DATASET)).fetchone()
    return dict(latest_observation_at=row[0], updated_at=row[1]) if row else None


def _stored(store, pair_id, now):
    records = store.read_all(pair_id, DATASET).to_pylist()
    if any(not row["is_complete"] or row["timestamp"] > minute_start(now)-MINUTE for row in records):
        raise CollectionError("stored_candles_unfinished_or_future")
    return sorted(records, key=lambda row: row["timestamp"])


def _coverage(data_dir, asset, now):
    records = _stored(MonthlyParquetStore(data_dir), asset["pair_id"], now)
    checkpoint = _checkpoint(data_dir, asset["pair_id"])
    earliest = records[0]["timestamp"] if records else None
    latest = records[-1]["timestamp"] if records else None
    gaps = []
    for left,right in zip(records,records[1:]):
        missing = int((right["timestamp"]-left["timestamp"])/MINUTE)-1
        if missing:
            gaps.append(dict(start=sql_time(left["timestamp"]+MINUTE), end=sql_time(right["timestamp"]-MINUTE),
                             minutes=missing, classification="unresolved_no_trade_or_missing_observation"))
    saved = read_time(checkpoint["latest_observation_at"]) if checkpoint and checkpoint["latest_observation_at"] else None
    status = "matches" if latest is not None and saved == latest else "empty" if latest is None and saved is None else "missing" if saved is None else "ahead" if latest is None or saved > latest else "behind"
    state_path = Path(data_dir)/"ohlcv_collection/pairs"/(asset["pair_id"]+".json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    attempt = state.get("last_attempt", {})
    return dict(pair_id=asset["pair_id"], earliest_candle=sql_time(earliest) if earliest else None,
        latest_candle=sql_time(latest) if latest else None, stored_rows=len(records),
        expected_minutes=int((latest-earliest)/MINUTE)+1 if records else 0,
        missing_minutes=sum(gap["minutes"] for gap in gaps), gaps=gaps, stored_duplicate_records=0,
        explicit_zero_trade_candles=sum(row["trade_count"] == 0 and row["volume_base"] == 0 for row in records),
        trade_count_unavailable=sum(row["trade_count"] is None for row in records),
        historical_coverage_days=(latest-earliest+MINUTE).total_seconds()/86400 if records else 0,
        trailing_unobserved_minutes=max(0,int((minute_start(now)-MINUTE-latest)/MINUTE)) if latest else None,
        checkpoint=checkpoint, checkpoint_status=status,
        pending_recovery=(Path(data_dir)/"ohlcv_collection/pending"/(asset["pair_id"]+".json")).is_file(),
        invalid_observations_last_attempt=attempt.get("invalid_observations_count", 0),
        collection_errors_last_attempt=[attempt["code"]] if attempt.get("status") == "error" else [],
        last_attempt=attempt, totals=state.get("totals", {}))


class OhlcvCollector:
    def __init__(self, data_dir, *, transport=None, clock=lambda:datetime.now(timezone.utc),
                 sleeper=time.sleep, monotonic=time.monotonic):
        self.data_dir, self.clock = Path(data_dir), clock
        self.transport, self.sleeper, self.monotonic = transport, sleeper, monotonic
        self.source = "kraken_public" if transport is None else "injected_test"
        self.root = self.data_dir/"ohlcv_collection"
        self.store = MonthlyParquetStore(data_dir)
        self.last_request = None
        self.rate_limited = False
        self.request_count = 0

    def _binding(self):
        path = self.root/"source.json"
        expected = {"version":1,"dataset":DATASET,"source":self.source}
        if path.is_file():
            if json.loads(path.read_text(encoding="utf-8")) != expected:
                raise CollectionError("real_and_injected_storage_cannot_mix")
        else:
            if list(self.store.root.glob("*/prices_1m/*.parquet")):
                raise CollectionError("existing_prices_have_unverified_source")
            _atomic_json(path, expected)

    def _request(self, params):
        if self.rate_limited:
            raise CollectionError("deferred_after_rate_limit", http_requested=False)
        limiter = self.root/"last_request.json"
        if self.last_request is not None:
            delay = 1.1-(self.monotonic()-self.last_request)
        elif limiter.is_file():
            previous = read_time(json.loads(limiter.read_text(encoding="utf-8"))["started_at"])
            elapsed = (utc_timestamp(self.clock())-previous).total_seconds()
            if elapsed < 0:
                raise CollectionError("request_clock_regressed")
            delay = 1.1-elapsed
        else:
            delay = 0
        if delay > 0:
            self.sleeper(delay)
        _atomic_json(limiter,{"started_at":sql_time(self.clock())})
        self.last_request = self.monotonic()
        transport = self.transport
        if transport is None:
            from .market_data import public_get
            transport = public_get
        self.request_count += 1
        try:
            body = transport("OHLC", params)
        except Exception as error:
            self.rate_limited = getattr(error, "code", None) == 429
            raise CollectionError("rate_limited" if self.rate_limited else "network_request_failed", http_requested=True) from None
        if isinstance(body, dict) and body.get("error"):
            self.rate_limited = any("rate limit" in str(error).lower() or "throttl" in str(error).lower() for error in body["error"])
            raise CollectionError("rate_limited" if self.rate_limited else "exchange_api_error", http_requested=True)
        return body

    def _set_checkpoint(self, asset, records):
        saved = _checkpoint(self.data_dir, asset["pair_id"])
        latest = records[-1]["timestamp"] if records else None
        if saved and saved["latest_observation_at"] and (latest is None or read_time(saved["latest_observation_at"]) > latest):
            raise CollectionError("checkpoint_ahead_of_verified_storage")
        if latest is None:
            return saved
        now = utc_timestamp(self.clock())
        if latest > minute_start(now)-MINUTE or saved and read_time(saved["updated_at"]) > now:
            raise CollectionError("checkpoint_clock_regressed")
        with connect_metadata(self.data_dir) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT latest_observation_at FROM ingestion_state WHERE pair_id=? AND dataset=?", (asset["pair_id"], DATASET)).fetchone()
            if current and current[0] and read_time(current[0]) > latest:
                raise CollectionError("checkpoint_would_move_backwards")
            db.execute("INSERT INTO ingestion_state VALUES (?,?,?,?) ON CONFLICT(pair_id,dataset) DO UPDATE SET latest_observation_at=excluded.latest_observation_at,updated_at=excluded.updated_at",
                       (asset["pair_id"], DATASET, sql_time(latest), sql_time(now)))
        return _checkpoint(self.data_dir, asset["pair_id"])

    def _persist_intent(self, asset, intent):
        if (intent.get("source") != self.source or intent.get("pair_id") != asset["pair_id"]
                or intent.get("kraken_pair_id") != asset["kraken_pair_id"] or intent.get("interval") != 1):
            raise CollectionError("pending_identity_or_source_mismatch")
        records = table_from_records(DATASET, _decode_records(intent["records"])).to_pylist()
        if any(not row["is_complete"] or row["pair_id"] != asset["pair_id"] or row["timestamp"] > minute_start(self.clock())-MINUTE for row in records):
            raise CollectionError("pending_candles_not_finalized")
        old = _stored(self.store, asset["pair_id"], self.clock())
        previous = {row["timestamp"]:row for row in old}
        for row in records:
            if row["timestamp"] in previous and previous[row["timestamp"]] != row:
                raise CollectionError("conflicting_stored_candle", invalid_observations_count=1)
        self.store.write(DATASET, records)
        actual = _stored(self.store, asset["pair_id"], self.clock())
        by_time = {row["timestamp"]:row for row in actual}
        if any(by_time.get(row["timestamp"]) != row for row in records):
            raise CollectionError("persisted_observation_verification_failed")
        checkpoint = self._set_checkpoint(asset, actual)
        return checkpoint, len(actual)-len(old), sum(row["timestamp"] in previous for row in records)

    def _collect_one(self, asset):
        pair = asset["pair_id"]
        pending = self.root/"pending"/(pair+".json")
        recovered = False
        if pending.is_file():
            intent = json.loads(pending.read_text(encoding="utf-8"))
            self._persist_intent(asset, intent)
            pending.unlink()
            recovered = True
        stored = _stored(self.store, pair, self.clock())
        checkpoint = self._set_checkpoint(asset, stored)  # Reconcile verified Parquet before resuming.
        params = {"pair":asset["kraken_pair_id"], "interval":1, "assetVersion":1}
        state_file = self.root/"pairs"/(pair+".json")
        prior = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
        success = prior.get("last_success") or {}
        if checkpoint and checkpoint["latest_observation_at"]:
            since = int(read_time(checkpoint["latest_observation_at"]).timestamp())
            if success.get("checkpoint") == checkpoint["latest_observation_at"] and type(success.get("last_marker")) is int:
                since = min(since, success["last_marker"])
            params["since"] = since
        body = self._request(params)
        fetched = utc_timestamp(self.clock())
        parsed = parse_ohlc(body, asset, fetched)
        response_path = self.root/"responses"/pair/(uuid.uuid4().hex+".json")
        _atomic_json(response_path, {"source":self.source,"params":params,"fetched_at":sql_time(fetched),"body":body})
        if parsed["invalid_observations"]:
            raise CollectionError("invalid_ohlcv_batch", invalid_observations_count=len(parsed["invalid_observations"]),
                                  invalid_observations=parsed["invalid_observations"], http_requested=True)
        intent = dict(source=self.source,pair_id=pair,kraken_pair_id=asset["kraken_pair_id"],interval=1,
                      records=_json_records(parsed["records"]),last_marker=parsed["last_marker"],fetched_at=sql_time(fetched))
        new_rows = overlap = 0
        if parsed["records"]:
            _atomic_json(pending, intent)
            checkpoint,new_rows,overlap = self._persist_intent(asset,intent)
        outcome = dict(pair_id=pair,status="ok" if checkpoint else "no_data",http_requested=True,
            source=self.source,request_params=params,fetched_at=sql_time(fetched),response_rows=parsed["response_rows"],
            uncommitted_rows=parsed["uncommitted_rows"],last_marker=parsed["last_marker"],
            identical_response_duplicates=parsed["identical_response_duplicates"],overlap_duplicates=overlap,
            new_rows=new_rows,recovered_pending=recovered,invalid_observations_count=0,
            checkpoint=checkpoint["latest_observation_at"] if checkpoint else None)
        self._save_state(asset,outcome)
        pending.unlink(missing_ok=True)
        return outcome

    def _save_state(self, asset, outcome):
        path = self.root/"pairs"/(asset["pair_id"]+".json")
        old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        totals = old.get("totals", {"collection_errors":0,"invalid_observations":0})
        totals["collection_errors"] += int(outcome["status"] == "error")
        totals["invalid_observations"] += outcome.get("invalid_observations_count",0)
        last_success = old.get("last_success")
        if outcome["status"] == "ok":
            last_success = {key:outcome[key] for key in ("checkpoint","last_marker","fetched_at")}
        _atomic_json(path, dict(source=self.source,kraken_pair_id=asset["kraken_pair_id"],last_attempt=outcome,last_success=last_success,totals=totals))

    def collect(self, *, pair=None, all_aud=False):
        assets,audit_id,unresolved_count = collection_universe(self.data_dir)
        if (pair is None) == (not all_aud):
            raise CollectionError("select_exactly_one_pair_or_all_aud")
        selected = assets if all_aud else [asset for asset in assets if asset["pair_id"] == pair]
        if not selected:
            raise CollectionError("pair_not_online_confirmed_aud_spot")
        lock = ProcessLock(self.data_dir/"ohlcv-collector")
        try:
            self._binding()
            results = []
            run = dict(run_id=uuid.uuid4().hex,source=self.source,catalogue_run_id=audit_id,status="running",
                       unresolved_catalogue_records_unchanged=unresolved_count,selected_pairs=[a["pair_id"] for a in selected],
                       attempted_pairs=0,results=results,started_at=sql_time(self.clock()))
            run_path = self.root/"runs"/(run["run_id"]+".json")
            _atomic_json(run_path,run)
            for asset in selected:
                requests_before = self.request_count
                try:
                    outcome = self._collect_one(asset)
                except Exception as error:
                    outcome = dict(pair_id=asset["pair_id"],status="error",http_requested=self.request_count>requests_before,
                                   attempted_at=sql_time(self.clock()),code=error.code if isinstance(error,CollectionError) else
                                   "conflicting_candle" if isinstance(error,DuplicateKeyError) else "persistence_or_checkpoint_failure")
                    if isinstance(error,CollectionError):
                        outcome.update(error.details)
                    try:
                        self._save_state(asset,outcome)
                    except Exception:
                        outcome["audit_error"] = "pair_state_receipt_write_failed"
                results.append(outcome)
                run.update(attempted_pairs=len(results),http_requests=self.request_count)
                _atomic_json(run_path,run)
            run.update(status="finished",finished_at=sql_time(self.clock()))
            _atomic_json(run_path,run)
            return run
        finally:
            lock.close()


def coverage_report(data_dir, *, now=None):
    assets,audit_id,unresolved_count = collection_universe(data_dir)
    now = utc_timestamp(now or datetime.now(timezone.utc))
    eligible = {asset["pair_id"] for asset in assets}
    catalogue = catalogue_report(data_dir)
    with connect_metadata(data_dir,readonly=True) as db:
        checkpoints = {row[0] for row in db.execute("SELECT pair_id FROM ingestion_state WHERE dataset=?",(DATASET,))}
    for asset in catalogue["aud_spot_pairs"]:
        if asset["pair_id"] not in eligible and (asset["pair_id"] in checkpoints or
                list((Path(data_dir)/"market_data"/asset["pair_id"]/DATASET).glob("*.parquet"))):
            assets.append(asset)
    rows = []
    for asset in assets:
        try:
            rows.append({**_coverage(data_dir,asset,now),"eligible_for_collection":asset["pair_id"] in eligible})
        except Exception:
            rows.append(dict(pair_id=asset["pair_id"],checkpoint_status="storage_validation_failed",stored_rows=None,
                             missing_minutes=None,stored_duplicate_records=None,collection_errors_last_attempt=["storage_validation_failed"]))
    binding = Path(data_dir)/"ohlcv_collection/source.json"
    source = json.loads(binding.read_text(encoding="utf-8"))["source"] if binding.is_file() else "unverified_or_empty"
    return dict(source=source,catalogue_run_id=audit_id,unresolved_catalogue_records_unchanged=unresolved_count,pair_count=len(rows),eligible_pair_count=len(eligible),pairs=rows)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("collect","report"))
    selection=parser.add_mutually_exclusive_group()
    selection.add_argument("--pair")
    selection.add_argument("--all-aud",action="store_true")
    parser.add_argument("--root",default=".")
    parser.add_argument("--data-dir")
    args=parser.parse_args(argv)
    try:
        config=load_config(args.root,data_dir=args.data_dir)
        if args.command == "collect":
            result=OhlcvCollector(config.data_dir).collect(pair=args.pair,all_aud=args.all_aud)
            code=1 if any(row["status"] == "error" for row in result["results"]) else 2 if any(row["status"] == "no_data" for row in result["results"]) else 0
        else:
            if args.pair or args.all_aud:
                raise CollectionError("report_has_no_collection_selection")
            result=coverage_report(config.data_dir)
            code=int(any(row["checkpoint_status"] not in ("matches","empty") for row in result["pairs"]))
        print(json.dumps(result,indent=2))
        return code
    except Exception as error:
        print(json.dumps({"error":error.code if isinstance(error,CollectionError) else "collector_operation_failed_private_details_suppressed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
