# Frozen Phase 2 validation outputs

These are derived historical statistics, not raw market data. Inputs are the
recovered, hash-verified original nine-asset study. No strategy was retuned.
The original inspected OOS period remains inspected, not pristine.

- `dsr_corrected.csv`: N=2/10/25/50/100 under training-registry, OOS-registry and
  explicitly assumed IID-null dispersion. DSR probabilities are **not p-values**.
- `bootstrap_robustness.csv`:1999 paired moving-block draws per12/36/72/144 bars,
  absolute strategy/baseline and paired-difference intervals and approximate tests.
- `placebo_robustness.csv`:999 draws per run-length/circular null; unconditional
  and exposure/turnover-conditioned finite-sample probabilities.
- `placebo_balance_diagnostics.csv`: observed levels, all-trial gaps, conditional
  accepted counts and remaining exposure/turnover imbalance.
- `placebo_distributions.csv`: observed/null Sharpe, wealth return, drawdown,
  exposure and turnover summaries.
- `btc_hac_sensitivity.csv`: gross/net BTC regressions and lag/diagnostic sensitivity.
- `train_oos_stability.csv`: split, annual and quarterly results for both strategies.
- `period_concentration.csv`, `positive_return_sensitivity.csv`: descriptive
  concentration and hindsight zeroing of the largest1/5/10 positive bars.
- `phase2_manifest.json`: original/source/output hashes, seeds, dependency versions,
  sample boundaries, original cleaning coverage and accounting verification.

Returns, drawdowns and alpha are fractions; multiply by100 for percentages.
Annual mean/alpha are arithmetic, not compound wealth. Turnover is summed full-L1
gross traded notional divided by each bar's starting equity; fee sums are not
fixed-initial-capital dollar costs. Exposure is mean absolute held notional/equity.
`undefined` explicitly labels Sharpe in flat warm-up/inactive periods or a
concentration share with no positive periods; the accompanying status explains it.

The batch evaluator changes only the trial axis and preserves the frozen55-step
fee solve. Its observed path matches the original at1e-12 tolerance; all trial
gross/net/fee/turnover identities are checked. Large local computational caches
are ignored, not committed.

Reproduce using `docs/phase2_input_contract.md`. Validate output integrity with:

```powershell
python -m research.phase2.verify_results --results phase2_results
```

The report's conclusion B is exploratory relative improvement, **not** positive
standalone alpha, a profitability claim or trading permission. Review full
methodology, statistical report and limitations under `docs/` before interpretation.

## 2026-10-16 reviewed version

The current report uses `reviewed_20261016/`, which preserves a complete regenerated output/manifest set. Seven root-level CSVs match byte for byte; the two expanded diagnostic tables preserve all shared fields. Root-level artifacts are retained as the previous generated version. No strategy, cost, sample or seed was retuned.
