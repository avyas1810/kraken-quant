"""Database-environment tests use scratch storage and mocked public/private reads."""
import json
import os
from pathlib import Path
import sys

import pytest

from kraken_quant import db_environment as db


@pytest.fixture
def config(tmp_path):
    return db.load_config(tmp_path)


def pair_response():
    return {"error": [], "result": {
        "XXBTZAUD": {"base": "XXBT", "quote": "ZAUD", "aclass_quote": "currency"},
        "ETHAUD": {"base": "XETH", "quote": "AUD"},
        "XXBTZUSD": {"base": "XXBT", "quote": "ZUSD"}}}


def test_configuration_defaults_and_dotenv_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("KRAKEN_DATA_DIR", "ambient-must-not-be-used")
    assert db.load_config(tmp_path).data_dir == tmp_path / "data"
    env = tmp_path / ".env"
    env.write_text("KRAKEN_DATA_DIR=local-data\nKRAKEN_READONLY_API_KEY=private-sentinel\n")
    assert db.load_config(tmp_path).data_dir == tmp_path / "local-data"
    assert db.load_config(tmp_path, data_dir="explicit").data_dir == tmp_path / "explicit"
    assert os.environ["KRAKEN_DATA_DIR"] == "ambient-must-not-be-used"
    assert "private-sentinel" in env.read_text()


def test_configuration_explicit_env_path(tmp_path):
    (tmp_path / "test.env").write_text("KRAKEN_DATA_DIR=test-data\n")
    assert db.load_config(tmp_path, env_file="test.env").data_dir == tmp_path / "test-data"


def test_directory_initialization_preserves_existing_data(config):
    config.data_dir.mkdir()
    existing = config.data_dir / "metadata.sqlite"
    existing.write_bytes(b"user-owned-db-do-not-open")
    assert db.initialize_directories(config) == {"directories_writable": 2}
    assert existing.read_bytes() == b"user-owned-db-do-not-open"
    assert sorted(p.name for p in config.data_dir.iterdir()) == ["market_data", "metadata.sqlite"]


def test_directory_file_conflict_fails_without_overwrite(config):
    config.data_dir.write_bytes(b"user-file")
    with pytest.raises(FileExistsError):
        db.initialize_directories(config)
    assert config.data_dir.read_bytes() == b"user-file"


def test_sqlite_round_trip_does_not_create_metadata_database(config):
    db.initialize_directories(config)
    assert db.sqlite_smoke(config)["temporary_round_trip"]
    assert list(config.data_dir.iterdir()) == [config.data_dir / "market_data"]


def test_parquet_round_trip_cleans_temporary_dataset(config):
    pytest.importorskip("pyarrow")
    db.initialize_directories(config)
    assert db.parquet_smoke(config)["temporary_round_trip"]
    assert list((config.data_dir / "market_data").iterdir()) == []


def test_public_parse_and_transport_are_strategy_independent():
    calls = []
    def transport(endpoint, params):
        calls.append((endpoint, params))
        return pair_response()
    assert db.public_connectivity(transport) == {
        "total_pairs": 3, "aud_spot_pair_count": 2, "aud_spot_pairs_present": True}
    assert calls == [("AssetPairs", {})]


@pytest.mark.parametrize("body", [None, {}, {"error": ["failure"]},
    {"error": [], "result": {}}, {"error": [], "result": []},
    {"error": [], "result": {"bad": {}}}, {"error": [], "result": {"bad": None}}])
def test_public_response_rejects_malformed_data(body):
    with pytest.raises(ValueError):
        db.parse_asset_pairs(body)


def test_missing_aud_is_identified_not_invented():
    result = db.parse_asset_pairs({"error": [], "result": {"BTCUSD": {"base": "BTC", "quote": "USD"}}})
    assert result["total_pairs"] == 1 and not result["aud_spot_pairs_present"]


def test_missing_readonly_credentials_skips_without_client(config):
    status, detail = db.readonly_connectivity(config, lambda path: pytest.fail("No credential fallback"))
    assert status == "SKIP" and "unavailable" in detail["reason"]


def test_incomplete_readonly_credentials_fail(config):
    config.env_file.write_text("KRAKEN_READONLY_API_KEY=sentinel\n")
    assert db.readonly_connectivity(config)[0] == "FAIL"


def test_readonly_success_reuses_checker_and_does_not_return_balances(config):
    config.env_file.write_text("KRAKEN_READONLY_API_KEY=sentinel\nKRAKEN_READONLY_API_SECRET=hidden\n")
    calls = []
    status, detail = db.readonly_connectivity(config, lambda path: calls.append(path))
    assert status == "PASS" and detail["balance_read"] and not detail["trading_required"]
    assert calls == [config.env_file]
    assert "sentinel" not in json.dumps(detail) and "hidden" not in json.dumps(detail)


def test_network_failures_and_secret_errors_are_sanitized(config, monkeypatch):
    config.env_file.write_text("KRAKEN_READONLY_API_KEY=KEY_SENTINEL\nKRAKEN_READONLY_API_SECRET=SECRET_SENTINEL\n")
    monkeypatch.setattr(db, "DEPENDENCIES", {})
    monkeypatch.setattr(db, "parquet_smoke", lambda config: {"mocked": True})
    def failure(*args):
        raise RuntimeError("KEY_SENTINEL SECRET_SENTINEL network failure")
    results = db.run_diagnostics(config, public_transport=failure, private_checker=failure)
    assert {r["check"]: r["status"] for r in results}["kraken_public"] == "FAIL"
    assert {r["check"]: r["status"] for r in results}["kraken_readonly"] == "FAIL"
    assert "SENTINEL" not in json.dumps(results)


def test_dependency_import_failure_is_individual(config, monkeypatch):
    monkeypatch.setattr(db, "DEPENDENCIES", {"missing": "module_that_does_not_exist_db_m01"})
    monkeypatch.setattr(db, "parquet_smoke", lambda config: {})
    results = db.run_diagnostics(config, offline=True)
    statuses = {r["check"]: r["status"] for r in results}
    assert statuses["dependency:missing"] == "FAIL"
    assert statuses["sqlite"] == "PASS" and statuses["kraken_public"] == "SKIP"


def test_offline_diagnostic_never_contacts_network(config, monkeypatch):
    pytest.importorskip("pyarrow")
    def forbidden(*args): pytest.fail("Offline must not contact Kraken")
    results = db.run_diagnostics(config, offline=True, public_transport=forbidden, private_checker=forbidden)
    statuses = {r["check"]: r["status"] for r in results}
    assert statuses["python"] == ("PASS" if sys.version_info >= (3, 11) else "FAIL")
    assert statuses["sqlite"] == statuses["parquet"] == "PASS"
    assert statuses["kraken_public"] == statuses["kraken_readonly"] == "SKIP"


@pytest.mark.parametrize("status,code", [("PASS", 0), ("SKIP", 0), ("FAIL", 1)])
def test_cli_exit_status_and_json(config, monkeypatch, capsys, status, code):
    monkeypatch.setattr(db, "run_diagnostics", lambda *a, **kw: [{"check": "fixture", "status": status, "detail": {}}])
    assert db.main(["--root", str(config.data_dir.parent), "--json"]) == code
    assert json.loads(capsys.readouterr().out)[0]["status"] == status


def test_cli_configuration_failure_sanitized(monkeypatch, capsys):
    def fail(*args, **kwargs): raise RuntimeError("SECRET_SENTINEL")
    monkeypatch.setattr(db, "load_config", fail)
    assert db.main([]) == 1
    output = capsys.readouterr().out
    assert "FAIL configuration" in output and "SECRET_SENTINEL" not in output


@pytest.mark.skipif(os.environ.get("KRAKEN_PUBLIC_SMOKE") != "1", reason="Explicit opt-in public network integration")
def test_real_kraken_public_connectivity():
    result = db.public_connectivity()
    assert result["total_pairs"] > 0
    assert result["aud_spot_pairs_present"]
