# Database M11: actual historical population and readiness

All financial, strategy, cost and eligibility mathematics remain reserved for the user. This milestone imports genuine source observations and performs data-integrity checks only.

## Sources actually acquired

The approved base includes merged PR #11, commit `13d6695c3609c888a9666c113fb2c41d3553ed7e`. Work is on `feature/db-m11-historical-population`.

* Official [Kraken OHLCVT archive](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data): five actual parts, reassembled ZIP **9,546,553,626 bytes**, SHA-256 **bc273e9027470b07d790a7fa4df9bb43f0dfc527c20c6ecae48402e123e7b1c7**, matching the published checksum. The internal manifest is schema 1, generated 2026-10-01, through 2026-09-30. All 14 exact AUD minute members were extracted and CRC-checked. The full archive, original parts, extracted source CSVs and download receipts remain local and ignored by Git.
* Official Q3 OHLCVT ZIP was also downloaded and inspected: **576,348,633 bytes**, SHA-256 **96daae63c7b3acb522892e1f602f9c564d1a3fb659089d9b0699282f30d41226**. It overlaps the full release, so it is retained for audit rather than imported a second time.
* Official [Kraken time-and-sales archive](https://support.kraken.com/articles/360047543791-downloadable-historical-market-data-time-and-sales-): actual AUD trade CSV members downloaded using supported public HTTP byte ranges across the 14-part archive. Each selected member passed ZIP CRC validation and has a pinned SHA-256. **The entire trade ZIP checksum was not verified** because unrelated markets were not downloaded. Original selected members, source manifest and range bytes are retained. Its schema-2 records preserve trade IDs, exact price and quantity strings, and timestamps.
* Kraken public `Trades`, `assetVersion=1`, native pair IDs, count 1000 and nanosecond `since` cursors: actual October-to-present pages retained immutably. This endpoint, unlike OHLC's approximately 720 recent candles, permits genuine trade-history replay. Requests are paced at least 1.2 seconds apart, bounded, and retried at most three times. A short cached tail older than the requested cutoff must be refreshed; it is never evidence for a newer window.
* [Tardis Kraken public sample](https://docs.tardis.dev/historical-data-details/kraken): actual unauthenticated `book_snapshot_5` CSV gzip observations for **2026-10-01**, all 14 AUD markets. Complete provided levels, up to five per side, are retained. Quotes are the actual best levels of the same snapshot, with shared provenance. The raw BTC/ETH quote streams were additionally downloaded and inspected. This is third-party capture of Kraken data, clearly distinguished from official REST observations.
* CryptoDataDownload was investigated; its Kraken OHLC download page reported unavailable. No other exchange, private credentials, purchase, or access bypass was used.

## Exact import and conflict policy

`historical_population.import_csv` streams monthly groups into the existing `MonthlyParquetStore`; its atomic replacement and writer locks remain authoritative. Import locks exclude competing imports for the same pair. Existing schemas, internal pair IDs, research/execution databases and collector bindings are unchanged. Explicit historical aliases are BTC to XBT and DOGE to XDG, backed by the existing verified Kraken catalogue; no arbitrary symbol substitutions classify assets.

Identical overlaps deduplicate. An existing validated observation wins a conflict. The incoming raw record and both values remain in immutable conflict evidence. No forward filling, interpolation, synthetic candles, volume rounding, or silent overwrites occur.

The official OHLCVT archive contains some serialized volume values exceeding decimal128(38,18). Such rows are quarantined, not rounded. Their candles are recovered from genuine official trades: first/highest/lowest/last trade prices, exact summed traded quantity, and trade count. This is deterministic source aggregation, **not** a transaction-cost, execution-price, VWAP, statistical or trading algorithm. The original defective values remain audit evidence. Recovery does not overwrite other valid observations.

Parquet persistence and round-trip verification precede the immutable monthly receipt, then SQLite checkpoint advancement. A crash between these steps replays safely. A missing or changed previously imported partition fails closed; a checkpoint never moves backwards. There is no cross-engine transaction claim.

Price validation and physical-key checks use equivalent Arrow operations to keep large-history reads practical. Required fields, exact decimal types, UTC minute boundaries, positive OHLC values, OHLC bounds, nonnegative volume/counts, pair/month isolation and duplicate rejection remain enforced. A validated 16,773-row BTC monthly partition read took 0.073 seconds locally. Source receipts and original monthly CSV dependencies live under `data/historical_imports/`. Actual archive/trade sources remain under ignored local data directories. Receipts record hashes, native identities, retrieval knowledge time, timestamp semantics, original member identity, conflict handling and dependency links. Historical observations are not backdated to imply collection at their event time.

## Coverage semantics and limitations

Kraken's archive contains **traded intervals only**. A missing archive minute is source-declared no-trade, not a fabricated zero-volume candle. The report separately exposes calendar-minute density, source availability, precision failures, unresolved invalid minutes, original source conflicts and actual continuous stored intervals. Actual zero-volume candles supplied by REST remain preserved.

The first genuine archived trade is used as the available market-history lower bound. We do not penalize a market for dates before this bound or invent a formal listing date. Current status, historical status changes, fee applicability and as-published data vintages are not inferred.

`PRICE_RESEARCH_READY` is deliberately scoped to retrospective, sparse traded-minute price studies with at least 365 observed-span days, verified archive-to-recent source continuity, and no unresolved invalid minutes or retained conflicts. This **does not certify 99% dense-minute data, M8 eligibility, or historical execution research**. Shorter or unresolved datasets are `LIMITED`/`NOT_READY`. Source precision problems remain reported even after exact-trade recovery resolves their missing candles.

Historical quotes/books cover one public sample day plus preserved recent observations, not years of continuous execution history. AUDX is genuinely shallow; absent levels are not manufactured. An official 2023 conditional trial announcement is retained as explicitly partial historical fee evidence (ADA/AUD and three announced volume tiers), never as a complete or account-applicable schedule. Current/public fee versions are preserved with retrieval times; historical effective schedules and account-specific historical applicability are unknown. No market is certified execution-research-ready. No calculated `costs_1m` records are created.

## Chronological partitions and Python access

The immutable readiness manifest records actual row hashes and chronological boundaries: floor(60%) training, floor(20%) validation, remainder untouched testing. No shuffling or overlapping timestamps. These are new AUD data partitions, separate from the existing locked USD research configuration, which remains unchanged. No strategy, performance or optimization has been evaluated.

```python
from kraken_quant.research_data import ResearchData
reader = ResearchData('data')
prices = reader.load_prices('BTC-AUD', '2025-01-01T00:00:00Z', '2025-02-01T00:00:00Z')
training = reader.load_partition('BTC-AUD', 'training', '<readiness-manifest-sha256>')
validation = reader.load_partition('BTC-AUD', 'validation', '<readiness-manifest-sha256>')
untouched_test = reader.load_partition('BTC-AUD', 'test', '<readiness-manifest-sha256>')
```

The API verifies exact source records, keeps Decimal values and UTC, honors receipt knowledge cutoffs, and fails if dependencies or pinned partition rows change. Large queries should use bounded month ranges. A current archive acquisition cannot certify an as-published historical vintage; metadata and fee limitations remain explicit.

## Reproduction commands

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.historical_population import-archive --archive data/historical_archives/kraken_official/Kraken_OHLCVT_Full.zip --expected-sha256 bc273e9027470b07d790a7fa4df9bb43f0dfc527c20c6ecae48402e123e7b1c7 --end 2026-10-01T00:00:00Z
.venv-db/Scripts/python.exe -m kraken_quant.historical_population trades --pair BTC-AUD --start 2026-10-01T00:00:00Z --end <completed-UTC-minute>
.venv-db/Scripts/python.exe -m kraken_quant.historical_population readiness
.venv-db/Scripts/python.exe -m kraken_quant.research_export export --all-aud --start 2026-09-01T00:00:00Z --end 2026-10-02T00:00:00Z
.venv-db/Scripts/python.exe -m pytest -q tests
```

`official_trade_archive.download_members`, `recover_precision` and `historical_execution.import_sample` expose the actual-source acquisition/recovery operations. No continuous collector is left running. Existing M6 input manifests, M8 screening evidence and M10 immutable exports are verified separately after append.

## Current database update - 5 November

The working database was updated again using a bounded public Trades replay and the existing public OHLC collector. All 14 markets were attempted successfully. Every prior November record was preserved; every other historical, quote and book partition remained byte-identical. Current November observations were independently loaded through the provenance-validating `ResearchData` API. The frozen 4 November 60/20/20 dataset remains unchanged; later observations are prospective additions outside that version.

| Dataset | Original | Current | Added |
|---|---:|---:|---:|
| Prices | 21,524 | 3,785,824 | 3,764,300 |
| Quotes | 215 | 18,996 | 18,781 |
| Book levels | 4,146 | 191,896 | 187,750 |
| Book snapshots | 215 | 18,996 | 18,781 |
| Calculated costs | 0 | 0 | 0 |

| Pair | Current last candle UTC | Candles | Calendar density % | Missing calendar minutes | Longest continuous minutes |
|---|---|---:|---:|---:|---:|
| ADA-AUD | 2026-11-05 04:51 | 114,296 | 3.756 | 2,929,093 | 952 |
| AUDX-AUD | 2026-11-04 06:41 | 1,963 | 0.426 | 458,331 | 722 |
| BCH-AUD | 2026-11-04 06:41 | 48,871 | 1.456 | 3,308,311 | 722 |
| BTC-AUD | 2026-11-05 04:51 | 835,270 | 24.871 | 2,523,192 | 955 |
| DOGE-AUD | 2026-11-05 04:51 | 38,953 | 3.065 | 1,231,851 | 952 |
| ETH-AUD | 2026-11-05 04:51 | 440,525 | 13.117 | 2,917,937 | 952 |
| EUR-AUD | 2026-11-05 04:51 | 127,900 | 3.808 | 3,230,562 | 951 |
| LINK-AUD | 2026-11-05 04:51 | 62,611 | 2.057 | 2,980,513 | 951 |
| LTC-AUD | 2026-11-05 04:51 | 150,502 | 4.482 | 3,207,432 | 951 |
| PEPE-AUD | 2026-11-05 04:51 | 12,343 | 1.211 | 1,006,623 | 951 |
| SOL-AUD | 2026-11-05 04:51 | 232,955 | 18.329 | 1,038,039 | 951 |
| USDC-AUD | 2026-11-05 04:51 | 353,681 | 11.621 | 2,689,792 | 951 |
| USDT-AUD | 2026-11-05 04:51 | 1,053,152 | 31.358 | 2,305,332 | 951 |
| XRP-AUD | 2026-11-05 04:51 | 312,802 | 9.314 | 3,045,660 | 951 |

AUDX and BCH returned no newer completed observations in this bounded refresh; their last genuine timestamps are reported without extrapolation. Source coverage and no-trade semantics remain separate from calendar density. No market has a certified dense 99% minute grid; sparse traded-minute price research is the stated readiness scope, not uniform-minute or execution-cost backtesting.

Current source audit: `data/historical_imports/readiness_updates/b80dacf24fb9c536316d5af223701a81da509af05de6ae52a5495b4b6a8d1d79.json` (**PASS**). This links the complete historical source report and new replay sources. Exact current coverage and incremental gap intervals are persisted under `data/historical_imports/coverage_updates/82a66238f3e469c112c58faa46e317855c926aaf5f5d59178ff973ab63e0ab23.json`. Both SQLite integrity and all 14 price-checkpoint checks pass; foreign-key violations are zero. All quotes and book levels were source-validated. All **25 M6 manifests** reload, both **M8 reports** reproduce, all reserved algorithms raise `NotImplementedError`, and calculated cost rows remain zero.

The official public fee schedule was refreshed on 5 November with `fees refresh --all-aud --public-only`, version `47d129d80391243e7c05ce85a685bdb0ef76956e85b89c2dee8838b406c7d66c`. Account retrieval was disabled. No historical effective schedule or account tier is inferred.

Large-data handling: date-bounded exports use the requested window for availability calculations. Frozen screening verification reads only its pinned months. Query-level quality metadata is shared rather than copied per candle. These preserve output values and avoid repeatedly materializing unrelated history. Unrestricted lifetime reports remain more expensive; sustained one-minute continuous cadence after this large population is not certified or activated.

## Frozen research dataset audit - 4 November

Snapshot knowledge time: `2026-11-04T06:56:37.861340Z`. All times below are UTC.

| Dataset | Before | After | Added |
|---|---:|---:|---:|
| Prices | 21,524 | 3,783,191 | 3,761,667 |
| Quotes | 215 | 18,996 | 18,781 |
| Book levels | 4,146 | 191,896 | 187,750 |
| Book snapshots | 215 | 18,996 | 18,781 |
| Calculated costs | 0 | 0 | 0 |

All 3,783,191 price observations have verified source matches. All 36,141 precision-defective archive rows were recovered exactly from official trades. Canonical conflicts, unresolved invalid minutes, duplicate archive timestamps and out-of-order archive rows: **zero**. All original **25,885 market records** retain their original value hashes; **174 protected files**, including notebooks, credentials, locked configuration and original export artifacts, remain byte-identical.

SQLite integrity: **ok**; foreign-key violations: **0**. All 14 price checkpoints equal the latest persisted observations. SQLite row counts: assets 1,465; eligibility 14; ingestion_state 42; collection_status 14; data_quality_issues 175. Of the issues, 133 preserve original source precision evidence; canonical recovery does not erase the original defective payloads.

| Pair | First price | Last price | Candles | Days | Calendar density % | Quotes / book snapshots | Price readiness |
|---|---|---|---:|---:|---:|---:|---|
| ADA-AUD | 2021-01-21 17:43 | 2026-11-04 06:41 | 114,237 | 2112.54 | 3.755 | 1456 | PRICE_RESEARCH_READY |
| AUDX-AUD | 2025-12-19 15:08 | 2026-11-04 06:41 | 1,963 | 319.65 | 0.426 | 82 | LIMITED |
| BCH-AUD | 2020-06-16 21:40 | 2026-11-04 06:41 | 48,871 | 2331.38 | 1.456 | 1456 | PRICE_RESEARCH_READY |
| BTC-AUD | 2020-06-16 22:30 | 2026-11-04 06:44 | 834,987 | 2331.34 | 24.872 | 1456 | PRICE_RESEARCH_READY |
| DOGE-AUD | 2024-06-05 16:48 | 2026-11-04 06:46 | 38,594 | 881.58 | 3.040 | 1456 | PRICE_RESEARCH_READY |
| ETH-AUD | 2020-06-16 22:30 | 2026-11-04 06:47 | 440,409 | 2331.35 | 13.119 | 1456 | PRICE_RESEARCH_READY |
| EUR-AUD | 2020-06-16 22:30 | 2026-11-04 06:48 | 127,509 | 2331.35 | 3.798 | 1454 | PRICE_RESEARCH_READY |
| LINK-AUD | 2021-01-21 22:08 | 2026-11-04 06:48 | 62,557 | 2112.36 | 2.057 | 1455 | PRICE_RESEARCH_READY |
| LTC-AUD | 2020-06-17 07:18 | 2026-11-04 06:49 | 150,427 | 2330.98 | 4.482 | 1455 | PRICE_RESEARCH_READY |
| PEPE-AUD | 2024-11-27 14:06 | 2026-11-04 06:49 | 12,147 | 706.70 | 1.194 | 1453 | PRICE_RESEARCH_READY |
| SOL-AUD | 2024-06-05 13:38 | 2026-11-04 06:49 | 232,760 | 881.72 | 18.332 | 1455 | PRICE_RESEARCH_READY |
| USDC-AUD | 2021-01-21 16:19 | 2026-11-04 06:50 | 353,447 | 2112.60 | 11.618 | 1454 | PRICE_RESEARCH_READY |
| USDT-AUD | 2020-06-16 22:08 | 2026-11-04 06:52 | 1,052,547 | 2331.36 | 31.352 | 1454 | PRICE_RESEARCH_READY |
| XRP-AUD | 2020-06-16 22:30 | 2026-11-04 06:54 | 312,736 | 2331.35 | 9.316 | 1454 | PRICE_RESEARCH_READY |

Calendar density is observed candles divided by expected calendar minutes within the actual first/last trading-history bounds. It is **not source completeness**: the official archive omits intervals with no trades. No dense 99% minute grid is claimed. All markets have verified price provenance; 13 meet the scoped one-year traded-minute price contract. AUDX began trading on 19 December 2025 and has only 319.65 genuine history days. PEPE began on 27 November 2024: its 706.70 days exceed one year but cannot yet reach the two-year target. Other markets have more than two years.

Execution readiness is **LIMITED for every market**. Actual historical samples cover 1 October 2026 plus preserved recent collection dates. Continuous historical quotes/books before and between those observations are absent. More public first-of-month samples exist, but they do not supply continuous execution history. Historical account-applicable fees remain unknown; the 2023 conditional trial evidence is partial and never treated as a complete historical schedule.

## Actual chronological partition ranges

Ranges are **[start, end-exclusive)**, UTC, based on actual observations. Counts are floor(60%), floor(20%), and the remaining 20%; gaps are not filled. AUDX partitions are available for constrained inspection but do not satisfy the one-year readiness requirement.

| Pair | Training range (rows) | Validation range (rows) | Untouched test range (rows) |
|---|---|---|---|
| ADA-AUD | 2021-01-21 17:43 to 2024-09-12 02:16 (68,542) | 2024-09-12 05:40 to 2025-06-23 11:30 (22,847) | 2025-06-23 11:38 to 2026-11-04 06:42 (22,848) |
| AUDX-AUD | 2025-12-19 15:08 to 2026-10-31 22:46 (1,177) | 2026-10-31 22:46 to 2026-11-01 05:18 (392) | 2026-11-01 05:18 to 2026-11-04 06:42 (394) |
| BCH-AUD | 2020-06-16 21:40 to 2023-12-28 13:05 (29,322) | 2023-12-28 13:35 to 2024-11-22 07:22 (9,774) | 2024-11-22 07:22 to 2026-11-04 06:42 (9,775) |
| BTC-AUD | 2020-06-16 22:30 to 2025-01-04 21:48 (500,992) | 2025-01-04 21:51 to 2025-12-04 05:03 (166,997) | 2025-12-04 05:04 to 2026-11-04 06:45 (166,998) |
| DOGE-AUD | 2024-06-05 16:48 to 2025-09-14 00:19 (23,156) | 2025-09-14 00:22 to 2026-03-20 16:10 (7,718) | 2026-03-20 18:38 to 2026-11-04 06:47 (7,720) |
| ETH-AUD | 2020-06-16 22:30 to 2025-03-11 13:09 (264,245) | 2025-03-11 13:09 to 2026-01-17 06:41 (88,081) | 2026-01-17 06:43 to 2026-11-04 06:48 (88,083) |
| EUR-AUD | 2020-06-16 22:30 to 2023-02-07 09:52 (76,505) | 2023-02-07 09:52 to 2024-12-18 22:19 (25,501) | 2024-12-18 22:33 to 2026-11-04 06:49 (25,503) |
| LINK-AUD | 2021-01-21 22:08 to 2024-10-03 09:06 (37,534) | 2024-10-03 09:20 to 2025-09-19 03:04 (12,511) | 2025-09-19 03:42 to 2026-11-04 06:49 (12,512) |
| LTC-AUD | 2020-06-17 07:18 to 2025-01-04 06:56 (90,256) | 2025-01-04 07:15 to 2025-11-25 08:57 (30,085) | 2025-11-25 08:58 to 2026-11-04 06:50 (30,086) |
| PEPE-AUD | 2024-11-27 14:06 to 2025-10-30 17:37 (7,288) | 2025-10-30 20:03 to 2026-06-12 06:16 (2,429) | 2026-06-12 09:57 to 2026-11-04 06:50 (2,430) |
| SOL-AUD | 2024-06-05 13:38 to 2026-02-09 10:33 (139,656) | 2026-02-09 10:39 to 2026-06-16 19:05 (46,552) | 2026-06-16 19:44 to 2026-11-04 06:50 (46,552) |
| USDC-AUD | 2021-01-21 16:19 to 2025-02-22 09:43 (212,068) | 2025-02-22 10:29 to 2026-01-09 22:02 (70,689) | 2026-01-09 22:02 to 2026-11-04 06:51 (70,690) |
| USDT-AUD | 2020-06-16 22:08 to 2025-01-28 16:03 (631,528) | 2025-01-28 16:03 to 2025-11-14 15:26 (210,509) | 2025-11-14 15:26 to 2026-11-04 06:53 (210,510) |
| XRP-AUD | 2020-06-16 22:30 to 2025-06-01 22:52 (187,641) | 2025-06-01 22:57 to 2026-01-29 02:10 (62,547) | 2026-01-29 02:10 to 2026-11-04 06:55 (62,548) |

Complete source/gap report: `data/historical_imports/readiness/4000c0ea94d95bac8a68e1bc50255b9397ba1bb00e0d39652b6842f9e2be75c1.json` (**379,359,638 bytes**). Compact partition contract: `data/historical_imports/partitions/111b403e5efb821d4187cc0509766c3ddf0ae42397f62fd72987356ef8dc6b8c.json` (**215,099 bytes**). The complete report cryptographically links the compact contract; split loading accepts either ID and streams the large report checksum without parsing its gap arrays. The earlier complete report is retained as a superseded immutable artifact.

Actual BTC and BCH training/validation/test frames were loaded through `ResearchData.load_partition`: BTC 500,992 / 166,997 / 166,998 rows; BCH 29,322 / 9,774 / 9,775 rows. Exact row hashes and source dependencies matched.

## Verification and delivery

Final local complete offline suite: `.venv-db/Scripts/python.exe -m pytest -q tests` — **919 passed, 7 skipped in 370.43 seconds**. Final focused historical/research-export suite — **63 passed in 25.13 seconds**. Earlier importer/storage suite — **90 passed, 1 skipped**. Skips are opt-in live/platform checks; actual public source acquisition and local persistence were validated separately.

The actual all-AUD CSV export for **2026-09-01 through 2026-10-02**, ID `de20b5eac677270dec785b9fbf926705a55cadbbf297c35d2be98f987bb23573`, passed source, hash and row-count verification: **57,645 prices, 18,781 quotes, 187,750 book levels, 6,160 fee-tier records and zero eligible markets**. Both original M10 exports were independently reverified successfully. Export files and manifests remain local under `data/research_exports/`; no market data is committed.

All **25 M6 input manifests** reload and both **M8 evidence reports** reproduce. SQLite integrity is `ok`, foreign-key violations are zero, all price checkpoints match persisted data, and calculated cost rows remain zero. A separate preservation audit verified all **25,885 original market records** and **174 protected files** unchanged. Current source audit and coverage-update identifiers are recorded above.

GitHub CI results are reported on the PR and in the delivery response after the commit is pushed. No continuous collection process is activated. No financial or trading algorithms were implemented. Overall delivery remains **PARTIAL for general dense-minute or execution backtesting**: the genuine price archive is populated, but sparse no-trade intervals, incomplete historical execution observations and unknown historical account fee applicability remain explicit limitations.
