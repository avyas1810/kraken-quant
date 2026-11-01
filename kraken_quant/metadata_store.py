"""Dedicated metadata.sqlite initialization and transactional version-1 migration."""
import argparse
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .db_environment import load_config
from .storage_schema import DATASETS, SCHEMA_VERSION

APPLICATION_ID = 0x4B514D44  # KQMD: separate from all research/execution SQLite stores.


class MetadataError(ValueError):
    pass


def _utc(name):
    pattern = "[0-9]" * 4 + "-" + "[0-9]" * 2 + "-" + "[0-9]" * 2 + "T" + "[0-9]" * 2 + ":" + "[0-9]" * 2 + ":" + "[0-9]" * 2 + "." + "[0-9]" * 6 + "Z"
    return (f"{name} IS NULL OR (length({name})=27 AND {name} GLOB '{pattern}' "
            f"AND substr({name},1,4) BETWEEN '0001' AND '9999' "
            f"AND substr({name},12,2) BETWEEN '00' AND '23' "
            f"AND substr({name},15,2) BETWEEN '00' AND '59' "
            f"AND substr({name},18,2) BETWEEN '00' AND '59' "
            f"AND coalesce(date(substr({name},1,10),'+0 days')=substr({name},1,10),0))")


def _decimal(name):
    # Canonical nonnegative decimal text, up to 20 integer and 18 fractional digits.
    return (f"{name} IS NULL OR (typeof({name})='text' AND length({name})>0 "
            f"AND {name} NOT GLOB '*[^0-9.]*' AND {name} NOT LIKE '%.%.%' "
            f"AND {name} NOT LIKE '.%' AND {name} NOT LIKE '%.' "
            f"AND (length({name})=1 OR substr({name},1,1)!='0' OR substr({name},2,1)='.') "
            f"AND length(substr({name},1,instr({name}||'.','.')-1))<=20 "
            f"AND CASE WHEN instr({name},'.')=0 THEN 0 ELSE length({name})-instr({name},'.') END<=18)")


def _time_column(name, nullable=False):
    return f"{name} TEXT {'NULL' if nullable else 'NOT NULL'} CHECK ({_utc(name)})"


def _decimal_column(name):
    return f"{name} TEXT NULL CHECK ({_decimal(name)})"


_DATASET_SQL = ",".join(repr(name) for name in DATASETS)
TABLE_DDL = {
    "assets": f"""CREATE TABLE assets (
        pair_id TEXT PRIMARY KEY NOT NULL,
        kraken_pair_id TEXT NOT NULL UNIQUE CHECK (length(trim(kraken_pair_id))>0),
        base_asset TEXT NOT NULL CHECK (length(base_asset) BETWEEN 1 AND 20 AND base_asset NOT GLOB '*[^A-Z0-9]*'),
        quote_asset TEXT NOT NULL CHECK (length(quote_asset) BETWEEN 1 AND 20 AND quote_asset NOT GLOB '*[^A-Z0-9]*'),
        status TEXT NOT NULL CHECK (length(trim(status))>0),
        {_time_column('first_seen')}, {_time_column('last_seen')},
        {_decimal_column('min_order_qty')}, {_decimal_column('min_order_value')},
        qty_precision INTEGER NULL CHECK (qty_precision BETWEEN 0 AND 18),
        price_precision INTEGER NULL CHECK (price_precision BETWEEN 0 AND 18),
        CHECK (pair_id=base_asset||'-'||quote_asset), CHECK (last_seen>=first_seen)
    ) STRICT""",
    "eligibility": f"""CREATE TABLE eligibility (
        pair_id TEXT NOT NULL REFERENCES assets(pair_id) ON DELETE RESTRICT,
        {_time_column('screened_at')},
        {_decimal_column('volume_24h_aud')}, {_decimal_column('avg_spread_pct')},
        {_decimal_column('depth_aud')},
        historical_days INTEGER NULL CHECK (historical_days>=0),
        {_decimal_column('completeness_pct')},
        decision TEXT NOT NULL CHECK (decision IN ('eligible','ineligible','unknown')),
        rejection_reasons TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(rejection_reasons) AND json_type(rejection_reasons)='array'),
        volume_24h_aud_status TEXT NOT NULL CHECK (volume_24h_aud_status IN ('known','unknown','unavailable')),
        avg_spread_pct_status TEXT NOT NULL CHECK (avg_spread_pct_status IN ('known','unknown','unavailable')),
        depth_aud_status TEXT NOT NULL CHECK (depth_aud_status IN ('known','unknown','unavailable')),
        historical_days_status TEXT NOT NULL CHECK (historical_days_status IN ('known','unknown','unavailable')),
        completeness_pct_status TEXT NOT NULL CHECK (completeness_pct_status IN ('known','unknown','unavailable')),
        PRIMARY KEY (pair_id,screened_at),
        CHECK ((volume_24h_aud_status='known')=(volume_24h_aud IS NOT NULL)),
        CHECK ((avg_spread_pct_status='known')=(avg_spread_pct IS NOT NULL)),
        CHECK ((depth_aud_status='known')=(depth_aud IS NOT NULL)),
        CHECK ((historical_days_status='known')=(historical_days IS NOT NULL)),
        CHECK ((completeness_pct_status='known')=(completeness_pct IS NOT NULL)),
        CHECK (completeness_pct IS NULL OR CAST(substr(completeness_pct,1,instr(completeness_pct||'.','.')-1) AS INTEGER)<100
            OR (CAST(substr(completeness_pct,1,instr(completeness_pct||'.','.')-1) AS INTEGER)=100
                AND trim(substr(completeness_pct,instr(completeness_pct||'.','.')+1),'0')=''))
    ) STRICT""",
    "ingestion_state": f"""CREATE TABLE ingestion_state (
        pair_id TEXT NOT NULL REFERENCES assets(pair_id) ON DELETE RESTRICT,
        dataset TEXT NOT NULL CHECK (dataset IN ({_DATASET_SQL})),
        {_time_column('latest_observation_at', nullable=True)}, {_time_column('updated_at')},
        PRIMARY KEY (pair_id,dataset),
        CHECK (latest_observation_at IS NULL OR latest_observation_at<=updated_at)
    ) STRICT""",
    "collection_status": f"""CREATE TABLE collection_status (
        pair_id TEXT PRIMARY KEY NOT NULL REFERENCES assets(pair_id) ON DELETE RESTRICT,
        readiness TEXT NOT NULL CHECK (readiness IN ('ready','not_ready','unknown')),
        operational_status TEXT NOT NULL CHECK (operational_status IN ('idle','collecting','paused','error','unknown')),
        {_time_column('updated_at')}, reason TEXT NULL
    ) STRICT""",
    "data_quality_issues": f"""CREATE TABLE data_quality_issues (
        issue_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(issue_id))>0),
        pair_id TEXT NOT NULL REFERENCES assets(pair_id) ON DELETE RESTRICT,
        dataset TEXT NOT NULL CHECK (dataset IN ({_DATASET_SQL})),
        {_time_column('observation_at', nullable=True)},
        {_time_column('period_start', nullable=True)}, {_time_column('period_end', nullable=True)},
        issue_type TEXT NOT NULL CHECK (length(trim(issue_type))>0),
        description TEXT NOT NULL CHECK (length(trim(description))>0),
        {_time_column('detected_at')},
        resolution_status TEXT NOT NULL CHECK (resolution_status IN ('open','resolved','ignored')),
        CHECK ((observation_at IS NOT NULL AND period_start IS NULL AND period_end IS NULL) OR
            (observation_at IS NULL AND period_start IS NOT NULL AND period_end IS NOT NULL AND period_start<=period_end))
    ) STRICT""",
}
INDEX_DDL = (
    "CREATE INDEX assets_status_idx ON assets(status)",
    "CREATE INDEX eligibility_time_idx ON eligibility(screened_at,pair_id)",
    "CREATE INDEX ingestion_updated_idx ON ingestion_state(dataset,updated_at)",
    "CREATE INDEX collection_operational_idx ON collection_status(operational_status,updated_at)",
    "CREATE INDEX quality_pair_idx ON data_quality_issues(pair_id,dataset,observation_at,period_start)",
    "CREATE INDEX quality_resolution_idx ON data_quality_issues(resolution_status,detected_at)",
)
MIGRATIONS = {1: tuple(TABLE_DDL.values()) + INDEX_DDL}


def _signature(db):
    return [(kind, name, " ".join(sql.strip().rstrip(';').split()))
            for kind, name, sql in db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]


def _expected_signature():
    db = sqlite3.connect(":memory:")
    try:
        for statement in (*TABLE_DDL.values(), *INDEX_DDL):
            db.execute(statement)
        return _signature(db)
    finally:
        db.close()


def _verify(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise MetadataError(f"Unsupported metadata schema version {version}; expected {SCHEMA_VERSION}")
    if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise MetadataError("Refusing unrelated database identity")
    if _signature(db) != _expected_signature():
        raise MetadataError("Metadata schema mismatch; automatic repair is prohibited")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MetadataError("Metadata foreign-key integrity check failed")


def metadata_path(data_dir):
    path = Path(data_dir) / "metadata.sqlite"
    if path.is_symlink():
        raise MetadataError("Metadata database must not be a symbolic link")
    return path


def initialize_metadata(data_dir):
    path = metadata_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None, timeout=10)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        identity = db.execute("PRAGMA application_id").fetchone()[0]
        if version == 0:
            if identity != 0 or _signature(db):
                raise MetadataError("Refusing unversioned or unrelated existing database")
            for target in range(1, SCHEMA_VERSION + 1):
                if target not in MIGRATIONS:
                    raise MetadataError("Required migration is unavailable")
                for statement in MIGRATIONS[target]:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={target}")
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        _verify(db)
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return path


@contextmanager
def connect_metadata(data_dir, *, readonly=False):
    """Open only an initialized, compatible metadata store; enforce foreign keys."""
    path = metadata_path(data_dir).resolve()
    db = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=10)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        _verify(db)
        with db:
            yield db
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "show"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--data-dir")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.root, data_dir=args.data_dir)
        if args.command == "init":
            path = initialize_metadata(config.data_dir)
            print(f"Initialized metadata schema v{SCHEMA_VERSION}: {path}")
        else:
            with connect_metadata(config.data_dir, readonly=True) as db:
                for name, sql in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
                    print(sql + ";")
    except MetadataError as error:
        print(f"Metadata failure: {error}")
        return 1
    except Exception:
        print("Metadata operation failed; no unrelated stores were initialized")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
