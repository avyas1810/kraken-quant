# Phase 2 statistical report - reconciled historical study

Updated 2026-10-16. Current tables: `phase2_results/reviewed_20261016/`. Earlier root-level tables remain preserved; the reviewed version adds diagnostics without changing any shared estimates. See the cumulative `research_log.md` for the complete study history and `infrastructure_log.md` for implementation boundaries.

## Executive finding

The recovered original nine-asset, four-hour study was successfully replayed and statistically evaluated. The low-correlation momentum filter improves on the losing unfiltered baseline, but there is insufficient evidence of positive standalone alpha or readiness for live trading. Interpretation **B: exploratory relative improvement without established standalone alpha**. The manifest retains `requires_independent_review_of_generated_evidence`; this report does not pretend to be a second person's independent sign-off.

The current reconciled OOS result is Sharpe **0.3717**, cumulative net return **12.0116%**, maximum drawdown **-30.0367%**. The earlier published **0.75 Sharpe / 29.84%** is superseded and must not be used as the current project result. All returns below are historical and hypothetical. OOS has already been inspected.

## Study, provenance and accounting

Assets: BTC, ETH, BNB, ADA, LINK, XRP, XLM, DOGE and AVAX. Four-hour Binance USDT data is distinct from the framework's five daily Kraken USD spot instruments. Effective continuous sample: 2021-09-29 15:59:59.999 UTC to 2026-06-26 23:59:59.999 UTC. Training: 4,941 bars through 2023-12-31; inspected OOS: 5,448 bars from 2024-01-01 03:59:59.999 UTC. Annualisation: 2,190 bars/year; frozen cost: 20bp per gross dollar traded.

Original notebook source, raw caches and exports were recovered outside the initial checkout. Checkpointed hashes identify original inputs. Both net-return series reconcile to the frozen original export within about 1e-16; batched trial accounting preserves the original 55-step self-financing solve and matches within about 4.5e-16. This resolves the earlier missing-input blocker locally. Independent reruns still require the original unredistributed inputs.

Original cleaning retained 2,103 daily and 10,389 four-hour observations. Four-hour original inputs had 3,810 missing rows and six irregular intervals; 2,230 observations were discarded before the continuous suffix. Daily original inputs had 628 missing rows; two observations preceded its continuous suffix. The separate five-asset daily data audit cannot be used to claim this study had no missing data.

Close-price execution, signed positions, excluded short financing and extra spread/slippage remain economic limitations. Correct arithmetic does not establish realistic Kraken fills or borrowing feasibility.

## Generated results

| Diagnostic | Generated result | Implication |
| --- | --- | --- |
| Reconciled OOS strategy | Sharpe 0.3717; return +12.0116%; drawdown -30.0367% | Positive observed return with substantial risk and uncertainty. |
| Unfiltered OOS baseline | Sharpe -1.0177 | Filter improves on a weak baseline. |
| Training strategy | Sharpe -0.8622; return -27.0338%; drawdown -39.7664% | Performance is unstable across splits. |
| Paired bootstrap Sharpe difference | Estimate 1.3894; every 95% interval positive; one-sided approximate p 0.0035..0.008 | Relative evidence under declared resampling assumptions. |
| Absolute bootstrap Sharpe | Every 95% interval crosses zero | Positive standalone edge is not established. |
| Run-length placebo | Sharpe p 0.027; balanced p 0.02887 (761 accepted) | Suggestive historical timing evidence; not selection-adjusted. |
| Circular-shift placebo | Sharpe p 0.018; balanced p 0.02429 (740 accepted) | Same interpretation under an alternative null. |
| Net BTC HAC alpha | Annual arithmetic intercept about 6.04%; p 0.566..0.601 | No significant BTC-adjusted net alpha. |
| Concentration | Largest OOS year contributes 95.04% of positive yearly log gains; largest quarter 59.86% of positive quarterly log gains | Gains are concentrated; these percentages are not shares of net total return. |
| Corrected DSR | Highly sensitive to trial count/dispersion assumptions | Incomplete correlated registry prevents a unique selection-adjusted confidence claim. |

Bootstrap uses 1,999 paired moving-block draws per specification:

| Block length (4H bars) | Strategy Sharpe 95% interval | Paired Sharpe-difference 95% interval |
| --- | --- | --- |
| 12 | [-1.0813, 1.6090] | [0.2972, 2.4935] |
| 36 | [-1.0024, 1.6245] | [0.2747, 2.4252] |
| 72 | [-1.1516, 1.6728] | [0.2203, 2.3982] |
| 144 | [-1.4246, 1.6703] | [0.2129, 2.4192] |

Each placebo method uses 999 trials. Conditioning allows exposure within 5% and turnover within 10%; accepted counts reduce Monte Carlo precision. Drawdown probabilities around 0.35..0.43 do not establish drawdown superiority. Full balance and distribution tables are retained.

The recovered initial DSR implementation omitted across-trial Sharpe dispersion. The corrected implementation uses per-bar units consistently and reports N=2/10/25/50/100 under training-registry, inspected-OOS-registry and explicitly assumed IID-normal dispersion. The IID-reference DSR is approximately 0.527/0.159/0.077/0.044/0.025; training-registry dispersion gives about 0.00136 at N=2 and much smaller values afterward. DSR probabilities are not p-values, and choosing the most favourable dispersion is invalid.

HAC lags 12/36/72/144 are all retained. Added tables include OLS-vs-HAC uncertainty, ARCH residual diagnostics and accepted-placebo balance ranges. Serial dependence remains strong; the full-bar ESS heuristic is not proof of IID observations.

## Verification on 2026-10-16

- Existing nine tables passed hash, finite-value, budget and period-reconciliation checks.
- Complete validation rerun used the verified original local inputs and existing sealed null cache, without provider downloads or parameter changes.
- Seven CSVs reproduced byte for byte. The two remaining CSVs preserve every shared field exactly and add diagnostics from the current code; reviewed outputs and their own manifest are preserved in a new directory.
- A separate fresh replay recomputed both original strategies directly from hash-verified raw inputs, bypassing the preparation cache:10,389 observations reconciled with maximum net-return errors below1e-16.
- The implementation suite passed246 tests;71 Python sources compiled. The separate publication suite passed3 tests. Synthetic tests verify contracts and formulas, not cryptocurrency alpha.

## Promotion decision and next research

**Do not promote this historical candidate to live trading.** It misses the architecture's proposed Sharpe >=0.5 and drawdown <=15% criteria, its absolute intervals include zero, and its signed nine-asset Binance construction does not map directly to five-asset Kraken spot execution. These criteria were proposed for the deployment framework, so this is a readiness comparison, not a retroactively preregistered hypothesis test for the recovered study.

Keep this historical result frozen. A Kraken-compatible candidate requires its own explicit causal long-only specification, point-in-time data/cost assumptions, trial registry and prospective acceptance protocol. Freeze those before new observations, retain every trial, and never tune on the inspected OOS. Collect actual unseen and sustained shadow evidence. If evidence fails, reject or register a new hypothesis rather than relabeling the old result as successful.

Live transport, private submitted-order reconciliation, operational drills and explicit approval remain separate gates. A completed statistical program is not trading permission.

## Reproduction and references

See `phase2_input_contract.md`, `phase2_methodology.md`, `phase2_limitations.md` and the reviewed manifest for exact inputs, source hashes, seeds, definitions and assumptions. Derived CSV returns/alpha are fractions, not percentages. Annual alpha is arithmetic, not compounded wealth. No raw dataset or credential is redistributed.
