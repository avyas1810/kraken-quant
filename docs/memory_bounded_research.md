# Bounded research access to existing Kraken observations

## Use

Canonical files remain `data/market_data/<PAIR>/prices_1m/YYYY-MM.parquet`.
The existing `ResearchData` interface now provides context-managed, single-use
Arrow batch and event cursors without another database or market-data copy.

```python
from kraken_quant.research_data import ResearchData

reader = ResearchData("data")
with reader.load_market_data(
    pairs=["BTC-AUD", "ETH-USD"],
    start="2026-07-01", end="2026-10-01",
    columns=["timestamp", "close", "volume_base"],
    batch_size=10000, memory_limit_mb=3072,
) as stream:
    for batch in stream:
        # Consume and discard each batch. Do not list/concatenate the stream.
        print(batch.num_rows, batch.schema)
    print(stream.verification)  # Monthly counts and original source references.
```

Ranges are `[start,end)`. Date-only strings explicitly mean UTC; datetime values
must be timezone-aware. Exact decimal128 values, UTC microseconds, field units
and schema-version metadata are preserved. Logical keys are always retained:
`pair_id`, `timestamp`, and book `side`/`level`. Optional `dataset` selects
`quotes_1m` or `orderbook`; snapshots keep all original levels unless the caller
projects value columns. Missing datasets return no invented observations.

Raw queryability is **not admission to a research study**. Use existing M12
`continuity()`/`load_for_research()` for EVENT_TIME, STRICT_GRID, ASOF_REFERENCE,
source-backed no-trade classification, receipt-time validation and explicit
quality requirements. No universal density threshold is introduced. An absent
candle is not automatically no trade, and a last trade is not an executable quote.

## Memory, validation and storage

Existing `MonthlyParquetStore`, schemas, exact historical CSV/dependency checks,
source receipts and `ResearchData._load` remain the integrity authorities.
Verification completes before the first batch is released, one asset/month at
a time. All canonical fields are needed for exact source equality. Arrow time
filtering now precedes Python-row materialization in the old loader; exclusive
month ends no longer parse the following month's rows. Historical-first lookup
avoids unrelated REST indexing when selected rows match verified historical
sources exactly; REST remains the fallback. Unverifiable selected rows fail closed.

Replay then uses Parquet projection and predicate pushdown, without pandas or
NumPy intermediates. A heap merges one Arrow cursor per asset into deterministic
original-timestamp/pair order; snapshot logical keys order ties. The output batch
is constructed once from merged selected values. There is no universe-sized
DataFrame, full-dataset cache, disk spill or persisted materialization.

Limits: 1-10,000 rows per batch, at most 128 assets, existing bounded time ranges,
and a 256-4096 MiB process working-set budget (3072 MiB default). Conservative
preflights allow 8192 bytes per physical monthly source row and 16384 bytes per
pinned phase input row. Native working set is checked between bounded units and
batches. Windows uses `GetProcessMemoryInfo`; Linux uses `/proc/self/status`.
These are resident/OS high-water measurements, not Python allocation estimates.
The Windows native binding is cached once, so sampling itself cannot accumulate
new ctypes pointer types. Repeated sampling is covered by a regression test.

Budget failures raise a clear `MemoryError`; they never relax research criteria
or drop assets. This is a cooperative process guard, not an OS reservation
against arbitrary consumer allocations. Consumers must not retain the full
stream. An unusually large validation unit can be conservatively rejected.
The old pandas-returning convenience APIs remain available but are not bounded.

Use `with`, also on early exit. Iterator cleanup closes scanners on failure.
This access path creates no temporary files. Canonical file identities are
pinned and rechecked; concurrent replacement aborts with a retry error instead
of mixing snapshots. Research takes no collector locks. Fixed historical ranges
avoid ordinary current-month replacement conflicts while collection continues.

## Guarded research and event replay

```python
with reader.stream_partition_phase(
    manifest_id, "training", purpose="fit",
    columns=["close", "volume_base"], batch_size=4096,
) as stream:
    for event in stream.events():
        print(event.pair_id, event.timestamp, event.available_at, event.fields)
```

The existing M13 guarded loader validates one asset/phase at a time before
replay. Source pins, M12 admission, fixed/automatic-window governance, lookback,
fold, purpose and immutable hashes remain authoritative. Exact sample hashes
and counts are checked again before release. Independent partitions cannot be
combined into a joint plan. Registration precedes research release; test access
records exposure. The actual benchmark inspects **training only**, with no
strategy-performance evaluation or holdout access.

`events()` exposes a completed candle no earlier than opening plus one minute,
while preserving the original timestamp. Its timing is explicitly retrospective
source-event time, not proof that an imported archive was received live.
Use `available_at` to advance a replay clock, not the candle opening timestamp.
Receipt-aware plans must use the existing M12 decision-time release API; this
event adapter rejects that mode. Candle replay requires a minute-aligned end.
Unadmitted data inspection requires explicit `events(allow_unadmitted=True)`;
that does not authorize a strategy run or deployment.

Legacy locked daily backtests, currencies, strategies, accounting and execution
are unchanged. They are not silently adapted into mixed-currency 64-pair minute
strategies. This is a data adapter, not a new simulator or a guarantee against
arbitrary caller/model leakage. No prices are forward-filled or converted to
executable bids/asks; no financial algorithms or transaction costs are calculated.

`PartitionAlgorithm.assess_partition_request(request)` reuses M13 for read-only
sufficiency inventories. It creates no manifest and returns `executable=False`,
`manifest_id=None`. Independently sufficient markets are not a joint admission.

## Reproduction

Use the existing environment, from the repository:

```powershell
.venv-db/Scripts/python.exe -m examples.research_stream_benchmark --scope day
.venv-db/Scripts/python.exe -m examples.research_stream_benchmark --scope quarter
.venv-db/Scripts/python.exe -m examples.research_stream_benchmark --scope ten
.venv-db/Scripts/python.exe -m examples.research_stream_benchmark --scope all
.venv-db/Scripts/python.exe -m examples.research_stream_benchmark --scope all --batch-size 4096
.venv-db/Scripts/python.exe -m examples.research_stream_benchmark --scope partition
.venv-db/Scripts/python.exe -m pytest -q tests
```

The consumer hashes exact projected observations and checks chronological order
without retaining the dataset. The partition scope replays the existing
50-market training phase. Detailed machine-local audits are under
`.shadow-runtime/bounded-*`. Run expensive scenarios sequentially on a 16 GB
machine; original-source verification is CPU-intensive even with bounded RAM.

## Measured validation (12 November 2026)

Inventory: **9,821,943 genuine stored price rows, 64 pairs**. Every price partition passed the existing schema/integrity/key checks; all 64 SQLite checkpoints matched Parquet. SQLite integrity was `ok`, foreign-key errors zero. The 195 protected hashes and 157 source receipt hashes belonging to the approved plan verified unchanged. The existing read-only DuckDB expanded view returned the same 9,821,943 rows / 64 pairs. All three existing exports passed immutable manifest, CSV checksum/header/count and source-reference verification. No archive or market-data download was initiated for this task.

Data directory baseline: 29,419,189,356 bytes; audit snapshot: 29,421,558,765 bytes (delta 2,369,409). Canonical Parquet: 354,503,110 → 354,503,110 bytes. The small change at that snapshot was catalogue/operational metadata from the independently scheduled collector, not a duplicate dataset. The ordinary scheduler remains pinned to commit `37e50a979faa6bcba5df2f293fbbf596edfa4aa1`; this branch does not change its task, configuration or wrapper.

### Fixed-window restrictions: the remaining 14

The exact original window is `[2026-07-01,2026-10-01)` UTC. Phase cuts remain August 25 04:00 and September 12 13:00 UTC. Minimum counts remain 1000/250/250, durations 30/10/10 days, hourly EVENT_TIME, complete verified source required, verified no-trade allowed, retrospective receipt policy. No dates or thresholds were relaxed. The nine successful assessments have 100% source completeness in all phases and no unknown source intervals; this is **not** dense minute coverage or historical execution readiness. They create no executable plan. The existing 50-market plan remains the joint authority; neither 59 nor 64 markets are newly authorized jointly.

| Pair | Training rows | Validation rows | Test rows | Independent fixed-window assessment |
|---|---:|---:|---:|---|
| AUDX-AUD | 44 | 22 | 25 | INSUFFICIENT_OBSERVATIONS |
| BCH-AUD | 414 | 64 | 184 | INSUFFICIENT_OBSERVATIONS |
| DOGE-AUD | 1535 | 300 | 149 | INSUFFICIENT_OBSERVATIONS |
| EUR-AUD | 722 | 217 | 365 | INSUFFICIENT_OBSERVATIONS |
| PEPE-AUD | 280 | 65 | 77 | INSUFFICIENT_OBSERVATIONS |
| XMR-EUR | 6885 | 5047 | 2415 | READY |
| XMR-USD | 50065 | 21502 | 20741 | READY |
| XRP-AUD | 11792 | 2657 | 2525 | READY |
| XRP-EUR | 63863 | 21114 | 21482 | READY |
| XRP-USD | 77635 | 26417 | 26559 | READY |
| XRP-USDT | 35745 | 11446 | 17121 | READY |
| XTZ-USD | 10169 | 5955 | 11697 | READY |
| ZEC-EUR | 28088 | 16268 | 18305 | READY |
| ZEC-USD | 52033 | 25127 | 26269 | READY |

### Per-market physical inventory

The following counts describe genuine persisted observations, not manufactured calendar grids. `Unobserved` is the raw calendar difference within July–September (132,480 possible minutes); it remains **MISSING_OR_UNVERIFIED unless existing M12 source evidence proves otherwise**. It must not be read as a count of corrupt candles or proof of no trading. Gap is the maximum distance between source observation times within that window. Use M12 for source-backed classification and strategy-specific admission.

| Pair | Stored rows (all history) | First UTC | Last UTC | Q3 rows | Q3 unobserved minutes | Max Q3 observation gap (min) |
|---|---:|---|---|---:|---:|---:|
| AAVE-USD | 94199 | 2026-06-01T00:01:00Z | 2026-11-11T07:41:00Z | 70511 | 61969 | 54 |
| ADA-AUD | 117578 | 2021-01-21T17:43:00Z | 2026-11-11T07:41:00Z | 1922 | 130558 | 1041 |
| ADA-EUR | 102756 | 2026-06-01T00:00:00Z | 2026-11-11T07:41:00Z | 77331 | 55149 | 36 |
| ADA-USD | 143987 | 2026-06-01T00:00:00Z | 2026-11-11T07:41:00Z | 105795 | 26685 | 18 |
| ALGO-USD | 82951 | 2026-06-01T00:03:00Z | 2026-11-11T07:41:00Z | 62621 | 69859 | 45 |
| ATOM-EUR | 41786 | 2026-06-01T00:06:00Z | 2026-11-11T07:42:00Z | 31190 | 101290 | 144 |
| ATOM-USD | 73216 | 2026-06-01T00:00:00Z | 2026-11-11T07:42:00Z | 53214 | 79266 | 47 |
| AUDX-AUD | 5303 | 2025-12-19T15:08:00Z | 2026-11-11T07:42:00Z | 91 | 132389 | 10798 |
| AVAX-USD | 109709 | 2026-06-01T00:00:00Z | 2026-11-11T07:42:00Z | 77939 | 54541 | 26 |
| BCH-AUD | 51128 | 2020-06-16T21:40:00Z | 2026-11-11T07:42:00Z | 662 | 131818 | 1813 |
| BCH-EUR | 44623 | 2026-06-01T00:03:00Z | 2026-11-11T07:42:00Z | 30358 | 102122 | 141 |
| BCH-USD | 67882 | 2026-06-01T00:01:00Z | 2026-11-11T07:42:00Z | 48699 | 83781 | 106 |
| BTC-AUD | 840002 | 2020-06-16T22:30:00Z | 2026-11-11T07:42:00Z | 38996 | 93484 | 110 |
| BTC-EUR | 170862 | 2026-06-01T00:00:00Z | 2026-11-11T07:42:00Z | 126139 | 6341 | 18 |
| BTC-USD | 176478 | 2026-06-01T00:00:00Z | 2026-11-11T07:42:00Z | 130996 | 1484 | 17 |
| BTC-USDC | 141483 | 2026-06-01T00:00:00Z | 2026-11-11T07:42:00Z | 104104 | 28376 | 18 |
| BTC-USDT | 119989 | 2026-06-01T00:01:00Z | 2026-11-11T07:42:00Z | 85061 | 47419 | 18 |
| DOGE-AUD | 41716 | 2024-06-05T16:48:00Z | 2026-11-11T07:42:00Z | 1984 | 130496 | 1286 |
| DOGE-EUR | 76598 | 2026-06-01T00:00:00Z | 2026-11-11T07:42:00Z | 56718 | 75762 | 71 |
| DOGE-USD | 134444 | 2026-06-01T00:01:00Z | 2026-11-11T07:42:00Z | 100436 | 32044 | 20 |
| DOT-EUR | 52743 | 2026-06-01T00:00:00Z | 2026-11-11T07:42:00Z | 39184 | 93296 | 158 |
| DOT-USD | 122772 | 2026-06-01T00:01:00Z | 2026-11-11T07:42:00Z | 95329 | 37151 | 30 |
| ETH-AUD | 444335 | 2020-06-16T22:30:00Z | 2026-11-11T07:42:00Z | 27618 | 104862 | 145 |
| ETH-EUR | 169680 | 2026-06-01T00:00:00Z | 2026-11-11T07:42:00Z | 125352 | 7128 | 17 |
| ETH-USD | 176793 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 131356 | 1124 | 18 |
| ETH-USDC | 124031 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 92707 | 39773 | 19 |
| ETH-USDT | 101732 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 75268 | 57212 | 29 |
| EUR-AUD | 130814 | 2020-06-16T22:30:00Z | 2026-11-11T07:43:00Z | 1304 | 131176 | 1272 |
| EUR-USD | 177377 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 131809 | 671 | 18 |
| GBP-USD | 164757 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 123621 | 8859 | 18 |
| LINK-AUD | 65739 | 2021-01-21T22:08:00Z | 2026-11-11T07:43:00Z | 1798 | 130682 | 1169 |
| LINK-EUR | 70188 | 2026-06-01T00:02:00Z | 2026-11-11T07:43:00Z | 53524 | 78956 | 102 |
| LINK-USD | 125853 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 93711 | 38769 | 23 |
| LTC-AUD | 153946 | 2020-06-17T07:18:00Z | 2026-11-11T07:43:00Z | 6679 | 125801 | 759 |
| LTC-EUR | 150447 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 109471 | 23009 | 18 |
| LTC-USD | 165117 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 122324 | 10156 | 19 |
| LUNA-USD | 66054 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 43080 | 89400 | 56 |
| PEPE-AUD | 15603 | 2024-11-27T14:06:00Z | 2026-11-11T07:43:00Z | 422 | 132058 | 2601 |
| PEPE-USD | 127253 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 95560 | 36920 | 24 |
| SHIB-USD | 85243 | 2026-06-01T00:08:00Z | 2026-11-11T07:43:00Z | 64051 | 68429 | 51 |
| SOL-AUD | 236814 | 2024-06-05T13:38:00Z | 2026-11-11T07:43:00Z | 34405 | 98075 | 118 |
| SOL-EUR | 171216 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 126795 | 5685 | 18 |
| SOL-USD | 177197 | 2026-06-01T00:00:00Z | 2026-11-11T07:43:00Z | 131654 | 826 | 18 |
| TRX-EUR | 108829 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 77167 | 55313 | 26 |
| TRX-USD | 132856 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 98335 | 34145 | 18 |
| UNI-USD | 100053 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 80474 | 52006 | 50 |
| USDC-AUD | 358267 | 2021-01-21T16:19:00Z | 2026-11-11T07:44:00Z | 21095 | 111385 | 191 |
| USDC-EUR | 177078 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 131514 | 966 | 18 |
| USDC-USD | 171677 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 128181 | 4299 | 17 |
| USDC-USDT | 120952 | 2026-06-01T00:02:00Z | 2026-11-11T07:44:00Z | 98385 | 34095 | 25 |
| USDT-AUD | 1058811 | 2020-06-16T22:08:00Z | 2026-11-11T07:44:00Z | 49784 | 82696 | 55 |
| USDT-EUR | 147157 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 107244 | 25236 | 22 |
| USDT-USD | 177727 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 132026 | 454 | 18 |
| XLM-EUR | 75645 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 50776 | 81704 | 63 |
| XLM-USD | 146891 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 105972 | 26508 | 18 |
| XMR-EUR | 21446 | 2026-06-01T00:07:00Z | 2026-11-11T07:44:00Z | 14347 | 118133 | 300 |
| XMR-USD | 126652 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 92308 | 40172 | 18 |
| XRP-AUD | 316541 | 2020-06-16T22:30:00Z | 2026-11-11T07:44:00Z | 16974 | 115506 | 288 |
| XRP-EUR | 145386 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 106459 | 26021 | 18 |
| XRP-USD | 175992 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 130611 | 1869 | 18 |
| XRP-USDT | 88526 | 2026-06-01T00:00:00Z | 2026-11-11T07:44:00Z | 64312 | 68168 | 53 |
| XTZ-USD | 35389 | 2026-06-01T00:01:00Z | 2026-11-11T07:45:00Z | 27821 | 104659 | 179 |
| ZEC-EUR | 84027 | 2026-06-01T00:00:00Z | 2026-11-11T07:45:00Z | 62661 | 69819 | 68 |
| ZEC-USD | 139647 | 2026-06-01T00:00:00Z | 2026-11-11T07:45:00Z | 103429 | 29051 | 29 |

### Resource/replay benchmarks

The machine suspended overnight during several concurrent validation runs. Affected elapsed times include suspension and are **not throughput claims**. Native OS peak working sets remain valid. Use CPU seconds where available; profiling and concurrent work also affect timings. Cold original-source equality/completeness verification is deliberately retained and CPU-intensive. The profile identified retained CSV/dependency verification, catalogue receipt digesting and exact replay serialization as costs. An unbounded all-universe pandas baseline was not run; the existing single-day pandas loader peaked at 303,710,208 bytes.

| Workload | Rows | Native peak RSS (MiB) | Elapsed seconds | CPU seconds |
|---|---:|---:|---:|---:|
| day-final | 590 | 295.35 | 6.015 | 6.062 |
| quarter | 38996 | 306.32 | 28.219 | suspension-affected run; see CPU audit |
| ten-final | 481276 | 411.96 | 1359.703 | 770.281 |
| all | 4631664 | 679.04 | 5005.781 | 4711.656 |
| all-repeat | 4631664 | 661.68 | 5053.515 | 4757.734 |
| partition | 2270535 | 777.69 | 6964.797 | 6510.906 |

Both all-64 runs returned 4,631,664 rows, with identical per-pair counts and SHA-256 `428404b779ebc0343c3a5b8c7c0f028b871a236531fc266fe254457c138753ac` despite 10,000 versus 4096 row batches. Every selected asset returned observations. The complete hash includes exact decimal values and original UTC timestamps in chronological pair order.

### Storage categories and remaining limitations

| Category | Baseline bytes | Purpose |
|---|---:|---|
| historical_archives | 26,347,959,063 | Retained official ZIPs and extracted original source members |
| historical_imports | 2,389,117,033 | Existing monthly source CSVs, trade evidence and receipts |
| market_data | 354,503,110 | Canonical monthly Parquet |
| research_exports | 214,996,880 | Existing approved CSV exports and a pre-existing partial export directory |
| partition_plans | 30,941,268 | Existing immutable study plans and governance |

Existing archives, extracted members, exports, partial exports, notebooks and viewer files were retained. No new persistent market-data copy or package environment is required. Test fixtures and the temporary profiler are separate from canonical data and are cleaned after verification; small aggregate audit reports remain. Research APIs do not start or stop scheduled collection. Only the previously configured scheduler may independently append new genuine recent candles.

The interrupted interactive all-asset workers were excluded. Completed all-64 and partition measurements came from finite hidden verification workers. Those workers had imported the earlier counter sampler, so their peaks conservatively include its redundant ctypes type-allocation overhead. The final maxsize-one native binding cache passed the repeated-sampling regression and the fresh 590-row day smoke test with identical data hash. Data selection, source checks and replay logic were unchanged.

Delivery snapshot: 9,875,561 price rows, 64 pairs, SQLite `ok`, 0 foreign-key errors, 64 matching checkpoints; 195 original hashes and original viewer/partitions unchanged. Data files total 29,431,090,709 bytes versus 29,419,189,356 initially. Catalogue/operational receipts and the independently scheduled incremental runs account for growth, not research dataset copies. Aggregate diagnostic files: 5,451,235 bytes.

Removed 227,222,055 bytes from seven explicitly owned test directories, plus the 551,717-byte temporary profiler after saving its text summary. No pre-existing source/archive/export cache was deleted. The existing M13 release created a 1,366-byte immutable registration sidecar; existing partition boundaries/manifests remain unchanged and no test-phase access was made.

The existing six-hour task independently completed a public-only Session 0 cycle on November 12, 15:32-16:24 AEDT: exit 0, 64/64 successful OHLC requests, zero errors, no new pairs, 383 unresolved records preserved, and 46,080 actual recent candles appended (9,821,943 to 9,868,023 total rows). Another ordinary cycle can append further current data while historical research reads continue. No manual collector/backfill was invoked for this task. This proves noninteractive Session 0 operation, not a separately forced physical sign-out/reboot test.

Completed queries verified technical access and replay for all 64 assets; that does not make all 64 admitted for this fixed study. The five explicit count failures remain rejected, the nine independent passes need appropriate immutable registration before a strategy study, and combining them with the approved 50 needs a newly validated joint plan. Dense-grid and historical execution studies have additional unmet requirements. No old daily financial backtest runner was converted or run on this mixed-currency holdout; the representative existing workflow is guarded, data-only training replay.
