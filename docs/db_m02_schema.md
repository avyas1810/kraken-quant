# Database Milestone 2: SQLite metadata and monthly Parquet storage

## Scope, audit and architecture

PR #2 was verified merged into `review-base/milestone1-20261029` at `b6e68b13ca575fcddd2a7da17e422b95ad560ce8` before work began. The feature branch starts there. Database Milestone 1 configuration, Python 3.11 environment, dependency lock and data exclusions are reused unchanged. Existing `market_data.public_get`, `streaming.WebSocketMarketData` and read-only credentials remain the future data sources; this milestone performs no network collection, screening calculations or trading.

The audit found purpose-specific SQLite stores in risk, shadow, operations and live execution, and safe process locking/backup helpers. Their schemas are not market metadata and are untouched. The existing `operations.ProcessLock` protects partition writers. Configuration uses `db_environment.load_config`; no locked research universe is redefined. Storage contracts are independent of strategy interfaces. PyArrow 19.0.1 from the existing lock is sufficient; no dependencies were added.

`metadata_store` owns only `<data_dir>/metadata.sqlite`. `storage_schema` defines Arrow v1 contracts and validation. `parquet_store.MonthlyParquetStore(data_dir)` owns monthly files under `<data_dir>/market_data`. No catalogue, backfill, scheduler, filtering rules, paper-execution system or subsequent milestone is implemented.

## Commands

From the repository root, using the Database Milestone 1 environment:

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.metadata_store init
.venv-db/Scripts/python.exe -m kraken_quant.metadata_store init  # safe repeat
.venv-db/Scripts/python.exe -m kraken_quant.metadata_store show
.venv-db/Scripts/python.exe -m examples.db_m02_storage
.venv-db/Scripts/python.exe -m pytest -q tests/test_metadata_store.py tests/test_parquet_store.py tests/test_storage_integration.py
.venv-db/Scripts/python.exe -m pytest -q tests
```

`init`/`show` accept `--root` and `--data-dir`; default storage comes from the existing `.env` `KRAKEN_DATA_DIR` setting or `data/`. Merely importing a module does not initialize a database. The synthetic example always uses a new temporary directory, never the configured permanent store. Generated stores remain ignored by Git. `show` reports actual SQLite definitions; it does not populate data.

## Initialization and migration contract

All five tables are STRICT. `PRAGMA user_version=1` records schema version; `PRAGMA application_id` identifies the dedicated metadata application. The current migration registry contains only empty version 0 -> version 1. An empty version-0 database is allowed; any unversioned existing tables or unrelated identity are refused. Unsupported versions fail clearly. Version-1 tables, CHECK constraints, foreign keys, indices and other schema objects must exactly match the expected normalized DDL; drift is rejected, never silently repaired.

Initialization uses `BEGIN IMMEDIATE`, statement-by-statement transactional DDL, identity/version updates in the same transaction, verification, then COMMIT. Exceptions roll back all schema changes and version updates; a failed fresh initialization may leave an empty version-0 file, not partially applied tables. Reinitialization changes neither existing records nor valid schema. `connect_metadata(data_dir, readonly=False)` refuses missing/unrecognized stores, enables foreign keys and commits/rolls back its context. External writers must also enable `PRAGMA foreign_keys=ON`. Database symlinks are rejected.

Versioning here covers the foundation's v1 contracts. A future migration must be explicitly implemented and reviewed before a new version is accepted. No external migration framework is needed. The application ID and exact schema check prevent initialization of an existing research/execution store even if it happens to be named metadata.sqlite.

## Shared data conventions

SQLite UTC timestamps are TEXT in fixed `YYYY-MM-DDTHH:MM:SS.ffffffZ` form, calendar-validated, year 0001..9999, no leap seconds. Nullable times mean explicitly unknown/not applicable. Arrow timestamps are `timestamp[us, tz=UTC]`; aware offset inputs normalize to UTC. Naive timestamps, nanosecond precision loss and mismatched schemas are rejected. Quote/book observation times are physical times, not silently rounded to minute boundaries.

SQLite monetary/percentage values are exact nonnegative decimal TEXT with no exponent, sign, leading integer zeros or dangling decimal point; at most 20 integer digits and 18 fractional digits. Callers bind exact decimal strings rather than binary floats. Trailing fractional zeros are allowed. Arrow monetary fields use `decimal128(38,18)`; the record builder accepts Decimal, decimal string or integer, rejects floats/nonfinite/unrepresentable values, and never rounds. Percentages use percentage points: 0.4 means 0.4%, not 40%. Cost fields may be signed to represent fee rebates or price improvement as documented below.

Internal pair identifiers are uppercase alphanumeric BASE-QUOTE, with 1..20 characters per asset. Example: BTC-AUD. Kraken's native key XXBTZAUD is separately preserved verbatim; it is not a filesystem path and is not guessed by the storage layer. All pair foreign keys reference assets and use ON DELETE RESTRICT to preserve traceability.

## SQLite data dictionaries

For every column below, type and nullability are taken from the initialized SQLite database. All examples are **SYNTHETIC**, illustrative records, never collected observations. JSON example objects display rejection_reasons as an array; SQL stores its JSON encoding as TEXT.
### assets

Purpose: Market identity and last observed instrument metadata.

Expected source: Kraken public AssetPairs plus application observation times; a later catalogue adapter supplies explicit normalization.

| Column | SQLite type | Nullable | Definition | Units | Key |
|---|---|---|---|---|---|
| `pair_id` | TEXT | No | Canonical internal BASE-QUOTE identifier; equals normalized base + hyphen + quote. | identifier | PK component 1 |
| `kraken_pair_id` | TEXT | No | Original Kraken AssetPairs response key; retained verbatim, never replaced by normalized symbol. | exchange identifier | Unique |
| `base_asset` | TEXT | No | Normalized uppercase alphanumeric base asset, e.g. BTC rather than Kraken XXBT. | asset code | None |
| `quote_asset` | TEXT | No | Normalized uppercase alphanumeric quote currency. | currency code | None |
| `status` | TEXT | No | Last observed exchange trading status, preserved as nonempty text. | exchange status | None |
| `first_seen` | TEXT | No | First time the application observed this exchange market. | UTC microseconds | None |
| `last_seen` | TEXT | No | Most recent application observation, at or after first_seen. | UTC microseconds | None |
| `min_order_qty` | TEXT | Yes | Exchange minimum base quantity, unknown when null. | base asset units | None |
| `min_order_value` | TEXT | Yes | Exchange minimum notional, unknown when null. | quote currency units | None |
| `qty_precision` | INTEGER | Yes | Number of allowed decimal places for base quantity; not an order quantity step. | decimal places, 0..18 | None |
| `price_precision` | INTEGER | Yes | Number of allowed decimal places for price; not a price tick amount. | decimal places, 0..18 | None |

Constraints: PK pair_id; UNIQUE kraken_pair_id; canonical pair equals base_asset-quote_asset; valid nonempty identities/status; last_seen >= first_seen; nullable decimal minima and integer precision 0..18. No exchange status vocabulary or screening threshold is invented.

SYNTHETIC example:

```json
{
  "pair_id": "BTC-AUD",
  "kraken_pair_id": "XXBTZAUD",
  "base_asset": "BTC",
  "quote_asset": "AUD",
  "status": "online",
  "first_seen": "2026-11-01T00:00:00.000000Z",
  "last_seen": "2026-11-01T00:00:00.000000Z",
  "min_order_qty": "0.0001",
  "min_order_value": "1",
  "qty_precision": 8,
  "price_precision": 2
}
```

### eligibility

Purpose: Append historical screening results; never overwrite a single latest boolean.

Expected source: A later caller that computes screening metrics from ticker, book, history coverage and AUD conversion inputs.

| Column | SQLite type | Nullable | Definition | Units | Key |
|---|---|---|---|---|---|
| `pair_id` | TEXT | No | Reference to assets.pair_id. | identifier | PK component 1 |
| `screened_at` | TEXT | No | As-of time for this historical screening result. | UTC microseconds | PK component 2 |
| `volume_24h_aud` | TEXT | Yes | Volume notional over the preceding 24 hours, converted into AUD by a future screen producer. | AUD | None |
| `avg_spread_pct` | TEXT | Yes | Average spread estimate supplied by the future screening calculation; methodology is not invented here. | percentage points | None |
| `depth_aud` | TEXT | Yes | Order-book depth notional supplied by the future screening calculation. | AUD | None |
| `historical_days` | INTEGER | Yes | Number of available historical days at screening time. | whole days, >=0 | None |
| `completeness_pct` | TEXT | Yes | Data coverage supplied by a future completeness check. | percentage points, 0..100 | None |
| `decision` | TEXT | No | Caller-supplied eligible / ineligible / unknown; no screening thresholds are encoded. | enum | None |
| `rejection_reasons` | TEXT | No | JSON array of caller-supplied reason codes or structured reason records; [] allowed. | JSON array | None |
| `volume_24h_aud_status` | TEXT | No | Availability of the corresponding metric: known, unknown, unavailable. | enum | None |
| `avg_spread_pct_status` | TEXT | No | Availability of the corresponding metric: known, unknown, unavailable. | enum | None |
| `depth_aud_status` | TEXT | No | Availability of the corresponding metric: known, unknown, unavailable. | enum | None |
| `historical_days_status` | TEXT | No | Availability of the corresponding metric: known, unknown, unavailable. | enum | None |
| `completeness_pct_status` | TEXT | No | Availability of the corresponding metric: known, unknown, unavailable. | enum | None |

Constraints: Composite PK (pair_id, screened_at) preserves history. Metrics must be nonnegative when present; completeness <=100 exactly. Each metric is nonnull iff its status is known; unknown means unestablished and unavailable means the source could not supply it. Decisions are eligible/ineligible/unknown. Rejection reasons must be a valid JSON array. No rule derives a decision from metrics.

SYNTHETIC example:

```json
{
  "pair_id": "BTC-AUD",
  "screened_at": "2026-11-01T00:00:00.000000Z",
  "volume_24h_aud": null,
  "avg_spread_pct": null,
  "depth_aud": null,
  "historical_days": null,
  "completeness_pct": null,
  "decision": "unknown",
  "rejection_reasons": [
    "synthetic_example_only"
  ],
  "volume_24h_aud_status": "unknown",
  "avg_spread_pct_status": "unavailable",
  "depth_aud_status": "unknown",
  "historical_days_status": "unavailable",
  "completeness_pct_status": "unknown"
}
```

### ingestion_state

Purpose: One checkpoint per market and dataset.

Expected source: A later ingestion coordinator, only after successful persistence. Storage writes do not advance checkpoints automatically.

| Column | SQLite type | Nullable | Definition | Units | Key |
|---|---|---|---|---|---|
| `pair_id` | TEXT | No | Reference to assets.pair_id. | identifier | PK component 1 |
| `dataset` | TEXT | No | One of prices_1m, quotes_1m, orderbook, costs_1m. | dataset name | PK component 2 |
| `latest_observation_at` | TEXT | Yes | Latest observation successfully persisted; null means no checkpoint yet. Never advance before persistence. | UTC microseconds | None |
| `updated_at` | TEXT | No | Checkpoint write time; at or after the latest observation. | UTC microseconds | None |

Constraints: Composite PK (pair_id, dataset). Dataset must be one of the four defined names; latest_observation_at is null or <= updated_at. A checkpoint is a caller assertion after persistence, not proof of global contiguity.

SYNTHETIC example:

```json
{
  "pair_id": "BTC-AUD",
  "dataset": "prices_1m",
  "latest_observation_at": "2026-11-01T00:00:00.000000Z",
  "updated_at": "2026-11-01T00:00:00.000000Z"
}
```

### collection_status

Purpose: Current collection readiness/operational state, deliberately separate from research eligibility.

Expected source: Operator or later collector health/readiness checks; no filtering thresholds are implemented.

| Column | SQLite type | Nullable | Definition | Units | Key |
|---|---|---|---|---|---|
| `pair_id` | TEXT | No | Reference to assets.pair_id, one current operational record per market. | identifier | PK component 1 |
| `readiness` | TEXT | No | Collection readiness: ready / not_ready / unknown, independent of research eligibility. | enum | None |
| `operational_status` | TEXT | No | Collector state: idle / collecting / paused / error / unknown. | enum | None |
| `updated_at` | TEXT | No | Time this readiness/operational state was recorded. | UTC microseconds | None |
| `reason` | TEXT | Yes | Optional operational explanation; must never contain credentials. | text | None |

Constraints: PK pair_id. Readiness is ready/not_ready/unknown; operational status is idle/collecting/paused/error/unknown. No dependency on eligibility.decision; this is a current operational record, not historical research screening.

SYNTHETIC example:

```json
{
  "pair_id": "BTC-AUD",
  "readiness": "not_ready",
  "operational_status": "idle",
  "updated_at": "2026-11-01T00:00:00.000000Z",
  "reason": "Synthetic example only"
}
```

### data_quality_issues

Purpose: Trace point/period quality anomalies and their resolution state.

Expected source: Future validators/collectors or operator review; synthetic test examples only in this milestone.

| Column | SQLite type | Nullable | Definition | Units | Key |
|---|---|---|---|---|---|
| `issue_id` | TEXT | No | Caller-generated stable identifier for the issue; nonempty and unique. | identifier | PK component 1 |
| `pair_id` | TEXT | No | Reference to assets.pair_id. | identifier | FK -> assets |
| `dataset` | TEXT | No | Affected named dataset. | dataset name | None |
| `observation_at` | TEXT | Yes | Affected point observation; set only when no affected period is supplied. | UTC microseconds | None |
| `period_start` | TEXT | Yes | Inclusive affected-period start; requires period_end and no observation_at. | UTC microseconds | None |
| `period_end` | TEXT | Yes | Inclusive affected-period end; at or after period_start. | UTC microseconds | None |
| `issue_type` | TEXT | No | Nonempty caller-defined quality code, e.g. gap or malformed_row; no screening rule is implied. | text code | None |
| `description` | TEXT | No | Nonempty diagnostic explanation, excluding secrets and raw private API responses. | text | None |
| `detected_at` | TEXT | No | Time the issue was discovered, not necessarily the event time. | UTC microseconds | None |
| `resolution_status` | TEXT | No | open / resolved / ignored. | enum | None |

Constraints: PK issue_id. Dataset must be defined; issue_type and description nonempty. Exactly one scope: observation_at, OR both period_start and period_end with start <= end. Period boundaries are inclusive. Resolution is open/resolved/ignored.

SYNTHETIC example:

```json
{
  "issue_id": "SYNTHETIC-example",
  "pair_id": "BTC-AUD",
  "dataset": "prices_1m",
  "observation_at": "2026-11-01T00:00:00.000000Z",
  "period_start": null,
  "period_end": null,
  "issue_type": "synthetic_example",
  "description": "Illustration, not real exchange data",
  "detected_at": "2026-11-01T00:00:00.000000Z",
  "resolution_status": "ignored"
}
```

### Indices

- assets_status_idx: (status).
- eligibility_time_idx: (screened_at, pair_id), in addition to historical composite PK.
- ingestion_updated_idx: (dataset, updated_at), in addition to pair/dataset PK.
- collection_operational_idx: (operational_status, updated_at).
- quality_pair_idx: (pair_id, dataset, observation_at, period_start).
- quality_resolution_idx: (resolution_status, detected_at).

## Parquet dictionaries and logical keys

Each schema has schema_version=1, dataset name and serialized logical_key in Arrow schema metadata; every field also records its units. Field types, order, nullability, units and schema-version metadata must match exactly, including on reads. Unknown extra record fields are rejected. Logical uniqueness spans a dataset; all keys include pair and timestamp so they fall in exactly one monthly partition. No uniqueness across unrelated datasets is implied.
### prices_1m

Purpose: One-minute OHLC/base-volume bars.

Expected source: Later Kraken OHLC API or explicit trade aggregation.

Logical key: `pair_id, timestamp`.

| Column | Arrow type | Nullable | Definition | Units |
|---|---|---|---|---|
| `pair_id` | `string` | No | Canonical internal market identifier; not Kraken native pair key. | identifier |
| `timestamp` | `timestamp[us, tz=UTC]` | No | UTC event/observation time; prices use the inclusive one-minute bar open. | UTC observation time; prices: minute-bar open |
| `open` | `decimal128(38, 18)` | No | First trade price in the minute. | quote currency per base asset |
| `high` | `decimal128(38, 18)` | No | Maximum trade price in the minute. | quote currency per base asset |
| `low` | `decimal128(38, 18)` | No | Minimum trade price in the minute. | quote currency per base asset |
| `close` | `decimal128(38, 18)` | No | Last trade price in the minute. | quote currency per base asset |
| `volume_base` | `decimal128(38, 18)` | No | Sum of base quantity traded in the minute. | base asset units |
| `trade_count` | `int64` | Yes | Number of trades, or null when unavailable. | trades in minute |
| `is_complete` | `bool` | No | Whether the bar is known completed. | completed bar flag |

Constraints: Logical key (pair_id,timestamp). Prices >0; OHLC bounds consistent; volume >=0; trade count null or >=0. Timestamp must be exactly minute aligned. Provisional is_complete=false is allowed, but replacement by a completed conflicting row needs a later explicit correction policy.

SYNTHETIC example (decimal strings are converted exactly by table_from_records):

```json
{
  "pair_id": "BTC-AUD",
  "timestamp": "2026-11-01T00:00:00+00:00",
  "open": "100000.123456789012345678",
  "high": "100001",
  "low": "99999",
  "close": "100000",
  "volume_base": "0.25",
  "trade_count": 3,
  "is_complete": true
}
```

### quotes_1m

Purpose: Physically timestamped top-of-book observations; the name denotes intended cadence, not forced minute rounding.

Expected source: Later Kraken public ticker/book observations with exchange event time.

Logical key: `pair_id, timestamp`.

| Column | Arrow type | Nullable | Definition | Units |
|---|---|---|---|---|
| `pair_id` | `string` | No | Canonical internal market identifier; not Kraken native pair key. | identifier |
| `timestamp` | `timestamp[us, tz=UTC]` | No | UTC event/observation time; prices use the inclusive one-minute bar open. | UTC observation time; prices: minute-bar open |
| `bid` | `decimal128(38, 18)` | No | Best observed bid price. | quote currency per base asset |
| `ask` | `decimal128(38, 18)` | No | Best observed ask price. | quote currency per base asset |
| `bid_size` | `decimal128(38, 18)` | Yes | Base quantity available at best bid, or null when unavailable. | base asset units |
| `ask_size` | `decimal128(38, 18)` | Yes | Base quantity available at best ask, or null when unavailable. | base asset units |

Constraints: Logical key (pair_id,timestamp). bid >0, ask >=bid; nullable sizes >=0. Seconds and microseconds are retained.

SYNTHETIC example (decimal strings are converted exactly by table_from_records):

```json
{
  "pair_id": "BTC-AUD",
  "timestamp": "2026-11-01T00:00:00.123456+00:00",
  "bid": "99999",
  "ask": "100001",
  "bid_size": null,
  "ask_size": "0.5"
}
```

### orderbook

Purpose: Timestamped depth levels on each book side.

Expected source: Later Kraken book snapshots or explicitly reconstructed updates.

Logical key: `pair_id, timestamp, side, level`.

| Column | Arrow type | Nullable | Definition | Units |
|---|---|---|---|---|
| `pair_id` | `string` | No | Canonical internal market identifier; not Kraken native pair key. | identifier |
| `timestamp` | `timestamp[us, tz=UTC]` | No | UTC event/observation time; prices use the inclusive one-minute bar open. | UTC observation time; prices: minute-bar open |
| `side` | `string` | No | Book: bid/ask. Costs: buy/sell. | bid or ask |
| `level` | `int32` | No | One-based level index on that book side. | one-based book level |
| `price` | `decimal128(38, 18)` | No | Observed price at the book level. | quote currency per base asset |
| `quantity` | `decimal128(38, 18)` | No | Base quantity at the book level; zero is allowed. | base asset units |

Constraints: Logical key (pair_id,timestamp,side,level). side bid/ask, level >=1, price >0, quantity >=0. No fabricated minute alignment or reconstruction is performed.

SYNTHETIC example (decimal strings are converted exactly by table_from_records):

```json
{
  "pair_id": "BTC-AUD",
  "timestamp": "2026-11-01T00:00:00.123456+00:00",
  "side": "bid",
  "level": 1,
  "price": "99999",
  "quantity": "0.25"
}
```

### costs_1m

Purpose: Versioned per-side/per-size execution-cost observations/estimates.

Expected source: Later fee metadata, spread measurements and cost models, including explicit AUD conversion inputs if needed.

Logical key: `pair_id, timestamp, side, order_size_aud, fee_type, model_version`.

| Column | Arrow type | Nullable | Definition | Units |
|---|---|---|---|---|
| `pair_id` | `string` | No | Canonical internal market identifier; not Kraken native pair key. | identifier |
| `timestamp` | `timestamp[us, tz=UTC]` | No | UTC event/observation time; prices use the inclusive one-minute bar open. | UTC observation time; prices: minute-bar open |
| `side` | `string` | No | Book: bid/ask. Costs: buy/sell. | buy or sell |
| `order_size_aud` | `decimal128(38, 18)` | No | Hypothetical order notional in AUD. | AUD notional |
| `fee_type` | `string` | No | Source/model classification, e.g. maker or taker; required nonempty text. | fee classification supplied by source/model |
| `fee_pct` | `decimal128(38, 18)` | Yes | Fee estimate, null when unknown; negative rebate is allowed. | percentage points (0.4 means 0.4%) |
| `half_spread_pct` | `decimal128(38, 18)` | Yes | Half-spread estimate, nonnegative or null. | percentage points (0.4 means 0.4%) |
| `slippage_pct` | `decimal128(38, 18)` | Yes | Modelled execution slippage; signed for adverse/favourable movement, or null. | percentage points (0.4 means 0.4%) |
| `total_cost_pct` | `decimal128(38, 18)` | Yes | Exact sum of the three cost components when known; null if any component is unknown. | percentage points (0.4 means 0.4%) |
| `model_version` | `string` | No | Nonempty identifier of the model and assumptions that generated the estimate. | cost-model identifier |

Constraints: Logical key (pair_id,timestamp,side,order_size_aud,fee_type,model_version). side buy/sell; order_size_aud >0; fee_type and model_version nonempty. Half spread nonnegative; fee/slippage/total signed. Total must exactly equal fee + half_spread + slippage, or be null when a component is unknown. No fee rate or slippage model is invented by storage.

SYNTHETIC example (decimal strings are converted exactly by table_from_records):

```json
{
  "pair_id": "BTC-AUD",
  "timestamp": "2026-11-01T00:00:00+00:00",
  "side": "buy",
  "order_size_aud": "1000",
  "fee_type": "taker",
  "fee_pct": "0.4",
  "half_spread_pct": "0.001",
  "slippage_pct": "0.02",
  "total_cost_pct": "0.421",
  "model_version": "SYNTHETIC-v1"
}
```

## Physical layout and persistence semantics

```text
data/
  metadata.sqlite
  market_data/
    BTC-AUD/
      prices_1m/2026-11.parquet
      quotes_1m/2026-11.parquet
      orderbook/2026-11.parquet
      costs_1m/2026-11.parquet
```

The pair/dataset directories are created only for actual writes. No empty pair universe is created. `partition_path` accepts only validated pair IDs, dataset names and YYYY-MM months; all child paths must remain under the configured root and symlinks are refused. The configured root itself is trusted and resolved once. This is not a sandbox against an attacker concurrently replacing filesystem components.

`table_from_records(dataset, records)` performs exact scalar conversion plus validation. `write(dataset, records_or_arrow_table)` routes by each row's UTC month and pair. Rows are sorted deterministically by logical key. Identical logical-key rows collapse; identical retries do not rewrite the existing file. A conflicting logical key raises DuplicateKeyError without overwriting that partition. Silent last-writer-wins corrections are intentionally absent.

For each monthly file, the existing OS ProcessLock serializes read-merge-write; a competing writer fails clearly and may retry. A unique temporary file is written in the same directory using Parquet 2.6, Zstandard, Arrow schema metadata and page checksums. The file is flushed/fsynced, reopened with checksum verification, schema/content checked, then os.replace atomically installs it. On POSIX the parent directory is fsynced; Windows relies on file fsync plus filesystem replace semantics. Writer exceptions clean up their own temporary file and release the lock. Readers see a valid old or new file; a hard crash may leave ignored orphan .tmp files. Stable .parquet.lock files remain to avoid lock-inode races.

Atomicity is **per monthly partition**, not a transaction across all months or between SQLite and Parquet. An interrupted batch may have committed earlier partitions; retry is idempotent and safely finishes. A failure after replacement (e.g. directory fsync failure) may have committed the valid new partition, so retry rather than assuming no write occurred. Power-loss durability depends on filesystem/OS guarantees. The caller must advance ingestion checkpoints only after persistence; cross-store crash coordination is future ingestion work.

`read(pair,dataset,month)` returns a typed empty Arrow table if missing, without creating a path. `months` lists validated .parquet partitions; temporary files are ignored. `read_all` concatenates sorted monthly files. Reads reject schema drift, wrong partition identity/month, duplicate physical keys, corrupt checksums and invalid values. Existing corrupt data is not auto-repaired.

## Synthetic observation representation and verification

`python -m examples.db_m02_storage` creates all five tables and one record per dataset in an isolated TemporaryDirectory, repeats writes, reopens partitions with a new store instance, compares exact schemas/records and writes a checkpoint only afterward. A Kraken-shaped synthetic observation with native key XXBTZAUD is explicitly mapped to internal pair BTC-AUD; prices remain quote-currency/base units, quantities remain base units, and cost size is AUD. No example is inserted into the real configured metadata store. The command returns verified records and a real Arrow schema; temporary files are removed before returning.

Local permanent initialization was explicitly run twice. All five permanent metadata tables are empty and integrity_check=ok. No permanent Parquet market observations were written. No real Kraken market-data population occurred.

## Limitations and acceptance

No collection/catalogue, AUD conversion, history reconciliation, automatic schema repair, automatic checkpoint coordination or correction/compaction policy is included. Schema v1 represents numbers with at most 18 fractional and 20 integer digits, timestamps to microseconds, and price bars on minute boundaries; unsupported precision is rejected. Collection_status is current state, while eligibility is historical. Research/execution stores, locked dataset splits, credentials, notebooks and research artifacts remain separate. The existing dependency lock and both Python CI profiles are reused.

Tests cover empty/repeated initialization, data preservation, keys/constraints, unsupported versions, drift and transactional rollback; every Parquet dataset, nullable/decimal/UTC fidelity, path safety, months, exact/conflicting duplicates, restart recovery, interruptions and writer locks; synthetic cross-store examples and generated-file Git exclusions. The real Windows symlink test may skip without privilege; Linux CI exercises it. See the delivery/PR acceptance matrix for AC-01 through AC-20 and exact run counts.

## Actual SQLite DDL evidence

The following definitions and indices were read from the initialized permanent database, not inferred from the data dictionaries. They include the exact integrity constraints. PRAGMA application_id and user_version are recorded separately. SQLite's JSON and STRICT-table support are required (provided by the approved environment).

```sql
PRAGMA application_id=1263619396;
PRAGMA user_version=1;
CREATE TABLE assets (
        pair_id TEXT PRIMARY KEY NOT NULL,
        kraken_pair_id TEXT NOT NULL UNIQUE CHECK (length(trim(kraken_pair_id))>0),
        base_asset TEXT NOT NULL CHECK (length(base_asset) BETWEEN 1 AND 20 AND base_asset NOT GLOB '*[^A-Z0-9]*'),
        quote_asset TEXT NOT NULL CHECK (length(quote_asset) BETWEEN 1 AND 20 AND quote_asset NOT GLOB '*[^A-Z0-9]*'),
        status TEXT NOT NULL CHECK (length(trim(status))>0),
        first_seen TEXT NOT NULL CHECK (first_seen IS NULL OR (length(first_seen)=27 AND first_seen GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(first_seen,1,4) BETWEEN '0001' AND '9999' AND substr(first_seen,12,2) BETWEEN '00' AND '23' AND substr(first_seen,15,2) BETWEEN '00' AND '59' AND substr(first_seen,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(first_seen,1,10),'+0 days')=substr(first_seen,1,10),0))), last_seen TEXT NOT NULL CHECK (last_seen IS NULL OR (length(last_seen)=27 AND last_seen GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(last_seen,1,4) BETWEEN '0001' AND '9999' AND substr(last_seen,12,2) BETWEEN '00' AND '23' AND substr(last_seen,15,2) BETWEEN '00' AND '59' AND substr(last_seen,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(last_seen,1,10),'+0 days')=substr(last_seen,1,10),0))),
        min_order_qty TEXT NULL CHECK (min_order_qty IS NULL OR (typeof(min_order_qty)='text' AND length(min_order_qty)>0 AND min_order_qty NOT GLOB '*[^0-9.]*' AND min_order_qty NOT LIKE '%.%.%' AND min_order_qty NOT LIKE '.%' AND min_order_qty NOT LIKE '%.' AND (length(min_order_qty)=1 OR substr(min_order_qty,1,1)!='0' OR substr(min_order_qty,2,1)='.') AND length(substr(min_order_qty,1,instr(min_order_qty||'.','.')-1))<=20 AND CASE WHEN instr(min_order_qty,'.')=0 THEN 0 ELSE length(min_order_qty)-instr(min_order_qty,'.') END<=18)), min_order_value TEXT NULL CHECK (min_order_value IS NULL OR (typeof(min_order_value)='text' AND length(min_order_value)>0 AND min_order_value NOT GLOB '*[^0-9.]*' AND min_order_value NOT LIKE '%.%.%' AND min_order_value NOT LIKE '.%' AND min_order_value NOT LIKE '%.' AND (length(min_order_value)=1 OR substr(min_order_value,1,1)!='0' OR substr(min_order_value,2,1)='.') AND length(substr(min_order_value,1,instr(min_order_value||'.','.')-1))<=20 AND CASE WHEN instr(min_order_value,'.')=0 THEN 0 ELSE length(min_order_value)-instr(min_order_value,'.') END<=18)),
        qty_precision INTEGER NULL CHECK (qty_precision BETWEEN 0 AND 18),
        price_precision INTEGER NULL CHECK (price_precision BETWEEN 0 AND 18),
        CHECK (pair_id=base_asset||'-'||quote_asset), CHECK (last_seen>=first_seen)
    ) STRICT;

CREATE TABLE collection_status (
        pair_id TEXT PRIMARY KEY NOT NULL REFERENCES assets(pair_id) ON DELETE RESTRICT,
        readiness TEXT NOT NULL CHECK (readiness IN ('ready','not_ready','unknown')),
        operational_status TEXT NOT NULL CHECK (operational_status IN ('idle','collecting','paused','error','unknown')),
        updated_at TEXT NOT NULL CHECK (updated_at IS NULL OR (length(updated_at)=27 AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(updated_at,1,4) BETWEEN '0001' AND '9999' AND substr(updated_at,12,2) BETWEEN '00' AND '23' AND substr(updated_at,15,2) BETWEEN '00' AND '59' AND substr(updated_at,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(updated_at,1,10),'+0 days')=substr(updated_at,1,10),0))), reason TEXT NULL
    ) STRICT;

CREATE TABLE data_quality_issues (
        issue_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(issue_id))>0),
        pair_id TEXT NOT NULL REFERENCES assets(pair_id) ON DELETE RESTRICT,
        dataset TEXT NOT NULL CHECK (dataset IN ('prices_1m','quotes_1m','orderbook','costs_1m')),
        observation_at TEXT NULL CHECK (observation_at IS NULL OR (length(observation_at)=27 AND observation_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(observation_at,1,4) BETWEEN '0001' AND '9999' AND substr(observation_at,12,2) BETWEEN '00' AND '23' AND substr(observation_at,15,2) BETWEEN '00' AND '59' AND substr(observation_at,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(observation_at,1,10),'+0 days')=substr(observation_at,1,10),0))),
        period_start TEXT NULL CHECK (period_start IS NULL OR (length(period_start)=27 AND period_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(period_start,1,4) BETWEEN '0001' AND '9999' AND substr(period_start,12,2) BETWEEN '00' AND '23' AND substr(period_start,15,2) BETWEEN '00' AND '59' AND substr(period_start,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(period_start,1,10),'+0 days')=substr(period_start,1,10),0))), period_end TEXT NULL CHECK (period_end IS NULL OR (length(period_end)=27 AND period_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(period_end,1,4) BETWEEN '0001' AND '9999' AND substr(period_end,12,2) BETWEEN '00' AND '23' AND substr(period_end,15,2) BETWEEN '00' AND '59' AND substr(period_end,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(period_end,1,10),'+0 days')=substr(period_end,1,10),0))),
        issue_type TEXT NOT NULL CHECK (length(trim(issue_type))>0),
        description TEXT NOT NULL CHECK (length(trim(description))>0),
        detected_at TEXT NOT NULL CHECK (detected_at IS NULL OR (length(detected_at)=27 AND detected_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(detected_at,1,4) BETWEEN '0001' AND '9999' AND substr(detected_at,12,2) BETWEEN '00' AND '23' AND substr(detected_at,15,2) BETWEEN '00' AND '59' AND substr(detected_at,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(detected_at,1,10),'+0 days')=substr(detected_at,1,10),0))),
        resolution_status TEXT NOT NULL CHECK (resolution_status IN ('open','resolved','ignored')),
        CHECK ((observation_at IS NOT NULL AND period_start IS NULL AND period_end IS NULL) OR
            (observation_at IS NULL AND period_start IS NOT NULL AND period_end IS NOT NULL AND period_start<=period_end))
    ) STRICT;

CREATE TABLE eligibility (
        pair_id TEXT NOT NULL REFERENCES assets(pair_id) ON DELETE RESTRICT,
        screened_at TEXT NOT NULL CHECK (screened_at IS NULL OR (length(screened_at)=27 AND screened_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(screened_at,1,4) BETWEEN '0001' AND '9999' AND substr(screened_at,12,2) BETWEEN '00' AND '23' AND substr(screened_at,15,2) BETWEEN '00' AND '59' AND substr(screened_at,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(screened_at,1,10),'+0 days')=substr(screened_at,1,10),0))),
        volume_24h_aud TEXT NULL CHECK (volume_24h_aud IS NULL OR (typeof(volume_24h_aud)='text' AND length(volume_24h_aud)>0 AND volume_24h_aud NOT GLOB '*[^0-9.]*' AND volume_24h_aud NOT LIKE '%.%.%' AND volume_24h_aud NOT LIKE '.%' AND volume_24h_aud NOT LIKE '%.' AND (length(volume_24h_aud)=1 OR substr(volume_24h_aud,1,1)!='0' OR substr(volume_24h_aud,2,1)='.') AND length(substr(volume_24h_aud,1,instr(volume_24h_aud||'.','.')-1))<=20 AND CASE WHEN instr(volume_24h_aud,'.')=0 THEN 0 ELSE length(volume_24h_aud)-instr(volume_24h_aud,'.') END<=18)), avg_spread_pct TEXT NULL CHECK (avg_spread_pct IS NULL OR (typeof(avg_spread_pct)='text' AND length(avg_spread_pct)>0 AND avg_spread_pct NOT GLOB '*[^0-9.]*' AND avg_spread_pct NOT LIKE '%.%.%' AND avg_spread_pct NOT LIKE '.%' AND avg_spread_pct NOT LIKE '%.' AND (length(avg_spread_pct)=1 OR substr(avg_spread_pct,1,1)!='0' OR substr(avg_spread_pct,2,1)='.') AND length(substr(avg_spread_pct,1,instr(avg_spread_pct||'.','.')-1))<=20 AND CASE WHEN instr(avg_spread_pct,'.')=0 THEN 0 ELSE length(avg_spread_pct)-instr(avg_spread_pct,'.') END<=18)),
        depth_aud TEXT NULL CHECK (depth_aud IS NULL OR (typeof(depth_aud)='text' AND length(depth_aud)>0 AND depth_aud NOT GLOB '*[^0-9.]*' AND depth_aud NOT LIKE '%.%.%' AND depth_aud NOT LIKE '.%' AND depth_aud NOT LIKE '%.' AND (length(depth_aud)=1 OR substr(depth_aud,1,1)!='0' OR substr(depth_aud,2,1)='.') AND length(substr(depth_aud,1,instr(depth_aud||'.','.')-1))<=20 AND CASE WHEN instr(depth_aud,'.')=0 THEN 0 ELSE length(depth_aud)-instr(depth_aud,'.') END<=18)),
        historical_days INTEGER NULL CHECK (historical_days>=0),
        completeness_pct TEXT NULL CHECK (completeness_pct IS NULL OR (typeof(completeness_pct)='text' AND length(completeness_pct)>0 AND completeness_pct NOT GLOB '*[^0-9.]*' AND completeness_pct NOT LIKE '%.%.%' AND completeness_pct NOT LIKE '.%' AND completeness_pct NOT LIKE '%.' AND (length(completeness_pct)=1 OR substr(completeness_pct,1,1)!='0' OR substr(completeness_pct,2,1)='.') AND length(substr(completeness_pct,1,instr(completeness_pct||'.','.')-1))<=20 AND CASE WHEN instr(completeness_pct,'.')=0 THEN 0 ELSE length(completeness_pct)-instr(completeness_pct,'.') END<=18)),
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
    ) STRICT;

CREATE TABLE ingestion_state (
        pair_id TEXT NOT NULL REFERENCES assets(pair_id) ON DELETE RESTRICT,
        dataset TEXT NOT NULL CHECK (dataset IN ('prices_1m','quotes_1m','orderbook','costs_1m')),
        latest_observation_at TEXT NULL CHECK (latest_observation_at IS NULL OR (length(latest_observation_at)=27 AND latest_observation_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(latest_observation_at,1,4) BETWEEN '0001' AND '9999' AND substr(latest_observation_at,12,2) BETWEEN '00' AND '23' AND substr(latest_observation_at,15,2) BETWEEN '00' AND '59' AND substr(latest_observation_at,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(latest_observation_at,1,10),'+0 days')=substr(latest_observation_at,1,10),0))), updated_at TEXT NOT NULL CHECK (updated_at IS NULL OR (length(updated_at)=27 AND updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z' AND substr(updated_at,1,4) BETWEEN '0001' AND '9999' AND substr(updated_at,12,2) BETWEEN '00' AND '23' AND substr(updated_at,15,2) BETWEEN '00' AND '59' AND substr(updated_at,18,2) BETWEEN '00' AND '59' AND coalesce(date(substr(updated_at,1,10),'+0 days')=substr(updated_at,1,10),0))),
        PRIMARY KEY (pair_id,dataset),
        CHECK (latest_observation_at IS NULL OR latest_observation_at<=updated_at)
    ) STRICT;

CREATE INDEX assets_status_idx ON assets(status);

CREATE INDEX collection_operational_idx ON collection_status(operational_status,updated_at);

CREATE INDEX eligibility_time_idx ON eligibility(screened_at,pair_id);

CREATE INDEX ingestion_updated_idx ON ingestion_state(dataset,updated_at);

CREATE INDEX quality_pair_idx ON data_quality_issues(pair_id,dataset,observation_at,period_start);

CREATE INDEX quality_resolution_idx ON data_quality_issues(resolution_status,detected_at);
```

## Local test receipt

Focused command: `python -m pytest -q tests/test_metadata_store.py tests/test_parquet_store.py tests/test_storage_integration.py`: **95 passed, 1 skipped**. Full regression command: `python -m pytest -q tests`: **532 passed, 2 skipped**. Storage integration file contains 3 passing tests. Skips: public connectivity is opt-in; real symlink creation needs Windows privilege. Linux CI exercises the real symlink case. `python -m examples.db_m02_storage` verified all four temporary datasets, exact decimals and checkpoints. Explicit permanent initialization twice and `integrity_check` succeeded; all five tables remain empty. Module compilation passed.
