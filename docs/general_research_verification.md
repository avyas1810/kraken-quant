# General research implementation verification

Verified in `C:\Users\avyas\kraken_quant` on the working tree based on `72a74f2`.
Changes are repository-local. No credentials were displayed and no orders sent.

| Check | Result |
| --- | --- |
| Full repository suite | 365 passed; 38 new targeted general-research cases |
| Asset counts | Complete 1, 3, 10 and 50 asset synthetic experiments |
| Timeframes | Known OHLCV aggregations and complete workflows for 4h, 8h, 16h, 24h, weekly, monthly |
| Calendar edges | Continuous epoch-anchored 16h; Monday weeks; leap-year February; no partial bins |
| Data boundaries | Duplicate/conflict/gap/unfinished checks, provider pagination/cache integrity, unsupported products, timezones |
| Net accounting | Hand-calculated next-open round trip; costs, FIFO, cash/assets/liabilities and PnL reconciliation |
| Cost effects | Higher commission, spread, slippage and impact; short borrow, margin financing and FX fees reduce net PnL |
| Performance | Sharpe from net returns; drawdown from net equity; gross mark PnL minus costs equals net PnL |
| Portfolio controls | Shared capital for simultaneous orders; eligibility/short restrictions; loss/drawdown halt |
| Leakage | Training-only model/features/Kelly; future bars/fits rejected; OOS perturbations do not change IS or training sensitivity |
| Validation | Separate accounts/reports, embargo, expanding walk-forward and overlapping-fold rejection |
| Handoff | Dry-run only; stale/future data, invalid reconciliation, shorts and live mode rejected |
| Notebook | Six code cells completed from fresh kernels, genuine data and explicit fixture modes, no cell errors |
| Dashboard | Eight figures generated and visually inspected; undefined Sharpe explicitly labelled |
| Reproducibility | Repeated final real-data run reproduced run identity and all 43 exported artifact hashes |
| Git/compile | Diff whitespace checks and relevant Python compilation passed; runtime and `.env` remain ignored |

The genuine-data run retrieved BTC/USD, ETH/USD and XRP/USD on Coinbase Exchange
for `[2023-11-01, 2024-03-01)` and produced 726 four-hour bars per asset. Historical
USD/AUD reference observations have separate provenance and conservative
publication timing. Real-provider availability for fifty assets was not tested;
the fifty-asset verification exercises reusable infrastructure with fixtures.

Final repeated run ID:

`41c5d4254589da74d1e6d1e8c1e435f67c2b11fd0225d4592cd1d46dccf71bfc`

Its source hashes match the verified modules. Manifests pin the pre-commit Git
revision plus actual source hashes, so committing the change does not make the
source identity ambiguous. Local immutable artifacts are under
`.notebook-runtime/general-results/<run_id>/`; they are intentionally not committed.
The executed real-data notebook is committed with its outputs. CI uses the
explicit fixture runner and does not require network market-data availability.

The default strategy remains flat under the supplied cost-adjusted Kelly
scenario. Net benchmark accounting and traded fixtures exercise nonzero fills;
execution success is not evidence of profitable signals. Historical costs and
intraday executable FX coverage are incomplete and flagged. Equities, ETFs,
closed-market calendars, futures/perpetual settlement, real borrow/margin
execution and live order transport are not claimed. The final holdout remains
locked. See [the framework contract](general_research_framework.md).
