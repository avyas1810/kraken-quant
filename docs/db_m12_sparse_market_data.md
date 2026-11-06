# M12: sparse market data and event-time integrity

This is a read-only data-integrity and admission layer. It does not implement
alpha, execution costs, trading, candle resampling or price filling.

## Audit and reuse

M12 starts from `review-base/milestone1-20261029`, merge commit
`7140ebc004d602b02adfaad1d9e58798b2786d3c` containing PR #12. The user explicitly
approved this base because the default `main` branch lacks the database work.
M13 is deferred until this PR is independently reviewed and merged.

Existing safeguards reused:

| Existing component | Safeguard reused |
|---|---|
| `ResearchData._load` | Exact canonical rows, schema validation, source hashes and row matching; known-time cutoff; no invalid-record dropping or current-data substitution |
| `MonthlyParquetStore`, `storage_schema` | Decimal/UTC types, logical keys, valid candles and complete book snapshots |
| `historical_population.source_records` | Immutable raw CSV and dependency hashes; exact schema validation; precision defects remain evidence |
| `history_coverage` | Existing absence/coverage semantics and distinction between sampled books and candle grids; the old APIs are unchanged |
| Catalogue receipts | Existing point-in-time identity validation, not reconstructed historical status |
| Existing exports and partition manifests | Remain readable and unchanged; no M11 manifest is rebalanced or overwritten |

Remaining gaps before M12: the legacy coverage reports do not expose a shared
strategy-admission policy, last-observation age, source-backed per-cell no-trade
proof, or strict-grid rejection. M11's broad archive gap labels alone do not prove
that each local monthly source slice is complete. The new source adapter performs
that additional check; it does not trust an absence or receipt alone.

## Public interfaces

`kraken_quant.market_continuity` provides `MarketDataContinuity`,
`ContinuityRequest`, `ContinuityRequirements`, `DataPolicy`, `ContinuityResult`
and `ContinuityRejected`. The existing `ResearchData` exposes:

* `continuity(request)`: inspect all asset/dataset reports, including failures.
* `load_for_research(request)`: raise `ContinuityRejected` if any mandatory
  asset/dataset requirement fails, otherwise return the result.

The same callable service can be used by a future production strategy-validation
service. Admission is not deployment authorization or permission to trade. Legacy
raw reader methods remain compatible; a caller must explicitly use admission
before starting a strategy run.

```python
from datetime import timedelta
from decimal import Decimal
from kraken_quant.research_data import ResearchData
from kraken_quant.market_continuity import (
    ContinuityRequest, ContinuityRequirements, DataPolicy, ContinuityRejected,
)

reader = ResearchData("data")  # receipt knowledge cutoff defaults to current UTC
request = ContinuityRequest(
    assets=("BTC-AUD", "BCH-AUD"),
    start="2026-09-01T00:00:00Z", end="2026-09-02T00:00:00Z",
    datasets=("prices_1m",), frequency=timedelta(minutes=1),
    policy=DataPolicy.STRICT_GRID,
    requirements=ContinuityRequirements(
        minimum_observations=100,
        minimum_coverage_pct=Decimal("95"),  # example only, not universal
        maximum_missing_gap=timedelta(minutes=5),
        maximum_staleness=timedelta(minutes=30),
        require_source_complete=True,
    ),
    reference_lookback=timedelta(days=1),
)
inspection = reader.continuity(request)
for report in inspection.reports:
    print(report["asset"], report["passed"], report["failure_reasons"])
try:
    released = reader.load_for_research(request)
except ContinuityRejected as error:
    print(error.result.reports)  # actionable asset/dataset-specific rejection
```

Defaults are visible in the typed request and report: one genuine observation,
no universal density/gap/staleness threshold, no source-completeness requirement,
and no acceptance of no-trade cells as strict-grid observations. Every explicit
threshold applies independently to each selected asset and required dataset.
ASOF_REFERENCE requires an explicit maximum staleness; a missing reference fails
that requirement. A caller may explicitly lower the observation minimum to zero
for inspection, but this does not manufacture information.

## Classification and complete source evidence

Each UTC cell `[interval_start, interval_end)` receives exactly one classification:

| Classification | Meaning |
|---|---|
| `OBSERVED` | At least one genuine, validated observation in the cell is available by cell end |
| `VERIFIED_NO_TRADE` | Complete selected-source coverage with explicit traded-interval semantics covers the entire cell, and no original source row exists in it |
| `MISSING_OR_UNVERIFIED` | Absent observation without sufficient proof, including a source row missing from local storage or partial coverage |
| `SOURCE_OUT_OF_COVERAGE` | Every applicable selected source explicitly places the whole cell outside its declared domain; not a claim that no other historical source exists |

`continuity_sources.archive_windows` is the initial trusted evidence adapter. It
recognizes the existing official full OHLCVT source and M11's two extraction
layouts. It checks receipt identity, raw source/dependency hashes, the entire
extracted member SHA-256, ZIP member size/CRC, source timestamp ordering and
uniqueness, manifest schema/coverage end, and exact monthly raw-slice equality.
It retains **all original timestamps**, including precision-rejected rows.
Consequently, a missing local record that exists in the source is unknown,
never a no-trade interval. Retained source conflicts block admission.

The pinned complete member and the official source's documented traded-interval
semantics jointly establish this proof. The whole multi-gigabyte ZIP SHA-256,
verified in M11, is not recomputed for every query; the queried complete member
is rehashed and CRC-checked. Coordinated replacement of all local trust anchors
is outside this local integrity model.

Absent extraction/ZIP artifacts do not become no-trade assertions. They leave
missing cells unknown and source completeness unverified. Missing monthly
receipts, public Trades replay, REST OHLC response gaps and sampled quote/book
gaps are conservatively unknown until an appropriate completeness adapter proves
them. No global listing/delisting date is inferred from an archive's first row.
Adjacent validated coverage windows can jointly cover a larger requested cell.

## Policies and timing

* `EVENT_TIME`: `result.data` returns only genuine source rows. The added
  `observation_available_at` column exposes earliest supported availability.
  No grid reference is inserted into a raw candle.
* `STRICT_GRID`: returns the same genuine rows only after admission. Every grid
  cell must be observed. An explicit `allow_verified_no_trade=True` can permit
  proven no-trade cells as source-complete empty intervals, **not** as prices.
  Unknown/out-of-coverage cells still reject. Configured counts, density, gap,
  staleness and source requirements still apply.
* `ASOF_REFERENCE`: `result.data` returns the separate interval/reference frames.
  References use the last observation available at the cell's **start**. Original
  timestamp, availability, source ID/hash, receipt time and age are retained.
  `reference_close`, `reference_bid` and `reference_ask` are labelled references;
  every row has `executable=False`. No midpoint or executable price is calculated.

All results also retain `observations`, `intervals` and `reports` separately.
`continuity()` may return failed diagnostics for inspection; failed diagnostics
are not permission to use the dataset. `load_for_research()` is the hard gate.

A one-minute candle's close is available no earlier than opening+one minute;
it cannot be referenced at its opening. Quotes/books preserve their actual
timestamps. No future event or receipt relative to `ResearchData.as_of` is used.
The requested range is end-exclusive and cannot exceed that knowledge cutoff.

Historical replay uses source event availability while exposing later archive
receipt times. It does **not** claim that today's archived data vintage or current
metadata was actually available historically. For actual received-information
admission, set `require_received_by_reference=True`; availability becomes the
later of event availability and receipt time. Completeness evidence must also
be known by the relevant interval end. Late receipts cannot satisfy observation
minimums before they arrive. Unversioned historical revisions remain a limitation.

The first reference must come from a genuine row in the explicitly bounded
`reference_lookback`; otherwise its age is unknown and a configured staleness
limit rejects. Staleness is measured from the original observation timestamp
(candle opening), conservatively including the candle's completion delay.
Reports include both grid/end-decision age and the conservative maximum age
between arrivals, so coarse grids cannot hide stale periods. Future execution
callers must validate fresh real quotes at their actual decision time using
explicit limits and received-time admission. This PR does not submit orders.

## Metrics and limits

Reports include requested/available periods, unique observation/snapshot count,
physical row count, grid coverage, classification counts, source completeness,
maximum inter-observation/boundary gap, maximum empty-grid run, maximum unresolved
gap, classified gap runs/distribution, missing references, staleness, source
references/hashes, policy, knowledge cutoff, thresholds and exact failure reasons.

Observation density counts cells with observations. Source completeness counts
cells whose **source coverage**, rather than price grid, is proven complete.
Verified no-trade periods can have 100% source completeness and very low observed
price density. Book snapshot counts are distinct timestamps, not level-row counts.

Frequency controls evidence buckets and decision times, not candle resampling.
Native observations retain their original timestamps and precision. Larger cells
mean at least one available observation in that cell, not a manufactured larger
candle or complete intra-cell quote history. Windows must divide exactly into
positive-frequency cells; price frequencies are whole minutes. The configurable
grid budget defaults to two million total asset/dataset cells. Use bounded windows
for large universes; this is not a continuously running scheduler.

## Actual read-only validation

Period: **2026-09-01 00:00 through 2026-09-02 00:00 UTC**, one-minute cells,
one-day reference lookback, actual persisted Kraken data. No collector ran.

| Pair | Observations | Observed density | Verified no-trade | Unknown | Source completeness | Strict observed grid |
|---|---:|---:|---:|---:|---:|---|
| BTC-AUD | 331 | 22.9861% | 1,109 | 0 | 100% | REJECT: 1,109 unacceptable cells |
| BCH-AUD | 4 | 0.2778% | 1,436 | 0 | 100% | REJECT: 1,436 unacceptable cells |

EVENT_TIME with the explicit default one-observation requirement permits raw
inspection for both markets; this is not general strategy or execution readiness.
ASOF_REFERENCE with a 30-minute maximum age rejects both. Detailed actual reports
are retained locally in `.shadow-runtime/dbm12-real-reports.json` and summarized
in the PR delivery. Maximum observed timestamp gap is 40 minutes for BTC and
419 minutes for BCH. BCH's initial reference is already 17 hours old at the
requested start, despite having a genuine earlier observation in the lookback.
The conservative between-arrival maximum age is **41 minutes for BTC** and
**1,021 minutes for BCH**; sampled grid/end-decision maxima are 40 and 1,020
minutes respectively. The stricter age includes the minute needed to complete
the next candle. The 30-minute admission limit therefore rejects both markets.

No canonical SQLite, Parquet, source receipt, old export, M11 manifest or notebook
is intentionally written by these commands. Before/after hashes and database
checks accompany the final test and export verification results in the delivery.

## Tests and remaining responsibilities

Run `.venv-db/Scripts/python.exe -m pytest -q tests/test_market_continuity.py`
and the full existing `tests` suite. Deterministic fixtures cover sparse/unknown
intervals, complete no-trade proof, source boundaries, partial/adjacent windows,
multiple assets/densities, precision, actual quote times, future receipts/events,
candle completion, missing types, strict admission, continuous staleness, retries,
corrupt/missing source artifacts, and byte preservation. Existing tests continue
to validate schema, provenance, imports and exports.

No financial placeholder was implemented or changed. No universal trading
threshold, live engine, production deployment, M13 partition algorithm or research
strategy is included. Source completeness does not establish executable liquidity,
profitability, historical fee applicability or arbitrary strategy-code leakage
safety. Those remain separate requirements.

Final local verification: **42 continuity tests passed**; the complete offline
suite finished with **961 passed, 7 skipped in 312.95 seconds**. Skips remain
existing opt-in/platform checks. A before/after SHA-256 audit verified **2,446
protected files unchanged**, including canonical Parquet, metadata SQLite,
historical manifests, notebooks and reserved algorithm/configuration files.
SQLite integrity was `ok` and foreign-key violations were zero. Actual historical
CSV export `de20b5eac677270dec785b9fbf926705a55cadbbf297c35d2be98f987bb23573`
passed its source, file-hash and row-count verifier. Both original M10 exports
(`d68088da...441ecb` and `d04de64d...8a068`) also reverified successfully. GitHub
CI results are reported with the PR delivery.
