# M13: dynamic multi-asset calendar partitions

M12 PR #13 was verified merged before implementation. The approved base is
`review-base/milestone1-20261029` at
`1fb6322fd3e8535f8f34f2faea48d1bbef67b89f`. `main`, existing market data,
research notebooks, reserved financial algorithms and M11 manifests are unchanged.

## Existing behavior and deliberate additions

M11 already provides immutable per-asset 60/20/20 observation-count splits,
raw-record hashes, source references and `ResearchData.load_partition`.
Those APIs remain readable; independent M11 dates are **not** certified jointly
safe. M12 provides exact source validation, event/receipt timing, source-backed
no-trade distinctions, gap/staleness metrics and configurable admission policies.
M13 calls those existing validators rather than redefining candle or book validity.

The new `PartitionAlgorithm` allocates **shared calendar cells**, validates every
asset/data type/phase, and stores immutable successful plans. The same service
handles research and future production-validation requests; neither mode submits
orders or authorizes deployment. No alpha, portfolio, fitting, fee or slippage
algorithm is implemented.

## Request and visible defaults

`PartitionRequest` contains selected assets, strategy/dataset identifier, mode,
required datasets, frequency, evaluation/knowledge cutoff, optional event-window
start/end, proportions, per-phase count/duration minima, M12 continuity requirements,
feature/reference lookback, label horizon, purge, embargo and optional rolling folds.
Canonical `BTC-AUD` and notebook spelling `BTC/AUD` select the same known market;
no unknown exchange identity or listing date is invented.

All defaults are versioned in the manifest configuration:

* Algorithm `common-calendar-v2`, manifest contract 1 (the original v1 plans remain readable).
* `FIXED_WINDOW` is the safe default: explicit start and exact grid-aligned bounds
  are required. `AUTO_WINDOW` must be requested explicitly.
* COMMON_CALENDAR, research mode, raw `prices_1m`, one-minute frequency.
* Calendar envelope proportions 0.60 / 0.20 / 0.20; minimum one genuine observation
  and one calendar minute in **each phase for each asset and dataset**.
* EVENT_TIME continuity; no universal density, gap or age threshold. Users must
  specify requirements appropriate to their intended experiment.
* Zero feature/reference lookback, label horizon, purge and embargo; minimum one
  context observation when a nonzero feature lookback is requested.
* Two million grid cells, one million physical input records per candidate asset
  group, 128 candidate windows and at most 32 rolling folds. Exceeding a bound
  returns INVALID_CONFIGURATION with instructions to narrow the request or
  explicitly raise the relevant budget; assets/thresholds are never silently changed.

The cutoff cannot exceed the reader's knowledge time or clock. `end` bounds event
history separately from the cutoff: a retrospective June 2024 study may use an
archive actually acquired in November 2026, **without** claiming that vintage was
available in June. Received-time validation, when requested, rejects such backdating
using M12. A future production caller chooses the same explicit data-vintage policy.

## Calendar selection and sufficiency

1. Inspect actual persisted observations and require known market identity for
   every selected asset and required dataset at the cutoff.
2. Derive the common available interval, apply optional event bounds and reserve
   explicit feature/reference warmup. Align boundaries to the UTC epoch/frequency.
   This describes AUTO_WINDOW. FIXED_WINDOW never clips, aligns or shifts the
   requested envelope: unaligned requests reject, unavailable history/context
   rejects, and phase minima/quality are checked on the original dates. Prior
   context can be loaded before the envelope without moving its sample start.
3. Verify original records and source dependencies through `ResearchData`.
4. Intersect per-asset M12 admissibility masks. Strict observed grids, complete-source
   requirements, excessive unknown gaps and excessive staleness can split history
   into separate usable runs. No-trade evidence is not an executable price.
5. Try the longest common runs first, preferring the most recent on equal spans.
   If a fixed-proportion window has too few observations, also try source-derived
   gap boundaries, including recent suffixes after long inactive history. The
   bounded search never uses prices/returns to select a window, relaxes a threshold,
   drops an asset, forward-fills or changes the requested proportions.
6. Allocate floor(N * training proportion), floor(N * validation proportion), and
   the remaining calendar cells to testing. Proportions apply to calendar envelopes,
   not each market's observation count. Purge/label/embargo reduce usable phases;
   the resulting phases must still meet all explicit minima.
7. Run M12 admission independently for every asset, dataset and phase, including
   feature context. Calendar duration, eligible observation counts, source coverage,
   gaps and staleness are checked. A long first-to-last span alone never passes.

The search is deterministic and bounded, not a financial optimization or proof
that every conceivable subwindow has been exhausted. If the candidate budget
runs out without a valid result, the response is INVALID_CONFIGURATION, not a
claim that no valid research window could exist. Increase the budget or narrow
the period explicitly. Otherwise failed requests explain the actual attempted
asset/phase requirements and observed limitations.

### Window selection governance

FIXED_WINDOW makes exactly one attempt on the requested calendar envelope. It
does not retry a recent suffix or change the anchor when a phase is insufficient.
An explicit rolling schedule still uses its configured durations/step inside
that envelope; unused schedule/context/label/purge/embargo periods are reported
separately, not disguised as automatic window selection.

AUTO_WINDOW may choose a shorter sufficient common period using only source
availability, timestamps, counts and M12 data quality. Every result reports
`window_selection`: original and resolved requested windows, common available
window, selected window (null on rejection), excluded periods and reasons, phase
guard exclusions, candidate attempts/failures and per-asset/dataset/phase
sufficiency. Unknown or budget-limited results have null counts rather than
invented observations. No returns, strategy scores or performance objective is
accepted as a selection input. Requested thresholds and assets never change.

For research, a successful plan is already immutable **before evaluation**.
Additionally, `freeze_partition_plan(id)` registers that exact manifest under its
stable research identifier. Guarded release of any new-v2 research phase performs
this registration automatically **before returning data**, including training
data on which a caller could calculate performance. Explicit registration before
fitting is recommended. Integrity-only `validate_partition_plan` remains read-only
and neither registers the study nor marks test exposure.

Registration uses the existing process lock and immutable atomic writer. The
identifier binds to one manifest/configuration/phase layout. Identical later
requests reopen that frozen version, even with a later creation clock. A changed
window, universe, cutoff, policy or thresholds under that identifier rejects;
there is no API to unfreeze/reset it. Before registration, a changed request creates
a new immutable candidate manifest. After registration, a genuinely new,
preregistered study/version requires a new identifier and a new manifest. Old
boundaries, source hashes and use history are retained permanently.

Registration and use records live in ignored `data/partition_plans/governance/`.
These are new audit sidecars, not changes to SQLite, Parquet, original manifests
or raw observations. Guarded M13 data release writes these sidecars; ordinary
M11/M12/raw reads and integrity validation remain data-read-only.

Loading actual test samples records first exposure. Frames distinguish
`out_of_sample` from `untouched_out_of_sample`: the latter is never certified by
this API. `record_partition_feedback(id, reason)` permanently records that test
results informed development; `partition_governance_status(id)` exposes the state.
**An immutable test partition is not an untouched holdout once its results have
influenced strategy development.** Recreating the same dates, changing a strategy
name or writing a new manifest does not restore scientific independence.

These guards cover the supported API and honest stable identifiers. They cannot
detect arbitrary external Python/SQL reads, undeclared strategy feedback, manual
deletion of local audit files, or renaming the same study to evade registration.
New genuinely independent holdout data and external preregistration discipline
remain the researcher's responsibility. No financial performance is calculated
or stored by this implementation. Production-validation schedules retain explicit
versioned configuration; reusing an already registered research identifier cannot
bypass its registration by merely changing modes.

Statuses: READY, INSUFFICIENT_HISTORY, INSUFFICIENT_OBSERVATIONS,
INSUFFICIENT_DATA_QUALITY, NO_COMMON_WINDOW, SOURCE_UNAVAILABLE,
INVALID_CONFIGURATION. Rejections have no manifest ID, no executable phases and
no persisted executable plan. Failures carry asset, dataset, fold, phase,
required/available counts or durations, and exact M12 reasons where applicable.

## Independence, feature and label boundaries

COMMON_CALENDAR uses identical boundaries for all selected assets in each fold.
ASSET_INDEPENDENT requires `independent_experiments=True`. Such plans are blocked
by default joint data access; `joint=False` is an explicit independent-experiment
declaration. Combining them requires a fresh common-calendar temporal validation.

Each phase is `[start, end_exclusive)`. Training precedes validation and validation
precedes testing. The label horizon is removed from each envelope's end; purge is
additionally removed before validation/test transitions, and embargo delays the
next phase's sample start. Label support cannot cross the next phase's start.
Feature context is a separately loaded, strictly earlier interval. Actual required
reference history is pinned too; a late context record cannot become known before
its receipt when received-time policy is enabled.

RollingConfig supplies three explicit envelope durations, a positive step and
fold count. Every fold is independently validated. Test windows cannot overlap
across folds. Training windows may intentionally overlap earlier folds, including
earlier test periods after they become past data: out-of-sample labels are **per
fold**, not a promise of a globally never-reused holdout.

`purpose='fit'` is accepted only for training. Validation/test frames cannot be
requested as fitting inputs through this API. This does not inspect arbitrary
user code, serialized transformers, feature definitions or external datasets.
Callers remain responsible for fitting transforms on training only, freezing them
for evaluation, declaring true feature/label horizons and avoiding performance-led
window selection. A validated dataset is not an approved strategy or live order.

## Immutable plans and reproducible loading

Only successful plans are written under `data/partition_plans/v1/<sha256>.json`.
They contain the normalized universe/configuration, mode, cutoff, creation time,
version, exact boundaries, all per-phase quality results, metadata/source hashes,
raw-record hashes and compressed logical-key identities. These identity pins do
not copy market prices or full order books. Full reference/feature inputs are pinned
by identity as well as selected samples, including late-arrival evidence.

Writes reuse M11's atomic immutable writer. Changing configuration, universe,
cutoff or source inputs creates a new content-addressed plan. A later generation
time is also a distinct audit version; a fixed-clock identical request reproduces
the same ID. Existing files are never rebalanced or overwritten.

The loader verifies manifest hash/version/chronology, pinned metadata and sources,
original input keys/hashes, and reruns M12 phase/context admission. Newly appended
records inside an old date range do not enter its pinned dataset. Missing or changed
dependencies fail closed. Sources remain local immutable trust anchors rather than
an external signature authority. The original M11 loader remains separate and
unchanged.

## Python usage

```python
from datetime import datetime, timedelta, timezone
from kraken_quant.research_data import ResearchData
from kraken_quant.partition_contracts import PartitionRequest
from kraken_quant.market_continuity import ContinuityRequirements

reader = ResearchData('data')
request = PartitionRequest(
    assets=('BTC/AUD', 'ETH/AUD', 'SOL/AUD'),
    identifier='user-experiment-001', cutoff=reader.as_of,
    window_mode='AUTO_WINDOW',  # opt into data-only period adjustment
    start='2024-06-01T00:00:00Z', end='2024-07-01T00:00:00Z',
    frequency=timedelta(hours=1),  # evidence buckets; raw minute candles unchanged
    minimum_observations=(10, 10, 10),
    minimum_durations=(timedelta(days=1),)*3,
    continuity=ContinuityRequirements(require_source_complete=True),
)
plan = reader.generate_partition_plan(request)
if not plan.ready:
    print(plan.status, plan.report['failures'])
else:
    reader.validate_partition_plan(plan.manifest_id)  # read-only whole-plan check
    print(plan.report['window_selection'])
    reader.freeze_partition_plan(plan.manifest_id)  # before fitting/performance evaluation
    train = reader.load_partition_phase(plan.manifest_id, 'training', 'BTC/AUD', purpose='fit')
    valid = reader.load_partition_phase(plan.manifest_id, 'validation', 'BTC/AUD')
    test = reader.load_partition_phase(plan.manifest_id, 'test', 'BTC/AUD')
    # If configured, load historical feature context separately:
    context = reader.load_partition_phase(plan.manifest_id, 'training', 'BTC/AUD', context=True, purpose='features')
    # If holdout results later influence development, record that fact:
    # reader.record_partition_feedback(plan.manifest_id, 'Test results informed the next strategy revision.')
```

The example selects raw observations for inspection. It does not implement a
strategy, normalization, backtest or performance calculation. Hourly continuity
buckets do not manufacture hourly candles or certify minute-level completeness.

## Verification and remaining limitations

Deterministic offline tests cover different listing/data starts, newly selected
assets, addition/removal, fresh cutoffs, intermittent usable runs, insufficient
counts/durations, strict grids, missing types, independence restrictions, feature
lookback, label/purge/embargo boundaries, rolling folds, receipt-time rejection,
immutable pins, new data additions, source corruption, missing partitions, whole-plan
revalidation, and the original M11 interface. Existing continuity and export tests
remain regression requirements. Actual examples and final counts accompany delivery.

Historical status, historical account-fee applicability and as-published archive
revisions remain unknown where prior milestones lack evidence. Sparse price-only
plans cannot authorize execution-cost studies or live trading. Research callers
must choose requirements before inspecting strategy performance; no financial
performance was examined during this implementation.

## Actual stored-data examples (original pre-governance v1 plans)

These are **real persisted Kraken observations**, not the synthetic test fixtures.
The retrospective event window is June 2024; the evaluation/knowledge cutoff is
the actual November 2026 run time. Requirements are raw prices, hourly continuity
buckets, at least 10 observations and one calendar day per phase, complete source
evidence, and the explicit EVENT_TIME policy. This is price-data inspection, not
continuous minute-grid or execution readiness. All bounds below are UTC and
end-exclusive; original minute timestamps remain unchanged.

BTC/AUD + ETH/AUD, READY, manifest
`4dc1815cc234998a6f62115dffbc454b382ee8553c48a4d852685b92cbe1baa2`:

| Phase | Shared start | Shared end | BTC rows | ETH rows |
|---|---|---|---:|---:|
| Training | 2024-06-01 00:00 | 2024-06-19 00:00 | 6,771 | 3,906 |
| Validation | 2024-06-19 00:00 | 2024-06-25 00:00 | 2,374 | 1,270 |
| Test | 2024-06-25 00:00 | 2024-07-01 00:00 | 2,303 | 1,181 |

Adding SOL/AUD automatically changes the common window and every joint phase.
READY manifest:
`cb7355c3b27336eb46c46274020014e6b6d499feea31f2298bdb9efb4e0f77de`.

| Phase | Shared start | Shared end | BTC rows | ETH rows | SOL rows |
|---|---|---|---:|---:|---:|
| Training | 2024-06-05 14:00 | 2024-06-20 20:00 | 5,798 | 3,433 | 1,505 |
| Validation | 2024-06-20 20:00 | 2024-06-25 22:00 | 2,053 | 1,011 | 477 |
| Test | 2024-06-25 22:00 | 2024-07-01 00:00 | 1,872 | 994 | 481 |

All 15 asset/phase frames were loaded through the guarded loader and their exact
pinned rows/source dependencies verified. Identical fixed-clock/cutoff requests
reproduced both IDs. Manifest files are 168,172 and 182,588 bytes respectively;
they remain ignored under `data/partition_plans/v1/`.

The newer AUDX/AUD data was also tested jointly with BTC/AUD for the requested
December 2025-February 2026 interval. A 180-day/10,000-observation training
requirement and 30-day/1,000-observation validation/test requirements were explicit:

| AUDX phase | Available duration | Available observations | Required duration | Required observations |
|---|---|---:|---|---:|
| Training | 26 days | 228 | 180 days | 10,000 |
| Validation | 8 days 16 hours | 72 | 30 days | 1,000 |
| Test | 8 days 16 hours | 101 | 30 days | 1,000 |

Result: **INSUFFICIENT_HISTORY**, with additional exact observation failures and
**no executable manifest**. The common window does not borrow older BTC history
that AUDX lacks. These are observed data bounds, not an invented formal listing date.

Initial M13 focused suites: **148 passed**, including **34 M13 cases**. Complete offline
suite: **1,004 passed, 7 skipped in 396.01 seconds**. The skips are existing opt-in
or platform checks. All **2,446 protected files** retained their SHA-256 hashes;
SQLite integrity is `ok` and foreign-key violations are zero. No canonical record,
old manifest, notebook or reserved algorithm was changed. GitHub CI results are
reported on the PR and in delivery.

Cold source verification can be expensive because existing provenance validators
rehash original artifacts. Bounded windows/budgets are deliberate; this release
does not introduce an unsafe persistent checksum cache or another data store.

### Governance revision compatibility

The original v1 manifests above retain their exact bytes, boundaries and source
hashes. They remain readable with `LEGACY_USE_HISTORY_UNKNOWN`, not a newly invented
untouched-holdout certificate. M11's original partition reader and all M12
continuity interfaces are unchanged. Legacy metadata flags are not proof that a
test result has never influenced strategy development.

Regression cases now cover fixed-window failure despite a usable later period,
explicit automatic adjustment and exclusion evidence, unchanged fixed dates,
unavailable initial history, rejected automatic requests, phase sufficiency,
pre-access registration, frozen-version reuse, changed-request rejection,
read-only integrity checks, recorded holdout exposure/feedback, rolling schedules
and old manifest compatibility. Final revised test/CI counts accompany delivery.

Governance revision verification: **47 partition tests passed**; the focused
partition/continuity/historical/export suites passed **161 tests**. The full offline
suite finished with **1,017 passed, 7 existing skips in 230.82 seconds**. All
**2,448 protected files**, including the two original M13 manifests, retained their
hashes. Both real v1 plan files remain readable with their original boundaries
and source references. No market observations, notebooks or old manifests changed.
