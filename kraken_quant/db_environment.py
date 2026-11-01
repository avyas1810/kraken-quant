"""Database Milestone 1 diagnostics. Temporary storage only; no catalogue/schema."""
from contextlib import closing
from dataclasses import dataclass
import argparse
import importlib
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
import sys
import tempfile


# Reuse the existing HTTP and streaming clients instead of introducing new ones.
DEPENDENCIES = {
    "numpy": "numpy", "pandas": "pandas", "pyarrow": "pyarrow",
    "ccxt": "ccxt", "websocket-client": "websocket", "pytest": "pytest",
    "python-dotenv": "dotenv",
}


@dataclass(frozen=True)
class EnvironmentConfig:
    data_dir: Path
    env_file: Path


def load_config(root=".", *, data_dir=None, env_file=".env"):
    """Explicit options override .env; never mutate os.environ or interpolate secrets."""
    from dotenv import dotenv_values
    root = Path(root).resolve()
    env_path = Path(env_file)
    env_path = env_path if env_path.is_absolute() else root / env_path
    values = dotenv_values(env_path, interpolate=False) if env_path.is_file() else {}
    location = Path(data_dir if data_dir is not None else values.get("KRAKEN_DATA_DIR") or "data")
    location = location if location.is_absolute() else root / location
    return EnvironmentConfig(location.resolve(), env_path.resolve())


def initialize_directories(config):
    directories = (config.data_dir, config.data_dir / "market_data")
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".db-m01-", dir=directory) as scratch:
            probe = Path(scratch) / "probe"
            probe.write_bytes(b"database-environment-smoke")
            if probe.read_bytes() != b"database-environment-smoke":
                raise ValueError("Directory round-trip failed")
    return {"directories_writable": len(directories)}


def sqlite_smoke(config):
    with tempfile.TemporaryDirectory(prefix=".db-m01-", dir=config.data_dir) as scratch:
        path = Path(scratch) / "smoke.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute("CREATE TABLE smoke (value INTEGER NOT NULL)")
                connection.execute("INSERT INTO smoke VALUES (42)")
        with closing(sqlite3.connect(path)) as connection:
            if connection.execute("SELECT value FROM smoke").fetchall() != [(42,)]:
                raise ValueError("SQLite round-trip failed")
    return {"sqlite_version": sqlite3.sqlite_version, "temporary_round_trip": True}


def parquet_smoke(config):
    import pandas as pd
    with tempfile.TemporaryDirectory(prefix=".db-m01-", dir=config.data_dir / "market_data") as scratch:
        path = Path(scratch) / "smoke.parquet"
        frame = pd.DataFrame({"value": pd.Series([1, None], dtype="Int64"), "label": ["a", "b"]})
        frame.to_parquet(path, engine="pyarrow", index=False)
        pd.testing.assert_frame_equal(frame, pd.read_parquet(path, engine="pyarrow"))
    return {"engine": "pyarrow", "temporary_round_trip": True}


def parse_asset_pairs(body):
    """Summarize the spot AssetPairs response without creating an asset catalogue."""
    if not isinstance(body, dict) or body.get("error") != []:
        raise ValueError("Kraken public response failed")
    pairs = body.get("result")
    if not isinstance(pairs, dict) or not pairs:
        raise ValueError("Kraken pair map missing")
    aud = []
    for pair_id, row in pairs.items():
        if not isinstance(pair_id, str) or not pair_id or not isinstance(row, dict):
            raise ValueError("Invalid pair metadata")
        if not all(isinstance(row.get(field), str) and row[field] for field in ("base", "quote")):
            raise ValueError("Missing asset identity")
        if row["quote"] in {"AUD", "ZAUD"} and row.get("aclass_quote", "currency") == "currency":
            aud.append(pair_id)
    return {"total_pairs": len(pairs), "aud_spot_pair_count": len(aud), "aud_spot_pairs_present": bool(aud)}


def public_connectivity(transport=None):
    if transport is None:
        from .market_data import public_get
        transport = public_get
    return parse_asset_pairs(transport("AssetPairs", {}))


def readonly_connectivity(config, checker=None):
    from dotenv import dotenv_values
    values = dotenv_values(config.env_file, interpolate=False) if config.env_file.is_file() else {}
    present = [bool(values.get(name)) for name in ("KRAKEN_READONLY_API_KEY", "KRAKEN_READONLY_API_SECRET")]
    if not any(present):
        return "SKIP", {"reason": "Dedicated read-only credentials unavailable; configuration unchanged"}
    if not all(present):
        return "FAIL", {"reason": "Dedicated read-only credential configuration incomplete"}
    if checker is None:
        from .milestone1 import check_connectivity
        checker = check_connectivity
    checker(config.env_file)
    return "PASS", {"authenticated": True, "balance_read": True, "trading_required": False}


def run_diagnostics(config, *, offline=False, public_transport=None, private_checker=None):
    results = []
    def record(name, status, detail):
        results.append({"check": name, "status": status, "detail": detail})
    def check(name, callback):
        try:
            record(name, "PASS", callback())
        except Exception:
            # Never serialize exceptions: API/config errors may contain credentials.
            record(name, "FAIL", {"reason": "Check failed; private error details suppressed"})
    record("python", "PASS" if sys.version_info >= (3, 11) else "FAIL", {"version": sys.version.split()[0], "minimum": "3.11"})
    for distribution, module in DEPENDENCIES.items():
        def dependency(distribution=distribution, module=module):
            importlib.import_module(module)
            return {"version": version(distribution)}
        check("dependency:" + distribution, dependency)
    check("directories", lambda: initialize_directories(config))
    check("sqlite", lambda: sqlite_smoke(config))
    check("parquet", lambda: parquet_smoke(config))
    if offline:
        record("kraken_public", "SKIP", {"reason": "Explicit offline mode; connectivity not verified"})
        record("kraken_readonly", "SKIP", {"reason": "Explicit offline mode; connectivity not verified"})
    else:
        check("kraken_public", lambda: public_connectivity(public_transport))
        try:
            status, detail = readonly_connectivity(config, private_checker)
            record("kraken_readonly", status, detail)
        except Exception:
            record("kraken_readonly", "FAIL", {"reason": "Read-only authentication/balance check failed; private details suppressed"})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--data-dir", help="Override KRAKEN_DATA_DIR from .env; default data/")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--offline", action="store_true", help="Skip both network checks; does not satisfy connectivity acceptance")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.root, data_dir=args.data_dir, env_file=args.env_file)
        results = run_diagnostics(config, offline=args.offline)
    except Exception:
        results = [{"check": "configuration", "status": "FAIL", "detail": {"reason": "Configuration unavailable; private details suppressed"}}]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for row in results:
            print(f"{row['status']} {row['check']}: {json.dumps(row['detail'], sort_keys=True)}")
    return int(any(row["status"] == "FAIL" for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
