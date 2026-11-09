# Automated public Kraken OHLCV pipeline

## Scope and commands

`python -m kraken_quant.automated_collection once --root . --config examples/automated_collection.json`

`status` reads the last report offline. `validate` runs offline M12/M13 admission.
An initialized metadata database and the retained official archive are required;
missing/incompatible storage fails closed. No private endpoint is used. The fee,
quote/book, financial eligibility, strategy and execution interfaces are unchanged.

The operation is catalogue refresh → verified new-pair detection → bounded
historical repair/import → incremental OHLCV → storage/coverage validation →
research-universe report. It reuses `discover`, `collection_universe`,
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
  imports complete July–September months for new pairs. Older archive history
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
Every phase reuses M12 checks. Results include rejections and their reasons.
This is scoped price-data admission, not all-ten-filter financial eligibility,
current exchange permission at historical dates, dense-minute readiness,
historical execution readiness, or permission to deploy a strategy.
Existing M11/M12/M13 interfaces default to AUD. Optional quote selection is an
explicit addition. New M13 plans pin completeness proofs as well as observation
sources; old plans remain readable and are not rewritten. No performance is
examined. A separate multi-asset request must pass COMMON_CALENDAR admission
before a joint strategy can use its data; individual readiness is insufficient.

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

Run offline regressions with `python -m pytest tests -q`; repository-root pytest
also discovers an existing account-connectivity example and is not offline.
