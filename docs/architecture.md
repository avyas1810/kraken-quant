# Research-to-Kraken architecture

## Scope and current slice

One strategy moves through research, backtest, validation, shadow, tiny Kraken
live execution, monitoring and reconciliation. Reuse TWSQ's Alpha and broker/order
contracts through new adapters; leave stable TWSQ files unchanged. Start with
daily, long-only spot strategies, one active strategy and one execution process.
No strategy performance, test release or live submission is implemented here.

`examples/research_config.py` remains the sole universe/date authority. New
`kraken_quant.config` imports it into frozen, validated settings. `data` accepts
caller-supplied historical bars, validates daily UTC OHLCV, records canonical JSON
snapshots with SHA-256 hashes and provides split/decision-time restricted copies.
`features` contains one causal momentum calculation for synthetic verification.
`synthetic` subclasses the actual TWSQ Alpha and injects an offline broker that
cannot submit orders. Its legacy TWSQ import attempts a settings lookup and can
create directories: the subprocess test isolates TWSQROOT and blocks credential
reads before import. The data/config package itself imports no TWSQ APIs.

Snapshots contain source venue and source instrument separately from research
symbol: audited Binance USDT history is not Kraken USD history. Naive source
timestamps require explicit acknowledgement that they already represent UTC.
Validation rejects incomplete bars, wrong boundaries, missing/duplicate dates,
missing/nonfinite values, nonpositive prices, negative volume and invalid OHLC.
Sorting and float64 normalization are the only cleaning transformations. No gap
filling, conflicting-row deletion, price adjustment or strategy evaluation occurs.

Snapshot manifests record schema version, source, source symbol, daily frequency,
UTC bounds, rows and hash. The hash includes canonical normalized bars, provenance
and explicit validation time; identical inputs/metadata produce identical hashes.
Changing validation time intentionally creates a different artifact. Snapshots
hold immutable bytes and frozen manifests; writes use exclusive creation and
reads check hashes and validation. Hashes detect corruption, not malicious
re-signing; filesystem permissions and durable atomic publication are future work.

Training/validation requests cannot cross their split boundaries. Validation
warm-up may use earlier training bars; warm-up is context, not authorization to
fit models or score a prior split. All returned bars must have completed by the
decision time. Test access is always rejected; no unlock flag exists. Full-range
snapshots can be audited/stored, but test observations cannot be requested through
the reader. This is an application boundary, not a security sandbox against a
developer deliberately reading snapshot files/private attributes.

## Event flow and planned responsibilities

Source/cache -> immutable snapshot -> validation -> authorized history -> completed
bar features -> Alpha targets -> order intents -> instrument/risk checks -> durable
submission record -> execution adapter -> fills/fees -> ledger -> reconciliation.

Future modules: experiment (hypotheses/trials/freeze records), runner (common clock
and lifecycle), broker (TWSQ facade), execution (backtest/shadow/Kraken adapters),
costs, instruments, risk, ledger (single-writer SQLite), reconciliation, reporting,
monitoring and CLI. Shadow observes Kraken but cannot submit orders. Strategies
support momentum, reversal, volume and causal regime conditioning through the
same Alpha methods in every mode. No message broker or distributed service is
needed for the minimum viable system.

Historical signals execute at the next executable event, never the close that
generated the signal. Version fees by pair/account and record historical fee
assumptions. Model spread, adverse slippage, latency and partial fills; marketable
limits incur taker costs. Reports use funded equity and identical-cost buy-and-hold
benchmarks, returns/volatility, Sharpe/Sortino, drawdown, turnover, order/fill/trade
counts, fees, slippage, sensitivity and cross-asset results. Store code/dependency
versions, data hashes, seeds and parameters with reproducible tables/charts.

## Required production controls

- Train-only fitting and chronological folds; small preregistered parameter grids;
  complete trial registry; purge overlapping labels. Validation is selection data.
- Test remains locked until a hashed strategy, parameters, feature fitting, costs,
  benchmarks and acceptance protocol are frozen. No test-driven retuning.
- Separate .env read-only and trading keys; no deposit/withdrawal permissions;
  only the live process loads trading credentials. Live requires user approval.
- Decimal/verified exchange precision; quantity and cost minimums; market status;
  funded sizing, fee/fund reservations, per-asset/order/total exposure limits and
  daily loss including unrealized P&L. No leverage or borrowing initially.
- Reject quotes older than 5 seconds, missing latest daily bars after a 5-minute
  allowance, or clock skew over 1 second. These are initial settings to verify.
- Persist unique decision/intent IDs before submission; deduplicate trade IDs;
  on ambiguous timeout reconcile before retrying; unresolved state freezes orders.
- Persistent kill switch blocks submissions and requests cancellation, without
  automatically liquidating holdings. Exchange cancellation timer: refresh every
  20 seconds with a 60-second timeout, only in a dedicated account.
- Reconcile orders, trades, balances, fees and reservations at startup/reconnect,
  after ambiguous requests and at least every 60 seconds. Freeze discrepancies.
- Single writer/process lock/nonce owner; restart halted until reconciliation;
  structured redacted logs, health checks, alerts and recovery drills.

Legacy TWSQ backtest timing/full bar-touch fills/default fees, credential YAML
factory, live order transport/reconnect handling, naive clocks, mutable caches and
CSV/pickle recovery are not production-approved. Analytics currently is scaffolding.
Keep baseline code, add guarded adapters, and verify their contracts independently.

## Proposed promotion gates (freeze before evaluation)

1. Research -> validation: data/causality tests pass; at most 20 preregistered
   configurations per hypothesis; all chronological training trials recorded.
2. Validation -> frozen candidate: net Sharpe >= 0.5, drawdown <= 15%, positive
   net results on >= 3/5 assets and positive benchmark-relative return; nonnegative
   return with doubled spread/slippage; two neighboring parameters stay positive;
   >= 30 completed round trips, otherwise collect more evidence.
3. Frozen candidate -> shadow: independent reproducibility and fault tests pass.
   Test may stay locked; shadow alone permits no profitability claim or live gate.
4. Tiny-live research eligibility requires a separately authorized one-time frozen
   test evaluation: positive net/benchmark-relative return, Sharpe >= 0.5,
   drawdown <= 15%, and 95% block-bootstrap lower bound above zero for mean net
   excess return. Failure cannot be repaired by tuning on the same test.
5. Shadow -> tiny live: >= 30 consecutive daily decisions and 20 simulated order
   lifecycles; zero unexplained duplicates/discrepancies; >= 99.5% scheduled decision
   completion; restart/partial-fill/timeout/kill drills pass; fresh instrument/fee
   metadata, validation-only requests and explicit user approval.

Proposed pilot caps: $200 equity, $20/order, $40/asset, $100 total exposure, $2 daily
loss, including pending commitments. Reject exchange minimum conflicts; no automatic
scaling. These are prospective gates, not findings or an authorization to trade.
Binance/USDT versus Kraken/USD, survivor-selected universe, cost-model uncertainty,
dependence, fat tails and short samples remain limitations. Profitability requires
untouched out-of-sample and live evidence.

## Seven-day roadmap

| Day | Outcome |
| --- | --- |
| 1 | Config, snapshot manifests, validation and split access (this slice) |
| 2 | Broker contract, deterministic runner and causal execution timing |
| 3 | Costs, funded ledger, benchmark/report fixtures |
| 4 | Signals/regimes, trial registry, train/validation experiment workflow |
| 5 | Instruments, sizing, reservations, duplicate controls and kill switch |
| 6 | Kraken shadow, reconciliation, restart recovery and monitoring |
| 7 | Recovery drills, reproducibility review, runbook/paper scaffolding; start shadow |

Seven days cannot satisfy the 30-day shadow gate or establish live profitability.

## Slice verification

Install the test dependency with `python -m pip install -r requirements-dev.txt`
if missing. The research modules also use the existing pandas/numpy environment.
Run `python -m pytest -q tests/test_research_slice.py`, compilation of the new
modules/tests, `python examples/audit_historical_data.py`,
`python examples/check_research_universe.py`, `git diff --check` and
`git status --short`. Tests use synthetic prices, isolated temporary files and a
fresh offline Alpha interpreter, not strategy-performance calculations. Inspect
the diff before any separately authorized commit.
