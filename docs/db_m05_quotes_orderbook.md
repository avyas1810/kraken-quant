# Database Milestone 5: bounded quotes and order books

## Scope and verified base

PR #5 was verified merged into `review-base/milestone1-20261029` at `f979db9708c5c8244f41e502155fd7dc9bafeece` before implementation. This branch contains Database Milestones 1?4. No market-data schema migration, research change, private call, trading, screening, cost model or scheduler is introduced. The 383 unresolved discovery records are unchanged.

## Source selection and reuse

The collector reuses `ohlcv_collector.collection_universe`, its consistent SQL/discovery audit, `metadata_store.connect_metadata`, `MonthlyParquetStore`, approved Arrow v1 schemas, `ProcessLock`, `_atomic_json`, and `db_environment.load_config`. `market_data.public_get` is extended only to permit public `Depth`; the research daily-bar interface is unchanged. No second HTTP client or metadata database is created.

Reviewed `streaming.WebSocketMarketData` supplies bounded ticker BBO but its strategy-facing Quote interface does not preserve depth or sizes. Building a delta-book state machine is unnecessary here. Instead, `GET https://api.kraken.com/0/public/Depth?pair=<stored-native-id>&count=10&assetVersion=1` returns a complete L2 top-depth snapshot. Its first bid and ask provide a public best-bid/ask source with quantities, sharing one response and observation time with the book. Neither dataset is reconstructed historical data.

References: [Kraken Depth contract](https://docs.kraken.com/api-reference/market-data/get-order-book), [Kraken REST rate-limit errors](https://docs.kraken.com/exchange/guides/rest/ratelimits). Public-source pacing is a conservative local policy, not a claim that private API-key counters apply to public requests.

## Identity, timestamps and freshness

Every selection is backed by the latest consistent discovery audit and SQL assets with normalized AUD quote, online status and positive standard-currency/international-venue proof. Native Kraken pair IDs are used verbatim. The response must contain exactly one expected native or metadata-derived BASE/QUOTE key. No strategy universe or fixed pair count is consulted.

`timestamp` is the actual timezone-aware UTC local **request-start** time, stored to microseconds without minute rounding. The response must finish within five seconds and the clock must not regress. This rejects slow/stale observations conservatively. REST does not supply a trustworthy whole-snapshot event timestamp or age. Per-level third fields are last-update epochs; old resting levels are valid and do not prove a stale snapshot. They are validated against future times and preserved in the raw/provenance receipt, never substituted for snapshot time. Local clock accuracy and upstream/cache freshness cannot be proven by this interface. Report age/stale flags concern local observations only.

The existing `quotes_1m` logical key is `(pair_id,timestamp)` and accepts multiple distinct observations within one minute. The name is retained, with its approved observation semantics. Default sampling is 60 seconds; configuring shorter intervals preserves actual timestamps rather than rounding or fabricating minute bars. Unknown quote sizes remain NULL under the unchanged nullable schema; this Depth source normally supplies both sizes.

## Parsing and snapshot validation

Envelope: `error=[]`, one result pair, `bids` and `asks`. Each level is `[price, base_quantity, last_update_epoch]`. Prices/quantities remain decimal strings until exact Arrow `decimal128(38,18)` conversion; no price/quantity float conversion is permitted. Prices must be positive and quantities nonnegative. Quote bid must not exceed ask. Missing/empty sides, malformed levels, duplicate prices, wrong ordering, future update times and crossed books reject the entire response.

Requested depth defaults to 10, configurable 1?100 **per side**. Bids descend strictly and asks ascend strictly. Returned side counts may be fewer than requested because the complete returned top-depth snapshot can have less available depth; both sides must be nonempty and actual counts are reported. This is complete returned L2 depth up to the requested limit, never a claim of the entire exchange book. All levels share one timestamp, with contiguous one-based levels per side. No deltas, sequence reconstruction or checksum claim is made for REST.

| Dataset | Source mapping | Logical key |
|---|---|---|
| quotes_1m | pair_id from SQL; local start timestamp; bid/ask and bid_size/ask_size from first bid/ask levels | pair_id, timestamp |
| orderbook | pair_id from SQL; same timestamp; side bid/ask; one-based index; price and quantity from each source level | pair_id, timestamp, side, level |

Paths remain `data/market_data/<PAIR>/<DATASET>/YYYY-MM.parquet`. Existing UTC schema metadata, units and version are unchanged. All book levels at one timestamp form an indivisible snapshot: adding/removing levels under an existing timestamp is rejected, even if overlapping individual keys agree.

## Persistence, audit and restart

The existing metadata database must already be initialized and validated. A collector-wide advisory process lock serializes book sessions; existing monthly partition writer locks remain in effect. A source binding prevents injected fixtures and real data from sharing this collector's namespace, and refuses unmarked preexisting quote/book partitions. It assumes cooperative local writers, not adversarial tampering.

1. Reconcile pending intents and verified Parquet maxima before requesting observations.
2. Save the raw public response and timestamp/provenance receipt in ignored `data/book_collection/responses/<PAIR>/`.
3. Save a pending intent independently for each selected pair/dataset.
4. Preflight logical keys and complete-snapshot shape; identical retries are safe, conflicts fail closed.
5. Write through the existing temporary-file/fsync/atomic-replace monthly store.
6. Reopen and compare all intended records, then update the corresponding `ingestion_state` checkpoint in SQLite.
7. Remove the pending intent only after successful verification/checkpoint commit.

Quote and book checkpoints are independent. A crash may leave one dataset ahead of another; replay is safe and reconciles both separately. A checkpoint ahead of actual storage fails rather than moving backwards. Recovery scans history by month and writes touch only incoming partitions. There is no distributed transaction across SQLite and Parquet. Source receipts and run journals are persistent operational evidence, not exchange sequence proofs. Incomplete journals do not imply a collector stayed alive after a crash.

## Sampling, bounds and operational reporting

Quote and book intervals are independently configurable from 5 to 86,400 seconds; defaults are 60 seconds. A response can serve both datasets when both are due. Saved observation timestamps enforce sampling across restarts. A zero-duration command performs one bounded sweep; timed sessions permit 0?3,600 seconds, skip future starts at the deadline, and may take up to the existing HTTP timeout (10 seconds) plus local verification to finish an in-flight operation. Ctrl+C records interruption and releases the lock; pending intents recover persisted work. Sessions neither fill missed slots nor promise uptime.

All requests are sequential with at least 1.1 seconds between starts, including across this collector's restarts. HTTP429 or exchange throttle/rate errors stop further HTTP requests in that session and explicitly account for deferred pairs. Other pair failures are recorded independently. Concurrent unrelated programs share neither a global public-IP limiter nor this pacing policy; operators must coordinate them. With many pairs, effective frequency can be slower than configured. No catch-up bursts or unbounded retry queues are used.

At default 60 seconds, ideal uninterrupted sampling produces 1,440 quotes and 1,440 snapshots per pair/day. Depth 10 implies 28,800 book-level rows per pair/day; for a 14-pair example this is 20,160 quotes, 20,160 snapshots and 403,200 level rows/day. Logical column payload is roughly 2 MB/day quotes plus 23 MB/day book rows before compression/encoding/metadata. Actual Parquet compression and JSON receipts change physical growth; budget additional tens of MB/day for raw receipts and journals. A 20-second policy roughly triples these ideal rates. These are capacity estimates, not promised observation counts. Retention is indefinite with no automatic pruning. Monthly merge/replacement cost and offline report memory grow with stored history; production compaction, retention and scheduling are outside scope.

Offline report includes earliest/latest, distinct observations (snapshots separately from level rows), mean observed spacing, local age/stale flags, unresolved within-session sampling gaps, source/invalid/conflict issue counts, duplicate validation, checkpoint consistency, pending recovery and pair-wide status. Gaps use whole interval lengths between session boundaries/observations, ignoring partial terminal slots. This conservative count is not a claim of exact exchange-event packet loss. Time between bounded sessions is not counted as promised sampling coverage.

`data_quality_issues` receives timestamped open source/validation/persistence failures and unresolved sampling intervals. Existing issues are never automatically resolved/deleted. `collection_status` records latest collector operational status but preserves readiness and externally owned paused/error/collecting controls. Its pair-wide schema cannot represent independent dataset readiness, so per-dataset state remains in the report and checkpoints. New readiness is `unknown`; no research eligibility records are changed.

## Commands

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.book_collector collect --pair BTC-AUD --dataset quotes_1m
.venv-db/Scripts/python.exe -m kraken_quant.book_collector collect --pair BTC-AUD --dataset orderbook
.venv-db/Scripts/python.exe -m kraken_quant.book_collector collect --all-aud --duration-seconds 120
.venv-db/Scripts/python.exe -m kraken_quant.book_collector collect --all-aud --duration-seconds 120 --quote-interval 20 --book-interval 20 --depth 10
.venv-db/Scripts/python.exe -m kraken_quant.book_collector report
```

`--data-dir` / `--root` reuse established configuration (`KRAKEN_DATA_DIR`). Omitting `--dataset` selects both. Reports make no network calls or writes. Collection exits nonzero for actual failures, interruption or an entirely uncollected requested pair/dataset. Normal deadline deferrals after all requested datasets have been accounted for are clean shutdown, not source errors.

## Tests

```powershell
.venv-db/Scripts/python.exe -m pytest -q tests/test_book_collector.py
.venv-db/Scripts/python.exe -m pytest -q tests
$env:KRAKEN_BOOK_SMOKE='1'
.venv-db/Scripts/python.exe -m pytest -q tests/test_book_collector.py::test_live_btc_depth
Remove-Item Env:KRAKEN_BOOK_SMOKE
```

Offline fixtures are synthetic, isolated, and source-bound as injected_test. Live integration copies only existing public catalogue/audit metadata to a temporary initialized database and requests actual public Depth. Live operational evidence below is real and never seeded from fixtures.

## Actual public collection audit

Actual Kraken public observations captured 2026-11-01 UTC, never synthetic fixtures. Four single-pair/dataset commands succeeded for BTC-AUD and ETH-AUD. Two bounded 120-second all-AUD sessions used 20-second quote/book intervals and requested depth 10. The final bounded command exited **0**, selected all **14** verified pairs, had **0 errors**, and left **0 uncollected pair/datasets**. Four requests were normally deferred at its deadline. The initial session exposed a CLI deadline-exit classification defect; it was corrected and regression-tested before the final successful session.

The subsequent all-AUD repeat exited **0** and added **284 actual newly timestamped rows**. New observations are legitimate even when prices do not change. Replaying identical stored real records left all **28 partition file hashes unchanged**. Total: **173 quote observations**, **173 book snapshots**, **3330 book-level rows**. **179** saved public responses were reparsed and **3503 exact persisted rows** compared successfully. All 28 checkpoints match. SQLite integrity_check=ok; foreign_key_check=[]. Open/stored quote-book quality issues at capture: **0**. Stored duplicate/conflict counts and invalid/source errors are zero.

Collection actual frequency is sequential and request/verification limited. Mean spacing in the report includes time between operator-invoked sessions; those offline periods are not missing promised samples. All report within-session unresolved gap lists were empty at capture. Local freshness will naturally become stale after a bounded collector stops.

### Actual checkpoint SQL output

```sql
SELECT pair_id,dataset,latest_observation_at,updated_at
FROM ingestion_state WHERE dataset IN ('quotes_1m','orderbook')
ORDER BY pair_id,dataset;
```

| pair_id | dataset | latest_observation_at | updated_at |
|---|---|---|---|
| ADA-AUD | orderbook | 2026-11-01T09:46:19.332710Z | 2026-11-01T09:46:20.199824Z |
| ADA-AUD | quotes_1m | 2026-11-01T09:46:19.332710Z | 2026-11-01T09:46:19.920784Z |
| AUDX-AUD | orderbook | 2026-11-01T09:46:20.439577Z | 2026-11-01T09:46:21.154853Z |
| AUDX-AUD | quotes_1m | 2026-11-01T09:46:20.439577Z | 2026-11-01T09:46:20.976071Z |
| BCH-AUD | orderbook | 2026-11-01T09:46:21.545513Z | 2026-11-01T09:46:22.371885Z |
| BCH-AUD | quotes_1m | 2026-11-01T09:46:21.545513Z | 2026-11-01T09:46:22.056261Z |
| BTC-AUD | orderbook | 2026-11-01T09:46:22.652593Z | 2026-11-01T09:46:23.465704Z |
| BTC-AUD | quotes_1m | 2026-11-01T09:46:22.652593Z | 2026-11-01T09:46:23.121362Z |
| DOGE-AUD | orderbook | 2026-11-01T09:46:23.774956Z | 2026-11-01T09:46:25.531655Z |
| DOGE-AUD | quotes_1m | 2026-11-01T09:46:23.774956Z | 2026-11-01T09:46:24.328347Z |
| ETH-AUD | orderbook | 2026-11-01T09:46:25.720001Z | 2026-11-01T09:46:26.797727Z |
| ETH-AUD | quotes_1m | 2026-11-01T09:46:25.720001Z | 2026-11-01T09:46:26.325660Z |
| EUR-AUD | orderbook | 2026-11-01T09:46:27.010355Z | 2026-11-01T09:46:27.864337Z |
| EUR-AUD | quotes_1m | 2026-11-01T09:46:27.010355Z | 2026-11-01T09:46:27.546715Z |
| LINK-AUD | orderbook | 2026-11-01T09:46:28.111866Z | 2026-11-01T09:46:28.950336Z |
| LINK-AUD | quotes_1m | 2026-11-01T09:46:28.111866Z | 2026-11-01T09:46:28.582466Z |
| LTC-AUD | orderbook | 2026-11-01T09:46:29.225463Z | 2026-11-01T09:46:31.304121Z |
| LTC-AUD | quotes_1m | 2026-11-01T09:46:29.225463Z | 2026-11-01T09:46:30.401775Z |
| PEPE-AUD | orderbook | 2026-11-01T09:46:31.869634Z | 2026-11-01T09:46:33.482152Z |
| PEPE-AUD | quotes_1m | 2026-11-01T09:46:31.869634Z | 2026-11-01T09:46:32.716277Z |
| SOL-AUD | orderbook | 2026-11-01T09:46:33.882866Z | 2026-11-01T09:46:35.161935Z |
| SOL-AUD | quotes_1m | 2026-11-01T09:46:33.882866Z | 2026-11-01T09:46:34.559725Z |
| USDC-AUD | orderbook | 2026-11-01T09:46:35.395825Z | 2026-11-01T09:46:36.373766Z |
| USDC-AUD | quotes_1m | 2026-11-01T09:46:35.395825Z | 2026-11-01T09:46:35.978126Z |
| USDT-AUD | orderbook | 2026-11-01T09:46:36.562073Z | 2026-11-01T09:46:37.235116Z |
| USDT-AUD | quotes_1m | 2026-11-01T09:46:36.562073Z | 2026-11-01T09:46:37.037510Z |
| XRP-AUD | orderbook | 2026-11-01T09:46:37.662510Z | 2026-11-01T09:46:38.266987Z |
| XRP-AUD | quotes_1m | 2026-11-01T09:46:37.662510Z | 2026-11-01T09:46:38.092081Z |

### Actual per-pair audit

| Pair | Quotes | Book snapshots | Book level rows | Earliest quote UTC | Latest quote UTC | Latest book UTC | Quote checkpoint | Book checkpoint | Issues |
|---|---|---|---|---|---|---|---|---|---|
| ADA-AUD | 13 | 13 | 260 | 2026-11-01T09:40:01.350699Z | 2026-11-01T09:46:19.332710Z | 2026-11-01T09:46:19.332710Z | matches | matches | 0 |
| AUDX-AUD | 13 | 13 | 130 | 2026-11-01T09:40:02.465476Z | 2026-11-01T09:46:20.439577Z | 2026-11-01T09:46:20.439577Z | matches | matches | 0 |
| BCH-AUD | 13 | 13 | 260 | 2026-11-01T09:40:03.580966Z | 2026-11-01T09:46:21.545513Z | 2026-11-01T09:46:21.545513Z | matches | matches | 0 |
| BTC-AUD | 13 | 13 | 260 | 2026-11-01T09:39:53.119608Z | 2026-11-01T09:46:22.652593Z | 2026-11-01T09:46:22.652593Z | matches | matches | 0 |
| DOGE-AUD | 13 | 13 | 260 | 2026-11-01T09:40:04.702040Z | 2026-11-01T09:46:23.774956Z | 2026-11-01T09:46:23.774956Z | matches | matches | 0 |
| ETH-AUD | 13 | 13 | 260 | 2026-11-01T09:39:56.105901Z | 2026-11-01T09:46:25.720001Z | 2026-11-01T09:46:25.720001Z | matches | matches | 0 |
| EUR-AUD | 13 | 13 | 260 | 2026-11-01T09:40:05.820871Z | 2026-11-01T09:46:27.010355Z | 2026-11-01T09:46:27.010355Z | matches | matches | 0 |
| LINK-AUD | 13 | 13 | 260 | 2026-11-01T09:40:06.945158Z | 2026-11-01T09:46:28.111866Z | 2026-11-01T09:46:28.111866Z | matches | matches | 0 |
| LTC-AUD | 12 | 12 | 240 | 2026-11-01T09:40:08.062685Z | 2026-11-01T09:46:29.225463Z | 2026-11-01T09:46:29.225463Z | matches | matches | 0 |
| PEPE-AUD | 12 | 12 | 240 | 2026-11-01T09:40:09.176182Z | 2026-11-01T09:46:31.869634Z | 2026-11-01T09:46:31.869634Z | matches | matches | 0 |
| SOL-AUD | 12 | 12 | 240 | 2026-11-01T09:40:10.325512Z | 2026-11-01T09:46:33.882866Z | 2026-11-01T09:46:33.882866Z | matches | matches | 0 |
| USDC-AUD | 11 | 11 | 220 | 2026-11-01T09:40:11.531230Z | 2026-11-01T09:46:35.395825Z | 2026-11-01T09:46:35.395825Z | matches | matches | 0 |
| USDT-AUD | 11 | 11 | 220 | 2026-11-01T09:40:12.935141Z | 2026-11-01T09:46:36.562073Z | 2026-11-01T09:46:36.562073Z | matches | matches | 0 |
| XRP-AUD | 11 | 11 | 220 | 2026-11-01T09:40:14.177199Z | 2026-11-01T09:46:37.662510Z | 2026-11-01T09:46:37.662510Z | matches | matches | 0 |

### Real persisted samples

Prices are AUD/base, sizes are base units. Timestamps are local UTC observation times. Exact decimal values below retain storage precision. These are actual Kraken records, including zero quantities if supplied, not synthetic data.

#### BTC-AUD quotes

| timestamp | bid | ask | bid_size | ask_size |
|---|---|---|---|---|
| 2026-11-01T09:39:53.119608Z | 110917.800000000000000000 | 111024.900000000000000000 | 0.001000000000000000 | 0.001000000000000000 |
| 2026-11-01T09:40:16.346549Z | 110872.100000000000000000 | 111010.000000000000000000 | 0.153000000000000000 | 0.213000000000000000 |
| 2026-11-01T09:40:38.808232Z | 110877.300000000000000000 | 110981.700000000000000000 | 0.001000000000000000 | 0.019000000000000000 |
| 2026-11-01T09:41:01.693098Z | 110881.400000000000000000 | 110951.100000000000000000 | 0.001000000000000000 | 0.001000000000000000 |
| 2026-11-01T09:41:24.223809Z | 110874.300000000000000000 | 110940.000000000000000000 | 0.001000000000000000 | 0.001000000000000000 |

#### BTC-AUD book levels

| timestamp | side | level | price | quantity |
|---|---|---|---|---|
| 2026-11-01T09:39:54.731724Z | bid | 1 | 110917.800000000000000000 | 0.001000000000000000 |
| 2026-11-01T09:39:54.731724Z | bid | 2 | 110917.600000000000000000 | 0.790000000000000000 |
| 2026-11-01T09:39:54.731724Z | bid | 3 | 110917.100000000000000000 | 0.001000000000000000 |
| 2026-11-01T09:39:54.731724Z | ask | 1 | 111024.900000000000000000 | 0.001000000000000000 |
| 2026-11-01T09:39:54.731724Z | ask | 2 | 111025.000000000000000000 | 0.010000000000000000 |

#### ETH-AUD quotes

| timestamp | bid | ask | bid_size | ask_size |
|---|---|---|---|---|
| 2026-11-01T09:39:56.105901Z | 3226.850000000000000000 | 3230.080000000000000000 | 2.506000000000000000 | 2.506000000000000000 |
| 2026-11-01T09:40:17.711934Z | 3225.740000000000000000 | 3229.460000000000000000 | 20.511000000000000000 | 5.910000000000000000 |
| 2026-11-01T09:40:39.901346Z | 3226.220000000000000000 | 3230.060000000000000000 | 2.506000000000000000 | 0.696000000000000000 |
| 2026-11-01T09:41:02.796065Z | 3226.630000000000000000 | 3230.060000000000000000 | 2.506000000000000000 | 2.506000000000000000 |
| 2026-11-01T09:41:26.104384Z | 3227.070000000000000000 | 3230.020000000000000000 | 2.506000000000000000 | 2.506000000000000000 |

#### ETH-AUD book levels

| timestamp | side | level | price | quantity |
|---|---|---|---|---|
| 2026-11-01T09:39:58.634173Z | bid | 1 | 3226.890000000000000000 | 2.506000000000000000 |
| 2026-11-01T09:39:58.634173Z | bid | 2 | 3226.880000000000000000 | 20.511000000000000000 |
| 2026-11-01T09:39:58.634173Z | bid | 3 | 3226.010000000000000000 | 0.200000000000000000 |
| 2026-11-01T09:39:58.634173Z | ask | 1 | 3230.080000000000000000 | 2.506000000000000000 |
| 2026-11-01T09:39:58.634173Z | ask | 2 | 3230.090000000000000000 | 0.896000000000000000 |

### Actual received depth

| Pair | Observed (bid levels, ask levels) |
|---|---|
| ADA-AUD | [(10, 10)] |
| AUDX-AUD | [(7, 3)] |
| BCH-AUD | [(10, 10)] |
| BTC-AUD | [(10, 10)] |
| DOGE-AUD | [(10, 10)] |
| ETH-AUD | [(10, 10)] |
| EUR-AUD | [(10, 10)] |
| LINK-AUD | [(10, 10)] |
| LTC-AUD | [(10, 10)] |
| PEPE-AUD | [(10, 10)] |
| SOL-AUD | [(10, 10)] |
| USDC-AUD | [(10, 10)] |
| USDT-AUD | [(10, 10)] |
| XRP-AUD | [(10, 10)] |

Fewer levels are preserved as returned; none are padded. These are complete returned top-depth snapshots. All sides and ordering were validated.


## Final local verification receipt

- Focused offline: **40 passed, 1 skipped** (opt-in public integration).
- Full offline regression: **651 passed, 5 skipped**. Skips: four opt-in public environment/catalogue/OHLCV/book integrations and privileged Windows symlink creation.
- Final opt-in real BTC/AUD public depth integration: **1 passed**.
- Actual report CLI: exit 0; no network or writes. Compilation passed.
- Preservation manifest: **24,093 preexisting files unchanged**, excluding only the authorized existing public endpoint allowlist and metadata.sqlite operational updates. This includes existing notebooks, credentials, research artifacts, price partitions and previous catalogue receipts.
- Credential-value scan of implementation/tests/docs passed without printing any values. Generated SQLite/Parquet/receipts remain ignored; none are tracked.
- No schema, locked universe, daily research, strategy, execution or future-milestone changes.
