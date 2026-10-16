# Reproduce Phase 2 on the original research machine

The five-asset daily `kraken_quant` system is preserved. The requested nine-asset
four-hour study is a different corpus. Its notebooks and caches were recovered
outside this repository under `C:/Users/avyas`; their exact source cells and small
provenance files are checkpointed in `research/phase1_frozen`. Raw data, original
notebooks/results and the separate source repository remain unchanged.

Install `requirements-ci.txt` and `requirements-research.txt`, then run:

```powershell
python -m pytest -q tests
python -m research.phase2.run_validation --original-research-root C:/Users/avyas --output .phase2-cache/reproduced-results
```

The recovered-study command verifies the original manifest/data hashes, executes
the exact frozen source functions and reconciles both full net-return series.
It retains signed weights, prior training holdings, original millisecond close
timestamps and20bp gross-dollar costs. An ignored local preparation cache avoids
recomputing unchanged signals. Original CSV comparisons and before/after hashes
are checked on every run. The batched trial evaluator changes only the independent
trial axis:55-step fee solve, drift, post-fee sizing and no final-close trade are
unchanged and tested against verbatim original functions at1e-12 tolerance.

The trial batch is evaluated once and reused by a content-hashed local cache,
including a digest of generated masks. No large cache or raw data is committed.
The prospective `--artifact-dir` import mode below is available for a separately
packaged frozen corpus; it is not needed for the recovered original study.

An absent manifest yields exit2 and an explicit blocked manifest with empty
schema-only CSVs. Invalid hashes, missing samples, changed replay or undefined
inferential statistics raise an error. There is no market download, substitute
portfolio engine, parameter tuner or existing-project test-unlock option.
Different output runs need separate directories; writes refuse changed existing
files. An interrupted publication can leave partial files, so publish in a new
directory and verify every manifest hash before external use.

## Required original inputs

Place `phase1_manifest.json`, frozen returns, original accounting source and
hashed market inputs under the artifact directory. Paths must stay within it.
Allowed research file types: CSV, Parquet, JSON and Python. Never put `.env`,
credentials, private keys or unreviewed executable artifacts there. Data/cache
directories are ignored; only small derived tables and manifests belong in Git.

The manifest schema requires these fields (these are field definitions, not
invented original dates or hashes):

| Field | Required value or source |
|---|---|
| `schema_version` | `1` |
| `study_status` | `historical_already_inspected` |
| `universe` | BTC, ETH, BNB, ADA, LINK, XRP, XLM, DOGE, AVAX, each once |
| `annualisation` | `2190` four-hour bars/year |
| `cost_per_gross_dollar` | `0.002` |
| `return_timestamp` | `bar_end_utc` |
| `accounting` | `exact_self_financing_drift_aware` |
| `returns_file`, `engine_file` | Relative filenames of frozen returns/replay adapter |
| `files` | Mapping of every original data/source artifact filename to SHA-256 |
| `data_files` | Nonempty list of hashed original daily/4h market inputs |
| `splits` | `train` and `oos`, each with original inclusive timezone-aware `start`/`end` |
| `trial_sharpe_std_periodic` | Across-trial per-four-hour Sharpe dispersion |
| `trial_dispersion_provenance` | Frozen trial-record derivation or justified explicit assumption |
| `coverage` | Original per-asset daily/4h date coverage, missingness and exclusions |

If original annualisation or timestamp conventions differ, reconcile explicitly
before use; do not silently relabel them. A file hash proves byte identity, not
correct provenance, causal signals or correctness of original strategy logic.

The return CSV requires a `timestamp` column and exactly aligned numeric columns:
`strategy_net`, `baseline_net`, `strategy_gross`, `btc_return`, `gross_exposure`,
`turnover`, `baseline_exposure`, `baseline_turnover`, `regime_active`.
Return columns are simple returns greater than-1; exposures/turnover are
nonnegative, activity is0/1. Timestamps are unique ordered gapless UTC4h bars.
`strategy_gross` is optional if Phase 1 did not record it; gross regression then
is explicitly marked unavailable, while net regression still runs. Naive timestamp
strings are rejected rather than silently interpreted as UTC.
Missing boundaries/values fail without filling or dropping. Gross and net return
definitions must come from Phase 1; this layer does not impose a false linear
gross-minus-net cost identity on exact self-financing accounting.

## Frozen accounting adapter

The supplied Python module implements `build_engine(root, manifest)` returning an
object with `replay()` and `simulate_filter(mask)`. This is trusted reviewed Python,
not a sandbox. Hash all its source dependencies and external artifacts; installed
dependencies must be frozen separately. No adapter has been manufactured here.

`replay()` returns the full frozen frame with the above columns/index, using the
original signals, weights, executions, costs and capital conventions.
Absolute numerical tolerance is1e-12, relative tolerance0. Every column and
timestamp must reconcile, not only headline Sharpe or cumulative return.

`simulate_filter(mask)` keeps original momentum signals/prices and the frozen
accounting unchanged. It supplies original training warm-up/portfolio entry
conventions, varies only the OOS binary filter and returns an exactly aligned
frame with `net_return`, `gross_exposure`, `turnover`. Applying the observed mask
must reproduce frozen OOS results before any placebo trial runs. Original engine
causality still requires independent source review; hashes alone cannot prove it.

The runner uses specified seeds,1999 bootstrap replications per block length and
999 trials for each of two placebo methods. It verifies input hashes again after
computation, records source/dependency versions, original splits, sample dates,
exposure/activity and output hashes. Source hashes capture implementation even
when a result commit necessarily follows the code commit.

OOS is already inspected, not a fresh holdout. No Phase 3 trading eligibility is
created by successful execution, and no result is automatically classified A/B/C
without reviewing the generated statistical evidence and selection history.
