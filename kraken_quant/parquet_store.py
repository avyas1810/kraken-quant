"""Validated monthly Parquet partitions with idempotent, atomic replacement."""
import os
from pathlib import Path
import re
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from .operations import ProcessLock
from .storage_schema import (LOGICAL_KEYS, dataset_schema, table_from_records,
                             utc_timestamp, validate_dataset, validate_pair_id, validate_table)


class DuplicateKeyError(ValueError):
    pass


def observation_month(timestamp):
    stamp = utc_timestamp(timestamp)
    return f"{stamp.year:04d}-{stamp.month:02d}"


def validate_month(month):
    if not isinstance(month, str) or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) is None or month.startswith("0000"):
        raise ValueError("Month must be YYYY-MM with a valid year and month")
    return month


def _rows_by_key(dataset, table, *, allow_identical=True):
    keys, rows = LOGICAL_KEYS[dataset], {}
    for row in table.to_pylist():
        key = tuple(row[name] for name in keys)
        if key in rows and (not allow_identical or rows[key] != row):
            raise DuplicateKeyError("Conflicting or physically duplicated logical key")
        rows[key] = row
    return rows


class MonthlyParquetStore:
    def __init__(self, data_dir):
        self.root = (Path(data_dir) / "market_data").resolve()

    def _safe(self, path):
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Partition path escapes market-data root")
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Symbolic links inside market-data storage are prohibited")
        return path

    def partition_path(self, pair_id, dataset, month):
        validate_pair_id(pair_id)
        validate_dataset(dataset)
        validate_month(month)
        return self._safe(self.root / pair_id / dataset / (month + ".parquet"))

    def _read_path(self, path, pair_id, dataset, month):
        table = pq.ParquetFile(path, page_checksum_verification=True).read()
        validate_table(dataset, table)
        if dataset == "prices_1m":
            # Same physical-key and partition checks without decoding every
            # Decimal/datetime row repeatedly during full-history reads.
            import pyarrow.compute as pc
            if any(value != pair_id for value in pc.unique(table["pair_id"]).to_pylist()):
                raise ValueError("Partition contains mismatched pair or month")
            if pc.count_distinct(table["timestamp"]).as_py() != table.num_rows:
                raise DuplicateKeyError("Conflicting or physically duplicated logical key")
            if table.num_rows:
                edges = pc.min_max(table["timestamp"]).as_py()
                if any(observation_month(edges[key]) != month for key in ("min", "max")):
                    raise ValueError("Partition contains mismatched pair or month")
            return table
        _rows_by_key(dataset, table, allow_identical=False)
        if any(row["pair_id"] != pair_id or observation_month(row["timestamp"]) != month for row in table.to_pylist()):
            raise ValueError("Partition contains mismatched pair or month")
        return table

    def read(self, pair_id, dataset, month):
        path = self.partition_path(pair_id, dataset, month)
        if not path.exists():
            return pa.Table.from_pylist([], schema=dataset_schema(dataset))
        return self._read_path(path, pair_id, dataset, month)

    def months(self, pair_id, dataset):
        directory = self.partition_path(pair_id, dataset, "2000-01").parent
        if not directory.exists():
            return ()
        return tuple(sorted(validate_month(path.stem) for path in directory.glob("*.parquet")))

    def read_all(self, pair_id, dataset):
        tables = [self.read(pair_id, dataset, month) for month in self.months(pair_id, dataset)]
        return pa.concat_tables(tables) if tables else pa.Table.from_pylist([], schema=dataset_schema(dataset))

    def write(self, dataset, records):
        """Merge by logical key; identical retries are no-ops, conflicts fail closed.

        Commit granularity is one monthly partition, not a multi-partition batch.
        A retry safely completes a batch interrupted between partitions.
        """
        validate_dataset(dataset)
        table = records if isinstance(records, pa.Table) else table_from_records(dataset, records)
        validate_table(dataset, table)
        rows = _rows_by_key(dataset, table)
        groups = {}
        for row in rows.values():
            key = (row["pair_id"], observation_month(row["timestamp"]))
            groups.setdefault(key, []).append(row)
        paths = []
        for (pair_id, month), incoming in sorted(groups.items()):
            path = self.partition_path(pair_id, dataset, month)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._safe(Path(str(path) + ".lock"))
            lock = ProcessLock(path)
            try:
                # Read only while holding the shared writer lock to avoid lost updates.
                old = self.read(pair_id, dataset, month)
                merged_rows = _rows_by_key(dataset, old)
                for row in incoming:
                    key = tuple(row[name] for name in LOGICAL_KEYS[dataset])
                    if key in merged_rows and merged_rows[key] != row:
                        raise DuplicateKeyError("Conflicting logical key; explicit corrections require a later policy")
                    merged_rows[key] = row
                # Both old and incoming tables already passed the exact schema contract.
                # Rebuilding their typed rows needs no repeated scalar coercion.
                merged = pa.Table.from_pylist([merged_rows[key] for key in sorted(merged_rows)], schema=dataset_schema(dataset))
                validate_table(dataset, merged)
                if not old.equals(merged, check_metadata=True):
                    self._replace(path, merged, pair_id, dataset, month)
                paths.append(path)
            finally:
                lock.close()
        return tuple(paths)

    def _replace(self, path, table, pair_id, dataset, month):
        descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                pq.write_table(table, handle, version="2.6", compression="zstd", store_schema=True,
                               write_page_checksum=True)
                handle.flush()
                os.fsync(handle.fileno())
            verified = self._read_path(temporary, pair_id, dataset, month)
            if not verified.equals(table, check_metadata=True):
                raise ValueError("Staged partition did not round-trip")
            os.replace(temporary, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            # Hard crashes can leave an orphan .tmp; readers never consume it.
            temporary.unlink(missing_ok=True)
