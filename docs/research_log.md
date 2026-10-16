# Research log

Maintained from 2026-10-16. This cumulative record covers the original question, data, study evolution, recovered accounting, statistical evidence and remaining research gates. Reconstructed history is identified by source/commit rather than invented conversation chronology. Update this file whenever methodology, results, interpretation or promotion status changes. Never retune frozen results to meet a target.

## Original question and deliverables

Do regime-conditioned momentum, reversal and volume signals generate robust risk-adjusted returns across liquid cryptocurrencies after realistic costs? Deliver a reusable framework, at least one properly validated candidate, reproducible tables/charts, a draft paper, Kraken shadow and risk-controlled live capability, with no profitability claim unsupported by untouched and live evidence.

The original Kraken framework uses BTC/USD, ETH/USD, XRP/USD, SOL/USD and ADA/USD daily bars. Full range 2020-08-11..2026-09-30; training through 2023, validation 2024, locked test 2025..2026-09-30. Baseline availability/liquidity and daily integrity checks were completed. Those checks must not be confused with the separate nine-asset study's dropped rows/continuous suffix.

## Historical record

| Stage | Evidence | Outcome |
| --- | --- | --- |
| Baseline and connectivity | 2026-09-09/15 commits | Stable TWSQ baseline and read-only Kraken connection. |
| Universe/configuration | 2026-10-01/02 commits | Five liquid Kraken pairs, central splits and daily audit. |
| Research foundation | e1a7b81, 341cba9 | Causal data restrictions, funded accounting, costs, metrics and benchmarks. |
| Signed experiments | 073aeb7 | Common pipeline, immutable trial registration, train/validation evaluation; synthetic verification. |
| Original empirical study | Separate published research repository and recovered notebook sources | Daily/4H momentum and reversal, correlation/dispersion/volatility/volume regimes, costs and initial paper/charts. Initial published 0.75 Sharpe is superseded; do not reuse as current finding. |
| Phase 1 reconciliation | Original local notebook/exports, source cells and hashes | Exact self-financing/drift-aware accounting produced reconciled results; original return exports replay within about 1e-16. |
| Initial Phase 2 inventory | dfd0538, checkpoint inventory | Original inputs absent from initial checkout; historical validation initially blocked. |
| Supplemental discovery | 58249f2 and discovery manifest | Original notebooks, caches and result exports found outside repo; selected source/provenance preserved. Missing-input blocker resolved locally. |
| Phase 2 implementation | Current source/results | Bootstrap, placebo, corrected DSR, BTC HAC and stability/concentration tables generated from frozen inputs. |
| 2026-10-16 integrity review | `python -m research.phase2.verify_results --results phase2_results` | All nine result tables passed integrity checks. Full historical computation was not rerun by this check. |

## Recovered study definition and data

Nine assets: BTC, ETH, BNB, ADA, LINK, XRP, XLM, DOGE, AVAX. Four-hour study uses Binance USDT; daily exploratory data uses Yahoo Finance. This introduces exchange/quote-currency and survivor-selection limitations for a Kraken USD spot deployment.

Original raw caches, notebooks and exports remain local outside the repository. The manifest records SHA-256 identity, frozen source, environment, sample boundaries and output hashes. Recovery must use those exact inputs, not new provider downloads or a substitute five-asset dataset.

Manifest coverage: daily 2,734 original rows, 628 missing rows and two observations discarded before the continuous suffix; 2,103 retained. Four-hour 16,430 original rows, 3,810 missing rows, six irregular intervals and 2,230 observations discarded before the continuous suffix; 10,389 retained. Effective 4H sample 2021-09-29 15:59:59.999 UTC through 2026-06-26 23:59:59.999 UTC. Training has 4,941 bars, through 2023-12-31. Inspected OOS has 5,448 bars, 2024-01-01 03:59:59.999 UTC onward. These are historical cleaning disclosures, not newly selected samples.

Frozen accounting uses signed weights, drift-aware turnover and self-financing transaction-cost solution with 55 iterations; cost 0.002 per gross dollar traded. Batch trials vectorize only the trial axis and match observed original calculations within 4.5e-16 (tolerance 1e-12). Numerical agreement does not validate close-price fills, borrowing availability, financing or actual execution. Extra spread/slippage and short financing are excluded in this historical study.

## Research/statistical component map

| Component | Responsibility |
| --- | --- |
| `phase1_frozen/source_cells.json` | Exact selected original notebook code with provenance; preserves recovered accounting source. |
| `phase1_frozen/discovery_manifest.json` and initial manifests/tables | Recovery identity and initial study records. Older outputs remain historical evidence, not current headline results. |
| `phase2/freeze_notebooks.py` | Extracts selected original notebook source/provenance. Does not authorize changing originals. |
| `original_study.py` | Hash-verified recovered study reconstruction and reconciliation. |
| `frozen_accounting.py` | Batched replay preserving frozen accounting equations/solver. |
| `common.py` | Input/time alignment, descriptive definitions, provenance and deterministic publication helpers. |
| `dsr.py` | Per-observation PSR/DSR with explicit trial dispersion, skew/kurtosis and selection benchmark; probabilities are not p-values. |
| `bootstrap.py` | Paired moving-block resampling, absolute and relative uncertainty across all declared block lengths. |
| `placebo.py` | Run-length and circular randomizations, activity/run preservation, exposure/turnover conditioning and empirical tail probabilities. |
| `btc_regression.py` | Gross/net BTC OLS with Bartlett HAC covariance, lag sensitivity and residual diagnostics. |
| `stability.py` | Train/OOS, year/quarter reconciliation, concentration and positive-bar sensitivity. Hindsight diagnostics are not tradable rules. |
| `run_validation.py`, `original_validation.py` | Generic contract and recovered-study orchestration; deterministic budgets, input checks and create-once outputs. |
| `verify_results.py` | Checks existing published tables/manifests and integrity; does not reproduce all inference or certify profitability. |

## Current empirical findings

Current reconciled OOS low-correlation momentum: Sharpe 0.3716717, cumulative net return 12.01161%, maximum drawdown -30.03665%. Baseline momentum Sharpe -1.0177455; paired Sharpe difference 1.3894172. The old published 0.75 Sharpe / 29.84% headline is superseded, as confirmed by the user on 2026-10-16. Do not mix its old accounting with reconciled outputs.

| Diagnostic | Specification | Current evidence and interpretation |
| --- | --- | --- |
| Absolute bootstrap | 1,999 draws each; block 12/36/72/144 bars; annualisation 2,190 | Strategy Sharpe 95% intervals [-1.081,1.609], [-1.002,1.625], [-1.152,1.673], [-1.425,1.670]. Every interval crosses zero. |
| Relative bootstrap | Paired strategy/baseline indices | Sharpe-difference intervals all positive; approximate one-sided p 0.0035..0.008. Supports relative improvement under the specified model, not absolute profitability. |
| Placebo | 999 trials each; run-length permutation and circular shift | Sharpe p 0.027 and 0.018. Conditioned p about 0.0289 and 0.0243, with 761/740 accepted trials. Probabilities are unadjusted for broader research selection. Drawdown evidence is weak. |
| BTC HAC | Net/gross, lags 12/36/72/144 | Net alpha p 0.566..0.601; annual arithmetic alpha about 6.04%, intervals crossing zero. Gross alpha is also sensitive/non-significant at 5%. |
| Corrected DSR | N=2/10/25/50/100; explicit dispersion variants | Original formula omitted trial-dispersion multiplier. Corrected results depend strongly on unknown effective trials and incomplete correlated registry. No uniquely identified DSR. |
| Stability | Split/year/quarter and outlier diagnostics | Negative training, positive inspected OOS; gains concentrated. Hindsight zeroing is a sensitivity diagnostic only. |

Interpretation: exploratory relative improvement (B), with no established positive standalone alpha. Manifest explicitly requires independent review of generated evidence. OOS has already been inspected and is not pristine. Tests, hashes and favourable relative p-values cannot promote the candidate to live eligibility.

## Outputs and publication state

`phase2_results/` contains nine derived CSVs plus manifest/README: corrected DSR; bootstrap; placebo robustness, balance and distribution diagnostics; BTC HAC; train/OOS stability; period concentration; positive-return sensitivity. Original raw data remains excluded. Current statistical report still describes the earlier missing-input blocker and must be refreshed.

`C:/Users/avyas/systematic-crypto-regime-research` is a separate clean publication repo at 6245db0. It contains independently written source, notebook, paper PDF/DOCX, charts and legacy machine-readable results. Its 0.75 headline must be marked superseded. `kraken-quant-github` is only a public overview, not the implementation repository. Do not publish licensed TWSQ source or secrets.

## Remaining research gates

1. Finish report consistency, review generated evidence, verify suite and commit the pending Phase 2 slice.
2. Review frozen close-price fills, shorting/financing assumptions and incomplete trial history; preserve limitations rather than silently changing the study.
3. Freeze a separately executable Kraken spot candidate and acceptance protocol before prospective evaluation. The 9-asset signed 4H result cannot become a daily 5-asset long-only candidate by renaming it.
4. Collect untouched prospective evidence; no elapsed time or market outcome can be simulated into compliance.
5. Current candidate misses proposed Sharpe >=0.5 / drawdown <=15% promotion criteria. Do not loosen thresholds retrospectively or claim completion of the validated profitable-strategy deliverable.
6. Sustained reviewed shadow evidence and eventually explicit live approval remain separate infrastructure/operational requirements.

## Dated updates

### 2026-10-16 - Cumulative logging established

Recorded original objectives, configuration distinction, full milestone history, recovered coverage/accounting, analysis components and actual findings before new implementation work. User confirmed the 0.75 Sharpe is old. Existing result integrity check passed nine tables. No fresh strategy tuning, provider download, locked-test evaluation or live order occurred. Next: reconcile documentation/publication, verify pending software and report research/promotion status honestly.

### 2026-10-16 - Full replay and report correction

Executed full frozen validation into `.phase2-cache/reproduced-20261016`, with verified original inputs and sealed null-cache reuse. Output integrity passed. Seven CSVs exactly match existing hashes; two add OLS/HAC/ARCH and accepted-placebo balance diagnostics, with no differences in shared fields. Preserved a new immutable `phase2_results/reviewed_20261016` set and its manifest, leaving earlier tables intact.

Replaced obsolete missing-input-blocked report with actual historical findings, training loss, bootstrap intervals, placebo conditioning, alpha/DSR limitations and explicit no-promotion decision. Added README/current-results links and remaining-work register. Full software suite passed246 tests after the isolated import-startup timeout fix. No strategy specification, parameter, split, market input or cost changed; no locked daily test was read. Paper/publication correction is being prepared; legacy artifacts remain archival.

### 2026-10-16 - Publication correction prepared locally

Updated the separate independent publication README to the reconciled0.37 headline and added the current report/methodology/limitations plus derived reviewed tables. Archived the original README; explicitly labeled old paper PDF/DOCX, notebook, charts and other CSVs as superseded artifacts instead of silently altering original evidence. No licensed TWSQ implementation, raw market cache or private account material was copied. No PDF/DOCX was authored:the current correction is Markdown; legacy binary papers remain marked archival. Remote publication is not yet performed.

### 2026-10-16 - Readiness remains open

Repeated real public paper attempts failed the5-second exchange-event freshness check despite valid daily coverage. No current live/shadow success is inferred from the246 passing synthetic tests. Independent publication's3 legacy invariance tests also passed; they do not validate current historical alpha. No eligible Kraken strategy, sustained collector, private account verification, live transport or new market evidence has been claimed. The completion register retains these open items explicitly.

### 2026-10-16 - Final verification before checkpoint

A separate fresh original replay bypassed the preparation cache, recomputed both frozen strategies from hash-verified raw inputs and reconciled10,389 observations; maximum net-return errors remained below1e-16. This strengthens replay verification but does not establish realistic execution or statistical independence. Current report and separate publication correction now include this verification. All nine reviewed output tables verify; original files/parameters remain unchanged.

### 2026-10-16 - Frozen-byte preservation

Detected and corrected a packaging issue:Git line-ending normalization would alter hash-frozen CSV/source/provenance bytes. Frozen artifacts now bypass text conversion; original Windows CRLF bytes are retained exactly in Git. This is a reproducibility packaging fix, not a numerical, strategy or methodology change. Current and legacy table manifests are preserved unchanged.

### 2026-10-16 - Checkout verification

Current reviewed output hashes and original source identities also pass after a fresh staged checkout. This closes the line-ending packaging defect rather than merely validating local files. Raw inputs, frozen parameters, seeds and results are unchanged.
