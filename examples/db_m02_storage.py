"""Reproducible SYNTHETIC-only storage demonstration; all files are temporary."""
from datetime import datetime, timedelta, timezone
import json
from tempfile import TemporaryDirectory

from kraken_quant.metadata_store import connect_metadata, initialize_metadata
from kraken_quant.parquet_store import MonthlyParquetStore
from kraken_quant.storage_schema import DATASETS, dataset_schema, table_from_records


STAMP = datetime(2026, 11, 1, tzinfo=timezone.utc)
SQL_STAMP = "2026-11-01T00:00:00.000000Z"


def synthetic_records():
    common = dict(pair_id="BTC-AUD", timestamp=STAMP)
    return {
        "prices_1m": [{**common, "open": "100000.123456789012345678", "high": "100001", "low": "99999",
                       "close": "100000", "volume_base": "0.25", "trade_count": 3, "is_complete": True}],
        "quotes_1m": [{**common, "timestamp": STAMP + timedelta(microseconds=123456), "bid": "99999",
                       "ask": "100001", "bid_size": None, "ask_size": "0.5"}],
        "orderbook": [{**common, "timestamp": STAMP + timedelta(microseconds=123456), "side": "bid",
                       "level": 1, "price": "99999", "quantity": "0.25"}],
        "costs_1m": [{**common, "side": "buy", "order_size_aud": "1000", "fee_type": "taker", "fee_pct": "0.4",
                      "half_spread_pct": "0.001", "slippage_pct": "0.02", "total_cost_pct": "0.421", "model_version": "SYNTHETIC-v1"}],
    }


def run_example():
    with TemporaryDirectory(prefix="kraken-db-m02-synthetic-") as scratch:
        initialize_metadata(scratch)
        with connect_metadata(scratch) as db:
            db.execute("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                "BTC-AUD", "XXBTZAUD", "BTC", "AUD", "online", SQL_STAMP, SQL_STAMP, "0.0001", "1", 8, 2))
            db.execute("INSERT INTO eligibility VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "BTC-AUD", SQL_STAMP, None, None, None, None, None, "unknown", '["synthetic_example_only"]',
                "unknown", "unavailable", "unknown", "unavailable", "unknown"))
            db.execute("INSERT INTO collection_status VALUES (?,?,?,?,?)", (
                "BTC-AUD", "not_ready", "idle", SQL_STAMP, "Synthetic example only"))
            db.execute("INSERT INTO data_quality_issues VALUES (?,?,?,?,?,?,?,?,?,?)", (
                "SYNTHETIC-example", "BTC-AUD", "prices_1m", SQL_STAMP, None, None,
                "synthetic_example", "Illustration, not real exchange data", SQL_STAMP, "ignored"))
        store = MonthlyParquetStore(scratch)
        records = synthetic_records()
        verified = {}
        for dataset in DATASETS:
            expected = table_from_records(dataset, records[dataset])
            store.write(dataset, expected)
            store.write(dataset, expected)  # Idempotent retry.
            actual = MonthlyParquetStore(scratch).read_all("BTC-AUD", dataset)
            assert actual.equals(expected, check_metadata=True)
            verified[dataset] = actual.to_pylist()
        # A future ingestion coordinator must commit checkpoints AFTER persistence.
        with connect_metadata(scratch) as db:
            db.execute("INSERT INTO ingestion_state VALUES (?,?,?,?)", ("BTC-AUD", "prices_1m", SQL_STAMP, SQL_STAMP))
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            checkpoint = db.execute("SELECT dataset,latest_observation_at FROM ingestion_state").fetchone()
        return dict(synthetic=True, temporary_files_removed_on_return=True, metadata_tables=tables,
                    verified_records=verified, checkpoint=checkpoint,
                    prices_schema=str(dataset_schema("prices_1m")))


def main():
    print(json.dumps(run_example(), indent=2, default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value)))


if __name__ == "__main__":
    main()
