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

### 2026-10-16 - Local checkpoint receipts and research status

Implementation checkpoint `1587c70` preserves the completed Phase2 slice; independent publication correction `8fec265` supersedes the old headline locally. The0.37 candidate remains exploratory and unqualified for live promotion. Original study and output versions are preserved. No new candidate was invented, tuned or declared successful to satisfy the delivery schedule. Independent review and genuinely new evidence remain required; the old PDF/DOCX/charts are marked archival and the current correction is Markdown. No remote publication or live execution occurred.

### 2026-10-16 - Independent research and strategy implementation

Known historical trial reconstruction identified18 recorded correlated four-hour strategies; full historicalexperiment count remains unknown. DSR sensitivity explicitly varies effective counts/dispersion without inventing trials. Expanded point-in-time eligibility, execution-cost/impact/delay/financing and prospective protocol implementations have passing tests; authentic20-30asset listing/delisting/liquidity inputs are not available locally.

Original five-asset Binance caches were found. A separately registered fixed long-only candidate was evaluated on training only through the shared engine:Sharpe1.3063, return+461.25%, drawdown56.90%, failing the unchanged15% drawdown gate. Venue/USDT proxy and financing assumptions are explicit. The current four-hour0.37Sharpe/-30.04%finding is unchanged. Independent audit receipt reconciles original inputs within1e-16 and verifies all9tables. Both executable examples produce fills, portfolio exports and reproducible manifests; custom factory and causalfeature interfaces are implemented. Promotion/evidence binding is undergoing peer QA; no future or unseen observations are manufactured.


### 2026-10-16 - Final integrated research, candidate and publication receipt

Frozen original question and evidence: The 9-asset, 4-hour Binance/USDT signed low-correlation momentum study is not a Kraken spot strategy. Its hashed raw inputs, parameters, costs and outputs were preserved; Phase 1 arithmetic/look-ahead/timing/turnover/fees/self-financing checks and Phase 2 bootstrap, placebo, corrected DSR, BTC HAC and train/OOS stability reproduce under the frozen implementation. Independent raw replay reconciles returns within about1e-16; all9 result tables pass the manifest verifier. No locked daily Phase 2 test was opened. Close-price fills, signed positions, excluded short financing, additional spread/impact, missing-data exclusions and survivor selection remain limitations.

Corrected inspected OOS: Sharpe0.3716717129 (published as0.3717), cumulative net return+12.01160961% (12.0116%), maximum drawdown-30.03665202% (-30.0367%). The earlier 0.75 headline is superseded. Relative improvement versus the losing unfiltered baseline is promising but every absolute Sharpe bootstrap interval crosses zero; net BTC-adjusted alpha is insignificant (p about0.57-0.60), result is concentrated and standalone alpha is not established. OOS is inspected, not pristine. Existing 15% maximum-drawdown and Sharpe/other acceptance thresholds were not relaxed.

Multiple testing: Reconstructed trial records show18 known correlated strategies/variants; the complete experiment history is unknown. DSR sensitivity spans declared effective trial counts and dispersion assumptions; it cannot identify the true effective count. Correlation participation rank is descriptive and not an estimate of effective N. No missing trials were invented. No new optimisation trials were added in the separately registered candidate evaluation.

New Kraken-compatible candidate: A separate fixed long-only five-asset daily BTC/ETH/XRP/SOL/ADA candidate was evaluated on training only using local historical Binance/USDT cache as a venue/quote proxy. Frozen candidate SHA256 `d60807ad8c61d47bce517f29ecbd237b2a7107965c8459024d992d4371cfc3f5`; training Sharpe1.3063267, return+461.2548%, maximum drawdown56.8999%. It fails the unchanged15% drawdown gate decisively. This is not validation, Kraken execution evidence or qualification. No tuned test/OOS result is claimed. Two new preregistration versions were overwritten/deleted during schema/source-identity fixes; exact original bytes and timestamps are unavailable. `registration_history_reconstruction.json` is clearly marked reconstructed/invalid, and history records one unchanged empirical evaluation, zero added optimisation trials and final candidate hash. The limitation affects audit chronology but does not change the failed result.

Point-in-time universe and realism: The implemented PIT engine carries instrument listing/delisting and known-at timestamps, prior-only liquidity eligibility, missing-data/provenance/survivorship guards and fail-closed symbol/hash/infinity checks. Thirty-asset fixtures verify logic. No authentic 20-30+ asset historical listing/delisting/liquidity archive was available; authentic expanded-universe robustness returns therefore remain unavailable. Execution stress runs nine fixed-notional training scenarios varying fees, spread, slippage, impact, liquidity, delay and financing. It is sensitivity work, not a queue simulator and does not replace frozen accounting. Binance signed findings are kept separate from executable long-only Kraken spot.

Validation/evidence: Daily aligned paired bootstrap, DSR sensitivity, benchmark HAC and correlation-alignment placebo are implemented; the latter is explicitly not a regime-profitability placebo. Frozen original Phase 2 runner retains its separate regime-mask placebo. Candidate/paper evidence is bound to candidate and data/code/output hashes and must contain actual consecutive post-freeze decisions, >=29 elapsed days, >=30 daily decisions, >=20 distinct order lifecycles and the unchanged profitability/reliability/stress/review gates. Synthetic demo and accelerated replay are always labelled and fail live eligibility. Fresh genuine public WebSocket five-asset flat observation had one complete decision, zero fills, zero faults, five actual quotes and no account-fee verification; prospective counts remain1/30 decisions and0/20 lifecycles in this fresh isolated database. Previously completed public diagnostics are preserved in their own DB. No unseen future data was claimed.

Current independent publication artifacts are `systematic-crypto-regime-research/report/crypto_momentum_regime_research_current.{md,pdf,docx}`, `figures/current/` and reviewed `results/reconciled_phase2_20261016/`. PDF, DOCX and chart CSV values agree exactly at displayed precision; public tests3 pass. The paper states the study is exploratory, supersedes0.75 and retains an independent-review checklist. No second-person external sign-off has occurred. Public repository excludes the licensed engine and raw caches. DOCX structural/numeric layout was verified; a visual Word/LibreOffice renderer is unavailable.

No strategy meets acceptance. No return of a paper/shadow operator or synthetic simulation changes that conclusion. Future market time, authentic expanded-universe archive, complete original trial logs, dedicated account credentials/verification, independent second-person report review and separate explicit approval for any first real order remain external/evidence dependencies.


### 2026-10-16 - Completion branch publication

The independent corrected paper and generated PDF/DOCX/charts are pushed in public commit `d34ee159661b834acc66e60de4de6d73730197ea`; the implementation and research audit artifacts are pushed privately in commit `44c79befe272459b015ec8c3b27a90fde0a5ae10`. Both remote branch hashes match local. Publication verifier still matches0.3717 Sharpe,+12.0116% net return and-30.0367% maximum drawdown. This publication does not claim a second-person independent sign-off. Original0.75 headline remains superseded.


### 2026-10-16 - Eric/Mark technical audit and frequency reconciliation

Original reviewer requests were accessible via Outlook Email dated 2026-10-08. Eric's turnover convention is full traded notional per beginning equity, with both buy and sell legs counted and fee 20bp; independent replay verifies the frozen self-financing equation within floating-point tolerance. The published 0.3717 is 4-hour sample Sharpe, zero risk-free rate, sqrt2190. A reproducible aggregation of the 5,448 already-inspected OOS bars into 908 compounded UTC days gives Sharpe 0.2999 at sqrt252 and 0.3609 at sqrt365; total return remains 12.0116096%, wealth reconciliation error 5.8e-15. These statistics are not pristine validation.

Eric's BTC regression is reproduced with beta -0.0113, R-squared 0.00120 and net arithmetic annual intercept +6.04%; HAC p-values 0.566-0.601. It is a single BTC-factor regression, not full CAPM excess alpha. Mark's same-share-of-time placebo is literally satisfied: both randomization generators preserve the active fraction and tests assert this. P-values and conditioning remain unadjusted for unknown historical experiments; nulls are not market-state matched. Internal independent QA re-ran the frozen study and nine artifact hashes and reviewed these definitions; this is not human external sign-off.

Eric completion 80%, Mark 87.5%, combined engineering 100%, combined empirical-inclusive closure 82.1%, under equal row weights and 50% engineering/50% empirical subweights. Authentic PIT 20-30+ asset data, complete original experiment history and truly future unseen observations remain unavailable. Full matrix, evidence paths and per-row formula: `docs/ERIC_MARK_TECHNICAL_AUDIT.md`. Current PDF/DOCX and table carry all Sharpe conventions; old 0.75 headline remains superseded.


### 2026-10-16 - Eric/Mark final empirical closure correction

The final technical-feedback pass resolved the prior expanded-universe data blocker with an authentic official Binance archive cohort. `research/extended/binance_pit_replay.py` selects 30 non-fiat/non-stable/non-leveraged USDT assets by August 2021 quote volume, before the October 2021 study start; all 297 daily selection snapshots and 1,709 4-hour archive inputs were verified against official SHA-256 sidecars. The cache remains outside Git. Replay uses frozen Phase 1 signal/regime/accounting functions, fixed 20bp cost and fail-closed unexplained-gap termination; two terminal-loss assumptions are published. This is static inception membership, not rolling top-30, and exact exchange-effective listing/delisting, token migration and recovery records remain incomplete.

The already-inspected OOS low-correlation cohort result is Sharpe -1.4519, return -43.9477%, maximum drawdown -44.9674%; the baseline Sharpe is -1.8367 to -2.2525 across terminal assumptions. This shows relative improvement from a poor baseline, not alpha. OOS is not pristine. Eric's daily-compounded 4-hour audit reconciles 5,448 bars into 908 UTC days: 4-hour Sharpe 0.3716717 at sqrt2190, compounded daily Sharpe 0.2998513 at sqrt252 and 0.3608711 at sqrt365; wealth reconciliation error 5.8e-15.

Formal row-weighted scores, with 50% implementation and 50% empirical evidence per requirement, are Eric 87.5%, Mark 87.5%, engineering 100%, combined reviewer closure 87.5%. Latest verification: private `python -m pytest -q` 323 passed; `python -m compileall -q research tests` passed; Phase 2 verifier passed nine tables, 1,999 bootstrap draws and 999 placebo draws per method. Public suite 3 passed; paper verifier confirmed 4 charts, six-page PDF and numeric agreement across source tables, PDF and DOCX. DOCX visual rendering remains unverified because LibreOffice/soffice is absent. Internal subagent QA is not an external human sign-off. Exact exchange event/recovery metadata, complete trial history, future unseen observations and external reviewer sign-off remain the evidence gaps. Full matrix: `docs/ERIC_MARK_TECHNICAL_AUDIT.md`.
