# Offline funded backtest core

`run_backtest` accepts `RestrictedHistory`, a selected subset of the imported
research universe, one train/validation interval, funded cash, constant fee and
adverse-slippage fractions, and a callable `strategy(context)`. No test unlock,
exchange, credential, network, disk cache or live submission code is present.
The engine never invokes legacy TWSQ execution/accounting: their same-close fills
and zero-funded trade accounting are unsuitable here. A future Alpha broker facade
can adapt target intents to this core without changing the accounting rules.

The strategy sees copies of completed OHLCV ending at its current decision bar,
cash, holdings and funded close equity. Optional prior warm-up comes through the
existing restricted reader and is not scored. A weight mapping targets the entire
selected portfolio; omitted symbols target zero, nonnegative weights must sum to
at most one, and the remainder is cash. `None` holds quantities without rebalancing.
Only the engine sees prefetched future execution/marking bars. As elsewhere in this
project, this is a causal application contract, not a hostile-code sandbox.

## Timing and accounting

Bar timestamps are UTC opens. A signal after bar t closes sets fixed desired
quantities q_i* = w_i E_t / C_i,t, where w is target weight, E is funded equity,
and C is the completed close. Differences from holdings fill at bar t+1 open.
Quantity sizing does not use that future open. Buy/sell price is
p = O_(t+1) (1 + d s), with open O, direction d = +1 buy / -1 sell, and adverse
slippage fraction s. Fees F = f |q| p use fee fraction f and signed traded quantity
q. Cash changes by -(q p + F); positions change by q. Close equity is cash plus
the sum of held quantities times completed closing prices.

Sells execute before buys, without shorts. If buys cost B including fees and
available cash is A, buy quantities scale together by min(1, A/B). Thus a 100%
target remains funded despite gaps/fees; realized weights may differ from targets.
Tiny changes below initial_cash * 1e-12 in reference notional are ignored only as
numerical noise. The final close generates no unfillable decision and holdings
are marked without fictitious liquidation. Initial funding and every daily close
are returned as aligned cash, quantity and equity records, alongside signed fills.

Example: $1,000 cash, close $100, 50% target sets 5 units. Next open $110,
1% slippage and 1% fees give fill $111.10, notional $555.50, fee $5.555,
remaining cash $438.945. A $120 close gives equity $1,038.945. Selling those
5 units at a later $120 open fills at $118.80, incurs $5.94 fees, and leaves
$1,027.005 cash. These synthetic figures verify mechanics, not strategy findings.

`buy_and_hold` submits one initial target after the first scored close, uses the
same next-open timing/costs/funding constraints, then holds quantities. Default
weights are equal over the explicitly selected assets. Custom weights may retain
cash. Neither benchmark nor strategy trades on the initial funding mark.

## Metrics

With E_0 funded equity and E_t daily close marks, r_t = E_t/E_(t-1) - 1.
Total return is E_T/E_0 - 1. Annualized volatility is sqrt(K) times sample standard
deviation of daily returns; K defaults to 365. Daily cash risk-free return is a
disclosed constant r_f, default zero. Sharpe is sqrt(K) mean(r-r_f)/sd(r-r_f),
using sample deviation (N-1 divisor). Sortino is
sqrt(K) mean(r-tau) / sqrt(mean(min(r-tau, 0)^2)), where tau is a disclosed daily
target, default zero; the denominator averages across every day, including zeros.
Zero deviations yield NaN, not claims of infinite risk-adjusted performance.

For returns +10%, -10%, +10%, mean is 1/30, sample deviation is sqrt(1/75),
and downside deviation is sqrt(1/300). With K=3, Sharpe=0.5 and Sortino=1.
This three-observation example is not statistically informative.

Drawdown is 1 - E_t / max(E_0,...,E_t); maximum drawdown is its largest value.
$1,000 -> $1,100 -> $990 -> $1,089 gives 10% maximum drawdown and 8.9% total return.
Turnover is total absolute executed notional / mean daily close equity (excluding
the initial funding mark). A $500 buy and $600 sale, with average equity $1,100,
give turnover 1.0. Trade count counts executed nonzero buy/sell orders, not round
trips; each order is modeled as one fill. Fees and adverse-slippage currency costs
are reported separately. Slippage is already reflected in fill prices and is not
subtracted from equity twice.

## Scope and verification

Fractional units, full fills, one common quote currency, no external flows, cash
interest or exchange precision/minimums are assumed. Fees/slippage are explicit
scenario assumptions, not claimed current Kraken rates. Slippage can include a
spread allowance; separate book/latency/impact and partial-fill models are future
work. Same-boundary next-open fills are optimistic about operational latency.
Daily-close drawdown understates intraday risk; square-root annualization ignores
serial dependence, and short samples/zero-downside periods limit ratio interpretation.
No strategy profitability or historical validation results are asserted here.

Run focused synthetic fixtures with:
`python -m pytest -q tests/test_backtest_core.py tests/test_research_slice.py`.
Fixtures cover hand accounting, gap funding, rotation, benchmark, metrics,
causality, warm-up, no future fills, invalid targets and locked-test rejection.
