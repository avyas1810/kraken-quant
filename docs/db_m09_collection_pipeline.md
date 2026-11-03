# Database Milestone 9: local public collection pipeline

## Scope

`kraken_quant.data_pipeline` is a small local process wrapping the existing discovery, OHLCV, quote/book, fee, history and optional eligibility components. It does not implement financial algorithms, trades, a new database, an orchestration platform, archive downloads or a production service. Public requests only; no credential adapter is imported or invoked. Account-specific fees remain outside this pipeline and require the separate existing explicitly authorized read-only workflow.

PR #9 was verified merged into `review-base/milestone1-20261029` at `c57825cdaeca14955f3a8e3e80d42cd33d586889`, with ancestry checked before creating `feature/db-m09-collection-pipeline`. Existing notebook modifications and research configurations are preserved.

## Commands and shutdown

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.data_pipeline dry-run --all-aud --config config/data_pipeline.example.json
.venv-db/Scripts/python.exe -m kraken_quant.data_pipeline once --all-aud --config config/data_pipeline.example.json
.venv-db/Scripts/python.exe -m kraken_quant.data_pipeline run --all-aud --config config/data_pipeline.example.json
.venv-db/Scripts/python.exe -m kraken_quant.data_pipeline status --all-aud
.venv-db/Scripts/python.exe -m kraken_quant.data_pipeline validate --all-aud
.venv-db/Scripts/python.exe -m kraken_quant.data_pipeline once --pair BTC-AUD
```

Use `--root`/`--data-dir` for established data storage configuration. The initialized, recognized metadata database must already exist; the pipeline never initializes an unknown file. Configuration is one JSON file with typed, bounded operational settings. CLI scope overrides configuration. Optional `--max-cycles 2` and `--duration-seconds 120` bound a continuous session. The duration limit is checked between cycles/waits; an in-flight request (existing ten-second timeout), collector atomic persistence and read-only checks complete safely. A request/time budget separately bounds each cycle's network activity. Continuous mode is started only by an operator; validation leaves no process running.

Press Ctrl+C to stop. The scheduler stops polling, retains the interrupted-cycle marker when a task was interrupted, and releases its OS lock. Existing collectors finish or leave their established pending intents for replay. A stopped process is not evidence of complete coverage.

## Dependency order and scope

1. `asset_catalogue.discover`: complete approved metadata refresh.
2. `OhlcvCollector.collect`: completed one-minute candles; bounded recent repair when needed.
3. `BookCollector.collect`: complete public REST Depth snapshots, with linked best quote and book.
4. `refresh_fees`: official public schedule only, `readonly=None`.
5. Existing history coverage, data-quality and exact receipt-backed checks.
6. Optional `EligibilityManager`: disabled by default, factual screening and reserved placeholders only.
7. Atomic cycle journal and concise per-market health report.

Due tasks retain this order. A required metadata-refresh failure blocks dependent network collection and eligibility during that cycle; read-only diagnostics can still run. When metadata is not due or deliberately disabled, collection requires a current consistent approved catalogue and bounded metadata age. Market scope comes from the verified SQLite catalogue/latest discovery audit, not the research universe or a fixed count. Unresolved broader class/venue records are not reclassified by pipeline logic.

Changing configuration causes due work to be reconsidered immediately. Changing pair/all-AUD scope does not reuse another scope's task success/schedule state; earlier journals and per-market checkpoints remain intact. Only one pipeline may operate in a data directory, irrespective of selected scope. Individual collectors retain their own locks, including protection against manual concurrent collector invocations.

## Cadence and monotonic scheduling

Example defaults: OHLCV and combined quote/book task 60 seconds; metadata, fees and coverage 86400 seconds; eligibility disabled (86400 seconds if explicitly enabled). Book depth defaults to ten per side. Tasks and cadences can be configured independently; quote and book acquisition intentionally share one REST snapshot and cadence so their identities remain linked.

Within a running process, intended deadlines advance on a monotonic schedule anchored to intended slots, not completion-plus-sleep. Missed slots are skipped, with delay recorded, instead of issuing a burst of catch-up requests. Persisted UTC `next_due_at` resumes scheduling after restart; wall-clock-to-monotonic conversion occurs at startup, not every sleep. Metadata freshness and public pacing reject clock regression where relevant. Operational UTC records preserve scheduled time, actual task start/finish and delay separately. Public request records preserve request start and receipt times; collector journals/receipts preserve persistence completion and source event/observation times. A task's last attempt and last fully successful completion are separate.

Do not interpret task cadence as a trading horizon. Do not claim perfect minute capture from polling frequency: actual coverage is reported separately.

## Public transport, rate limits and retries

The only deliberate existing-interface change is an optional task-local `ContextVar` policy in `market_data.public_get`. With no policy, existing callers behave identically. During a pipeline cycle, the policy wraps the existing HTTP function; no second HTTP client or replacement collector parser is introduced, and collectors retain their true `kraken_public` storage namespace.

The policy shares a minimum 1.1-second request-start spacing across discovery, OHLCV, depth, fees and repair. Existing collector-specific pacing is also retained. It records the last public request across restarts. This does not coordinate other applications/IP users; operators must budget for other public clients.

Kraken states that [public calls at one per second or less remain within its limits](https://support.kraken.com/ca/articles/206548367-what-are-the-api-rate-limits-). Public rate limits depend on IP and, for OHLC/Trades, pair. Default steady polling requires approximately one OHLC plus one Depth call per selected market per minute; quotes are derived from the same Depth snapshot. For fourteen markets this is 28 calls/minute before retry/startup/daily work. This is a request-count plan, not a latency or coverage guarantee. Measure actual durations on the deployment machine and IP.

Default requests attempt at most twice with five-second exponential backoff, bounded by 60 seconds. HTTP 429, transient server/network errors and recognized exchange throttling/service-unavailable responses are eligible for retry. Validation errors are not financial calculations and are not repeatedly retried. Retry-After is honored when inside the configured wait bound; longer delays stop the attempt rather than retrying early. Exhausted throttling sets a persisted circuit-breaker cooldown, deferring other markets instead of repeating a per-market throttle storm. Subsequent attempts remain on their task cadence, not a tight retry loop. Default cycle budgets are 150 requests and 600 seconds of request activity; raw errors, headers and secrets are never journaled.

## Recovery and preservation

The scheduler records an active cycle before work and updates task attempts durably. An active marker at restart identifies an interrupted cycle. Collectors retain per-partition atomic replacement, conflict checks, source receipts, pending-intent replay, verified persistence before checkpoint advancement and monotonic checkpoint reconciliation. The pipeline does not claim SQLite/Parquet distributed transaction atomicity.

Normal OHLC collection resumes through existing checkpoints. On first use, a sufficiently long shutdown, or an interrupted cycle, the existing HistoryManager repairs only the most recent 720-minute window. It validates overlaps, preserves genuine zero-volume candles, rejects conflicting values and reports partial/unavailable intervals. Fresh ordinary restarts avoid redundant repair. No old-date loop attempts to overcome Kraken retention. Quote/book recovery replays pending snapshot intents and reconciles each dataset independently. Partial market failures are retained with sanitized codes; successful markets are preserved and other tasks continue unless their metadata dependency failed.

No observation is manufactured, zero-filled or backdated. Quote/book timestamps remain local REST snapshot observation times, not certified exchange-wide event times. Candle opening timestamps remain original UTC Kraken event timestamps. Source receipts and existing schemas retain exact monetary values and complete snapshot levels.

## Offline status, validation and health

`dry-run` reports due work without creating locks, directories, files or requests. `status` reads persisted task state and actual collector storage/checkpoints offline, recalculating ages at actual current UTC. `validate` performs read-only SQLite integrity/foreign-key checks, existing 90-day history coverage and receipt-backed history/quote/book provenance checks. Missing datasets/provenance are errors, not successful validation. History insufficiency is reported separately from integrity failure.

Per-market status includes latest OHLCV/quote/book, observation ages, checkpoint consistency, stored candles, quote observations, book snapshot counts, history span, missing-minute count, health and last error. Healthy/delayed/failing/unavailable describes collection health only. Freshness defaults to 180 seconds for OHLCV and quote/book. Status must not treat stale observations as current or indicate research profitability. Quote/book sampled coverage is distinct from minute candle completeness.

Output locations under the configured existing data directory:

```text
data/collection_pipeline/state.json
data/collection_pipeline/last_public_request.json
data/collection_pipeline/latest.json
data/collection_pipeline/cycles/<run-id>.json
```

Existing metadata, market partitions, collector receipts/checkpoints, history availability and fee snapshots stay at their approved locations. Only pipeline-owned cycle journals are bounded (1000 by default). Market data and provenance are retained to support reproducibility; there is no automatic raw-data deletion. Disk growth from indefinitely retained raw data needs an operator-managed capacity plan. No generated output is committed to Git. Process memory retains only the current cycle; validation reuses existing readers, some of which load a pair's complete retained history. Larger-than-current research histories may require a later measured reader optimization, not a second storage system.

## Automated tests

```powershell
.venv-db/Scripts/python.exe -m pytest -q tests/test_data_pipeline.py
.venv-db/Scripts/python.exe -m pytest -q tests
```

Synthetic isolated fixtures cover ordering, due selection, policy changes, monotonic no-drift scheduling, skipped overrun slots, bounded sessions, clean Ctrl+C/restart, lock contention, partial failures, metadata dependency failures, sanitized errors, pacing/restart cooldown, timeout/throttle budgets, real collector reuse, exact stored/checkpoint health, offline non-mutation and all cost placeholder exceptions. No synthetic fixture is copied into production storage.

## Actual validation

All observations below are actual public Kraken data. No synthetic fixture was used in production. One complete `once` cycle and a bounded two-cycle `run --max-cycles 2` session completed; no collector process remains active. Eligibility was disabled.

| Cycle | Duration seconds | Public requests | Tasks | Result |
|---|---:|---:|---|---|
| 4f7426aebb4a4ea6b72d7d5d75b3a0d0 | 164.750 | 42 | metadata, ohlcv, books, fees, coverage | finished |
| 13a7ab44fdfd4c9d9e89e20dff268fa0 | 63.718 | 43 | ohlcv, books | finished |
| f9b3c24bf6fd438c9d93c904d17f2fd6 | 35.875 | 14 | ohlcv, books | finished |

Mean cycle duration: **88.114 seconds**; maximum **164.750 seconds**. These three bounded cycles are a deployment sample, not a long-term uptime/coverage guarantee. Startup includes discovery, repair, fees and a 94.516-second daily read-only coverage/provenance audit. The restart cycle includes recent repair; the final 35.875-second cycle performed fourteen OHLC requests and correctly skipped all not-yet-due book samples. Total public requests: **99**; network/exchange errors and throttles: **0**. Minimum measured UTC request-start spacing was **1.093383 seconds** (monotonic pacing configured at 1.1 seconds), above the official one-second guidance.

**Cadence finding:** A strict one-minute all-task cycle is not supported by these measurements: startup took 164.750 seconds and repair restart took 63.718 seconds. Daily validation blocks this single-process scheduler while it runs. OHLC can recover accessible recent minutes afterward; missed quote/book intervals cannot be reconstructed. Fixed deadlines plus the collector's actual-observation sampling gate can skip a book poll when it is too early. No one-minute quote completeness is claimed. A conservative initial operating plan is OHLC every 60 seconds and books every 120 seconds, leaving extra pacing/retry capacity; change `cadences.books` explicitly and monitor actual coverage. This is a proposal, not an automatically activated configuration. A full recurring 28-request cycle has not been proven to finish under 60 seconds by this short sample. Do not lower pacing to force throughput.

End-of-final-cycle health at 2026-11-03T08:13:17.730920Z (offline status recalculates ages and later correctly marks observations delayed):

| Pair | New OHLCV rows | New quotes / book snapshots | Latest OHLCV UTC | Latest book UTC | Health at cycle end | Errors |
|---|---:|---:|---|---|---|---|
| ADA-AUD | 197 | 2 / 2 | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:28.684408Z | healthy | none |
| AUDX-AUD | 0 | 2 / 2 | 2026-11-01T08:28:00.000000Z | 2026-11-03T08:12:29.795972Z | delayed | none |
| BCH-AUD | 0 | 2 / 2 | 2026-11-01T08:28:00.000000Z | 2026-11-03T08:12:30.912290Z | delayed | none |
| BTC-AUD | 197 | 2 / 2 | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:32.031657Z | healthy | none |
| DOGE-AUD | 197 | 2 / 2 | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:33.148887Z | healthy | none |
| ETH-AUD | 197 | 2 / 2 | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:34.287334Z | healthy | none |
| EUR-AUD | 197 | 2 / 2 | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:35.414253Z | healthy | none |
| LINK-AUD | 197 | 2 / 2 | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:36.520983Z | healthy | none |
| LTC-AUD | 198 | 2 / 2 | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:12:37.615952Z | healthy | none |
| PEPE-AUD | 197 | 2 / 2 | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:38.759130Z | healthy | none |
| SOL-AUD | 198 | 2 / 2 | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:12:39.864852Z | healthy | none |
| USDC-AUD | 198 | 2 / 2 | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:12:41.064017Z | healthy | none |
| USDT-AUD | 198 | 2 / 2 | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:12:42.284294Z | healthy | none |
| XRP-AUD | 198 | 2 / 2 | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:12:43.401113Z | healthy | none |

AUDX/BCH returned no new completed candles in these runs. Their old checkpoints match genuine stored data and are visibly delayed; no zero-volume substitute was manufactured. Other markets have genuine returned candles but no claim is made that their latest API candle equals the immediately preceding wall-clock minute.

Actual checkpoint SQL:

```sql
SELECT pair_id,dataset,latest_observation_at,updated_at
FROM ingestion_state
WHERE dataset IN ('prices_1m','quotes_1m','orderbook')
ORDER BY pair_id,dataset;
```

| Pair | Dataset | Latest observation UTC | Checkpoint updated UTC |
|---|---|---|---|
| ADA-AUD | orderbook | 2026-11-03T08:12:28.684408Z | 2026-11-03T08:13:15.428846Z |
| ADA-AUD | prices_1m | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:50.128009Z |
| ADA-AUD | quotes_1m | 2026-11-03T08:12:28.684408Z | 2026-11-03T08:13:15.377892Z |
| AUDX-AUD | orderbook | 2026-11-03T08:12:29.795972Z | 2026-11-03T08:13:15.548362Z |
| AUDX-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-03T08:12:50.307048Z |
| AUDX-AUD | quotes_1m | 2026-11-03T08:12:29.795972Z | 2026-11-03T08:13:15.489886Z |
| BCH-AUD | orderbook | 2026-11-03T08:12:30.912290Z | 2026-11-03T08:13:15.649314Z |
| BCH-AUD | prices_1m | 2026-11-01T08:28:00.000000Z | 2026-11-03T08:12:50.972331Z |
| BCH-AUD | quotes_1m | 2026-11-03T08:12:30.912290Z | 2026-11-03T08:13:15.601609Z |
| BTC-AUD | orderbook | 2026-11-03T08:12:32.031657Z | 2026-11-03T08:13:15.776729Z |
| BTC-AUD | prices_1m | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:53.771683Z |
| BTC-AUD | quotes_1m | 2026-11-03T08:12:32.031657Z | 2026-11-03T08:13:15.697971Z |
| DOGE-AUD | orderbook | 2026-11-03T08:12:33.148887Z | 2026-11-03T08:13:15.983751Z |
| DOGE-AUD | prices_1m | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:55.294913Z |
| DOGE-AUD | quotes_1m | 2026-11-03T08:12:33.148887Z | 2026-11-03T08:13:15.847084Z |
| ETH-AUD | orderbook | 2026-11-03T08:12:34.287334Z | 2026-11-03T08:13:16.158642Z |
| ETH-AUD | prices_1m | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:56.874900Z |
| ETH-AUD | quotes_1m | 2026-11-03T08:12:34.287334Z | 2026-11-03T08:13:16.047619Z |
| EUR-AUD | orderbook | 2026-11-03T08:12:35.414253Z | 2026-11-03T08:13:16.380341Z |
| EUR-AUD | prices_1m | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:58.200777Z |
| EUR-AUD | quotes_1m | 2026-11-03T08:12:35.414253Z | 2026-11-03T08:13:16.247687Z |
| LINK-AUD | orderbook | 2026-11-03T08:12:36.520983Z | 2026-11-03T08:13:16.588604Z |
| LINK-AUD | prices_1m | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:12:59.703197Z |
| LINK-AUD | quotes_1m | 2026-11-03T08:12:36.520983Z | 2026-11-03T08:13:16.476189Z |
| LTC-AUD | orderbook | 2026-11-03T08:12:37.615952Z | 2026-11-03T08:13:16.782195Z |
| LTC-AUD | prices_1m | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:13:02.329159Z |
| LTC-AUD | quotes_1m | 2026-11-03T08:12:37.615952Z | 2026-11-03T08:13:16.668554Z |
| PEPE-AUD | orderbook | 2026-11-03T08:12:38.759130Z | 2026-11-03T08:13:16.955962Z |
| PEPE-AUD | prices_1m | 2026-11-03T08:11:00.000000Z | 2026-11-03T08:13:03.030999Z |
| PEPE-AUD | quotes_1m | 2026-11-03T08:12:38.759130Z | 2026-11-03T08:13:16.827933Z |
| SOL-AUD | orderbook | 2026-11-03T08:12:39.864852Z | 2026-11-03T08:13:17.130189Z |
| SOL-AUD | prices_1m | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:13:06.743308Z |
| SOL-AUD | quotes_1m | 2026-11-03T08:12:39.864852Z | 2026-11-03T08:13:17.018804Z |
| USDC-AUD | orderbook | 2026-11-03T08:12:41.064017Z | 2026-11-03T08:13:17.291361Z |
| USDC-AUD | prices_1m | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:13:09.446486Z |
| USDC-AUD | quotes_1m | 2026-11-03T08:12:41.064017Z | 2026-11-03T08:13:17.193652Z |
| USDT-AUD | orderbook | 2026-11-03T08:12:42.284294Z | 2026-11-03T08:13:17.427648Z |
| USDT-AUD | prices_1m | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:13:12.285873Z |
| USDT-AUD | quotes_1m | 2026-11-03T08:12:42.284294Z | 2026-11-03T08:13:17.352686Z |
| XRP-AUD | orderbook | 2026-11-03T08:12:43.401113Z | 2026-11-03T08:13:17.656440Z |
| XRP-AUD | prices_1m | 2026-11-03T08:12:00.000000Z | 2026-11-03T08:13:14.845281Z |
| XRP-AUD | quotes_1m | 2026-11-03T08:12:43.401113Z | 2026-11-03T08:13:17.510886Z |

All **42 checkpoints** match verified Parquet maxima. Integrity check **ok**; foreign-key violations **0**. No duplicate logical keys were found. All **22243 original market records** retained exact hashes after collection. Twenty-five M6 input manifests load; both original M8 reports retain their original SQL projections and verify against original pinned input selections. Dry-run and status hashes proved non-mutation; an actual `validate --all-aud` also returned ok for fourteen pairs with no data-file changes.

### Confirmed compatibility fix

Real append validation exposed an M8 verification bug: a raw response contained an old completed row outside a prior bounded ingestion request. Later legitimate persistence of that row enlarged fresh historical reconstruction and changed the old report hash. The report itself and original records had not changed. `verify_evidence` now pins its original timestamp selection and source receipt set, still recalculating values and checking dependency hashes. New reports record all selected intervals; legacy reports can use their complete coverage intervals only when those intervals represent every originally known record. Unrepresentable legacy selections fail clearly. Missing/changed original dependencies still fail verification. This is an existing-interface bug fix required to preserve M8 reproducibility, not a new eligibility algorithm or schema migration. New independent evaluations may legitimately see additional old-source rows and remain separate immutable reports; old reports are never rewritten.

### Actual raw examples

The following are exact last persisted BTC/ETH rows (Decimal strings retain precision). Quote/book timestamps are local observations; OHLC timestamps are Kraken candle opens.

```json
[
  {
    "dataset": "prices_1m",
    "record": {
      "pair_id": "BTC-AUD",
      "timestamp": "2026-11-03T08:11:00.000000Z",
      "open": "109663.000000000000000000",
      "high": "109663.000000000000000000",
      "low": "109663.000000000000000000",
      "close": "109663.000000000000000000",
      "volume_base": "0E-18",
      "trade_count": 0,
      "is_complete": true
    }
  },
  {
    "dataset": "quotes_1m",
    "record": {
      "pair_id": "BTC-AUD",
      "timestamp": "2026-11-03T08:12:32.031657Z",
      "bid": "109652.100000000000000000",
      "ask": "109717.600000000000000000",
      "bid_size": "0.009000000000000000",
      "ask_size": "0.001000000000000000"
    }
  },
  {
    "dataset": "orderbook",
    "record": {
      "pair_id": "BTC-AUD",
      "timestamp": "2026-11-03T08:12:32.031657Z",
      "side": "ask",
      "level": 1,
      "price": "109717.600000000000000000",
      "quantity": "0.001000000000000000"
    }
  },
  {
    "dataset": "prices_1m",
    "record": {
      "pair_id": "ETH-AUD",
      "timestamp": "2026-11-03T08:11:00.000000Z",
      "open": "3227.390000000000000000",
      "high": "3227.390000000000000000",
      "low": "3227.390000000000000000",
      "close": "3227.390000000000000000",
      "volume_base": "0E-18",
      "trade_count": 0,
      "is_complete": true
    }
  },
  {
    "dataset": "quotes_1m",
    "record": {
      "pair_id": "ETH-AUD",
      "timestamp": "2026-11-03T08:12:34.287334Z",
      "bid": "3223.230000000000000000",
      "ask": "3226.390000000000000000",
      "bid_size": "0.218000000000000000",
      "ask_size": "2.005000000000000000"
    }
  },
  {
    "dataset": "orderbook",
    "record": {
      "pair_id": "ETH-AUD",
      "timestamp": "2026-11-03T08:12:34.287334Z",
      "side": "ask",
      "level": 1,
      "price": "3226.390000000000000000",
      "quantity": "2.005000000000000000"
    }
  }
]
```

### Actual research availability

| Pair | Stored candles | Historical span days | 90-day completeness % | Missing minutes in requested window | 90 days available |
|---|---:|---:|---:|---:|---|
| ADA-AUD | 1639 | 2.489583333 | 1.264660494 | 127961 | no |
| AUDX-AUD | 722 | 0.501388889 | 0.557098765 | 128878 | no |
| BCH-AUD | 722 | 0.501388889 | 0.557098765 | 128878 | no |
| BTC-AUD | 1639 | 2.489583333 | 1.264660494 | 127961 | no |
| DOGE-AUD | 1639 | 2.489583333 | 1.264660494 | 127961 | no |
| ETH-AUD | 1639 | 2.489583333 | 1.264660494 | 127961 | no |
| EUR-AUD | 1638 | 2.489583333 | 1.263888889 | 127962 | no |
| LINK-AUD | 1638 | 2.489583333 | 1.263888889 | 127962 | no |
| LTC-AUD | 1639 | 2.490277778 | 1.264660494 | 127961 | no |
| PEPE-AUD | 1638 | 2.489583333 | 1.263888889 | 127962 | no |
| SOL-AUD | 1639 | 2.490277778 | 1.264660494 | 127961 | no |
| USDC-AUD | 1639 | 2.490277778 | 1.264660494 | 127961 | no |
| USDT-AUD | 1639 | 2.490277778 | 1.264660494 | 127961 | no |
| XRP-AUD | 1639 | 2.490277778 | 1.264660494 | 127961 | no |

No market meets the ninety-day requirement. Missing means absent locally; recent repair results retain the source availability distinction. Legacy long gaps are not interpreted as evidence of no trades. The initial all-task coverage audit and explicit read-only validate both passed integrity while reporting insufficient history.


## Research-enablement boundary

Kraken's [official OHLC contract](https://docs.kraken.com/api-reference/market-data/get-ohlc-data) exposes at most 720 recent entries, including the final uncommitted candle, regardless of `since`. New minute observations now accumulate automatically while the local process runs. Recent missing minutes can be repaired only while available. Older local gaps remain unavailable through REST; hypothetical archive coverage is not counted as observed data.

The database does not yet establish a complete 90-day research window. The shortest practical next step after Milestone 10 is an explicitly approved audit of Kraken's official downloadable historical archive (or another user-approved genuine historical source), checking AUD pair coverage, timestamps, provenance and compatibility before any import. No archive or third-party dataset has been downloaded or integrated here. Fresh ongoing collection supplements an approved historical dataset; it does not retroactively manufacture missing history. All mathematical financial algorithms remain reserved for user implementation after Milestone 10.

## Final verification

Focused command (pipeline plus eligibility compatibility): **102 passed**. The pipeline contributes 39 cases and the eligibility suite 63. Full offline suite: **856 passed, 7 skipped**. GitHub Actions results are recorded in the PR delivery report. Compilation, credential-value scan and tracked-data exclusion checks passed. Existing research, notebook, credential and reserved-algorithm hashes remain unchanged. All operational collection processes stopped after bounded validation.
