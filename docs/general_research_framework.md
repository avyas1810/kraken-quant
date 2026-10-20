# General research workbench

`notebooks/quant_kraken_research.ipynb` is the interface. Python modules perform
retrieval, cleaning, aggregation, fitting, sizing, simulation, reporting and
recommendations. The daily frozen-study path remains separate and compatible.
General experiments support arbitrary unique asset counts; they do not redefine
the original `examples/research_config.py` universe. The notebook defaults import
that central configuration. Neither path releases the final holdout.

## Implemented scope

- Public Coinbase Exchange crypto spot hourly history, paginated and cached.
  Product/base/quote metadata is checked. Unsupported products, missing history,
  inconsistent OHLCV, conflicting duplicates and unfinished bars raise errors.
  Identical overlapping observations are removed and counted. No prices filled.
- UTC 24/7 resampling to 4h, 8h, 16h, 24h, Monday weeks and calendar months.
  Bins use exclusive ends and require all source hours. 16h is epoch anchored;
  partial edge bins are omitted and diagnosed. Equities/ETFs, FX sessions,
  futures, corporate actions, token migrations and asynchronous markets require
  additional tested adapters and are explicitly unsupported.
- AUD cash accounting with historical currency reference valuation. The
  Frankfurter v1 adapter retrieves daily references; availability is conservatively
  delayed to the following UTC midnight, with at most seven days of carry.
  These are valuation references, not executable intraday FX quotes. Native AUD
  instruments use identity conversion. Unsupported currencies fail explicitly.
- Arbitrary `Instrument` keys and `Asset` metadata, independent of the original
  five-asset restriction. Asset class, calendar, timezone, multiplier, eligibility
  and shortability are explicit. The implemented spot multiplier is one. Test
  fixtures exercise 1, 3, 10 and 50 assets; provider availability is checked for
  every actual requested product, without claiming fifty real products verified.

Provider contracts: [Coinbase candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)
and [Frankfurter historical references](https://frankfurter.dev/v1/).

## Strategy and fitting contract

Supply a factory `(parameters, seed)` returning an object with
`target_weights(context)`. Return signed strengths in `[-1,1]` or `None` to hold
quantities. All selected assets share one portfolio; cross-sectional strategies
use the same historical mapping as single-asset strategies. Optional
`fit(training_frames)` runs on calibration/training only. Optional feature
factories return callable `(histories, timestamp)` builders, with optional
training-only `fit`. `required_features` requires a supplied builder. A strategy
must verify its own custom feature schema; arbitrary feature objects are allowed.
Rebalancing is configured in completed bars.

The shared `StrategyPipeline` accepts optional explicit bar ends. Legacy callers
retain daily behavior. Strategies/builders receive copies ending at the decision
timestamp, including prior-period warm-up, never future execution prices.
Trusted Python plug-ins can access other Python resources: this is a causal API
contract, not a hostile-code sandbox.

Chronological evaluation splits at configurable `ResearchSpec.train_end`, with
an optional embargo. A training prefix fits the model and Kelly for the scored
training suffix; a fresh strategy and feature builder fit full training for OOS.
Training and OOS both start with independent A$5,000 accounts by default. Bars
straddling the split are excluded. No forward overlapping labels are generated;
custom models with forward labels must purge them inside their own training
implementation. Optional expanding walk-forward folds reject overlapping OOS
windows. Future folds may fit prior folds, never their own evaluation data.
Reusing validation for selection invalidates claims it remains unseen.

## Kelly and risk

Default fractional Kelly is 0.25. The estimator uses training-only AUD return
observations after subtracting explicit round-trip and carry assumptions.
Covariance shrinkage, diagonal regularization, minimum sample size, finite-input
checks and direction checks guard unstable estimates. Covariance and estimates
are frozen within each scored period. Negative long returns caused solely by
costs cannot create profitable short estimates. Insufficient history yields flat
allocations. Both raw estimates and constrained targets are reported.

This conservative unconditional one-bar proxy is not a fitted conditional return
model. Charging a round trip every bar can recommend flat, especially intraday.
Changing its assumptions after viewing OOS is another disclosed trial, not a
cost-free tuning step. The default real-data example is flat under its disclosed
cost scenario; benchmark results and tests exercise nonzero transactions.

Hard position, gross leverage, shortability, margin/cash and eligibility controls
override Kelly. Daily loss and drawdown breaches persistently halt further risk
and schedule flattening at the next available open. Exposure/margin breaches at
marks are recorded. Gaps and intrabar moves can exceed limits; the simulator does
not fabricate fills at a stop price. Mark-based daily-loss checks do not prove
intrabar risk compliance. Signed/margin accounting is a research scenario;
operational borrow availability, real margin rules and short broker execution
are not implemented. Nonpositive equity stops a run as insolvent.

## Cost and accounting contract

Signals size fixed desired units using completed-close equity/prices. At the next
open, reductions execute before additions; additions scale as one batch against
cash, gross leverage and individual exposure. No simultaneous order can reuse
the same capital independently. Initial funding, signed quantities, cash, long
assets, short liabilities and net equity are recorded at each mark.

`CostModel` requires explicit values for all applicable categories. Unknown
commission, spread, slippage, impact, FX, exchange/regulatory costs, or applicable
borrow/margin interest cause rejection. Explicit zero is an assumption, never a
default for an unknown material cost. Funding is explicitly zero for spot;
derivatives are rejected rather than omitting their funding/settlement.

Commission, FX, exchange and regulatory rates apply to absolute reference
notional in AUD. Spread (half the supplied full spread), slippage and constant
impact affect adverse fill prices; they are reported separately but subtracted
only once. Borrow accrues on short opening exposure over actual elapsed bar
time. Cash financing accrues on negative cash using the maximum supplied annual
margin rate across selected instruments, an explicit conservative account-level
proxy. Annual carry uses 365.2425 days. More complex broker financing, stochastic
market impact and executable FX spreads are not claimed. The manifest flags
incomplete historical quote/impact coverage and labels supplied cost scenarios.

Per-asset gross mark PnL less allocated execution/holding costs equals net
attribution. Attribution sums to changes in equity. Cash plus long assets minus
short liabilities equals equity. Average execution basis gives realized plus
unrealized ledger PnL. A separate FIFO closed-quantity record allocates entry,
exit and carry costs; closed net PnL plus open net lot PnL reconciles to total net
PnL. Partial exits and reversals retain the appropriate costs. Metrics use net
equity, not a second independent return calculation.

## Reports and recommendations

Every run reports separate IS/OOS net AUD PnL, return, annualized return,
Sharpe/Sortino, drawdown, Calmar, volatility, net trade win rate/profit factor,
turnover and costs. Closed-trade statistics count FIFO matched quantities;
portfolio net-bar statistics are separately named. Open trade PnL remains in
net equity. Undefined ratios remain NaN. Annualization uses elapsed time and
observed bar counts; monthly bars are not assumed to have fixed length.

Eight dashboard figures show IS/OOS equity and cost-matched passive benchmarks,
drawdown, rolling net Sharpe, weights, cumulative net asset attribution, cost
breakdown, exposures and constrained Kelly targets. Fully funded equal-weight
buy-and-hold uses identical execution costs/timing without Kelly or strategy
stops, and its separate limits are in the manifest.

Recommendations reuse the final completed signal, frozen Kelly estimate,
positions, capital, risk status and estimated costs. They show current and target
weights, delta, direction, timestamp, next eligible open, following bar end,
costs, cash, gross/net exposure and eligibility. For the only implemented calendar,
the completed bar end is the next bar open. The final bar always receives a fresh
signal preview; a longer rebalance schedule delays the reported execution boundary
without changing historical fills. Recompute when that boundary arrives. These historical targets are not
current executable allocations; fills are not guaranteed.

`DryRunExecution.submit_target_weights` is a tested adapter contract, not a live
broker. It demands independently reconciled fresh AUD account/quotes/rules,
checks eligibility, risk approval, lot/minimum limits and funded buying power,
then returns rounded order intents. Long/short research allocations cannot be
silently sent to a long-only spot account. All modes except `dry-run` raise.
Existing `ProductionAdapter` remains disabled and has no live order transport.

## Reproduction and verification

Install `requirements-ci.txt`, `requirements-research.txt` and
`requirements-notebook.txt`. Execute real data with:

```powershell
python examples/execute_research_notebook.py
python -m pytest -q tests --basetemp=.notebook-runtime/pytest-all
```

Explicit offline clean-kernel integration:

```powershell
python examples/execute_research_notebook.py --fixture
```

The fixture runner saves a separate ignored notebook; it never replaces the
genuine-data default or silently falls back after retrieval failures. Runtime
Jupyter, plotting and temporary directories are repository-local. Real provider
requests are independently verified during implementation, not required by CI.

Manifests contain asset metadata, data provenance/version hashes, processed-panel
hashes, timeframe/date/split windows, strategy factory/class code, feature code,
parameters, cost assumptions, Kelly/risk settings, seed, environment, Git revision,
engine hashes and net performance. Existing `ParameterRegistry` records trials.
Immutable exports include every strategy/benchmark ledger, fills, FIFO trades,
costs, attribution, signals, weights, recommendations and report with content
hashes. Rewriting different bytes to an existing run raises an error. Neither
successful execution nor synthetic tests establish strategy profitability.
