# Database Milestone 8: point-in-time eligibility screening

Screening is a data-availability and exchange-fact assessment, **not an investment recommendation**. The thresholds below are provisional. All mathematical financial screening and cost algorithms are reserved for user implementation after Milestone 10. No market can receive production approval while a required filter is unknown or unimplemented.

## Scope and reviewed base

PR #8 was verified merged into `review-base/milestone1-20261029`, with merge commit `e1e38a357946f74c9573c0d3dad0abff2a763392` verified in the base ancestry before creating `feature/db-m08-eligibility-screening`. This milestone adds three modules and focused tests; existing collectors, storage schemas, research configuration, cost algorithms and notebooks are unchanged.

## Ten-filter contract and policy

`eligibility_filters.FilterResult` is a frozen contract containing filter ID, PASS/FAIL/UNKNOWN/NOT_IMPLEMENTED, observed value, threshold, evidence references and reason. PASS requires a non-null observation and evidence. Missing is never zero. Every evaluation must contain exactly the ten distinct required filters.

| Filter | Default requirement | Implementation |
|---|---|---|
| F01 | Online | Verified discovery observation; status is valid only at its observed instant |
| F02 | Standard spot | Approved catalogue parser and original accepted record |
| F03 | AUD quote | Normalized quote in the verified record |
| F04 | 24-hour AUD volume at least 1000000 | NOT_IMPLEMENTED |
| F05 | Average spread at most 0.20 percent | NOT_IMPLEMENTED |
| F06 | Execution-side depth: 5 times proposed AUD1000 within 0.5 percent of mid | NOT_IMPLEMENTED; parameters stored separately, no multiplication or depth calculation |
| F07 | At least 90 days of historical availability | Verified completed candle span, with observed-minute days separately reported |
| F08 | At least 99 percent minute completeness | Existing Milestone 7 `minute_coverage`, over the requested trailing history window |
| F09 | Minimum quantity/notional supports proposed size | Raw exchange minimums/precision and request preserved; feasibility calculation NOT_IMPLEMENTED, missing constraints UNKNOWN |
| F10 | One-way execution cost at most 0.50 percent | NOT_IMPLEMENTED |

`ScreeningPolicy` holds all numeric thresholds once, using exact finite Decimal values. History days, proposed side/type, and metadata/market/fee freshness are configurable. Identity gates remain online/spot/AUD and cannot be widened through policy. Default freshness limits are 86400/300/86400 seconds respectively. Percent thresholds must be positive and no greater than 100; notional and other numeric thresholds must be finite and positive. History days must be an integer from 1 through 3660. Unknown policy keys and invalid types fail validation.

Example `policy.json` (partial policies use remaining defaults):

```json
{"history_days": 90, "min_completeness_pct": "99", "min_volume_aud": "1000000", "max_spread_pct": "0.20", "depth_multiple": "5", "proposed_notional_aud": "1000", "depth_band_pct": "0.5", "max_cost_pct": "0.50", "order_side": "buy", "execution_type": "taker"}
```

F07 compares earliest through latest completed candle (inclusive minute) against the requested duration. It also exposes actual observed minutes and observed-minute days. A sparse span alone cannot establish eligibility: F08 independently requires completeness in the explicit trailing window. F08 reuses the existing deterministic coverage measurement without an alternative estimator. Zero-volume source candles remain observations; absent minutes are not filled.

## Point-in-time evidence

Every request has a UTC `as_of`. Omitted CLI values use actual current UTC once; future as-of values fail. All commands operate offline. The historical union of AUD assets in discovery receipts known by `as_of` supplies the universe, including previously observed offline markets. Today's mutable assets row is not a source of historical status; its native ID is checked only as an identity safeguard.

`eligibility_evidence.PointInTimeEvidence` checks retained catalogue records against their original public metadata through the approved parser. It does not classify the 383 unresolved class/venue records. Missing, inconsistent or unreadable discovery evidence yields UNKNOWN rather than a guessed market identity.

There is no status-transition history or validity interval in the existing catalogue. F01 therefore establishes online/offline only at the exact recorded observation instant. Between observations it is UNKNOWN, even inside the configurable freshness limit; beyond that limit the reason is `stale_metadata_status`. F02/F03 use previously verified identity, with its source timestamp, and do not claim a current trading status. A fresh collection is not silently substituted for a historical observation.

OHLCV uses original candle-open event time and receipt `fetched_at` knowledge time. Both must be no later than `as_of`, and the candle must have completed. Only exact persisted rows matching validated original source receipts contribute to coverage. A past candle collected in a later backfill is excluded from an earlier evaluation. Original raw response rows outside an earlier collection request need not have been persisted; they are not invented. Conflicting persisted values fail the history evidence check.

Quote/book references include exact observation and receipt times, native identity, response hash, row hash, source receipt, latency and timestamp semantics. A future receipt is never used. Quote/book linkage is checked and stale/missing/unaligned inputs are explicit issues. `resolve_input_reference` retrieves all exact referenced raw rows for later approved algorithms; it computes no market metric. Fee lookup reuses the Milestone 6 time-safe lookup with present-day historical assumptions disabled. Unknown tier/effective time/category remains explicit, and private payloads/account volumes are not copied into screening reports.

Known quality issue IDs and detection/affected times are included only when detected by `as_of`. Current resolution status and ingestion checkpoints lack version history: they are not applied retrospectively. Checkpoint history is explicitly UNKNOWN. Later checkpoint updates or issue resolution cannot rewrite an earlier evidence-backed decision.

## Readiness and aggregation

- Exchange: F01-F03.
- Data: F07-F08.
- Liquidity: F04-F06.
- Execution: F09-F10.

Within a dimension, FAIL takes precedence over UNKNOWN, then NOT_IMPLEMENTED, then PASS. A confirmed filter FAIL establishes overall REJECTED even with reserved filters. All ten PASS are required for ELIGIBLE. Otherwise exchange uncertainty gives UNKNOWN; unavailable data gives INSUFFICIENT_DATA; other unknowns give UNKNOWN; fully evaluated facts with only reserved calculations give PENDING_EVALUATION. Production persistence explicitly rejects any claimed calculated/PASS result for reserved filters. Thus an unimplemented algorithm can never accidentally authorize a market.

## Existing SQLite mapping and immutable evidence

No schema migration is introduced. The approved `eligibility` primary key remains `(pair_id, screened_at)`, where `screened_at` is the screening **as-of timestamp**, as defined in Milestone 2. Actual evaluation wall-clock time is separate in the sidecar. Foreign keys to `assets` are retained; unknown pairs are sidecar-only and are never inserted into assets.

SQL projection stores decision eligible/ineligible/unknown, history span in whole integer days, completeness, and reason codes. Volume/spread/depth remain NULL with `unavailable` statuses. Missing history/completeness are NULL/unknown. Detailed four-state filters, dimensions, source hashes, configuration and policy hash are retained in:

```text
data/eligibility_screening/runs/<sha256-of-evaluation>.json
```

The SQL decimal representation permits at most 18 fractional digits. Completeness is rounded only for that SQL representation; the original Milestone 7 measurement and filter comparison are preserved without rounding in the sidecar. No financial algorithm is involved.

Repeated identical as-of/policy/evidence evaluations produce the same report ID and reuse SQL rows and the original evaluation timestamp. Different results/policies at the same SQL key are retained as distinct immutable sidecars, explicitly marked as SQL projection conflicts; earlier SQL decisions are never overwritten, and a conflicting batch inserts no new rows. To maintain separate SQL runs, use distinct as-of timestamps. This limitation avoids changing approved schema semantics without approval.

A process lock serializes screening writers. Sidecars use existing atomic JSON replacement; SQL inserts use a transaction. Sidecar publication precedes SQL insertion. A failed SQL commit may leave an orphan sidecar, reported as sidecar-only/uncommitted; retry can complete it. There is no cross-engine transaction claim. Load verifies the sidecar hash, policy and filter contract and actual SQL projection. `report --verify-evidence` regenerates the same historical evaluation from dependencies. Missing or changed dependencies fail verification; export refuses unverified reports. Historical reports remain inspectable even when a dependency is missing, but must not be described as currently reproducible.

## CLI and CSV

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.eligibility_manager evaluate --all-aud
.venv-db/Scripts/python.exe -m kraken_quant.eligibility_manager evaluate --pair BTC-AUD --as-of 2026-11-01T07:09:01.303719Z --policy policy.json
.venv-db/Scripts/python.exe -m kraken_quant.eligibility_manager report --all-aud --verify-evidence
.venv-db/Scripts/python.exe -m kraken_quant.eligibility_manager filters status
.venv-db/Scripts/python.exe -m kraken_quant.eligibility_manager export --format csv
```

Use `--root` and `--data-dir` for established storage configuration. A recognized initialized metadata database is required. `--report-id` selects an exact persisted run for report/export. Export defaults to the latest all-AUD run, even if a newer single-pair report exists. Evaluation returns zero for a valid persisted rejection; status is not an investment approval. Unknown universe/projection failures return nonzero. Reporting does not reevaluate unless evidence verification is requested. No network or trading client is constructed.

CSV directory: `data/eligibility_screening/exports/<report-id>/`. Files are atomic, immutable on conflicting content, SHA256-audited and ignored by Git:

| CSV | Selection |
|---|---|
| all_verified_aud_spot.csv | Evidence-backed AUD spot identity, including stale status |
| exchange_eligible.csv | Exchange dimension PASS |
| rejected.csv | Overall REJECTED |
| pending_mathematical_evaluation.csv | At least one NOT_IMPLEMENTED filter, with overall status and blockers |
| insufficient_history.csv | F07 or F08 FAIL/UNKNOWN |
| eligible.csv | Only overall ELIGIBLE; header-only is valid |
| filter_evidence.csv | One row per market/filter, preserving observed value, threshold, sources and reason |

These are intentionally overlapping diagnostic categories: a rejected market can also await mathematical evaluation. `eligible.csv` must never be populated from exchange metadata alone. No raw order books or other large source datasets are duplicated.

## Automated verification

```powershell
.venv-db/Scripts/python.exe -m pytest -q tests/test_eligibility_screening.py
.venv-db/Scripts/python.exe -m pytest -q tests
```

The focused suite covers policy validation, ten-filter registration, factual failures, every reserved method exception, F09 deferral, sparse history/completeness, historical knowledge-time selection, later asset/fee/quality/candle exclusion, mutable-checkpoint exclusion, SQL idempotency/conflicts/rollback recovery, CSV contents/hashes/immutability, read-only CLI, missing dependencies and full raw book access. AST and contract guards forbid calculated placeholder results. Existing mathematical cost placeholders are unchanged.

The repository's offline suite is explicitly `pytest -q tests`, as used by both CI workflows. An unrestricted `pytest -q` also collects standalone examples that perform account connectivity; an attempted unrestricted run stopped on the existing example's Kraken invalid-nonce error. No order was submitted. This is not an offline regression test and was not altered in this milestone.

## Actual persisted-data validation

Evaluation as of **2026-11-03T06:16:07.963679Z**, using actual persisted Kraken observations, not synthetic fixtures. Report ID: `4b23fe9dc9763eae3cb27d5f7e5e582332b09ee1d4bb97459c1e96df790041dc`. Actual wall-clock evaluation time is separately recorded.

**14 REJECTED; 0 ELIGIBLE.** Every market has F01 UNKNOWN, F02/F03 PASS, F04-F06/F09/F10 NOT_IMPLEMENTED, and F07/F08 FAIL. Latest verified discovery is `2026-11-01T07:09:01.303719Z`; it does not prove current online status. Quotes/books are present but stale at this as-of; fees preserve unknown applicable account tier/effective time/category.

| Pair | Exchange | History | Completeness | Liquidity | Execution | Overall | Main blocker |
|---|---|---|---|---|---|---|---|
| ADA-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| AUDX-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| BCH-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| BTC-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| DOGE-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| ETH-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| EUR-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| LINK-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| LTC-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| PEPE-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| SOL-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| USDC-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| USDT-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |
| XRP-AUD | UNKNOWN | FAIL | FAIL | NOT_IMPLEMENTED | NOT_IMPLEMENTED | REJECTED | History <90 days; completeness <99% |

Actual minute-coverage evidence uses the trailing 90-day window (129600 expected minutes). Span days are not dense observed days:

| Pair | First completed candle UTC | Last completed candle UTC | Span days | Known minutes | Completeness % |
|---|---|---|---:|---:|---:|
| ADA-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1442 | 1.112654320988 |
| AUDX-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 0.501388889 | 722 | 0.557098765432 |
| BCH-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-01T08:28:00.000000Z | 0.501388889 | 722 | 0.557098765432 |
| BTC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1442 | 1.112654320988 |
| DOGE-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1442 | 1.112654320988 |
| ETH-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1442 | 1.112654320988 |
| EUR-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1441 | 1.111882716049 |
| LINK-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1441 | 1.111882716049 |
| LTC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1441 | 1.111882716049 |
| PEPE-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1441 | 1.111882716049 |
| SOL-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1441 | 1.111882716049 |
| USDC-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1441 | 1.111882716049 |
| USDT-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1441 | 1.111882716049 |
| XRP-AUD | 2026-10-31T20:27:00.000000Z | 2026-11-03T04:54:00.000000Z | 2.352777778 | 1441 | 1.111882716049 |

Actual SQLite query (all rows are at the as-of above):

```sql
SELECT pair_id, historical_days, completeness_pct, decision,
       volume_24h_aud_status, avg_spread_pct_status, depth_aud_status
FROM eligibility
WHERE screened_at = '2026-11-03T06:16:07.963679Z'
ORDER BY pair_id;
```

| Pair | historical_days | completeness_pct | decision | Volume/spread/depth status |
|---|---:|---:|---|---|
| ADA-AUD | 2 | 1.112654320987654300 | ineligible | unavailable / unavailable / unavailable |
| AUDX-AUD | 0 | 0.557098765432098800 | ineligible | unavailable / unavailable / unavailable |
| BCH-AUD | 0 | 0.557098765432098800 | ineligible | unavailable / unavailable / unavailable |
| BTC-AUD | 2 | 1.112654320987654300 | ineligible | unavailable / unavailable / unavailable |
| DOGE-AUD | 2 | 1.112654320987654300 | ineligible | unavailable / unavailable / unavailable |
| ETH-AUD | 2 | 1.112654320987654300 | ineligible | unavailable / unavailable / unavailable |
| EUR-AUD | 2 | 1.111882716049382700 | ineligible | unavailable / unavailable / unavailable |
| LINK-AUD | 2 | 1.111882716049382700 | ineligible | unavailable / unavailable / unavailable |
| LTC-AUD | 2 | 1.111882716049382700 | ineligible | unavailable / unavailable / unavailable |
| PEPE-AUD | 2 | 1.111882716049382700 | ineligible | unavailable / unavailable / unavailable |
| SOL-AUD | 2 | 1.111882716049382700 | ineligible | unavailable / unavailable / unavailable |
| USDC-AUD | 2 | 1.111882716049382700 | ineligible | unavailable / unavailable / unavailable |
| USDT-AUD | 2 | 1.111882716049382700 | ineligible | unavailable / unavailable / unavailable |
| XRP-AUD | 2 | 1.111882716049382700 | ineligible | unavailable / unavailable / unavailable |

Volume/spread/depth values are SQL NULL in every row; historical_days/completeness statuses are known. **14 SQL rows** after first evaluation, identical repeated evaluation, and overlapping BTC-AUD single-pair evaluation. Integrity check: **ok**; foreign-key violations: **0**. Reopened SQL projections and regenerated evidence hashes verify. Seven CSVs were read and repeated exports returned identical bytes and SHA256 values.

| Export | Actual data rows |
|---|---:|
| all_verified_aud_spot.csv | 14 |
| exchange_eligible.csv | 0 |
| rejected.csv | 14 |
| pending_mathematical_evaluation.csv | 14 |
| insufficient_history.csv | 14 |
| eligible.csv | 0 |
| filter_evidence.csv | 140 |

Local immutable evidence: `data/eligibility_screening/runs/4b23fe9dc9763eae3cb27d5f7e5e582332b09ee1d4bb97459c1e96df790041dc.json`. Local CSVs: `data/eligibility_screening/exports/4b23fe9dc9763eae3cb27d5f7e5e582332b09ee1d4bb97459c1e96df790041dc/`. Generated reports, SQLite and CSVs are deliberately not committed. Aggregate actual audit results are documented here for independent review. No collector ran and no market observation was modified.

Focused tests: **61 passed**. Full offline regression: **815 passed, 7 skipped** (opt-in integrations and platform-dependent symlink check). Actual-data validation used offline persisted Kraken sources; no additional live market collection or account authentication was needed.


## Limitations and next research decision

No current market is research-ready. A 90-day real historical dataset remains the main data blocker; Kraken's approximately 720-candle REST horizon cannot supply it through repeated requests. Follow the Milestone 7 recommendation for an explicitly approved historical source/archive assessment. No external source is integrated here. Quotes/books need fresh collection before future contemporaneous calculations; historical exchange-status reconstruction needs versioned status evidence. Fee tiers remain unassigned where account-specific evidence is unavailable. No strategy, screening financial formula, cost calculation, eligibility-table migration, trading call or subsequent milestone is implemented.
