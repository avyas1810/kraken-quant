# Automated collection validation ? 11 November 2026

Draft PR #15 remains unmerged. These are actual public-source/local-storage
results, not strategy-performance or live-trading certification.

## Acceptance results

| Check | Verified result |
|---|---|
| Legacy validation worker | Exited before this turn; no administrator cleanup or unrelated process termination needed |
| Collection universe | 64 verified online spot pairs; 14 AUD, with USD/USDT/USDC/EUR expansion |
| Clean scheduled execution | S4U Session 0; exit 0; all 64 pairs successful |
| Scheduled noon trigger | 12:00:01?12:02:37 AEDT, while M13 was still running |
| Source checks | Public namespace/native identities, completed-candle parsing, 70 successful public requests; zero errors/throttling/invalid candles |
| SQLite | Integrity `ok`; foreign-key errors 0; 64/64 price checkpoints match persisted maxima |
| Parquet | 1,162 partitions validated for schema, logical keys and partition placement |
| Full original 64-market M13 request | INSUFFICIENT_OBSERVATIONS; no executable plan |
| Separate, disclosed 50-market joint proposal | READY; 150 asset/phase checks pass |
| Original 87 non-AUD repair windows | All accounted for: 86 resumed, one already complete; 23 complete, 64 still budget-limited |
| Protected artifacts | Original DuckDB database and three original partition manifests unchanged |
| Tests | 1,058 passed, 7 skipped in the full offline suite |

## Actual storage and collection

The verified storage snapshot contains **9,795,836 genuine OHLCV
observations** across 64 pairs. Earliest UTC:
`2020-06-16T21:40:00.000000Z`; latest UTC:
`2026-11-11T01:01:00.000000Z`. The previous 10 November snapshot
contained 9,734,186 rows. No zero-filling, interpolation or locally invented
candles were used.

The noon cycle `20261111T010003Z-4b75e277` grew from
9,793,884 to
9,795,836 rows: **1,952 newly persisted
candles**. It safely deduplicated 63 overlapping source observations. Every pair
is accounted for and its checkpoint matches successfully persisted data.

Latest catalogue: 1,814 source records, 1,431 classified standard spot records,
383 unresolved records, and 1,470 retained SQL identities. The 383 broader-class/
venue records remain unresolved. There are 14 verified online AUD markets and
1,219 active verified candidates across the five configured quote currencies.
Collection is enrolled for 64, not all 1,219. Historical absence never deletes an
asset or proves delisting. Native Kraken identifiers remain preserved.

Existing quotes: 18,996. Existing order-book snapshots: 18,996, represented by
191,896 level rows. Calculated cost records: zero. Expanded price availability
is not historical quote/book, fee-applicability or execution-cost readiness.

## Exact M13 result and immutable evidence

Manifest: `46aae41665516ec51e0b9a9f5a2f42156362f6341ba23e23ec121918e7b219f6`.

Result evidence SHA-256: `1e15959eb1f66d93433534c59a5cdd10bf1eba970cea66401b210b4f4356c752`.
Analysis code: `baf5e82f01060d7131f9616312848c3b4e5aedde`.
The completed validation ran from `2026-11-11T00:40:41.021565Z` to
`2026-11-11T01:12:20.707963Z` and persisted the result and immutable plan.

All 50 admitted instruments use **COMMON_CALENDAR**, **FIXED_WINDOW**, and the
original **[2026-07-01, 2026-10-01) UTC** envelope. No dates, observations,
minimums or quality requirements were relaxed. Every selected sample is an
original one-minute observation; the continuity grid is hourly, not resampled
OHLCV. Policy: EVENT_TIME, complete source evidence required, verified no-trade
allowed, minimum phase observations 1000/250/250 and durations 30/10/10 days,
60/20/20 calendar proportions, five-million-record work budget, proposal cap 50.
Maximum missing-gap/staleness and minimum observed-coverage thresholds remain
explicitly unconfigured, as in the existing policy. Received-by-reference
validation remains false for this retrospective study; this does not certify
that downloaded archive data was available to a historical live decision.

| Phase | Start UTC (inclusive) | End UTC (exclusive) |
|---|---|---|
| Training | 2026-07-01 00:00 | 2026-08-25 04:00 |
| Validation | 2026-08-25 04:00 | 2026-09-12 13:00 |
| Test | 2026-09-12 13:00 | 2026-10-01 00:00 |

The plan pins **4,008,279 selected observations**. All 150 asset/phase results
pass; source completeness is 100%, with zero unknown/out-of-coverage grid
intervals. All 157 referenced receipt hashes and the manifest hash were verified.
The lowest observed-hour coverage is 37.47%; the remaining intervals have verified
no-trade evidence. This is **scoped event-time price-study admission**, not a
complete executable minute grid, financial eligibility, or deployment permission.
A strategy needing stricter continuity/staleness must obtain its own passing plan.
No strategy-performance evaluation was performed and no untouched-holdout claim
is made. Existing guarded loading/registration requirements remain in force.

### Original request: fail closed

All 64 original instruments remain in the rejected request. Five fail necessary
phase observation minima (these are genuine source observations, not filled grid
rows):

| Pair | Training (need 1000) | Validation (need 250) | Test (need 250) |
|---|---:|---:|---:|
| AUDX-AUD | 44 | 22 | 25 |
| BCH-AUD | 414 | 64 | 184 |
| DOGE-AUD | 1535 | 300 | 149 |
| EUR-AUD | 722 | 217 | 365 |
| PEPE-AUD | 280 | 65 | 77 |

The original request has twelve failed phase minima and no executable manifest.
Count sufficiency alone never grants readiness. Necessary checks only reject;
surviving requests still receive full source/M12 validation.

Nine further count-qualified instruments are **NOT_EVALUATED_CAPACITY**, rather
than rejected as bad data: XMR-EUR, XMR-USD, XRP-AUD, XRP-EUR, XRP-USD, XRP-USDT, XTZ-USD, ZEC-EUR, ZEC-USD.
They were explicitly excluded from the separate capped proposal, not silently
removed from the original request. Their market data remains collected.

### Admitted proposal: actual per-phase observations

| Pair | Training | Validation | Test |
|---|---:|---:|---:|
| AAVE-USD | 36641 | 14452 | 19418 |
| ADA-AUD | 1341 | 271 | 310 |
| ADA-EUR | 39571 | 16199 | 21561 |
| ADA-USD | 60292 | 22007 | 23496 |
| ALGO-USD | 28579 | 14069 | 19973 |
| ATOM-EUR | 12759 | 8411 | 10020 |
| ATOM-USD | 24814 | 12239 | 16161 |
| AVAX-USD | 40847 | 15363 | 21729 |
| BCH-EUR | 14205 | 6573 | 9580 |
| BCH-USD | 21657 | 11381 | 15661 |
| BTC-AUD | 25919 | 6256 | 6821 |
| BTC-EUR | 76100 | 24868 | 25171 |
| BTC-USD | 78070 | 26379 | 26547 |
| BTC-USDC | 60568 | 21137 | 22399 |
| BTC-USDT | 50623 | 16916 | 17522 |
| DOGE-EUR | 30718 | 11902 | 14098 |
| DOGE-USD | 52634 | 23110 | 24692 |
| DOT-EUR | 16518 | 9026 | 13640 |
| DOT-USD | 45286 | 25520 | 24523 |
| ETH-AUD | 18475 | 5130 | 4013 |
| ETH-EUR | 76212 | 24535 | 24605 |
| ETH-USD | 78348 | 26434 | 26574 |
| ETH-USDC | 54336 | 18041 | 20330 |
| ETH-USDT | 40473 | 16205 | 18590 |
| EUR-USD | 78779 | 26455 | 26575 |
| GBP-USD | 71134 | 26076 | 26411 |
| LINK-AUD | 1206 | 294 | 298 |
| LINK-EUR | 24527 | 12974 | 16023 |
| LINK-USD | 49293 | 21283 | 23135 |
| LTC-AUD | 5512 | 656 | 511 |
| LTC-EUR | 67444 | 20249 | 21778 |
| LTC-USD | 70390 | 25670 | 26264 |
| LUNA-USD | 26614 | 7543 | 8923 |
| PEPE-USD | 52455 | 19760 | 23345 |
| SHIB-USD | 33993 | 13222 | 16836 |
| SOL-AUD | 24618 | 5908 | 3879 |
| SOL-EUR | 77348 | 24725 | 24722 |
| SOL-USD | 78624 | 26454 | 26576 |
| TRX-EUR | 48976 | 14655 | 13536 |
| TRX-USD | 54803 | 21385 | 22147 |
| UNI-USD | 36400 | 19944 | 24130 |
| USDC-AUD | 12607 | 4545 | 3943 |
| USDC-EUR | 78551 | 26410 | 26553 |
| USDC-USD | 75950 | 26082 | 26149 |
| USDC-USDT | 53413 | 22541 | 22431 |
| USDT-AUD | 29221 | 9823 | 10740 |
| USDT-EUR | 66025 | 20548 | 20671 |
| USDT-USD | 79029 | 26442 | 26555 |
| XLM-EUR | 27614 | 9601 | 13561 |
| XLM-USD | 61023 | 20969 | 23980 |

## Historical gap recovery

The exact original 87-window cohort was preserved separately from newly detected
gaps. One window was already complete; 86 were resumed under the existing five
new-request per-window allowance. The pass used **370 actual public Trades
requests**, with zero reported API errors or throttling. Cached pages, native
identity, original trade IDs, Decimal values and cursor chains are retained.

**23 windows are complete; 64 remain pending.** M12 validated all 23 complete
windows: **11,034 observed intervals + 9,616 verified no-trade intervals**, zero
unknown intervals inside those windows. No-trade classifications are virtual
evidence-backed labels, not inserted price bars.

Every remaining original window exhausted its five-request allowance, ended on
a full 1,000-trade page and advanced its cursor. This is a measured request-budget
bottleneck, **not source exhaustion or proven unrecoverability**. Exact ranges and
resume cursors are retained in
`data/automated_collection/verification/pr15-pending-cursors.json` and immutable
raw response pages. A later bounded maintenance invocation continues the chain.

The complete queue currently has 163 historical warnings, of which 127 are still
pending: the 64 original non-AUD windows plus 63 newer downtime windows. Advancing
a current checkpoint does not erase them. Kraken OHLC's recent ~720-candle limit
cannot repair these through repeated old-date requests. Public Trades repair is
separate and resumable; unknown intervals never become invented no-trade candles.
No full-to-present history claim is made.

## Scheduler, independence and host conditions

`KrakenQuant-OHLCV-6Hourly` retains S4U, the original six-hour TimeTrigger, startup
BootTrigger (two-minute delay), LogonTrigger, StartWhenAvailable, IgnoreNew,
bounded restarts and the two-hour execution limit. The existing project Python
and local wrapper remain the action. Next observed trigger: **18:00 AEDT on
11 November 2026**. S4U stores no password. Public HTTPS worked in Session 0.
A physical logged-out reboot was not performed; Session 0 is not presented as
proof that the desktop user was physically logged out.

Observed successful executions include:
- 10:23?10:33: initial expanded S4U execution, exit 0.
- 10:40:45?10:43:26: revised interactive verification, exit 0 (Session 1).
- 10:59:55?11:18:21: revised S4U under concurrent validation load, exit 0.
- 11:24:48?11:33:45: shared-pacing S4U verification, exit 0.
- 12:00:01?12:02:37: **normal scheduled trigger**, exit 0 while M13 remained active.

The routine task refreshes metadata, detects enrollment, collects incremental
OHLCV and validates storage/checkpoints. It **never waits for historical
maintenance or M13**. Maintenance and validation are explicit separate bounded
commands/locks. This preserves independent routine collection; it does not
silently claim deferred work has completed. Shared request-start reservations
coordinate routine/maintenance pacing without holding a mutex during HTTP I/O.
Existing request/retry budgets and per-partition atomic writes remain intact.

Battery/Energy Saver was observed at 16% battery with AC disconnected; Windows
queued the requested S4U instance with EnginePID 0. Only that queued manual
attempt was cancelled for an interactive verification. After AC was connected
and Saver was off, S4U launched normally. No global power/security policy was
bypassed or changed. The host must be awake, powered and online. See
[Microsoft Energy Saver rules](https://learn.microsoft.com/windows-hardware/design/component-guidelines/energy-saver)
and [Kraken public rate limits](https://support.kraken.com/gb/articles/206548367-what-are-the-api-rate-limits-).

## Correctness fixes and regression evidence

- Source lifetime metadata can legitimately exceed ten years. Ordered UTC source
  domains no longer inherit the bounded-query limit; requested queries remain
  bounded. This fixed the actual legacy `bounded_ordered_time_range_required`
  failure without widening the study window.
- Impossible fixed envelopes and necessary phase count deficits reject early,
  retaining exact requested dates and asset/phase failures. These checks cannot
  authorize data.
- Full archive timestamp validation now uses integer UTC bounds, while retaining
  full SHA-256, ZIP CRC, size, alignment, order and exact monthly-byte comparison.
  Actual `ATOMUSD_1.csv`: 79,410,544 bytes processed in 7.28 seconds, SHA/CRC verified.
- The approved archive is reused, not downloaded again: 9,546,553,626 bytes,
  SHA-256 `bc273e9027470b07d790a7fa4df9bb43f0dfc527c20c6ecae48402e123e7b1c7`.
- Child-owned UTC deadlines prevent orphaned validation after parent/venv-launcher
  termination. Reconnecting after a daemon restart does not relaunch an active
  bounded worker. Interrupted read-only attempts are recorded, not called READY.

Commands executed:

```powershell
.venv-db/Scripts/python.exe -m pytest -q
# 1058 passed, 7 skipped, 447.54 seconds
.venv-db/Scripts/python.exe -m pytest -q tests/test_automated_collection.py tests/test_market_continuity.py tests/test_partition_algorithm.py
# 131 passed
.venv-db/Scripts/python.exe -m pytest -q tests/test_data_pipeline.py tests/test_automated_collection.py tests/test_ohlcv_collector.py
# 108 passed, 1 skipped
```

Real validation additionally used the actual Windows task, the existing public
Trades repair helpers, M12, and the existing M13 `research_audit`/PartitionAlgorithm
service with unchanged policy. The service persisted the immutable result before
reporting READY. Final-head CI results are recorded in PR #15.

## Reproduction and retained evidence

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.automated_collection once --root . --config examples/automated_collection.json
.venv-db/Scripts/python.exe -m kraken_quant.automated_collection maintenance --root . --config examples/automated_collection.json
.venv-db/Scripts/python.exe -m kraken_quant.automated_collection validate --root . --config examples/automated_collection.json
.venv-db/Scripts/python.exe -m kraken_quant.automated_collection status --root .
```

Local evidence is ignored by Git; no market datasets or credentials are committed:
- `data/partition_plans/v1/46aae41665516ec51e0b9a9f5a2f42156362f6341ba23e23ec121918e7b219f6.json`
- `data/automated_collection/research.json` and `verification/1e15959eb1f66d93433534c59a5cdd10bf1eba970cea66401b210b4f4356c752-m13-result.json`
- `verification/4344f9432fe407a50fbff1f1082d76a0e4db2ac9d00462a0b97f6aa6b0902e29-storage-audit.json` beneath `data/automated_collection/`
- `data/automated_collection/verification/pr15-original87-status.json`
- `data/automated_collection/verification/pr15-recovered-m12.json`
- `data/automated_collection/verification/pr15-pending-cursors.json`
- Actual routine runs, response receipts, trade pages and import receipts.

The original viewer database and three original immutable partition files still
match baseline SHA-256 values. Notebook/user changes were not staged or altered.
The separate read-only expanded DuckDB catalogue reads all 64 pairs and exactly
9,795,836 rows; the original viewer remains unchanged. No financial algorithms,
live/private trading calls, strategy fitting or performance calculations were added.

Remaining limitations: 127 unresolved historical warnings outside the admitted
quarter; only June?September history initially populated for new markets; no
historical execution-data readiness claim; nine instruments not evaluated under
the proposal cap; and host sleep/Energy Saver can defer scheduled operation.
