# Signed research and guarded Kraken boundary

This milestone extends the architecture with **new modules**, leaving the verified
long-only `backtest.py`, metrics, configuration, locked reader and all TWSQ files
unchanged. No exchange client, key loading, funding endpoint or live transport is
implemented. Tests and examples below use synthetic prices only; there are no
historical strategy findings and the test period remains locked.

## Files and interface

- `strategy.py`: `Instrument`, per-venue `VenueHistory`, `StrategyContext`, and
  `StrategyPipeline`. Implement `target_weights(context)` once. It returns a mapping
  from instrument to signed target weight, or `None` to hold quantities. Omitted
  instruments in a mapping target zero. Positive means long, negative short,
  zero flat. Research/shadow/production use this exact strategy/feature interface.
- `research_execution.py`: signed cash/position accounting, next-bar execution,
  trade ledger, financing and benchmarks, reusing the existing metric functions.
- `experiment.py`: create-once parameter registration and train/validation runs,
  reproducibility manifests and a paper-ready benchmark/metric CSV table.
- `risk.py`: instrument-bound Decimal rules, timestamped quotes/account snapshots,
  order intents, transactionally persistent reservations and a persistent kill switch.
- `boundary.py`: signed shadow decisions (or long-only Kraken rehearsal) and a
  disabled-by-default production evaluator/reservation adapter. `submit` never
  submits: shadow always refuses, and even explicitly enabled production raises
  `NotImplementedError`. Enablement here authorizes dry validation only.

## Data and arbitrary features

Each source is identified by venue, actual symbol, base, quote and market type.
Daily frames also carry `timestamp` and `data_type=ohlcv`; quotes and ledger fills
carry timestamps/type and their instrument identity. Identical symbols on different
venues have distinct keys/readers. The locked research universe is still imported
from `examples/research_config.py`, and the old restricted reader enforces dates.
Source base must match the research asset. USD and USDT remain different: simultaneous
mixed-quote execution is rejected until an explicit FX model exists. No universe
expansion or derivatives data adapter is silently introduced.

Snapshot validation/cleaning is unchanged: unique complete daily UTC coverage,
finite positive prices, nonnegative volume and valid OHLC. No gap filling or
conflicting-row deletion. The engine alone reads execution bars for the authorized
split; strategy and feature builders receive completed-history copies up to the
current decision time. Future bars are rejected by the common pipeline.

A feature builder takes `(historical_data, timestamp)` and may return **any Python
object**. Graphs, networks, category objects, morphisms/functors or tensors do not
require infrastructure changes or optional libraries. This is extensibility, not
a claim that arbitrary transformations are mathematically valid or profitable.
Feature fitting must use training only; precomputed/learned artifacts need input
hashes and feature identities. Arbitrary Python can deliberately read external
data, and cannot be made leakage-safe simply by an interface. The runner does not
automatically fit models, tune parameters or authorize test access.

## Signed accounting and assumptions

At completed close C_i,t with funded equity E_t, target quantities are
q_i* = w_i E_t / C_i,t. The quantities are decided before seeing the next open;
execution uses O_i,t+1. Fill price is p = O [1 + d (s/2 + l)], where d is +1 for a
buy and -1 for a sell, s is the full spread fraction and l adverse slippage.
Fees are F = f |delta_q| p. Cash changes by -(delta_q p + F), quantities by delta_q,
and equity is cash + sum(q_i C_i). Negative q represents a simulated borrowed
asset liability; short-sale proceeds are **not profit**.

Reduce existing positions first, including covering shorts, then add exposure.
Additions scale together to available funds and G <= g E, where gross notional
G = sum(|q_i| O_i) and g is the explicit research gross budget (default 1).
This prevents recycling short-sale proceeds into unbounded exposure. Cash
borrowing is disabled by default; leveraged cash-negative scenarios require
`ResearchPolicy(max_gross=..., allow_cash_borrowing=True)`. They never cross into
Kraken production. Infeasible reductions, insolvency or mark-to-market margin
breaches stop the run; there is no invented liquidation/insurance payoff.

Daily short financing is b_s sum(max(-q_i,0) C_i); cash financing is
b_c max(-cash,0). Rates b_s/b_c are daily fractions and are explicitly recorded,
as are fees/spread/slippage. Zero defaults are disclosed scenario assumptions,
not real borrowing availability or Kraken fee estimates.

Hand example: $1,000, close100, weight -0.5 sets q=-5. Next open100, spread2%,
slippage1%, fee1% gives sell98 and fee4.90: cash1485.10, liability450 at close90,
equity1035.10. Cover5 at next open80 fills81.60, fee4.08, ending cash1073.02.
The ledger counts two executed trades; spread costs9, slippage9, fees8.98. These
are accounting checks, not evidence of returns from a cryptocurrency strategy.

The buy-and-hold comparator buys once after the first scored close under identical
timing/cost/funding assumptions, then holds. Equal weights are the default. Final
holdings are marked, not liquidated. Metric definitions/sample examples and
undefined-ratio behavior remain those in `backtest_core.md`: net returns,
annualized volatility/Sharpe/Sortino, maximum daily drawdown, two-sided turnover,
executed-order count and fees; new results add spread and financing cost totals.
Daily full fills, fractional amounts, constant costs, simplified collateral,
same-boundary execution and no true order-book/borrow-market simulation remain
research assumptions. Short results cannot justify a Kraken spot deployment.

## Experiments and paper outputs

Registry records hypothesis, strategy, JSON parameters, feature identity, code
revision, seed and external input hashes before running. A hypothesis defaults to
20 registered trials. Registry entries are immutable hash-addressed files; duplicate
registration is idempotent. The registry is single-writer (not a distributed tuner).

Runner accepts exactly training and validation windows within the imported splits.
It rejects a test request before constructing strategies/features or reading bars.
Each split gets a fresh strategy/feature instance, factory parameters and seed;
factories must use the supplied seed and preregistered training artifacts.
The manifest includes source snapshot hashes, factory source hashes, engine source
hashes, dependency/Python versions, windows, costs, collateral policy and funding.
Factory code must be inspectable Python source. External/global state and learned
weights require explicit input hashes; source hashing does not capture them magically.

The paper CSV has train/validation metrics, benchmark metrics, parameter identity,
dates and benchmark-relative return. Undefined ratios are null in JSON and blank
in CSV, never promoted to infinite Sharpe. There are no test rows. Run IDs and CSV
hashes are deterministic for identical inputs; files are create-once and table
mutation before publication is rejected. Registry/artifact writes are not yet
crash-atomic publications. Per-asset analysis, sensitivity grids, confidence
intervals, charts and automated model fitting are subsequent reporting work.

## Kraken dry approval rules

Only locked Kraken USD **spot** instruments, long-only weights with sum <=1,
leverage exactly1 and no borrowing/derivatives pass. Sells cannot exceed available
base holdings after pending sells; pending buys cannot fund sells. Quantity rounds
down to its increment, buy limit down/sell limit up to the tick, then quantity and
notional minimums are checked. Rules are instrument-bound, online and no older than
24h; quote ages must be between0 and5 seconds and account age <=60 seconds. Daily
loss baseline must belong to the current UTC day. Current instruments must be
retrieved and normalized by a future adapter, using the official
[Kraken instrument metadata](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs).
No example rule/fee values are asserted to be live exchange values.

Hard caps: $20 execution notional/order, $40/asset and $100 total exposure. Cash
reservations include fees. Exposure includes pending buys and worst-case limit
premiums; pending sells do not reduce exposure. Daily loss uses current bid-marked
equity, including unrealized losses, and reserves adverse execution/fee costs of
pending/new orders; reaching $2 stops approval. External cash flows are not
supported: transfers/manual account activity require reconciliation/halt.

SQLite `BEGIN IMMEDIATE` validates and reserves each order atomically with unique
order IDs and decision keys. Multiple gateway connections share reservations.
Identity remains after terminal release, preventing replay across restarts. The
kill switch persists and is never automatically reset. `release_after_reconciliation`
is an explicit trusted operation only after terminal order/fills and balances
have been reconciled; it is not cancellation and must not release UNKNOWN orders.
This milestone does not implement exchange reconciliation itself.

## Remaining before the first tiny live order

Fresh Kraken instrument/fee/account metadata, completed Kraken bars and quote
capture; separate .env read-only/trading keys with no funding permissions; verified
key permissions; a dedicated account; actual broker/Alpha lifecycle integration;
order/trade/balance reconciliation, UNKNOWN-submission recovery and trade-ID
deduplication; supervised startup halted until reconciliation; public/private stream
recovery; nonce ownership, exchange dead-man timer, structured logging/alerts and
kill/restart drills; a reviewed live transport with validation-only tests; frozen
research gates, required sustained shadow evidence and explicit user approval.
Use a persistent gateway database for that lifecycle; `:memory:` is for unit tests.
Existing architecture promotion gates apply; this milestone does not satisfy them.

## Verification

Only relevant synthetic suites run:
`python -m pytest -q tests/test_production_milestone.py tests/test_backtest_core.py tests/test_research_slice.py`.
Compile new modules/tests and inspect/check the staged diff before committing.
Tests cover long/short hand accounting, costs/financing, metric fixtures, causal
custom features, venue fields, next-bar fills, locked test access, reproducibility,
production short/leverage rejection, precision/minimums, risk caps, freshness,
pending reservations, duplicates/restart, kill switch, shadow non-submission and
enabled-production absence of transport. No real strategy performance is inspected.
