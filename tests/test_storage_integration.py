"""Synthetic SQLite/Parquet integration; no market collection or live routing."""
import subprocess
from pathlib import Path

import pytest
pytest.importorskip("pyarrow")

from examples.db_m02_storage import SQL_STAMP, run_example, synthetic_records
from kraken_quant.metadata_store import connect_metadata, initialize_metadata
from kraken_quant.parquet_store import MonthlyParquetStore


def test_synthetic_storage_example_and_checkpoint():
    result = run_example()
    assert result["synthetic"] and result["temporary_files_removed_on_return"]
    assert len(result["metadata_tables"]) == 5
    assert all(len(rows) == 1 for rows in result["verified_records"].values())
    assert result["checkpoint"] == ("prices_1m", SQL_STAMP)
    assert "decimal128(38, 18)" in result["prices_schema"]


def test_partition_writes_do_not_mutate_metadata_checkpoints(tmp_path, monkeypatch):
    initialize_metadata(tmp_path)
    with connect_metadata(tmp_path) as db:
        db.execute("INSERT INTO assets (pair_id,kraken_pair_id,base_asset,quote_asset,status,first_seen,last_seen) VALUES (?,?,?,?,?,?,?)",
                   ("BTC-AUD", "XXBTZAUD", "BTC", "AUD", "online", SQL_STAMP, SQL_STAMP))
        db.execute("INSERT INTO ingestion_state VALUES (?,?,?,?)", ("BTC-AUD", "prices_1m", None, SQL_STAMP))
    before = (tmp_path / "metadata.sqlite").read_bytes()
    store = MonthlyParquetStore(tmp_path)
    original = store._replace
    def fail(*args): raise OSError("synthetic persistence failure")
    monkeypatch.setattr(store, "_replace", fail)
    with pytest.raises(OSError): store.write("prices_1m", synthetic_records()["prices_1m"])
    assert (tmp_path / "metadata.sqlite").read_bytes() == before
    monkeypatch.setattr(store, "_replace", original)
    store.write("prices_1m", synthetic_records()["prices_1m"])
    assert (tmp_path / "metadata.sqlite").read_bytes() == before
    with connect_metadata(tmp_path) as db:
        assert db.execute("SELECT latest_observation_at FROM ingestion_state").fetchone() == (None,)


def test_generated_storage_is_not_tracked_and_is_ignored():
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(["git", "ls-files", "--", "data/", "*.sqlite", "*.sqlite3", "*.parquet"], cwd=root, text=True)
    assert tracked.strip() == ""
    ignored = subprocess.run(["git", "check-ignore", "data/metadata.sqlite", "data/market_data/BTC-AUD/prices_1m/2026-11.parquet"], cwd=root, capture_output=True, text=True)
    assert ignored.returncode == 0 and len(ignored.stdout.splitlines()) == 2
