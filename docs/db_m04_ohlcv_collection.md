# Database Milestone 4: Kraken 1-minute OHLCV collection

## Scope and verified base

PR #4 was verified actually merged into review-base/milestone1-20261029 at `488292a9d7c62dd603407ee94f3468a03470d268` before branch creation. The updated base contains Database Milestones 1?3. This milestone only collects available completed 1-minute OHLCV. No order books, bid/ask collection, cost model, screening, continuous scheduler or live orders are implemented. The 383 unresolved catalogue records remain unchanged.

New module `ohlcv_collector` reuses `db_environment.load_config`, `asset_catalogue.catalogue_report` and its atomic JSON persistence helper, validated `metadata_store.connect_metadata`, `MonthlyParquetStore`, `storage_schema.table_from_records` and `operations.ProcessLock`. The existing public HTTP client is reused unchanged. `KrakenMarketData.bars()` retains its daily research/locked-date contract and is never called by this collector. No schema migration or dependency change is required.

## Universe and identity

The database-backed catalogue report selects quote_asset=AUD, status=online, last_seen equal to the latest consistent discovery observation. The collector also verifies the accepted discovery proof: canonical/native identities, online status, quote/base identity, matching last_seen, currency asset classes and international venue. Unresolved native IDs cannot enter selection. Stale/missing audit fingerprints or contradictory proof fail closed. No hardcoded research universe, expected market count or liquidity/history filter is used.

`--pair` must match this qualified catalogue set. `--all-aud` attempts every selected pair and returns individual terminal outcomes, including network failures and rate-limit deferrals. Reporting also retains previously collected AUD markets that later become ineligible, while marking their current eligibility. Catalogue refresh/classification is not performed. A missing/incompatible metadata database is never initialized or repaired automatically.

## Public endpoint, parsing and availability

The only collector request is GET `https://api.kraken.com/0/public/OHLC` with `interval=1`, `pair=<assets.kraken_pair_id>` and `assetVersion=1`. A validated checkpoint supplies an incremental `since` timestamp. For example, SQLite's actual native key XBTAUD is sent; BTC/AUD is the expected canonical response key. A response may alternatively use the exact stored native key. A different singleton pair, extra pair or missing pair is rejected; identity is never inferred merely from there being one result.

The [Kraken OHLC documentation](https://docs.kraken.com/api-reference/market-data/get-ohlc-data) describes a recent window limited to 720 entries and a final, always-present uncommitted entry; older history cannot be recovered by repeatedly changing since. Actual responses here contained 720 completed entries plus the final entry. The collector does not hardcode an expected row count or implement unlimited historical pagination. One bounded response is requested per selected pair per run; local history can grow beyond the REST window through repeated collection.

Kraken rows are `[time, open, high, low, close, vwap, volume, count]`. The optional missing/null count is retained as null. VWAP is not part of prices_1m and is not substituted for volume. API error envelopes, malformed structure, missing requested identity, invalid markers and request failures are reported distinctly.

| prices_1m field | Parsing and validation |
|---|---|
| pair_id | Verified canonical SQLite ID, never the response key alone |
| timestamp | Integer epoch seconds -> timezone-aware UTC; exact minute boundary |
| open | Exact decimal from source field 1; strictly positive |
| high | Exact decimal field 2; at least open, close and low |
| low | Exact decimal field 3; no greater than open, close and high |
| close | Exact decimal field 4; strictly positive |
| volume_base | Exact decimal field 6; nonnegative base-asset quantity |
| trade_count | Nonnegative integer field 7, or null if unavailable |
| is_complete | True only for validated prefix observations |

The final array entry is always discarded, even when its timestamp looks historical. Prefix observations must be no later than the most recently completed minute at response-validation time. Ordering, minute alignment and contradictory unfinished timestamps are checked. Malformed observations reject the whole pair batch; valid-looking rows are not persisted around an invalid row. Prices/volume never pass through binary floats; decimal128(38,18) and UTC microseconds are enforced by the existing schema. Identical source duplicates collapse; conflicting source or stored observations fail without overwrite.

### Continuation marker and pacing

`result.last` is validated and retained as a continuation hint, not a candle or proof of persistence. SQLite stores only the latest verified persisted candle time. After success, the next since is bounded by `min(saved_marker, checkpoint_epoch)` to overlap the last stored candle and avoid skipping unpersisted minutes. Missing/stale per-pair marker state falls back to the verified checkpoint. No checkpoint is advanced from last alone.

Requests are at least 1.1 seconds apart within a collector and across one-shot restarts using a persisted request-start time under the collector's process lock. [Kraken's public API guidance](https://support.kraken.com/articles/206548367-what-are-the-api-rate-limits-) recommends one request per second or less. A rate-limit response stops further HTTP requests for that run; remaining pairs receive explicit deferred_after_rate_limit outcomes. There are no unbounded retries. Other independent applications sharing the IP require their own coordination.

## Persistence, provenance and recovery

Layout remains `<data_dir>/market_data/<PAIR>/prices_1m/YYYY-MM.parquet`. Directories are created only when needed. The existing writer handles schema checks, logical keys, per-month process locks, checksums, fsync, temporary files and atomic replacement. Previously collected months are retained.

`ohlcv_collection/source.json` binds the prices dataset to kraken_public or injected_test. Injected test data cannot be mixed with a real namespace. Existing unmarked price partitions are refused rather than silently adopted. Synthetic tests use isolated temporary directories. This is a cooperative-writer provenance guard, not a sandbox against manual file tampering.

For a validated nonempty response:

1. Save the actual public response and an atomic pending intent containing exact normalized records, identity, interval, source and marker.
2. Preflight existing candles for conflicts; write through MonthlyParquetStore.
3. Reopen partitions and verify every intended observation equals the stored row exactly.
4. Commit ingestion_state.latest_observation_at = maximum verified completed timestamp, with a UTC updated_at.
5. Publish the per-pair success receipt and remove the pending intent.

On restart, pending intents are identity/source checked, revalidated and replayed before another poll. This completes a batch interrupted between monthly replacements, even if the REST window has subsequently moved. Exact retries are no-ops. After replay, checkpoint reconciliation compares SQL with validated Parquet. A missing/behind checkpoint can advance to verified stored data; an ahead-of-storage checkpoint is an integrity failure, never moved backwards. Existing data can be reverified before a new request; updated_at therefore records checkpoint verification/commit time, not necessarily a new candle.

SQLite and Parquet are separate engines: no distributed atomicity is claimed. A checkpoint failure leaves verified Parquet plus a replayable intent. A hard interruption releases the OS lock, preserves the intent and leaves the progressive run journal incomplete. Historical incomplete journals are evidence of interruption, not proof that a process remains alive. Conflicts remain blocked for review; no automatic correction/deletion policy is introduced.

Generated audit data lives under `ohlcv_collection/responses/`, `pending/`, `pairs/` and `runs/`. Pair state records latest outcome, cumulative invalid/error counts and the last successful continuation hint. Run journals record selected identities and completed outcomes progressively. All generated data, SQLite and Parquet files remain ignored by Git.

## Coverage semantics

The read-only report makes no API requests and performs no checkpoint updates. For each current or historically collected AUD market it reports earliest/latest completed candles, row count, inclusive expected minute slots, missing-minute ranges, stored duplicates, observed duplicate overlap, invalid observations, historical coverage days, errors and checkpoint status.

- A source-provided candle with count=0 and volume=0 is an explicit zero-trade observation and is retained.
- An absent minute between stored endpoints is unresolved_no_trade_or_missing_observation. No zero-volume bar is generated.
- The final uncommitted entry is counted separately and never persisted.
- Network/exchange failures are collection errors, not evidence of missing trades.
- Unavailable trade counts remain null.
- Trailing unobserved minutes after the last stored candle are reported separately from gaps inside observed coverage.

Coverage days = (latest_open - earliest_open + one minute)/86400. A checkpoint is a persisted high-water observation, not a promise of contiguous history. A source window that can no longer bridge a downtime gap is recorded by coverage, not repaired with invented data. Malformed partitions cause an explicit storage-validation error and unknown coverage counts.

## Commands

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.ohlcv_collector collect --pair BTC-AUD
.venv-db/Scripts/python.exe -m kraken_quant.ohlcv_collector collect --pair ETH-AUD
.venv-db/Scripts/python.exe -m kraken_quant.ohlcv_collector collect --all-aud
.venv-db/Scripts/python.exe -m kraken_quant.ohlcv_collector report
```

Established --root / --data-dir and .env KRAKEN_DATA_DIR configuration are supported. No API credential is required. Collect exits 0 when every selected pair succeeds, 1 for errors and 2 for unavailable completed history without a checkpoint. Report returns nonzero for inconsistent checkpoint/storage state. A valid empty coverage report is not a claim that collection succeeded.

## Actual Kraken collection audit: real observations

Actual catalogue snapshot: `e948f19df02a4f309385447167dc0b9e`; eligible universe observed: **14 AUD pairs**. Unresolved catalogue records remain **383**, with no rediscovery or classification changes. All four collect commands and the report command returned exit 0.

| Command | Pairs attempted | HTTP requests | New rows | Overlap duplicates | Uncommitted excluded |
|---|---:|---:|---:|---:|---:|
| btc | 1 | 1 | 720 | 0 | 1 |
| eth | 1 | 1 | 720 | 0 | 1 |
| all-first | 14 | 14 | 8640 | 2 | 14 |
| all-repeat | 14 | 14 | 28 | 14 | 14 |

Before the all-AUD repeat, storage held 10,080 rows. Afterward it held **10108**: exactly 28 new source candles, with 14 overlapping observations contributing no duplicate rows. Source verification reopened **30 actual public responses**, checked **10124 source-to-Parquet row equalities**, and confirmed exclusion of **30 uncommitted rows**.

### Required SQLite checkpoint query

```sql
SELECT pair_id, dataset, latest_observation_at, updated_at
FROM ingestion_state
WHERE dataset = 'prices_1m'
ORDER BY pair_id;
```

Actual output:

| pair_id | dataset | latest_observation_at | updated_at |
|---|---|---|---|
| ADA-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:41.225254Z |
| AUDX-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:42.402176Z |
| BCH-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:43.643165Z |
| BTC-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:44.691614Z |
| DOGE-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:45.711673Z |
| ETH-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:46.824282Z |
| EUR-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:48.002977Z |
| LINK-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:49.008642Z |
| LTC-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:50.127696Z |
| PEPE-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:51.241759Z |
| SOL-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:52.375886Z |
| USDC-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:53.596386Z |
| USDT-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:54.619767Z |
| XRP-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-01T08:29:55.697455Z |

SQLite integrity_check=ok; foreign_key_check=[]. Asset rows and catalogue audit fingerprints remain unchanged. All checkpoints match verified Parquet maxima.

### Per-pair captured coverage

| Pair | Earliest UTC candle | Latest UTC candle | Rows | Expected minutes | Missing | Duplicates | Invalid latest attempt | Checkpoint | Zero-trade source candles |
|---|---|---|---:|---:|---:|---:|---:|---|---:|
| ADA-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 721 |
| AUDX-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 722 |
| BCH-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 722 |
| BTC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 563 |
| DOGE-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 722 |
| ETH-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 685 |
| EUR-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 714 |
| LINK-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 715 |
| LTC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 709 |
| PEPE-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 721 |
| SOL-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 681 |
| USDC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 616 |
| USDT-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 425 |
| XRP-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 722 | 0 | 0 | 0 | matches | 699 |

Every pair had 0 collection errors and 0 trailing unobserved minutes at the captured report time. Historical coverage was 0.5013888888888889 days per pair. These are observations from a bounded one-shot run, not a continuing freshness guarantee. All pairs span October and November partitions; existing monthly storage was reused.

### Five actual persisted BTC/AUD and ETH/AUD observations

These are **real Kraken observations**, including exchange-provided zero-trade candles. They are not the synthetic fixtures used in automated tests. Values below omit insignificant trailing zeros for readability, using exact decimal formatting, not float conversion. Prices are AUD/base; volume is base units.

#### BTC-AUD

| UTC candle open | Open | High | Low | Close | Volume base | Trade count | Complete |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026-10-31 20:27:00+00:00 | 110279.3 | 110279.3 | 110279.3 | 110279.3 | 0.00181358 | 1 | True |
| 2026-10-31 20:28:00+00:00 | 110279.3 | 110279.3 | 110279.3 | 110279.3 | 0 | 0 | True |
| 2026-10-31 20:29:00+00:00 | 110279.3 | 110279.3 | 110279.3 | 110279.3 | 0 | 0 | True |
| 2026-10-31 20:30:00+00:00 | 110279.3 | 110279.3 | 110279.3 | 110279.3 | 0 | 0 | True |
| 2026-10-31 20:31:00+00:00 | 110279.3 | 110279.3 | 110279.3 | 110279.3 | 0 | 0 | True |
#### ETH-AUD

| UTC candle open | Open | High | Low | Close | Volume base | Trade count | Complete |
|---|---:|---:|---:|---:|---:|---:|---|
| 2026-10-31 20:27:00+00:00 | 3205.8 | 3205.8 | 3205.8 | 3205.8 | 0 | 0 | True |
| 2026-10-31 20:28:00+00:00 | 3205.8 | 3205.8 | 3205.8 | 3205.8 | 0 | 0 | True |
| 2026-10-31 20:29:00+00:00 | 3205.8 | 3205.8 | 3205.8 | 3205.8 | 0 | 0 | True |
| 2026-10-31 20:30:00+00:00 | 3205.8 | 3205.8 | 3205.8 | 3205.8 | 0 | 0 | True |
| 2026-10-31 20:31:00+00:00 | 3205.8 | 3205.8 | 3205.8 | 3205.8 | 0 | 0 | True |

## Automated tests and limitations

```powershell
.venv-db/Scripts/python.exe -m pytest -q tests/test_ohlcv_collector.py
.venv-db/Scripts/python.exe -m pytest -q tests
$env:KRAKEN_OHLCV_SMOKE = '1'
.venv-db/Scripts/python.exe -m pytest -q tests/test_ohlcv_collector.py::test_live_btc_aud
Remove-Item Env:KRAKEN_OHLCV_SMOKE
```

The opted-in integration test copies only existing public catalogue metadata/audit to a temporary database and fetches actual BTC/AUD OHLCV; it does not rediscover unresolved instruments. Offline tests use isolated injected namespaces and mocked API data. Cases cover audited selection, invalid identities/candles, exact decimals, final candles, duplicate conflicts, network/rate errors, empty history, checkpoint failure, hard interruption, month-boundary partial writes, restart replay, source mixing prevention and report-only behavior.

REST cannot supply unlimited older OHLC history, and manual one-shot collection does not provide production uptime. Missing intervals remain unresolved until source evidence can distinguish inactivity from data loss. Conflicting completed-candle revisions need review rather than automatic overwrite. Source markers trust cooperative local writers. No distributed transaction or automatic scheduler is claimed. The existing 383 unresolved catalogue records remain outside this collector. No future milestone is implemented.

## Local verification receipt

Focused command: `python -m pytest -q tests/test_ohlcv_collector.py`: **37 passed, 1 skipped**. Full offline command: `python -m pytest -q tests`: **611 passed, 4 skipped**. Opt-in live public BTC/AUD integration: **1 passed**. Offline skips: public environment connectivity, public catalogue integration, public OHLCV integration and privileged Windows symlink creation. Compilation passed. All 450 original user/research/runtime files checked are unchanged, excluding only authorized metadata.sqlite checkpoint updates. Catalogue assets/audit and all 383 unresolved records remain unchanged. No real datasets, generated receipts or secrets are included in the implementation commit.
