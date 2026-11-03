# Database Milestone 10: research access and reproducible exports

## Purpose and boundary

This milestone exposes existing, verified raw data to Python. It adds no financial algorithms, strategies, statistical models, signals, backtesting, cost estimates or trading. All ten cost/eligibility mathematical placeholders remain reserved for user implementation and approval. Collection health and data availability do not establish profitability or investment suitability.

PR #10 was verified merged into `review-base/milestone1-20261029` at `614535b6f2fb8c258afe327ea3feff39873b0b68`, and its ancestry was verified before creating `feature/db-m10-research-exports`. Existing source modules, notebooks, credentials and research configurations remain unchanged.

## Notebook interface

Use the existing Python 3.11 database environment. From the repository root:

```python
from pathlib import Path
from datetime import datetime, timedelta, timezone
from kraken_quant.research_data import ResearchData

reader = ResearchData(Path('data'))
end = reader.as_of
start = end - timedelta(days=7)
prices = reader.load_prices('BTC-AUD', start, end)
quotes = reader.load_quotes('BTC-AUD', start, end)
levels = reader.load_orderbooks('BTC-AUD', start, end)
fees = reader.load_fee_schedules('BTC-AUD')
metadata = reader.load_market_metadata('BTC-AUD')
quality = reader.load_quality_report('BTC-AUD', start, end)
print(prices.head(), prices.attrs)
print(quality.datasets['prices_1m']['coverage'])
```

A notebook launched inside `notebooks/` must make the repository root importable and bind its actual data directory explicitly, for example `sys.path.insert(0, str(Path.cwd().parent))` and `ResearchData(Path.cwd().parent / 'data')`. No notebook or kernel configuration was changed. Avoid reinstalling the legacy package's unrelated dependency set merely to run this interface.

Module-level equivalents accept `data_dir=` and `as_of=`:

```python
from kraken_quant.research_data import load_prices
prices = load_prices('BTC-AUD', start, end, data_dir='data', as_of=end)
```

| Method | Return and interpretation |
|---|---|
| load_prices(pair,start,end) | DataFrame of verified completed candles; original schema fields plus provenance |
| load_quotes(pair,start,end) | DataFrame of physical best-quote observations, with actual timestamps and nullable sizes |
| load_orderbooks(pair,start,end) | DataFrame retaining every bid/ask level in each selected complete snapshot |
| load_fee_schedules(pair,start=None,end=None) | DataFrame of full source tier rows across retained versions; range selects retrieval time |
| load_market_metadata(pair,as_of=None) | One-row receipt-backed asset DataFrame, or explicit historical uncertainty |
| load_quality_report(pair,start,end) | Frozen `QualityReport` object with UTC bounds, inventory, source-verified requested counts, coverage and issue references |

### Precision, ranges and uncertainty

- Monetary and quantity fields remain Python `Decimal`; no float conversion or financial transformation occurs. Nullable integer fields use Pandas `Int64`; timestamps are UTC-aware Pandas datetimes.
- All query ranges are **[start,end)**. Inputs must be timezone-aware; offsets normalize to UTC. Submicrosecond boundaries are rejected rather than silently rounded beyond storage precision. A request is bounded to 3660 days. Future knowledge cutoffs and query ends later than the cutoff are rejected.
- `as_of` is receipt knowledge time, separate from event time. Defaults use actual current UTC. An old event-time query without an old `as_of` means historical observations available now, not data known then.
- Metadata status is last observed status, not a reconstructed status transition. `trading_status_as_of` stays unknown between observed instants. Current metadata is never substituted for missing historical evidence.
- Price coverage uses completed, minute-aligned intervals inside the requested range. Quote/book timestamps are never rounded. Quote/book sampling is not minute-candle completeness.
- Fee retrieval and effective dates are separate. Unknown effective dates, account applicability and tiers remain explicit; no tier/category is chosen and missing rates never become zero.
- Quality reports label current physical inventory separately from source-verified requested observations. Future source-availability probes and unversioned checkpoint updates are not applied retrospectively.

### Provenance and failure behavior

The API reuses `MonthlyParquetStore`, approved candle/depth parsers, historical receipt identity validation, existing fee views and M7 coverage. Selected records must match original source payloads exactly. Complete books retain both sides and all received levels. No filling, resampling, aggregation, historical snapshot reconstruction or synthetic rows occur.

DataFrames retain `source_id`, canonical JSON `source_sha256`, source receipt time, endpoint, timestamp semantics and namespace. `query_quality_issue_ids` references issues affecting the query window, not an assertion that every returned record is bad. Attributes include units, source references, source warnings, range exclusions, latest observation and known-time operational checkpoint information.

Invalid schemas, duplicate/conflicting keys, incomplete candles, broken snapshots or missing/mismatched selected provenance raise `ResearchDataError` with a structured `.report`. Structural request errors raise `ValueError`. Invalid selected records are never silently discarded. Empty valid selections return typed empty frames marked unavailable. A short window with no completed minute has no candle-coverage percentage.

Receipt parsing uses a bounded, per-pair cache. Source bytes are rehashed before reuse, and metadata dependencies are rechecked. Returned mutable reference dictionaries do not mutate reader caches. All reader methods remain read-only; returned DataFrames can be edited without changing storage.

## Commands

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.research_export populate --all-aud
.venv-db/Scripts/python.exe -m kraken_quant.research_export validate --all-aud
.venv-db/Scripts/python.exe -m kraken_quant.research_export export --all-aud --format csv
.venv-db/Scripts/python.exe -m kraken_quant.research_export export --pair BTC-AUD --format csv
.venv-db/Scripts/python.exe -m kraken_quant.research_export report --all-aud
.venv-db/Scripts/python.exe -m kraken_quant.research_export verify --export-id <export-id>
.venv-db/Scripts/python.exe examples/research_data_quickstart.py
```

`--root`/`--data-dir` reuse established storage configuration. Export supports `--start`, `--end`, `--as-of`; report supports `--as-of` and explicitly assesses a trailing 90-day completed-minute window. Defaults export all available selected observations, beginning at their actual earliest record. For large collections, run pair-specific exports into separate immutable directories instead of building an unnecessarily large combined file.

Only `populate` can access Kraken. It calls the existing public-only pipeline for one bounded cycle, explicitly makes all enabled refresh tasks due, captures before/after counts and leaves no continuous process running. Other commands operate offline. `validate` audits all five SQLite tables, integrity/foreign keys, all selected market schemas/keys/checkpoints/provenance, global empty cost storage, retained M6 manifests, M8 evidence and the ten unimplemented method boundaries. An integrity PASS does not imply fresh observations or sufficient research history.

The quickstart prints original BTC/AUD candles, quotes, complete book levels, fee tiers and missing-data information. It prepares raw inputs through the existing M6 interface and catches the expected `NotImplementedError`; it calculates no costs and writes no data.

## CSV export contract

Output: `data/research_exports/<export-id>/`. All files are UTF-8 with LF line endings, deterministic row/column ordering and `\N` as the explicit null representation. Decimal text preserves stored precision. UTC timestamps use six fractional digits and `Z`. Read raw CSV as strings before deliberate type conversion; the Python API already supplies exact typed values.

| File | Contents and units |
|---|---|
| assets.csv | Canonical/native asset identities, normalized assets, observed status/times, decimal minimums, precisions and original catalogue reference |
| prices_1m.csv | Original candle schema; prices in quote/base, base volume, integer trades, completeness and source/quality references |
| quotes_1m.csv | Original observation schema; quote/base prices and base-asset sizes, actual UTC observation and source references |
| orderbook_levels.csv | Original pair/timestamp/side/level/price/quantity rows; snapshot counts remain separate from level counts |
| fee_schedules.csv | Full retained maker/taker tier structures, condition JSON, threshold currency, source/version/times, exact percentage points and explicit account uncertainty |
| all_markets.csv | Original M8 per-market decisions, dimensions, filter evidence, screening ID and screening as-of |
| eligible.csv | Only original verified ELIGIBLE decisions; header-only when none passed all ten filters |
| rejected.csv | Original confirmed REJECTED decisions |
| pending_evaluation.csv | Original reserved-filter cases; may overlap rejected, matching M8's diagnostic semantics |
| data_quality.csv | Relevant existing issue IDs/periods/types; current resolution is labelled unversioned, not reconstructed historically |
| coverage.csv | Factual availability, 90-day completeness/gaps, continuous intervals, snapshot counts, fee uncertainty and operational freshness |
| manifest.json | Selection/cutoff, schemas/units, file counts/hashes, source dependencies, original screening reference, warnings and unavailable cost-engine status |

No second screening system is introduced. M8 decisions retain their original evaluation date; new observations do not silently change an earlier decision. The CSV categories are diagnostic and can overlap. Public fee tiers are retained even when their account applicability is unknown. Private account payloads, account identifiers, volume histories and credentials are not copied into exports.

The export ID is the SHA-256 of its canonical manifest body, including file hashes and selection. Files are staged, flushed and published as one completed directory. An identical repeat reuses the directory without rewriting it; conflicts fail without overwriting user content. Interrupted staging cannot corrupt a prior export. Only newly created staging files are cleaned up.

Verification checks the manifest identity, file schemas/counts/hashes, original JSON dependencies and exact selected current Parquet values. It pins original record keys and sources: later genuine appends do not invalidate an earlier export. Missing or changed dependencies fail clearly. No cross-engine global snapshot atomicity is claimed; the manifest describes the exact exported selection. No calculated costs are exported: `costs_1m` must remain empty, and its unavailable algorithm state is recorded instead.

## Actual validation and availability

Actual public-only population cycle `3cc8f86e10514d9db50ea734652737a7` started **2026-11-03T08:47:08.397653Z** and finished **2026-11-03T08:48:43.416416Z**, taking **98.453 seconds**. All five enabled tasks passed; eligibility stayed disabled. Public requests: **40**; API errors/throttles: **0**. No continuous collection remains running.

Actual before/after observations (books count snapshots, not price levels):

| Pair | Candles before ? after | Quotes before ? after | Book snapshots before ? after | Final book-level rows |
|---|---:|---:|---:|---:|
| ADA-AUD | 1639 ? 1674 | 15 ? 16 | 15 ? 16 | 320 |
| AUDX-AUD | 722 ? 722 | 15 ? 16 | 15 ? 16 | 166 |
| BCH-AUD | 722 ? 722 | 15 ? 16 | 15 ? 16 | 320 |
| BTC-AUD | 1639 ? 1674 | 15 ? 16 | 15 ? 16 | 320 |
| DOGE-AUD | 1639 ? 1674 | 15 ? 16 | 15 ? 16 | 320 |
| ETH-AUD | 1639 ? 1674 | 15 ? 16 | 15 ? 16 | 320 |
| EUR-AUD | 1638 ? 1673 | 15 ? 16 | 15 ? 16 | 320 |
| LINK-AUD | 1638 ? 1673 | 15 ? 16 | 15 ? 16 | 320 |
| LTC-AUD | 1639 ? 1673 | 14 ? 15 | 14 ? 15 | 300 |
| PEPE-AUD | 1638 ? 1673 | 14 ? 15 | 14 ? 15 | 300 |
| SOL-AUD | 1639 ? 1673 | 14 ? 15 | 14 ? 15 | 300 |
| USDC-AUD | 1639 ? 1673 | 13 ? 14 | 13 ? 14 | 280 |
| USDT-AUD | 1639 ? 1673 | 13 ? 14 | 13 ? 14 | 280 |
| XRP-AUD | 1639 ? 1673 | 13 ? 14 | 13 ? 14 | 280 |

All **25184 original market records** retain exact hashes. All original research/notebook/credential and implementation files are unchanged; authorized changes are only population appends and operational metadata/checkpoint state. AUDX/BCH supplied no new completed candles; their existing history is preserved without synthetic replacements.

SQLite validation, all five tables:

| Table | Actual rows |
|---|---:|
| assets | 1465 |
| eligibility | 14 |
| ingestion_state | 42 |
| collection_status | 14 |
| data_quality_issues | 42 |

Integrity: **ok**; foreign-key violations: **0**. All selected market rows passed schema, logical-key and exact source verification; **42 checkpoints** match persisted dataset maxima. Cost rows: **0**. M6 manifests reloaded: **25**. Original M8 reports reproducible: **2**. All ten mathematical boundaries raise `NotImplementedError`. The real quickstart returned meaningful raw inspection output, reported stale cost inputs/account-tier uncertainty, caught the reserved algorithm exception and left source storage unchanged.

Export ID: `d04de64d903eaecfc59af00b1a7dea22addc8f306978116148ad66fb4f58a068`. Selection cutoff: **2026-11-03T09:41:34.474987Z**. Directory: `data/research_exports/d04de64d903eaecfc59af00b1a7dea22addc8f306978116148ad66fb4f58a068/`. All eleven CSVs plus `manifest.json` exist. Identical re-execution produced identical IDs/files, and dependency verification passed for **317 original sources**. Generated data and manifests are ignored by Git.

| CSV | Actual rows | SHA-256 |
|---|---:|---|
| assets.csv | 14 | `e19b16dddfb55766e7044cfdf36a90abbe4dc46e0ee44d79c2394ccd758d9bc9` |
| prices_1m.csv | 21524 | `d896f41e2ad84e77dbf64a68352010439a0453024ec357e400f5f03e84babddc` |
| quotes_1m.csv | 215 | `440cd04bc1f839448931c827e0b8e6f81241a6eb6e97d220a9680cffaa0e48d8` |
| orderbook_levels.csv | 4146 | `b8e5c96724d95295b8f8941eddeff192fbf25c421b7a158e68da29d34c92ef65` |
| fee_schedules.csv | 4928 | `897e0d225ee860ffd4b8b37e455185aea4ae9799f1a86d690e2234c2ac8203f6` |
| all_markets.csv | 14 | `10c7bb9c6946491eba7802a5e2a898b86658a5884b78c06ec416c1321b76288c` |
| eligible.csv | 0 | `9be6db50b0199a9648d4fce0d928a1173efc70063f4ba587cde3ade5b03b3a37` |
| rejected.csv | 14 | `10c7bb9c6946491eba7802a5e2a898b86658a5884b78c06ec416c1321b76288c` |
| pending_evaluation.csv | 14 | `10c7bb9c6946491eba7802a5e2a898b86658a5884b78c06ec416c1321b76288c` |
| data_quality.csv | 24 | `c97fd865cec57b89c43230abb28252ae225844d4d6d3fd99fbac6f7609dd7946` |
| coverage.csv | 14 | `fe12d862a011b922ea7227d9785fc8e01104480e90ab705cf755792c1c39147a` |

Availability at the export cutoff (90-day completeness is a factual minute count, not a model):

| Pair | First price UTC | Last price UTC | Candles | Span days | 90-day completeness % | Longest continuous minutes | Quotes | Books |
|---|---|---|---:|---:|---:|---:|---:|---:|
| ADA-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1674 | 2.513888889 | 1.291666667 | 952 | 16 | 16 |
| AUDX-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 0.501388889 | 0.557098765 | 722 | 16 | 16 |
| BCH-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 0.501388889 | 0.557098765 | 722 | 16 | 16 |
| BTC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1674 | 2.513888889 | 1.291666667 | 952 | 16 | 16 |
| DOGE-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1674 | 2.513888889 | 1.291666667 | 952 | 16 | 16 |
| ETH-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1674 | 2.513888889 | 1.291666667 | 952 | 16 | 16 |
| EUR-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1673 | 2.513888889 | 1.290895062 | 951 | 16 | 16 |
| LINK-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1673 | 2.513888889 | 1.290895062 | 951 | 16 | 16 |
| LTC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1673 | 2.513888889 | 1.290895062 | 951 | 15 | 15 |
| PEPE-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1673 | 2.513888889 | 1.290895062 | 951 | 15 | 15 |
| SOL-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1673 | 2.513888889 | 1.290895062 | 951 | 15 | 15 |
| USDC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1673 | 2.513888889 | 1.290895062 | 951 | 14 | 14 |
| USDT-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1673 | 2.513888889 | 1.290895062 | 951 | 14 | 14 |
| XRP-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T08:46:00.000000Z | 1673 | 2.513888889 | 1.290895062 | 951 | 14 | 14 |

All fourteen markets have public fee sources but unknown account applicability/effective historical dates. At the export cutoff their stopped-session market observations are delayed; no stale input is presented as live. Exact missing intervals, classifications and snapshot ages are retained in `coverage.csv` and `manifest.json`. No market has a complete 90-day minute dataset. All original M8 decisions remain REJECTED, while pending mathematical-filter diagnostics also include all fourteen. `eligible.csv` contains only its header.

Final focused suite: **30 passed**. Complete offline regression: **886 passed, 7 skipped**. Skips are opt-in integrations and platform-dependent symlink support. Actual final audit, export/repeat/verification, original-record preservation and quickstart checks all passed. Archive content/pair coverage remains unverified under the no-download instruction; the following proposal is separate from this delivery.


## Historical acquisition: investigation, not import

Kraken's [official OHLCVT page](https://support.kraken.com/au/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data) describes headerless CSV rows `timestamp,open,high,low,close,volume,trades`, including one-minute data. It says only traded intervals are present and describes a full history through June 2026 plus quarterly updates. Its general pair-coverage statement is not an independently verified list of these 14 AUD files. Public documentation does not establish each file's exact bounds or archive epoch/timezone convention. Those remain unverified without the internal manifest/samples. No zero-filled interpretation is imported into current storage.

HEAD-only checks on the official links returned HTTP 200: `Kraken_OHLCVT_2026Q3.zip` is **576348633 bytes**, last modified **2026-10-16**; full part00 is **2097152000 bytes**, last modified **2026-10-06**. **Zero archive-body bytes were downloaded.** No payment was requested by these links; commercial reuse/redistribution rights were not established. Confirm applicable terms before broader use. Locale pages expose differing update dates/names, so approval must pin the exact artifact URL, build and checksum.

### Separately approved post-M10 proposal

1. Approve the historical window, artifact download, disk budget and use rights. Existing locked research splits remain unchanged. A 2026Q3 archive lies in the locked test period: inspect data integrity only, not strategy performance or parameter selection. Prefer an approved training-window dataset for initial mathematical experiments; that may require the larger full archive rather than the smaller quarterly file.
2. Inspect the build manifest and actual CSV names for all 14 AUD identities. Confirm exact first/last timestamps, epoch units/UTC opening semantics, traded-interval sparsity, decimals, trade counts and corrupt/duplicate records. Report absent or newly listed markets instead of inventing history.
3. Selectively extract only approved AUD one-minute files. Preserve original downloaded checksums, build metadata and row provenance. Current REST-only source receipts cannot be bypassed: approve a minimal archive-provider/receipt adapter using the existing historical-provider interface and storage, with migration review only if genuinely necessary.
4. Reject conflicting existing candles, preserve current observations and checkpoints, and verify overlaps where available. Do not create synthetic no-trade candles or change F08 completeness semantics without separate user approval.
5. Produce a bounded import plan and acceptance report before publishing imported data for research. OHLCVT archives do not establish historical bid/ask or order-book availability.

If official candles do not cover the approved period/pairs, Kraken's [official historical time-and-sales source](https://support.kraken.com/in/articles/360047543791-downloadable-historical-market-data-time-and-sales-) and [Trades API documentation](https://support.kraken.com/in/articles/advanced-api-faq) provide another same-exchange route to investigate. Trade data would require separately approved deterministic candle construction, larger request/storage budgets, duplicate/gap checks and terms review; it is not historical depth data. No alternative provider is integrated or purchased, and no trade aggregation is implemented here. A paid vendor is premature until the official artifact coverage is actually checked; any later quote must specify Kraken AUD pairs, dates, licensing, format, provenance and whether historical L2 snapshots exist.

## Tests and remaining limits

```powershell
.venv-db/Scripts/python.exe -m pytest -q tests/test_research_exports.py
.venv-db/Scripts/python.exe -m pytest -q tests
```

Focused tests cover read-only methods/commands, Decimal and UTC preservation, bounded/monthly queries, nulls, no historical substitution, future receipt rejection, complete snapshot identity, all fee tiers, private-payload exclusion, bad schemas/duplicates/incomplete records, source tampering, empty eligibility/cost safeguards, immutable repeat/conflict handling, failed exports, selected dependency verification after append and the quickstart's unimplemented algorithm boundary.

Current data supports inspection and user-approved short-window work with explicitly reported gaps. Sparse recent quotes/books do not support a historical execution-cost study. A complete 90-day dataset, approved mathematical algorithms and a separately approved validation/trading phase remain outstanding. This milestone adds no research performance result or live-trading capability.
