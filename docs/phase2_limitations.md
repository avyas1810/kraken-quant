# Phase 2 limitations and unresolved prerequisites

- Original data and notebooks were recovered outside the repository and verified
  against checkpointed hashes. Raw datasets are deliberately not redistributed;
  independent historical reruns require those same original local inputs.
- Phase 1 full net returns reconcile within approximately1e-16; batch trial
  accounting matches the observed original within4.5e-16. This proves numerical
  consistency, not validity of close-price fills, available short borrowing,
  financing assumptions or real exchange execution. Close execution is idealised;
  short financing, funding rates and additional spread/slippage are excluded.
- The true effective number of tried strategies and across-trial Sharpe dispersion
  are unknown. DSR sensitivity2/10/25/50/100 does not repair an incomplete trial
  registry. PSR/DSR's IID and Gaussian trial-max approximations can be unreliable
  for dependent, heavy-tailed four-hour returns.
- Moving-block bootstrap assumes local dependence is captured by chosen blocks.
  Long memory, structural breaks, short samples and overlapping signals can defeat
  coverage. Effective sample sizes are descriptive mean heuristics. Centered
  nonlinear Sharpe-difference tests are approximate; block-length sensitivity is
  reported rather than selecting a favourable specification.
- Placebo episode permutations condition on the fully observed filter. They are
  historical randomisations, not implementable strategies. Exchangeability is
  questionable across market regimes. Exact activity/run-length matching does not
  imply identical capital exposure, turnover or costs; conditional tolerances and
  remaining imbalances are reported. Too few accepted/unique masks weaken inference.
- HAC lag sensitivity cannot cure endogenous factors, omitted crypto risks,
  nonlinear beta, unstable parameters or short-sample uncertainty. Annual alpha is
  an arithmetic intercept transform, not compounded wealth or proof of arbitrage.
- Period/outlier diagnostics use hindsight and cannot be turned into trading rules
  without new research. Zeroing positive bars does not replay positions or costs.
  Different coverage and varying activity can explain apparent train/OOS changes.
- Existing OOS has already been inspected. Repeated tests, reports, methods and
  parameter histories raise multiple-testing concerns. No pristine holdout claim
  or profitability conclusion follows from favourable diagnostics.
- Hashes prove identity, not causal correctness or malicious-code safety. The
  original replay adapter must be reviewed. File publication is create-once but
  not atomic across a complete report directory; verify all output hashes.
- Generated results support B as exploratory relative improvement, not positive
  standalone alpha. Absolute Sharpe intervals cross zero at every block length;
  net BTC alpha is insignificant, training is negative and gains are concentrated.
  Placebo probabilities are unadjusted for all historical research selections.
  Effective independent trial count is unknown and may exceed the reported grid.
- The initial-positive-autocorrelation ESS heuristic returns the full5448 bars
  here because the first correlation is nonpositive. That does not establish IID:
  residual Ljung-Box tests strongly reject no serial correlation. Block counts
  fall to38 at length144, so long-block uncertainty remains substantial.
