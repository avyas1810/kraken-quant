"""Synthetic partition persistence/failure drills; no network or account data."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq

from kraken_quant import parquet_store as store_module
from kraken_quant.parquet_store import DuplicateKeyError, MonthlyParquetStore, observation_month
from kraken_quant.storage_schema import dataset_schema, table_from_records, validate_table

STAMP = datetime(2026, 11, 1, tzinfo=timezone.utc)


def record(dataset="prices_1m", **changes):
    result = dict(pair_id="BTC-AUD", timestamp=STAMP)
    if dataset == "prices_1m":
        result.update(open="100.123456789012345678", high="101", low="99", close="100", volume_base="0.000000000000000001", trade_count=None, is_complete=True)
    elif dataset == "quotes_1m":
        result.update(timestamp=STAMP + timedelta(microseconds=123456), bid="100", ask="101", bid_size=None, ask_size="0.5")
    elif dataset == "orderbook":
        result.update(timestamp=STAMP + timedelta(microseconds=123456), side="bid", level=1, price="100", quantity="1.5")
    else:
        result.update(side="buy", order_size_aud="100", fee_type="taker", fee_pct="0.4", half_spread_pct="0.01", slippage_pct="0.02", total_cost_pct="0.43", model_version="synthetic-v1")
    result.update(changes)
    return result


@pytest.mark.parametrize("dataset", ["prices_1m", "quotes_1m", "orderbook", "costs_1m"])
def test_all_schemas_round_trip_and_restart(tmp_path, dataset):
    store = MonthlyParquetStore(tmp_path)
    table = table_from_records(dataset, [record(dataset)])
    paths = store.write(dataset, table)
    assert paths == (tmp_path / "market_data" / "BTC-AUD" / dataset / "2026-11.parquet",)
    restored = MonthlyParquetStore(tmp_path).read("BTC-AUD", dataset, "2026-11")
    assert restored.equals(table, check_metadata=True)
    before, modified = paths[0].read_bytes(), paths[0].stat().st_mtime_ns
    MonthlyParquetStore(tmp_path).write(dataset, table)
    assert paths[0].read_bytes() == before and paths[0].stat().st_mtime_ns == modified


def test_decimal_and_utc_precision_preserved(tmp_path):
    exact = Decimal("12345678901234567890.123456789012345678")
    row = record(open=exact, high=exact, low=exact, close=exact)
    store = MonthlyParquetStore(tmp_path)
    store.write("prices_1m", [row])
    result = store.read_all("BTC-AUD", "prices_1m").to_pylist()[0]
    assert result["open"] == exact and result["volume_base"] == Decimal("0.000000000000000001")
    assert result["timestamp"] == STAMP and result["timestamp"].utcoffset() == timedelta(0)
    assert result["trade_count"] is None


def test_nullable_costs_and_sizes(tmp_path):
    store = MonthlyParquetStore(tmp_path)
    store.write("costs_1m", [record("costs_1m", fee_pct=None, total_cost_pct=None)])
    assert store.read_all("BTC-AUD", "costs_1m").to_pylist()[0]["fee_pct"] is None
    store.write("quotes_1m", [record("quotes_1m", bid_size=None, ask_size=None)])
    assert store.read_all("BTC-AUD", "quotes_1m").to_pylist()[0]["ask_size"] is None


@pytest.mark.parametrize("pair", ["../BTC-AUD", "BTC/AUD", "btc-AUD", "BTC-AUD/other", "BTC-AUD..", "C:\\BTC-AUD", "BTC-AUD-USD", "", "A"*21+"-AUD"])
def test_invalid_pair_paths(tmp_path, pair):
    with pytest.raises(ValueError): MonthlyParquetStore(tmp_path).partition_path(pair, "prices_1m", "2026-11")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("dataset", ["../prices_1m", "prices", "costs_1m/../../escape", ""])
def test_invalid_dataset(tmp_path, dataset):
    with pytest.raises(ValueError): MonthlyParquetStore(tmp_path).write(dataset, [])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "0000-01", "../2026-11", "2026-11.parquet"])
def test_invalid_month(tmp_path, month):
    with pytest.raises(ValueError): MonthlyParquetStore(tmp_path).read("BTC-AUD", "prices_1m", month)


def test_month_boundaries_offsets_and_multiple_partitions(tmp_path):
    store = MonthlyParquetStore(tmp_path)
    offset = datetime(2026, 12, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    assert observation_month(offset) == "2026-11"
    store.write("prices_1m", [record(timestamp=offset), record(timestamp=datetime(2026, 12, 1, tzinfo=timezone.utc)), record(pair_id="ETH-AUD")])
    assert store.months("BTC-AUD", "prices_1m") == ("2026-11", "2026-12")
    assert store.read_all("BTC-AUD", "prices_1m").num_rows == 2
    assert store.read_all("ETH-AUD", "prices_1m").num_rows == 1
    assert store.read("BTC-AUD", "prices_1m", "2026-11").to_pylist()[0]["timestamp"].hour == 23


def test_missing_and_empty_are_typed_and_do_not_create_directories(tmp_path):
    store = MonthlyParquetStore(tmp_path)
    table = store.read("BTC-AUD", "prices_1m", "2026-11")
    assert table.num_rows == 0 and table.schema.equals(dataset_schema("prices_1m"), check_metadata=True)
    assert store.write("prices_1m", []) == ()
    assert store.read_all("BTC-AUD", "prices_1m").num_rows == 0
    assert list(tmp_path.iterdir()) == []


def test_duplicate_logical_keys_conflict_and_idempotency(tmp_path):
    store = MonthlyParquetStore(tmp_path)
    row = record()
    paths = store.write("prices_1m", [row, row])
    assert store.read_all("BTC-AUD", "prices_1m").num_rows == 1
    before = paths[0].read_bytes()
    with pytest.raises(DuplicateKeyError): store.write("prices_1m", [record(close="100.5")])
    assert paths[0].read_bytes() == before
    with pytest.raises(DuplicateKeyError): store.write("prices_1m", [row, record(close="100.5")])


def test_distinct_book_and_cost_logical_keys(tmp_path):
    store = MonthlyParquetStore(tmp_path)
    store.write("orderbook", [record("orderbook"), record("orderbook", side="ask"), record("orderbook", level=2)])
    assert store.read_all("BTC-AUD", "orderbook").num_rows == 3
    store.write("costs_1m", [record("costs_1m"), record("costs_1m", order_size_aud="200"), record("costs_1m", model_version="synthetic-v2")])
    assert store.read_all("BTC-AUD", "costs_1m").num_rows == 3


@pytest.mark.parametrize("changes", [dict(open=1.5), dict(open="NaN"), dict(open="0"), dict(open="1e-19"),
    dict(timestamp=STAMP.replace(tzinfo=None)), dict(timestamp=STAMP+timedelta(seconds=1)),
    dict(high="98"), dict(volume_base="-1"), dict(trade_count=-1), dict(trade_count=True),
    dict(is_complete=1), dict(close=None), dict(unexpected="value")])
def test_invalid_price_records_rejected(tmp_path, changes):
    with pytest.raises((ValueError, pa.ArrowException)):
        MonthlyParquetStore(tmp_path).write("prices_1m", [record(**changes)])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("dataset,changes", [
    ("quotes_1m", dict(ask="99")), ("quotes_1m", dict(bid_size="-1")),
    ("orderbook", dict(side="buy")), ("orderbook", dict(level=0)),
    ("costs_1m", dict(side="bid")), ("costs_1m", dict(total_cost_pct="0.42")),
    ("costs_1m", dict(fee_pct=None)), ("costs_1m", dict(model_version=""))])
def test_invalid_other_records(dataset, changes):
    with pytest.raises(ValueError): table_from_records(dataset, [record(dataset, **changes)])


def test_unknown_version_types_nullability_and_timezone_rejected(tmp_path):
    table = table_from_records("prices_1m", [record()])
    metadata = dict(table.schema.metadata)
    metadata[b"schema_version"] = b"999"
    with pytest.raises(ValueError, match="schema mismatch"):
        MonthlyParquetStore(tmp_path).write("prices_1m", table.replace_schema_metadata(metadata))
    for schema in (table.schema.remove_metadata(), table.schema.set(0, pa.field("pair_id", pa.string(), nullable=True)),
                   table.schema.set(1, pa.field("timestamp", pa.timestamp("us"), nullable=False))):
        raw = pa.Table.from_pylist([], schema=schema)
        with pytest.raises(ValueError, match="schema mismatch"): validate_table("prices_1m", raw)


def test_physical_quote_and_book_times_are_not_forced_to_minutes():
    for dataset in ("quotes_1m", "orderbook"):
        table = table_from_records(dataset, [record(dataset)])
        assert table.to_pylist()[0]["timestamp"].microsecond == 123456


def test_symlink_path_is_rejected(tmp_path):
    root = tmp_path / "market_data"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    try:
        (root / "BTC-AUD").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation requires Windows privilege")
    with pytest.raises(ValueError): MonthlyParquetStore(tmp_path).write("prices_1m", [record()])
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("failure", ["write", "replace", "interrupt"])
def test_interrupted_write_preserves_existing_partition(tmp_path, monkeypatch, failure):
    store = MonthlyParquetStore(tmp_path)
    path, = store.write("prices_1m", [record()])
    before = path.read_bytes()
    if failure == "write":
        def fail(table, handle, **kwargs):
            handle.write(b"incomplete temporary file")
            raise OSError("synthetic disk failure")
        monkeypatch.setattr(store_module.pq, "write_table", fail)
    else:
        def fail(*args):
            if failure == "interrupt": raise KeyboardInterrupt("synthetic interruption")
            raise OSError("synthetic replace failure")
        monkeypatch.setattr(store_module.os, "replace", fail)
    with pytest.raises((OSError, KeyboardInterrupt)):
        store.write("prices_1m", [record(timestamp=STAMP+timedelta(minutes=1))])
    assert path.read_bytes() == before
    assert MonthlyParquetStore(tmp_path).read_all("BTC-AUD", "prices_1m").num_rows == 1
    assert list(path.parent.glob("*.tmp")) == []


def test_orphan_temporary_file_is_ignored_after_restart(tmp_path):
    store = MonthlyParquetStore(tmp_path)
    path, = store.write("prices_1m", [record()])
    (path.parent / ".2026-11.parquet.orphan.tmp").write_bytes(b"hard-crash orphan")
    restored = MonthlyParquetStore(tmp_path)
    assert restored.read_all("BTC-AUD", "prices_1m").num_rows == 1
    restored.write("prices_1m", [record()])
    assert restored.months("BTC-AUD", "prices_1m") == ("2026-11",)


def test_multi_month_interruption_retry_is_safe(tmp_path, monkeypatch):
    store = MonthlyParquetStore(tmp_path)
    rows = [record(), record(timestamp=datetime(2026,12,1,tzinfo=timezone.utc))]
    original = store._replace
    def fail_second(path, table, pair_id, dataset, month):
        if month == "2026-12": raise OSError("synthetic second-partition failure")
        return original(path, table, pair_id, dataset, month)
    monkeypatch.setattr(store, "_replace", fail_second)
    with pytest.raises(OSError): store.write("prices_1m", rows)
    assert store.read_all("BTC-AUD", "prices_1m").num_rows == 1
    MonthlyParquetStore(tmp_path).write("prices_1m", rows)
    assert store.read_all("BTC-AUD", "prices_1m").num_rows == 2


def test_existing_schema_or_partition_identity_mismatch_is_not_overwritten(tmp_path):
    store = MonthlyParquetStore(tmp_path)
    path, = store.write("prices_1m", [record()])
    wrong = table_from_records("prices_1m", [record(pair_id="ETH-AUD")])
    pq.write_table(wrong, path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="mismatched pair"): store.write("prices_1m", [record()])
    assert path.read_bytes() == before
    pq.write_table(wrong.replace_schema_metadata({b"schema_version":b"999"}), path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="schema mismatch"): store.read_all("BTC-AUD", "prices_1m")
    assert path.read_bytes() == before


def test_writer_lock_prevents_lost_updates(tmp_path):
    store = MonthlyParquetStore(tmp_path)
    path, = store.write("prices_1m", [record()])
    before = path.read_bytes()
    lock = store_module.ProcessLock(path)
    try:
        with pytest.raises(PermissionError): store.write("prices_1m", [record(timestamp=STAMP+timedelta(minutes=1))])
    finally:
        lock.close()
    assert path.read_bytes() == before
