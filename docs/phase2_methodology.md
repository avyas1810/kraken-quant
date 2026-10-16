# Phase 2 statistical validation methodology

This layer audits a frozen historical study; it does not select a strategy or
replace Phase 1 accounting. The described nine-asset study was absent from the
initial repository inventory but a broader local search located its notebooks,
frozen results and raw-data cache outside the repository. The recovered notebook
source and manifests are preserved with provenance under `research/phase1_frozen`;
historical reconstruction must verify their source and raw-data hashes before
inference. The statistical report contains generated historical findings. All
worked examples below are synthetic unless explicitly identified as recovered
study diagnostics. The separate daily five-asset research configuration and its
locked test period remain unchanged.

## Frozen inputs and common definitions

The historical study requires BTC, ETH, BNB, ADA, LINK, XRP, XLM, DOGE and AVAX;
four-hour, UTC, bar-end return timestamps; exact self-financing accounting;
drift-aware turnover; and costs of 0.002 per gross dollar traded. Input files,
accounting source, sample boundaries and provenance must be supplied with verified
SHA-256 hashes. Existing OOS observations are already inspected, not pristine.
No new parameter tuning, data substitution or cost-model changes are permitted.

Returns, BTC observations, activity, exposure and turnover must have exactly
aligned, unique, ordered, regular four-hour timestamps. Missing, nonfinite or
insolvent simple returns (at or below -100%) fail validation rather than being
filled or silently removed. The integration runner also reconciles frozen engine
outputs before conducting inference.

For simple net return r_t and executed gross dollar amount A_t, costs are charged
in execution currency according to the frozen engine:

\[
C_t=0.002A_t,\quad W_0=1,\quad W_t=W_{t-1}(1+r_t).
\]

Here C_t is the monetary transaction cost and W is compounded wealth. The
accounting engine, not this statistical layer, defines drift-aware turnover u_t,
capital normalisation and fee attribution. Exact self-financing execution can
interact with intrabar returns and position drift; independently simulated gross
and net portfolios can follow different capital trajectories. Therefore the
integration does not impose an unconditional identity between gross-minus-net
return and 0.002 times turnover.

For illustration only, $500 of gross trading incurs a $1 fee. Relative to unchanged
$1,000 capital this immediate fee contribution is 0.001 of capital; subtracting
that contribution from a 0.003 return gives 0.002 only under this explicitly
simplified attribution convention. It is not a replacement for the frozen
accounting engine's return calculation.

For T returns, mean m, sample standard deviation s and P=365×6=2190 bars/year:

\[
m=T^{-1}\sum_t r_t,\quad
s^2=(T-1)^{-1}\sum_t(r_t-m)^2,\quad
S=\sqrt P\,m/s,\quad R=W_T-1,
\]
\[
D=\min_t\left(W_t/\max_{0\le j\le t}W_j-1\right).
\]

S is the conventionally annualised Sharpe with zero risk-free return; R is
compounded cumulative return; D is nonpositive maximum drawdown including initial
wealth. The square-root scaling is a reporting convention, not evidence that
four-hour returns are independent. Mean exposure is the average supplied gross
exposure; total turnover is the sum of u_t. Wealth 1, 1.10, 0.99 gives R=-1% and
D=-10%. Constant-return Sharpe is undefined; descriptive tables carry an explicit
status, while inferential computations reject undefined inputs.

## A. Probabilistic and deflated Sharpe

The question is whether observed Sharpe exceeds a benchmark after considering
sample size, nonnormality and research selection. All Sharpe quantities inside
the calculation are **per four-hour observation**, never a mixture of annualised
and per-period values. Define q=m/s, benchmark q₀, skewness a₃ and raw kurtosis a₄
(Gaussian raw kurtosis equals 3):

\[
\operatorname{PSR}(q_0)=\Phi\left(
\frac{(q-q_0)\sqrt{T-1}}
{\sqrt{1-a_3q+(a_4-1)q^2/4}}\right).
\]

Φ is the standard-normal cumulative distribution. Skewness and kurtosis are
population central-moment ratios of the observed sample; q uses sample standard
deviation. Their finite-sample estimator conventions are explicit. Inconsistent
moments, nonpositive variance or nonfinite inputs fail. A completed-square
variance calculation reduces cancellation, and a stable normal CDF avoids
unstable probability arithmetic.

For N effective independent trials, explicit across-trial per-period Sharpe
standard deviation σ_q and Euler's constant γ≈0.577215665, the benchmark is:

\[
q_0(N)=\sigma_q\{(1-\gamma)\Phi^{-1}(1-1/N)
+\gamma\Phi^{-1}(1-1/(Ne))\},\quad N>1.
\]

The default null mean is zero; N=1 returns that null mean directly. DSR is PSR
evaluated at this selection-adjusted benchmark. The expected-maximum expression
is a Gaussian independent-trial approximation, not an exact maximum formula.
These definitions follow the [authors' DSR paper](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).

For example, T=1001, q=0.08, q₀=0, a₃=0 and a₄=3 give PSR≈0.994.
That probability is a model-based confidence diagnostic, not a conventional
null-tail p-value, proof of profitability or Bayesian posterior without a prior.
Increasing the selection benchmark reduces this probability.

Sensitivity uses N=2,10,25,50,100. The actual independent research-trial count is
unknown, and correlations among tried models matter. σ_q must have provenance
from the frozen trial record or an explicitly justified assumption; it is not
silently estimated from a single selected strategy. Four-hour serial dependence
and unstable high moments can invalidate the IID PSR approximation, so DSR never
overrides the dependence-aware bootstrap or the absent untouched/live evidence.

The recovered Phase 2 `dsr_approx` function omitted σ_q, effectively fixing it at
1.0 in per-bar Sharpe units. Its recorded annual null benchmarks of roughly
24.32–118.43 are therefore not valid selection benchmarks for the stated study.
The correction restores the multiplier and makes its provenance explicit.

The recovered 18-column Phase 1 net-return export permits an empirical
training-only dispersion estimate (4,941 bars): σ_q≈0.092723. The corresponding
already-inspected OOS dispersion (5,448 bars) is a separate sensitivity, not
training information. These correlated strategies are an incomplete registry;
heterogeneous, severely losing variants contribute substantial cross-strategy
dispersion. Their sample dispersion is not necessarily the Gaussian independent
null sampling dispersion assumed by the expected-maximum approximation. Neither
sample identifies the true independent trial count or a uniquely valid DSR.

For triangulation, an explicitly assumed normal-IID null reference uses
σ_q=1/√(T−1), approximately 0.013549 for the 5,448-bar OOS sample. It is labeled
`iid_gaussian_null_reference_not_empirical_registry`; it is not an empirical trial
registry estimate and does not correct serial dependence. Its DSR probabilities
are roughly 0.527,0.159,0.077,0.044,0.025 across the five trial counts, versus about
0.00136 at N=2 and much smaller values thereafter using training registry
dispersion. This sensitivity is evidence of selection-model uncertainty, not a
reason to choose the more favourable assumption. Population-moment and original
bias-corrected-moment versions are retained; their differences are negligible
relative to dispersion uncertainty. DSR probabilities must not be interpreted as
frequentist p-values or standalone evidence for live deployment.

## B. Regime-filter placebo

The question is whether the low-correlation filter adds information beyond
arbitrary reduction in trading activity. The original momentum signals, prices,
costs and execution alignment remain fixed inside the supplied Phase 1 engine.
Only the binary active/inactive filter is randomised and the engine is rerun.

The primary null permutes active-run lengths among active runs and inactive-run
lengths among inactive runs, retaining the alternating state sequence and endpoint
states. It preserves active-bar count and both run-length multisets exactly.
A separate circular-shift benchmark rolls the observed mask by a nonzero offset;
it preserves activity but wraps the boundary. Both use fixed seeds and 999 trials
per method by default. Duplicate masks are retained and diagnosed rather than
silently treated as distinct evidence.

Each trial reports net Sharpe, return, drawdown, turnover and mean gross exposure.
Unconditional results and balance-conditioned results are both retained. Relative
balance gaps for exposure E and turnover U are:

\[
\delta_E=(E_b-E_{obs})/E_{obs},\quad
\delta_U=(U_b-U_{obs})/U_{obs}.
\]

The predeclared acceptance criteria are |δ_E|≤5% and |δ_U|≤10%. These tolerances
were chosen prospectively without the unavailable study data, not tuned to its
results. For example, observed E=0.40 and trial E=0.42 give a 5% gap; observed
U=10 and trial U=11.2 give a 12% gap, so that trial fails the turnover condition.
Exact frequency/run matching does not guarantee exposure or turnover matching.
Acceptance counts and gap ranges/means quantify the remaining imbalance.

For higher-is-better statistic H and B selected trials:

\[
p=(1+\sum_{b=1}^{B}\mathbf1[H_b\ge H_{obs}])/(B+1).
\]

Drawdown uses its signed, nonpositive value, so less negative is better. With 29
exceedances among 999 trials, p=30/1000=0.030. Zero accepted draws yield no
probability and an explicit status; low counts warn of limited Monte Carlo
precision. An all-identical-to-observed null is explicitly degenerate.

This is retrospective conditional randomisation, not a point-in-time trading
rule: constructing the null uses the full historical filter. Signals inside the
frozen engine must remain causal. The inference assumes plausible exchangeability
of episode locations conditional on the preserved structure; market nonstationarity
can violate it. Conditioning and multiple reported metrics also limit inference.

## C. Paired moving-block bootstrap

Absolute uncertainty asks whether the selected strategy itself has positive mean
or Sharpe. Relative uncertainty asks whether it improves on baseline momentum.
These are different questions: a reliable improvement over a losing baseline
need not imply a profitable standalone strategy.

The implementation samples overlapping, noncircular contiguous blocks of length
L=12,36,72,144 four-hour bars (2,6,12,24 days). It concatenates ceil(T/L) blocks,
then truncates to T observations. Identical indices select both strategy and
baseline returns, preserving contemporaneous dependence. Each length gets 1,999
replications and a deterministic seed derived from the master seed and L.

For each draw, compute S and arithmetic annual mean Pm for both series, plus
paired differences ΔS=S_strategy−S_baseline and Δm=P(m_strategy−m_baseline).
95% percentile intervals are the 2.5th and 97.5th percentiles. An approximate
one-sided centered-null test uses H_b*=H_b−H_obs and counts H_b*≥H_obs with the
same plus-one correction above. This is a centered bootstrap approximation,
particularly for the nonlinear Sharpe difference, not an exact permutation test.

For example, a paired mean-difference interval [-0.02,0.06] in annual arithmetic
return units includes zero and does not establish positive relative mean return.
A strategy's absolute interval can include zero while its paired difference
interval excludes zero because paired resampling preserves shared market shocks.

The reported approximate mean effective sample size is:

\[
T_{eff}=\min\{T,T/(1+2\sum_{k=1}^{K}\rho_k)\}.
\]

ρ_k uses full-sample centered variance in its denominator; summation stops at the
first nonpositive lag or L. For T=1000 and included autocorrelations 0.20,0.10,
T_eff=625. This heuristic is not a Sharpe-specific degrees-of-freedom correction.
Block counts, endpoints, long-range dependence, regime changes and the finite
sample constrain accuracy. No block length is selected because it gives the
most favourable inference.

## D. BTC alpha/beta robustness

OLS estimates r_strategy,t=α+βr_BTC,t+ε_t separately for gross and net returns.
α is per-bar arithmetic intercept, β contemporaneous BTC sensitivity, and
R²=1−Σ ε²/Σ(r_strategy−mean)² is explained variance. All lag specifications use
the identical aligned sample.

For design matrix X with columns 1 and BTC return, residual score z_t=X_t ε_t,
and lag limit L, Bartlett HAC covariance is:

\[
V=\frac{T}{T-2}(X'X)^{-1}
\left[\sum_t z_tz_t'+\sum_{k=1}^{L}(1-k/(L+1))
\sum_{t=k+1}^{T}(z_tz_{t-k}'+z_{t-k}z_t')\right](X'X)^{-1}.
\]

Standard errors are square roots of its diagonal. L=12,36,72,144 are reported,
using finite-sample correction and Student-t inference with T−2 residual degrees
of freedom. See the [statsmodels HAC interface](https://www.statsmodels.org/dev/generated/statsmodels.regression.linear_model.OLSResults.get_robustcov_results.html).
Residual Ljung–Box and Breusch–Pagan p-values flag autocorrelation and
heteroskedasticity descriptively; they are not independent confirmation of alpha.

Annual arithmetic alpha is Pα and its interval is [Pα_low,Pα_high]. If per-bar
α=0.00001 with CI [-0.00002,0.00004], annual alpha is 2.19%, CI [-4.38%,8.76%].
This includes zero and does not establish positive alpha. It is not the compounded
wealth expression (1+α)^P−1. Near-zero BTC beta excludes neither nonlinear BTC
exposure nor altcoin, liquidity, volatility or other crypto risk factors. Short
samples, extreme lag/sample ratios and structural breaks can undermine inference.

## E. Stability and research integrity

Train and already-inspected OOS boundaries come exclusively from the frozen
historical manifest. Inclusive endpoints must exist and splits must not overlap.
Within each split, report full-period, calendar-year and calendar-quarter return,
Sharpe, drawdown, turnover, exposure and activity fraction. Different period
lengths and activity frequencies must be considered when comparing results.

Period reconciliation uses additive log wealth:

\[
\log W_T=\sum_t\log(1+r_t)=\sum_{periods}\log(1+R_{period}).
\]

For quarterly returns +10% then −10%, full return is −1%, not zero; their log
returns sum to log(0.99). The module checks this identity within 1e-12 tolerance.
Concentration reports the best-period log gain and its share of all positive
period log gains. With positive gains 0.06 and 0.02, that share is 75%; it is not
75% of net total gain when other periods lose money. No-positive-period cases
carry an explicit undefined status.

Sensitivity sets the largest 1,5,10 positive individual returns to zero, retaining
timestamps; it records how many were actually available. For returns +10%,−5%,
+2%, original wealth is 1.0659; zeroing +10% gives 0.969. This illustrates
dependence on outliers, not a tradable counterfactual: positions, costs and later
execution are not recalculated. It must not become a strategy-selection rule.

Coverage discrepancies, sample changes and data hashes must be disclosed before
interpreting train/OOS differences. OOS reinspection cannot restore pristine
holdout status. Any conclusion A/B/C must be supported by the actual frozen
study artifacts and generated historical results. Multiple-testing concerns,
selection history, omitted risks and sustained prospective/live evidence remain
separate requirements even if individual diagnostics are favourable.
