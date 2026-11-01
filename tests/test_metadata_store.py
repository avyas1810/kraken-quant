"""Isolated metadata migration and integrity tests; never opens research stores."""
from contextlib import closing
import hashlib
import sqlite3

import pytest

from kraken_quant import metadata_store as meta

STAMP = "2026-11-01T00:00:00.000000Z"
LATER = "2026-11-02T00:00:00.000000Z"


def asset(db, **changes):
    record = dict(pair_id="BTC-AUD", kraken_pair_id="XXBTZAUD", base_asset="BTC", quote_asset="AUD",
                  status="online", first_seen=STAMP, last_seen=STAMP, min_order_qty="0.0001",
                  min_order_value="1.25", qty_precision=8, price_precision=2)
    record.update(changes)
    names = ",".join(record)
    db.execute(f"INSERT INTO assets ({names}) VALUES ({','.join('?' for _ in record)})", tuple(record.values()))


def screen(db, **changes):
    record = dict(pair_id="BTC-AUD", screened_at=STAMP, volume_24h_aud=None, avg_spread_pct=None,
                  depth_aud=None, historical_days=None, completeness_pct=None, decision="unknown",
                  rejection_reasons='["synthetic_unknown"]', volume_24h_aud_status="unknown",
                  avg_spread_pct_status="unavailable", depth_aud_status="unknown",
                  historical_days_status="unavailable", completeness_pct_status="unknown")
    record.update(changes)
    db.execute(f"INSERT INTO eligibility ({','.join(record)}) VALUES ({','.join('?' for _ in record)})", tuple(record.values()))


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    meta.initialize_metadata(path)
    return path


def test_fresh_initialization_and_identity(tmp_path):
    path = meta.initialize_metadata(tmp_path / "empty")
    with meta.connect_metadata(path.parent, readonly=True) as db:
        assert db.execute("PRAGMA application_id").fetchone()[0] == meta.APPLICATION_ID
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert set(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")) == set(meta.TABLE_DDL)
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_repeat_init_preserves_data_and_file(data_dir):
    with meta.connect_metadata(data_dir) as db:
        asset(db)
    path = data_dir / "metadata.sqlite"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    meta.initialize_metadata(data_dir)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with meta.connect_metadata(data_dir) as db:
        assert db.execute("SELECT kraken_pair_id,min_order_qty FROM assets").fetchone() == ("XXBTZAUD", "0.0001")


@pytest.mark.parametrize("changes", [dict(pair_id="../AUD"), dict(base_asset="btc"),
    dict(last_seen="2026-10-31T00:00:00.000000Z"), dict(qty_precision=-1), dict(price_precision=19),
    dict(min_order_qty="NaN"), dict(min_order_qty="1e-8"), dict(min_order_qty="-1"),
    dict(min_order_qty="01.2"), dict(min_order_qty="1.0000000000000000001"),
    dict(min_order_qty="100000000000000000000"), dict(status="")])
def test_asset_constraints(data_dir, changes):
    with pytest.raises(sqlite3.IntegrityError):
        with meta.connect_metadata(data_dir) as db:
            asset(db, **changes)


@pytest.mark.parametrize("stamp", ["2026-11-01", "2026-02-30T00:00:00.000000Z", "2026-11-01T24:00:00.000000Z",
    "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00.00000Z", "2026-11-01T00:00:60.000000Z"])
def test_sqlite_utc_timestamp_validation(data_dir, stamp):
    with pytest.raises(sqlite3.IntegrityError):
        with meta.connect_metadata(data_dir) as db:
            asset(db, first_seen=stamp, last_seen=stamp)


def test_primary_unique_foreign_keys_and_nullables(data_dir):
    with meta.connect_metadata(data_dir) as db:
        asset(db, min_order_qty=None, min_order_value=None, qty_precision=None, price_precision=None)
    with pytest.raises(sqlite3.IntegrityError):
        with meta.connect_metadata(data_dir) as db: asset(db)
    with pytest.raises(sqlite3.IntegrityError):
        with meta.connect_metadata(data_dir) as db: asset(db, pair_id="ETH-AUD", base_asset="ETH")
    with pytest.raises(sqlite3.IntegrityError):
        with meta.connect_metadata(data_dir) as db: screen(db, pair_id="ETH-AUD")


def test_historical_eligibility_and_collection_are_separate(data_dir):
    with meta.connect_metadata(data_dir) as db:
        asset(db)
        screen(db)
        screen(db, screened_at=LATER, decision="ineligible", rejection_reasons='["caller_supplied_reason"]')
        db.execute("INSERT INTO collection_status VALUES (?,?,?,?,?)", ("BTC-AUD", "ready", "idle", STAMP, None))
    with meta.connect_metadata(data_dir) as db:
        assert db.execute("SELECT decision FROM eligibility ORDER BY screened_at").fetchall() == [("unknown",), ("ineligible",)]
        assert db.execute("SELECT readiness FROM collection_status").fetchone()[0] == "ready"
    with pytest.raises(sqlite3.IntegrityError):
        with meta.connect_metadata(data_dir) as db: screen(db)


@pytest.mark.parametrize("changes", [dict(volume_24h_aud_status="known"), dict(depth_aud="1"),
    dict(rejection_reasons='{}'), dict(decision="maybe"), dict(historical_days=-1, historical_days_status="known"),
    dict(completeness_pct="100.000000000000000001", completeness_pct_status="known")])
def test_eligibility_integrity(data_dir, changes):
    with meta.connect_metadata(data_dir) as db: asset(db)
    with pytest.raises(sqlite3.IntegrityError):
        with meta.connect_metadata(data_dir) as db: screen(db, **changes)


def test_eligibility_exact_decimals_and_full_completeness(data_dir):
    with meta.connect_metadata(data_dir) as db:
        asset(db)
        screen(db, volume_24h_aud="12345678901234567890.123456789012345678", volume_24h_aud_status="known",
               completeness_pct="100.000000000000000000", completeness_pct_status="known")
        assert db.execute("SELECT volume_24h_aud,typeof(volume_24h_aud) FROM eligibility").fetchone() == ("12345678901234567890.123456789012345678", "text")


def test_ingestion_and_quality_constraints(data_dir):
    with meta.connect_metadata(data_dir) as db:
        asset(db)
        db.execute("INSERT INTO ingestion_state VALUES (?,?,?,?)", ("BTC-AUD", "prices_1m", None, STAMP))
        db.execute("INSERT INTO data_quality_issues VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "synthetic-gap", "BTC-AUD", "prices_1m", None, STAMP, LATER, "gap", "Synthetic gap", LATER, "open"))
    invalid_statements = [
        ("INSERT INTO ingestion_state VALUES (?,?,?,?)", ("BTC-AUD", "prices_1m", STAMP, LATER)),
        ("INSERT INTO ingestion_state VALUES (?,?,?,?)", ("BTC-AUD", "bad", None, STAMP)),
        ("UPDATE ingestion_state SET latest_observation_at=?,updated_at=?", (LATER, STAMP)),
        ("UPDATE data_quality_issues SET observation_at=?", (STAMP,)),
        ("UPDATE data_quality_issues SET resolution_status=?", ("maybe",)),
        ("DELETE FROM assets WHERE pair_id=?", ("BTC-AUD",)),
    ]
    for sql, params in invalid_statements:
        with pytest.raises(sqlite3.IntegrityError):
            with meta.connect_metadata(data_dir) as db: db.execute(sql, params)


def test_unsupported_version_and_schema_mismatch_fail_closed(data_dir):
    path = data_dir / "metadata.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version=999")
    before = path.read_bytes()
    with pytest.raises(meta.MetadataError, match="Unsupported.*999"):
        meta.initialize_metadata(data_dir)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version=1")
        db.execute("DROP INDEX assets_status_idx")
    before = path.read_bytes()
    with pytest.raises(meta.MetadataError, match="schema mismatch"):
        meta.initialize_metadata(data_dir)
    assert path.read_bytes() == before


def test_failed_migration_rolls_back_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(meta, "MIGRATIONS", {1: (*meta.MIGRATIONS[1], "CREATE TABLE broken (")})
    with pytest.raises(sqlite3.OperationalError): meta.initialize_metadata(tmp_path)
    with closing(sqlite3.connect(tmp_path / "metadata.sqlite")) as db:
        assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("PRAGMA application_id").fetchone()[0] == 0


def test_unrelated_research_database_is_never_modified(tmp_path):
    path = tmp_path / "metadata.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE shadow_state (body TEXT)")
        db.execute("INSERT INTO shadow_state VALUES ('protected')")
        db.commit()
    before = path.read_bytes()
    with pytest.raises(meta.MetadataError, match="unrelated"):
        meta.initialize_metadata(tmp_path)
    assert path.read_bytes() == before
    other = tmp_path / "research.sqlite"
    other.write_bytes(b"existing research database")
    store = tmp_path / "separate"
    meta.initialize_metadata(store)
    assert other.read_bytes() == b"existing research database" and path.read_bytes() == before


def test_cli_init_show_and_unsupported_error(tmp_path, capsys):
    args = ["--root", str(tmp_path)]
    assert meta.main(["init", *args]) == 0
    assert meta.main(["show", *args]) == 0
    output = capsys.readouterr().out
    assert "CREATE TABLE assets" in output and "schema v1" in output
    with closing(sqlite3.connect(tmp_path / "data" / "metadata.sqlite")) as db: db.execute("PRAGMA user_version=42")
    assert meta.main(["init", *args]) == 1
    assert "Unsupported metadata schema version 42" in capsys.readouterr().out
