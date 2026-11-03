"""Offline data-integrity inventory. No prices are filled or financially modelled."""
from datetime import timedelta
import json
from pathlib import Path

from . import ohlcv_collector as ohlc
from . import book_collector as books
from .parquet_store import MonthlyParquetStore
from .storage_schema import dataset_schema, validate_table, table_from_records, LOGICAL_KEYS, utc_timestamp

MINUTE=timedelta(minutes=1)


def window(start,end):
    start,end=utc_timestamp(start),utc_timestamp(end)
    if start>=end or any(t.second or t.microsecond for t in (start,end)):
        raise ValueError("Ordered exact UTC minute boundaries required; end is exclusive")
    if end-start>timedelta(days=3660):
        raise ValueError("Bounded research window required (at most 3660 days)")
    return start,end


def intervals(times):
    result=[]
    for stamp in sorted(set(times)):
        if result and stamp==result[-1][1]:
            result[-1]=(result[-1][0],stamp+MINUTE)
        else:
            result.append((stamp,stamp+MINUTE))
    return result


def availability(data_dir,pair):
    path=Path(data_dir)/"history/availability"/(pair+".json")
    return json.loads(path.read_text()) if path.exists() else None


def minute_coverage(times,start,end,now,*,invalid_times=(),source=None):
    start,end=window(start,end)
    cutoff=min(end,ohlc.minute_start(now))
    cutoff=max(start,cutoff)
    present={t for t in times if start<=t<cutoff}
    invalid={t for t in invalid_times if start<=t<cutoff}-present
    runs=intervals(present)
    missing=[];cursor=start
    for left,right in runs+[(cutoff,cutoff)]:
        if cursor<left:
            cuts={cursor,left}
            floor=ohlc.read_time(source["oldest_accessible"]) if source and source.get("floor_verified") and source.get("oldest_accessible") else None
            empty_until=ohlc.read_time(source["no_completed_history_as_of"]) if source and source.get("no_completed_history_as_of") else None
            if floor and cursor<floor<left:
                cuts.add(floor)
            if empty_until and cursor<empty_until<left:
                cuts.add(empty_until)
            for t in invalid:
                if cursor<=t<left:
                    cuts.update((t,t+MINUTE))
            points=sorted(cuts)
            for a,b in zip(points,points[1:]):
                classification=("confirmed_invalid_local_observation" if a in invalid else
                    "unavailable_from_kraken_rest_at_probe" if (floor and b<=floor) or (empty_until and b<=empty_until) else
                    "not_collected_source_availability_unresolved")
                item=dict(start=ohlc.sql_time(a),end_exclusive=ohlc.sql_time(b),minutes=int((b-a)/MINUTE),classification=classification)
                if missing and missing[-1]["end_exclusive"]==item["start"] and missing[-1]["classification"]==classification:
                    missing[-1]["end_exclusive"]=item["end_exclusive"];missing[-1]["minutes"]+=item["minutes"]
                else:
                    missing.append(item)
        cursor=right
    expected=int((cutoff-start)/MINUTE)
    longest=max((int((b-a)/MINUTE) for a,b in runs),default=0)
    return dict(start=ohlc.sql_time(start),end_exclusive=ohlc.sql_time(end),completed_end_exclusive=ohlc.sql_time(cutoff),
        expected_completed_minutes=expected,present_minutes=len(present),missing_minutes=expected-len(present),
        coverage_pct=100*len(present)/expected if expected else None,
        missing_intervals=missing,complete_intervals=[dict(start=ohlc.sql_time(a),end_exclusive=ohlc.sql_time(b),minutes=int((b-a)/MINUTE)) for a,b in runs],
        longest_continuous_minutes=longest,missing_beginning_minutes=int((runs[0][0]-start)/MINUTE) if runs else expected,
        missing_ending_minutes=int((cutoff-runs[-1][1])/MINUTE) if runs else expected,
        uncommitted_or_future_minutes=int((end-cutoff)/MINUTE),
        unavailable_at_probe_minutes=sum(r["minutes"] for r in missing if r["classification"]=="unavailable_from_kraken_rest_at_probe"))


def missing_timestamps(coverage):
    """Lazily enumerate exact absent UTC minutes; reports use compact intervals."""
    for item in coverage["missing_intervals"]:
        stamp=ohlc.read_time(item["start"])
        while stamp<ohlc.read_time(item["end_exclusive"]):
            yield stamp
            stamp+=MINUTE


def inventory(data_dir,pair,dataset,start,end,now):
    """Inspect raw batches so corrupt/duplicate partitions are reported, not hidden.

    Memory is bounded by one established monthly partition plus requested timestamps.
    Existing validated writers are never bypassed for mutation.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    store=MonthlyParquetStore(data_dir)
    counts=dict(records=0,unique_timestamps=0,duplicate_records=0,conflicting_keys=0,invalid_records=0,
                invalid_snapshots=0,uncommitted_records=0,out_of_order_stored_rows=0,calendar_days=0)
    first=last=None;requested=set();invalid_times=set();errors=[]
    zero_trade=0
    for month in store.months(pair,dataset):
        keys={};conflicts=set();bad=set();previous=None
        try:
            path=store.partition_path(pair,dataset,month)
            parquet=pq.ParquetFile(path,page_checksum_verification=True)
            if not parquet.schema_arrow.equals(dataset_schema(dataset),check_metadata=True):
                raise ValueError("schema_mismatch")
            for batch in parquet.iter_batches(batch_size=8192):
                rows=batch.to_pylist()
                counts["records"]+=len(rows)
                try:
                    validate_table(dataset,pa.Table.from_batches([batch]))
                    batch_valid=True
                except ValueError:
                    batch_valid=False
                for row in rows:
                    stamp=row.get("timestamp")
                    try:
                        stamp=utc_timestamp(stamp)
                        if previous is not None and stamp<previous:
                            counts["out_of_order_stored_rows"]+=1
                        previous=stamp
                        if not batch_valid:
                            table_from_records(dataset,[row])
                        if row["pair_id"]!=pair or stamp.strftime("%Y-%m")!=month:
                            raise ValueError("partition_identity_mismatch")
                        if dataset=="prices_1m" and not row["is_complete"]:
                            counts["uncommitted_records"]+=1
                            bad.add(stamp)
                            continue
                        if stamp>(ohlc.minute_start(now)-MINUTE if dataset=="prices_1m" else utc_timestamp(now)):
                            raise ValueError("future_completed_observation")
                        key=tuple(row[name] for name in LOGICAL_KEYS[dataset])
                        if key in keys:
                            counts["duplicate_records"]+=1
                            if keys[key]!=row:
                                counts["conflicting_keys"]+=1;conflicts.add(stamp)
                        else:
                            keys[key]=row
                    except Exception:
                        counts["invalid_records"]+=1
                        if stamp is not None and hasattr(stamp,"tzinfo") and stamp.tzinfo is not None:
                            bad.add(stamp)
            groups={}
            for row in keys.values():
                groups.setdefault(row["timestamp"],[]).append(row)
            usable=set(groups)-conflicts-bad
            if dataset=="orderbook":
                for stamp in list(usable):
                    try:
                        books.validate_snapshot(groups[stamp])
                    except ValueError:
                        counts["invalid_snapshots"]+=1;counts["invalid_records"]+=len(groups[stamp]);usable.remove(stamp);bad.add(stamp)
            counts["unique_timestamps"]+=len(usable)
            counts["calendar_days"]+=len({t.date() for t in usable})
            if usable:
                first=min(min(usable),first) if first else min(usable)
                last=max(max(usable),last) if last else max(usable)
            requested.update(t for t in usable if start<=t<end)
            invalid_times.update(t for t in bad|conflicts if start<=t<end)
            if dataset=="prices_1m":
                zero_trade+=sum(r["trade_count"]==0 and r["volume_base"]==0 and r["timestamp"] in usable for r in keys.values())
        except Exception:
            errors.append(dict(month=month,code="unreadable_or_incompatible_partition"))
    folder="ohlcv_collection" if dataset=="prices_1m" else "book_collection"
    binding=Path(data_dir)/folder/"source.json"
    source=json.loads(binding.read_text())["source"] if binding.exists() else "unverified_or_empty"
    counts.update(pair_id=pair,dataset=dataset,first=ohlc.sql_time(first) if first else None,last=ohlc.sql_time(last) if last else None,
        observed_minute_days=counts["unique_timestamps"]/1440 if dataset=="prices_1m" else None,
        span_days=(last-first+(MINUTE if dataset=="prices_1m" else timedelta(0))).total_seconds()/86400 if first else 0,
        source=source,partition_errors=errors,integrity_unknown=bool(errors),
        explicit_source_zero_trade_candles=zero_trade if dataset=="prices_1m" else None,
        complete_snapshots=counts["unique_timestamps"] if dataset=="orderbook" else None)
    if errors:
        counts["known_readable_records"]=counts["records"]
        for key in ("records","unique_timestamps","calendar_days","observed_minute_days","complete_snapshots"):
            counts[key]=None
    return counts,requested,invalid_times


def source_audit(data_dir,asset):
    paths=sorted((Path(data_dir)/"ohlcv_collection/responses"/asset["pair_id"]).glob("*.json"))
    unordered=malformed=placeholders=0
    for path in paths:
        try:
            receipt=json.loads(path.read_text())
            result=receipt["body"]["result"]
            keys=set(result)-{"last"}
            if len(keys)!=1 or not keys<={asset["kraken_pair_id"],asset["base_asset"]+"/"+asset["quote_asset"]}:
                raise ValueError()
            rows=result[next(iter(keys))]
            times=[ohlc._integer(row[0]) for row in rows]
            unordered+=int(times!=sorted(times))
            placeholders+=int(len(rows)==1 and times==[0])
        except Exception:
            malformed+=1
    return dict(response_receipts=len(paths),out_of_order_source_responses=unordered,malformed_source_receipts=malformed,unfinished_epoch_zero_placeholder_responses=placeholders,
                scope="receipt_structure_and_timestamp_order; not a substitute for record hashes")


def coverage_report(data_dir,*,start,end,now,pair=None,all_aud=False):
    start,end=window(start,end)
    assets,audit,unresolved=ohlc.collection_universe(data_dir)
    if (pair is None)==(not all_aud):
        raise ValueError("Select one verified AUD pair or all AUD")
    assets=assets if all_aud else [a for a in assets if a["pair_id"]==pair]
    if not assets:
        raise ValueError("Pair not in verified AUD universe")
    sampled=books.report(data_dir,now=now)
    sample_map={(r["pair_id"],r["dataset"]):r for r in sampled["pairs"]}
    with ohlc.connect_metadata(data_dir,readonly=True) as db:
        operational={r[0]:dict(zip(("readiness","operational_status","updated_at","reason"),r[1:])) for r in db.execute("SELECT pair_id,readiness,operational_status,updated_at,reason FROM collection_status")}
    result=[]
    for asset in assets:
        pair_id=asset["pair_id"]
        datasets={}
        for dataset in ("prices_1m","quotes_1m","orderbook"):
            stats,times,invalid=inventory(data_dir,pair_id,dataset,start,end,now)
            if dataset=="prices_1m":
                stats["requested_window"]=minute_coverage(times,start,end,now,invalid_times=invalid,source=availability(data_dir,pair_id))
                if stats["integrity_unknown"]:
                    cov=stats["requested_window"]
                    cov["unverified_absent_or_unreadable_minutes"]=cov["missing_minutes"]
                    cov["missing_minutes"]=None;cov["coverage_pct"]=None
                    for gap in cov["missing_intervals"]:
                        gap["classification"]="unverified_partition_contents"
                ninety_end=min(end,ohlc.minute_start(now))
                ninety_start=ninety_end-timedelta(days=90)
                if start<=ninety_start:
                    ninety=minute_coverage(times,ninety_start,ninety_end,now)
                else:
                    _,ninety_times,_=inventory(data_dir,pair_id,dataset,ninety_start,ninety_end,now)
                    ninety=minute_coverage(ninety_times,ninety_start,ninety_end,now)
                stats["ninety_days_available"]=ninety["missing_minutes"]==0 and not stats["integrity_unknown"] and stats["invalid_records"]==0 and stats["duplicate_records"]==0 and stats["out_of_order_stored_rows"]==0
                stats["checkpoint"]=ohlc._checkpoint(data_dir,pair_id)
                latest=stats["checkpoint"]["latest_observation_at"] if stats["checkpoint"] else None
                stats["checkpoint_status"]="matches" if latest==stats["last"] and latest else "empty" if not latest and not stats["last"] else "mismatch"
                stats["source_audit"]=source_audit(data_dir,asset)
            else:
                previous=sample_map[(pair_id,dataset)]
                stats["requested_window"]=dict(start=ohlc.sql_time(start),end_exclusive=ohlc.sql_time(end),unique_observations=len(times),expected_observation_count=None,missing_observations=None)
                stats["sampling"]=dict(expected_policy="bounded recorded sessions only; no promised offline sampling",expected_observation_count=None,missing_observations=None,
                    expected_count_limitation="legacy receipts do not define exact scheduled slots; do not impose minute bars on sampled observations",
                    unresolved_sampling_intervals=sum(g["unresolved_sampling_intervals"] for g in previous.get("session_sampling_gaps",[])),
                    session_gaps=previous.get("session_sampling_gaps",[]),mean_observed_interval_seconds=previous.get("mean_interval_seconds"),checkpoint_status=previous["checkpoint_status"])
                stats["sampling"]["gap_scope"]="existing collector's recorded sessions, not the entire OHLCV research window"
            datasets[dataset]=stats
        price=datasets["prices_1m"];cov=price["requested_window"]
        ready=(cov["expected_completed_minutes"]>0 and not cov["uncommitted_or_future_minutes"] and cov["missing_minutes"]==0 and not price["invalid_records"] and not price["duplicate_records"] and not price["out_of_order_stored_rows"] and price["checkpoint_status"]=="matches")
        readiness="Unknown" if price["integrity_unknown"] or price["source"]=="unverified_or_empty" else "Sufficient" if ready else "Insufficient"
        result.append(dict(pair_id=pair_id,datasets=datasets,data_readiness=readiness,collection_status=operational.get(pair_id,{"operational_status":"unknown"}),readiness_scope="OHLCV integrity for explicit requested window only; no research eligibility decision",source_availability=availability(data_dir,pair_id)))
    return dict(catalogue_run_id=audit,unresolved_records_unchanged=unresolved,requested_start=ohlc.sql_time(start),requested_end_exclusive=ohlc.sql_time(end),pairs=result)
