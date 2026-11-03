# Database Milestone 7: historical coverage, bounded backfill and gap repair

## Research-enablement result

The collector can obtain genuinely accessible completed Kraken candles and repair reachable gaps. It cannot recover a 90-day minute history from Kraken REST. The reports measure the requested research window explicitly; sparse samples and a long first-to-last span do not establish research readiness. No strategy, statistical model, financial algorithm, automatic research eligibility or external provider is implemented.

PR #7 was verified merged at `d374418e19895daaadce200faba5ae8f96e7c2c2` into `review-base/milestone1-20261029`; ancestry was verified before branching. AGENTS.md and Database Milestones 1?6 were reviewed. Existing research, credential and execution configurations remain unchanged.

## Official source limits and actual verification

[Kraken's official OHLC contract](https://docs.kraken.com/api-reference/market-data/get-ohlc-data) describes approximately 720 recent entries and a final uncommitted entry that is always returned. Older candles cannot be obtained by choosing an arbitrarily early `since`. The continuation marker supports incremental updates, not unlimited reverse pagination. Runtime counts are measured rather than hardcoded; the live responses can contain 720 completed rows plus the final entry.

The provider requests an old desired start, then performs at most one epoch-start confirmation probe when truncation or empty history first needs verification. Both raw responses and their returned bounds are retained. Matching/non-earlier oldest timestamps verify no older data was obtained. Unexpected older results are explicitly reported; there is no endless paging loop or completeness claim. Subsequent calls reuse verified source evidence. A narrow incremental reply never replaces a global retention floor with its query-specific first row.

The source reports oldest accessible/returned timestamps, newest completed timestamp, query scope, requested-start availability, whether earlier history is exhausted, probe outcome, completed-row counts and excluded unfinished rows. Requested-window records are available separately from raw audit batches. The manager independently reports whether the entire requested interval was actually retrieved. Exit 2 means partial/unavailable history, not successful completion of a 90-day request.

Kraken currently returns only an epoch-zero unfinished placeholder for AUDX/AUD and BCH/AUD in the live audit. Neither zero prices nor timestamp-zero observations are persisted. An additional public epoch-start probe confirms no finalized candles were supplied. That fact is cached through the probe's completed-minute cutoff; repeated attempts for the same historical period make no HTTP requests. It does not establish delisting or prove an absence of trading. Existing older local observations remain intact.

## Minimal architecture and reuse

- `history_coverage.py`: deterministic offline inventory and exact minute-gap analysis.
- `history_provider.py`: typed `HistoricalDataProvider.fetch(pair_id,start_time,end_time,interval_minutes=1)` interface and the only approved adapter, `KrakenOHLCProvider`.
- `history_manager.py`: public backfill/repair orchestration and CLI.

The provider reuses the existing public HTTP client, native asset identities, OHLC parser, credential-free endpoint guard, collector pacing and source receipts. The manager reuses the collector-wide lock, pending intents, conflict checks, monthly Parquet writer, verified checkpoint updates and legacy pair-state format. No SQLite table or Arrow schema is migrated; no second database is created. No existing research, quote/book or cost-input module is changed.

Future approved providers can emit the established canonical `prices_1m` records through the provider interface without redefining market storage schemas. Activating another source requires review of identity/provenance integration; none is registered or contacted here. The CLI accepts only `--source kraken`. There are no paid services, accounts, purchases or additional credentials.

## Coverage methodology

All windows use aware UTC timestamps, exact minute boundaries and **inclusive start / exclusive end**. Default is the latest 90 days ending at the current minute boundary. OHLCV expected count is the number of completed minute openings in that window, not the number of returned API rows. The current unfinished minute and future intervals are reported separately and never counted as completed observations.

Inventory reads raw Parquet in batches of 8,192 using the existing validated path resolver. Validation happens before accepting timestamps; a fallback isolates invalid rows when a batch fails validation. Logical duplicates and conflicting duplicate values are detected. Books are additionally validated as complete returned two-sided snapshots; level rows and distinct snapshots are separate counts. Zero-volume candles actually supplied by Kraken remain valid. No missing timestamp is filled with prices, zero volume or synthetic data.

Counts include raw physical records, valid unique timestamps, calendar dates with observations, invalid/duplicate/conflicting records, physical out-of-order rows, first/last valid observations and source binding. Memory is bounded by one established monthly partition plus timestamps within the requested window. Unsupported/unreadable partitions produce Unknown integrity and unknown coverage/counts rather than a fabricated zero percent. Source receipts are separately checked for timestamp-order problems and unfinished placeholders; this receipt summary is not a substitute for the existing per-record source hashes.

For prices:

- **Observed minute days** = unique valid candle count / 1,440. This is the Days column in the actual audit table.
- **Span days** = elapsed first-to-last candle span including the last minute. It can include large gaps.
- **Calendar days** = distinct UTC dates containing accepted observations.
- **Coverage percentage** = unique present completed minutes / expected completed minutes in the explicitly requested window.
- Missing beginning/end, compact exact missing intervals, complete intervals and longest continuous interval are reported. `missing_timestamps()` lazily enumerates individual absent timestamps without materializing a giant JSON list.
- A positive 90-day flag requires all 129,600 completed minutes in the relevant latest 90-day window, with valid integrity and no duplicate/order problems. Merely spanning 90 days is insufficient.

Quote and book observations are **not minute candles**. Their actual timestamps are preserved. The report reuses the existing bounded-session sampling diagnostics and mean observed spacing. Legacy receipts do not define exact scheduled slots for every session, so expected/missing quote/book counts remain explicitly unknown rather than being inferred from the OHLCV minute grid. Recorded session gaps are labelled as such; time when no session ran is not evidence of exchange inactivity. Requested-window sample counts and global inventory counts are separate.

## Gap classifications

| Classification | Meaning |
|---|---|
| unavailable_from_kraken_rest_at_probe | Locally absent and earlier than a verified REST retention floor, or absent during a confirmed empty-history probe scope. This is source availability at the probe, not proof of no trades. |
| not_collected_source_availability_unresolved | Locally absent; source availability or no-trade meaning is not established. |
| confirmed_invalid_local_observation | Stored observation is invalid, conflicting or unusably incomplete for that completed minute. |
| uncommitted_or_future_minutes | Requested minutes not finalized; excluded from completed coverage. |
| unverified_partition_contents | Partition cannot be validated/read; coverage is unknown. |

Existing stored observations remain present even if today's source cannot retrieve them. Exchange-provided explicit zero-trade candles are present observations, never gaps. No-trade versus unavailable historical data is unresolved unless a source contract supplies evidence. Existing source/quality history is preserved.

## Backfill, repair and restart

1. Validate the SQL-backed, latest-audited online standard AUD spot universe; use stored Kraken-native IDs. Unresolved broader-class/venue records remain excluded.
2. Acquire the existing `ohlcv-collector` lock. Replay existing pending intents and reconcile verified Parquet maxima with SQLite before requesting data.
3. Inventory the requested window. Skip a request when only already-verified inaccessible gaps remain. Fail closed on corrupt local partitions rather than erasing/rebuilding them.
4. Fetch bounded public OHLC responses. Overlap one minute for potentially exclusive `since` semantics. Save original responses in the existing `ohlcv_collection/responses/<PAIR>/` layout.
5. Validate all completed candles and cross-probe consistency. Exclude the final uncommitted row unconditionally. Clamp accepted records to the actual requested interval.
6. For repair, start at the first potentially accessible missing interval and persist only missing observations. Unavoidable source overlap is validated; conflicting historical values fail without overwrite.
7. Save a pending intent; use the existing atomic per-partition writer; reopen/verify persisted observations; update the existing `ingestion_state` checkpoint; then remove the intent.
8. Recalculate coverage and record remaining gaps as idempotent open `data_quality_issues` with affected periods. Preserve prior issues without automatic resolution. Do not change research eligibility or readiness tables.

A crash after Parquet but before SQLite is recovered by replay/reconciliation. A failure between months leaves a replayable pending batch; valid committed partitions remain intact. Checkpoints cannot move backwards or advance before verification. There is no distributed SQLite/Parquet transaction. Run journals account for each pair, including source/network/persistence failures and interruption. Repeating a partial historical request is not described as gaining older history if nothing new was retrieved.

The existing persistence helper still validates the selected pair's history during recovery/commit; offline inventory uses bounded batches. Very large future archives may justify a reviewed optimization of that established helper, outside this minimal milestone.

## Readiness assessment

`Sufficient` means valid, gap-free OHLCV for the explicit completed research window with matching checkpoint and provenance binding. `Insufficient` means the available observations do not satisfy it. `Unknown` means integrity/provenance cannot be established. This assessment does not write `eligibility` or declare profitability, strategy validity or research approval. Stored quote/book samples are reported separately and cannot establish a historical microstructure dataset.

## Commands

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.history_manager coverage --all-aud
.venv-db/Scripts/python.exe -m kraken_quant.history_manager coverage --pair BTC-AUD --days 90
.venv-db/Scripts/python.exe -m kraken_quant.history_manager backfill --pair BTC-AUD --source kraken
.venv-db/Scripts/python.exe -m kraken_quant.history_manager backfill --all-aud --source kraken
.venv-db/Scripts/python.exe -m kraken_quant.history_manager gaps --pair BTC-AUD --days 7
.venv-db/Scripts/python.exe -m kraken_quant.history_manager repair --pair BTC-AUD --source kraken
```

`--start` and `--end` accept UTC ISO timestamps for reproducible windows; `--days` defaults to 90. Established `--data-dir`/`--root` configuration is reused. Reports are offline/read-only and return success when a reliable report is generated, even when it says Insufficient. Backfill/repair exit 0 only for fully covered requested intervals, 2 for partial/unavailable coverage, and 1 for errors/interruption. A missing/incompatible database is never initialized automatically.

## Practical next research-enablement decision

[Kraken's official downloadable OHLCVT archive](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data) is the first source to assess before paying another provider. The page inspected November 3, 2026 describes complete archives through September 30, 2026 and quarterly updates. It supplies minute data but omits intervals without trades; those omissions must be preserved/classified under that source's contract, not padded. Per-pair archive availability/listing dates still need verification, especially for newer markets.

The shortest practical route to initial experiments is to approve a small importer for the official archive, choose a supported 90-day window within the permitted training period, and audit per-market completeness and no-trade semantics before experiments. Respect the existing locked validation/test rules. A commercial/third-party provider is not automatically necessary for that goal.

If the requirement is the **latest** 90 days ending November 3, the described quarterly archive stops September 30 and REST cannot bridge the intervening period. An additional approved historical source or a later official archive release is required for that gap; uninterrupted forward collection is a slower alternative. Historical bid/ask and books require their own approved archive or forward observation and cannot be reconstructed from OHLCV. No archive download/import, external integration, purchase or scheduler is implemented here.

## Actual Kraken validation

Actual public validation used the fixed window **2026-08-05T04:55:00.000000Z to 2026-11-03T04:55:00.000000Z exclusive**, exactly **129,600 expected completed minutes**. First BTC/ETH backfills deliberately requested an end five minutes earlier, preserving 715 new genuine observations each. Subsequent repairs collected the five genuinely missing accessible candles each. No existing data was deleted to manufacture a repair demonstration.

All 14 verified markets were attempted and accounted for. Twelve supplied recent completed candles; AUDX/AUD and BCH/AUD supplied only unfinished epoch-zero placeholders, confirmed with public `since=0` probes. Their 722 original local rows each were retained. All remaining requested history is explicitly unavailable from the checked REST endpoint at its probe; no 90-day success is claimed. Backfill/repair returned **exit 2** for these truthful partial results, with no remaining implementation error. A final repeat added **0 rows with 0 HTTP requests across all 14 pairs**.

### Actual persisted coverage

Days below means observed minute days, not elapsed span. Coverage and missing minutes refer to the fixed 90-day window. Prices are real Kraken observations; no synthetic fixtures appear in this audit.

| Pair | First OHLCV UTC | Last OHLCV UTC | Candles | Observed days | 90-day coverage % | Missing minutes | Quotes | Book snapshots | 90 days available | Readiness |
|---|---|---|---|---|---|---|---|---|---|---|
| ADA-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1442 | 1.001388889 | 1.112654321 | 128158 | 13 | 13 | False | Insufficient |
| AUDX-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 0.501388889 | 0.557098765 | 128878 | 13 | 13 | False | Insufficient |
| BCH-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 722 | 0.501388889 | 0.557098765 | 128878 | 13 | 13 | False | Insufficient |
| BTC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1442 | 1.001388889 | 1.112654321 | 128158 | 13 | 13 | False | Insufficient |
| DOGE-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1442 | 1.001388889 | 1.112654321 | 128158 | 13 | 13 | False | Insufficient |
| ETH-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1442 | 1.001388889 | 1.112654321 | 128158 | 13 | 13 | False | Insufficient |
| EUR-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1441 | 1.000694444 | 1.111882716 | 128159 | 13 | 13 | False | Insufficient |
| LINK-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1441 | 1.000694444 | 1.111882716 | 128159 | 13 | 13 | False | Insufficient |
| LTC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1441 | 1.000694444 | 1.111882716 | 128159 | 12 | 12 | False | Insufficient |
| PEPE-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1441 | 1.000694444 | 1.111882716 | 128159 | 12 | 12 | False | Insufficient |
| SOL-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1441 | 1.000694444 | 1.111882716 | 128159 | 12 | 12 | False | Insufficient |
| USDC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1441 | 1.000694444 | 1.111882716 | 128159 | 11 | 11 | False | Insufficient |
| USDT-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1441 | 1.000694444 | 1.111882716 | 128159 | 11 | 11 | False | Insufficient |
| XRP-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 1441 | 1.000694444 | 1.111882716 | 128159 | 11 | 11 | False | Insufficient |

### Additional actual inventory

| Pair | Calendar dates | Span days | Longest continuous minutes | Duplicates | Invalid stored candles | Out-of-order stored | Book level rows | Checkpoint |
|---|---|---|---|---|---|---|---|---|
| ADA-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 260 | matches |
| AUDX-AUD | 2 | 0.501388889 | 722 | 0 | 0 | 0 | 130 | matches |
| BCH-AUD | 2 | 0.501388889 | 722 | 0 | 0 | 0 | 260 | matches |
| BTC-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 260 | matches |
| DOGE-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 260 | matches |
| ETH-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 260 | matches |
| EUR-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 260 | matches |
| LINK-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 260 | matches |
| LTC-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 240 | matches |
| PEPE-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 240 | matches |
| SOL-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 240 | matches |
| USDC-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 220 | matches |
| USDT-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 220 | matches |
| XRP-AUD | 4 | 2.352777778 | 722 | 0 | 0 | 0 | 220 | matches |

No quote/book samples were added or transformed. All existing 173 quotes / 173 book snapshots remain separate from candle coverage. Quote/book expected sampling counts remain unknown where legacy session slots were not defined; missing OHLCV minutes are not evidence of missing book packets or no exchange trades.

### Actual source availability evidence

| Pair | REST oldest accessible at probe | REST newest completed at probe | Floor verified | No completed history through UTC | Older probe found older | Completed rows per response |
|---|---|---|---|---|---|---|
| ADA-AUD | 2026-11-02T16:55:00.000000Z | 2026-11-03T04:54:00.000000Z | True | None | False | [720, 720] |
| AUDX-AUD | None | None | False | 2026-11-03T05:00:00.000000Z | False | [0, 0] |
| BCH-AUD | None | None | False | 2026-11-03T05:01:00.000000Z | False | [0, 0] |
| BTC-AUD | 2026-11-02T16:55:00.000000Z | 2026-11-03T04:54:00.000000Z | True | None | None | [6] |
| DOGE-AUD | 2026-11-02T16:55:00.000000Z | 2026-11-03T04:54:00.000000Z | True | None | False | [720, 720] |
| ETH-AUD | 2026-11-02T16:55:00.000000Z | 2026-11-03T04:54:00.000000Z | True | None | None | [6] |
| EUR-AUD | 2026-11-02T16:56:00.000000Z | 2026-11-03T04:55:00.000000Z | True | None | False | [720, 720] |
| LINK-AUD | 2026-11-02T16:56:00.000000Z | 2026-11-03T04:55:00.000000Z | True | None | False | [720, 720] |
| LTC-AUD | 2026-11-02T16:56:00.000000Z | 2026-11-03T04:55:00.000000Z | True | None | False | [720, 720] |
| PEPE-AUD | 2026-11-02T16:56:00.000000Z | 2026-11-03T04:55:00.000000Z | True | None | False | [720, 720] |
| SOL-AUD | 2026-11-02T16:56:00.000000Z | 2026-11-03T04:55:00.000000Z | True | None | False | [720, 720] |
| USDC-AUD | 2026-11-02T16:56:00.000000Z | 2026-11-03T04:55:00.000000Z | True | None | False | [720, 720] |
| USDT-AUD | 2026-11-02T16:56:00.000000Z | 2026-11-03T04:55:00.000000Z | True | None | False | [720, 720] |
| XRP-AUD | 2026-11-02T16:56:00.000000Z | 2026-11-03T04:55:00.000000Z | True | None | False | [720, 720] |

Response-window bounds can extend beyond the deliberately fixed requested end; only requested-window rows were persisted. A narrow repair reply retains the earlier verified global floor. Probe evidence is source/time scoped and does not delete previously collected observations. No claim is made that an unavailable minute had no trades.

### Actual SQLite checkpoint query

```sql
SELECT pair_id,dataset,latest_observation_at,updated_at
FROM ingestion_state WHERE dataset='prices_1m' ORDER BY pair_id;
```

| Pair | Dataset | Latest observation UTC | Updated UTC |
|---|---|---|---|
| ADA-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:04.401864Z |
| AUDX-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-03T05:01:04.545967Z |
| BCH-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-03T05:01:04.656246Z |
| BTC-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:04.822671Z |
| DOGE-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:05.033749Z |
| ETH-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:05.230456Z |
| EUR-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:05.423715Z |
| LINK-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:05.619445Z |
| LTC-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:05.814192Z |
| PEPE-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:06.012312Z |
| SOL-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:06.207797Z |
| USDC-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:06.392621Z |
| USDT-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:06.630717Z |
| XRP-AUD | prices_1m | 2026-11-03T04:54:00.000000Z | 2026-11-03T05:01:06.838314Z |

SQLite integrity_check: **ok**. Foreign-key violations: **0**. All checkpoints match verified persisted maxima. Reparsed **30 original source receipts** and confirmed **10124 exact original source-row comparisons** still match storage. All **25 Milestone 6 pinned input manifests** reloaded successfully after the append. All five mathematical cost methods remain unimplemented.

### Verification commands

```powershell
.venv-db/Scripts/python.exe -m pytest -q tests/test_history_manager.py
.venv-db/Scripts/python.exe -m pytest -q tests
$env:KRAKEN_HISTORY_SMOKE='1'
.venv-db/Scripts/python.exe -m pytest -q tests/test_history_manager.py::test_live_kraken_retention_and_persistence
Remove-Item Env:KRAKEN_HISTORY_SMOKE
```

Final focused result: **39 passed, 1 skipped**. Full offline regression: **754 passed, 7 skipped**. Opt-in actual public history integration: **1 passed**. Offline skips are six opt-in public integrations and privileged Windows symlink creation. Earlier fixture-provenance failures were corrected before final verification. No current automated test failures remain.

### Remaining data requirements

All 14 pairs are **Insufficient** for the requested 90-day minute window. The longest stored continuous block is 722 minutes, despite several calendar dates appearing in inventory. Approve historical archive assessment/import before rigorous 90-day experiments; do not infer research sufficiency or profitability from these short samples. Latest-period coverage and historical bid/ask/books require the additional source/collection decisions described above. No alternative provider or archive importer has been implemented.


## Preservation and delivery checks

715 original files retained matching hashes, including existing code, notebooks, credentials, quote/book partitions, fee snapshots and cost manifests. The snapshot-directory file inventory remained unchanged. The 44 authorized ingestion exclusions cover price partitions, collector pair state/limiter and metadata.sqlite; original candle values were separately verified by 10,124 source-row comparisons and 25 pinned cost-manifest reloads. No existing implementation file is modified by this milestone. Generated data and audit receipts remain ignored; no database or Parquet file is tracked. Credential-value scan, compilation and staged-diff checks passed.
