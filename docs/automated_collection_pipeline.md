# Automated public Kraken OHLCV pipeline

## Scope and commands

`python -m kraken_quant.automated_collection once --root . --config examples/automated_collection.json`

`status` reads the last report offline. `validate` runs offline M12/M13 admission.
An initialized metadata database and the retained official archive are required;
missing/incompatible storage fails closed. No private endpoint is used. The fee,
quote/book, financial eligibility, strategy and execution interfaces are unchanged.

The six-hour operation is catalogue refresh, verified new-pair detection,
incremental OHLCV, checkpoint/integrity validation and an operational report.
Historical imports/Trades repair run only through the separate bounded command:

`python -m kraken_quant.automated_collection maintenance --root . --config examples/automated_collection.json`

M12/M13 research admission runs only through `validate`, with its own lock and
UTC deadline. Neither historical maintenance nor research validation holds the
routine pipeline lock. Existing per-partition atomic locks still serialize writes.
The former `validate_research` option no longer adds blocking work to `once`;
its report explicitly marks the work deferred. Admission is not inferred from a
successful collection exit. The commands reuse `discover`, `collection_universe`,
`OhlcvCollector`, `import_csv`, `fetch_gap`, `MonthlyParquetStore`, `PublicPolicy`,
`ProcessLock`, M12 admission and M13 immutable fixed-window plans.

## Universe and historical sources

AUD remains every existing interface's default. Expanded collection explicitly
opts into AUD/USD/USDT/USDC/EUR, current online status, international standard spot
identity and a consistent latest discovery receipt. Broader unresolved products
remain unresolved. Fourteen eligible AUD pairs cannot supply 50 admissible AUD
pairs, so the pilot selects 64 pairs. Additional pairs require actual official
archive members; deterministic historical source-byte availability ranks them.
This is not trading-volume, liquidity or strategy-performance screening.
Previously enrolled active markets are retained when archive rankings change.
Inactive or absent markets' stored data and enrollment history are never deleted.

Sources:

- [Official Kraken OHLCVT archive](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data):
  the retained full ZIP covers first trades through 30 September 2026. The pilot
  imports complete June–September months for new pairs. June provides genuine pre-window context; the research envelope remains July–September. Older archive history
  remains available but is outside this explicitly configured initial import.
- [Kraken public historical Trades](https://support.kraken.com/articles/360029077772-python-code-to-retrieve-historical-time-and-sales-trading-history-):
  exact trade IDs and decimal values are replayed with `since`/`last` pagination.
  Cached pages do not consume the *new-request* budget, so restarts make progress.
- Public OHLC `interval=1` supplies recent completed candles. Its final unfinished
  row is excluded. Its approximately 720-candle history is not historical paging.

No prices are filled or inferred. Full original CSVs, ZIP CRCs, package SHA-256,
monthly source slices, response checksums and existing-wins conflict evidence are
retained. An import may be interrupted between atomic monthly commits; replay
verifies previous receipts and resumes. Checkpoints follow verified persistence
and do not move backwards. SQLite/Parquet are not one distributed transaction.

## Gaps and time integrity

The first gap audit replays all 28 retained downtime windows across 14 AUD pairs.
The resulting complete cursor windows are immutable, checksum-pinned evidence.
M12 validates native identity, cursor continuity, end-of-window completion, source
trade IDs and knowledge timestamps before accepting no-trade semantics. A source
trade missing locally remains missing, never a no-trade interval. Receipt-time
admission still hides both observations and completeness evidence until receipt.

New gaps between the saved checkpoint and the actual oldest OHLC response are
persisted in `automated_collection/gap-queue.json`. Advancing a checkpoint does
not erase them. Fair oldest-attempt-first bounded repair jobs resume public trade
pages. Budget exhaustion is pending recovery, not successful backfill or proof of
no trades. A complete archive ending in September cannot certify November gaps.

## Explicit research policy

The initial availability study requests **[2026-07-01, 2026-10-01) UTC**, EVENT_TIME,
an hourly continuity grid over original one-minute observations, complete source
evidence, and allowance for verified no-trade intervals. There is no fabricated
hourly candle, universal coverage threshold or executable reference price.

M13 FIXED_WINDOW retains the requested envelope, 60/20/20 chronological calendar
phases, minimum observations 1000/250/250 and minimum durations 30/10/10 days.
Every phase reuses M12 checks. The original full-universe request and its
rejection are retained. Necessary per-phase observation counts use M13
`phase_cuts`; they cannot grant readiness. A count-qualified proposal is a
separate, explicitly reported COMMON_CALENDAR request with unchanged dates and
thresholds. Only a successful joint plan admits its members. The explicit
pilot joint proposal is capped at 50 pairs / five million selected records
for this 16 GB machine. Count-qualified surplus pairs are explicitly
NOT_EVALUATED_CAPACITY, not rejected as bad data. Collection still covers all 64.
These capacity limits are configurable and never silently increased.
Results include rejections and their reasons.
This is scoped price-data admission, not all-ten-filter financial eligibility,
current exchange permission at historical dates, dense-minute readiness,
historical execution readiness, or permission to deploy a strategy.
Existing M11/M12/M13 interfaces default to AUD. Optional quote selection is an
explicit addition. New M13 plans pin completeness proofs as well as observation
sources; old plans remain readable and are not rewritten. No performance is
examined. The pipeline
requires COMMON_CALENDAR admission for the proposed joint universe; independent
pair results cannot authorize joint strategy use.

## Budgets, Windows operation and reports

The six-hour Windows task retains S4U, the startup trigger, StartWhenAvailable,
IgnoreNew, bounded restarts and the original action. The project interpreter
runs a local wrapper. A reviewed code snapshot can be bound to that wrapper so
changing the interactive checkout cannot change scheduled code unexpectedly.
S4U stores no password. Batch Session 0 API execution is verified separately from
an actual logged-out reboot, which is not forced during validation.

The initial operator import has a one-hour budget. Routine imports have six
monthly jobs / 300 seconds (checked between bounded file jobs), and trade repair
has two jobs / five new requests each. Existing public pacing, retries, the
1000-request / one-hour request budget and the task's two-hour process limit
bound collection. Existing collector and per-partition locks prevent overlap.
An expensive archive job can finish its current atomic unit before yielding;
no infinite historical pagination or retry loop is used.

`data/automated_collection/runs/<run-id>.json` records start/end, catalogue counts,
enrollment, import/repair results, source request timing, before/after rows,
per-pair errors, latest timestamps, integrity, checkpoints and research admission.
Separate repair/backfill/research logs preserve progress. `latest.json` is a
convenience pointer; immutable source receipts remain in their existing folders.
Data and local operator reports are ignored by Git. Frozen research artifacts,
credentials, notebooks and the original read-only DuckDB viewer are not changed.

Run offline regressions with `python -m pytest -q` (or explicitly `tests`).
`pytest.ini` limits default discovery to tests, preventing interactive account
examples from authenticating during ordinary test collection.


## Resume and validation runtime safeguards

A sleeping machine exposed a stale cycle and misleading budget errors. Public
requests now enforce both monotonic and UTC wall deadlines, and retain the
underlying budget-expiry code plus actual HTTP-request counts. Each response
attempt's sanitized timing is journaled before a cycle can be interrupted.
Incremental checkpoint reconciliation validates every partition in Arrow;
only incoming months are decoded for exact conflict/duplicate comparisons.
This preserves full schema, key, finalized-candle and future-time checks.

Heavy M13 work runs in a child with a UTC watchdog (default 1800 seconds; operator example 3600 seconds), a
separate validation lock and the same pinned code/environment. Timeout leaves
research pending, never admitted, while completed collection remains recorded.
A cycle older than two wall-clock hours fails instead of reporting fresh health.
Research work is deferred when collection has failed, allowing prompt recovery.

Successful joint plans can be reused only as **the same immutable historical
snapshot**. Verification fingerprints cover algorithm source, policy/universe,
identity fields, relevant canonical partitions and source receipts, raw slices,
trade dependencies, complete archive/member hashes, quality issues, pinned
metadata and the plan itself. Inputs must remain unchanged during validation.
A changed dependency invalidates reuse and requires a new plan; existing plans
are never rewritten. New out-of-window live candles do not change that fixed
historical study. Cached reports retain their original evaluation cutoff and
separately record the current input-verification time. They are not a fresh
production-strategy validation or a newly untouched holdout.

An impossible FIXED_WINDOW envelope now rejects immediately after the existing
M13 source/identity index, before expensive phase materialization. The report
retains the requested dates, exact unavailable envelope and unevaluated phase
status. This necessary rejection cannot admit a market or select different dates.

The validation child also enforces the UTC deadline itself. Windows may terminate
the scheduled parent or the virtual-environment launcher without terminating the
actual Python grandchild. The child guard therefore exits independently on expiry;
no completed result or admission is fabricated. Source and manifest writes retain
their existing atomic behavior. Existing older workers do not acquire this guard
retroactively and may require authorized administrator cleanup.

Source-history domains may legitimately exceed ten years (for example Kraken
BTC/USD since 2013). Domain endpoints must remain UTC and ordered, but the
ten-year bounded-query limit applies to requested materialization, not source
lifetime metadata. The fixed July?September query and all M12 requirements remain
unchanged. No old manifest or source timestamp is rewritten by this correction.
