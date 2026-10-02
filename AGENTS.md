\# Kraken Quant Research Project



\## Objective



Build a reusable quantitative research-to-live-trading system using the existing TWSQ framework.



The system must support:



1\. Historical-data research

2\. Reproducible backtesting

3\. Train/validation/test evaluation

4\. Transaction-cost modelling

5\. Kraken shadow trading

6\. Risk-controlled Kraken live trading

7\. Additional strategies without rebuilding the infrastructure



\## Research Question



Do regime-conditioned momentum, reversal and volume signals generate robust risk-adjusted returns across liquid cryptocurrencies after realistic trading costs?



\## One-Week Deliverables



\- Reusable research and strategy framework

\- At least one properly validated strategy

\- Reproducible results, tables and charts

\- Draft research paper

\- Kraken shadow-trading capability

\- Risk-controlled live-trading capability

\- Professional GitHub repository and README



Profitability must not be claimed unless supported by untouched out-of-sample and live evidence.



\## Existing Framework



Reuse TWSQ’s existing:



\- Alpha strategy interface

\- Order abstraction

\- Broker abstraction

\- Backtest runner

\- Portfolio accounting

\- Kraken and Binance components where reliable



Do not rewrite working infrastructure unnecessarily.



\## Research Universe



\- BTC/USD

\- ETH/USD

\- XRP/USD

\- SOL/USD

\- ADA/USD



The universe must be imported from `examples/research\_config.py`. Do not redefine it in other files.



\## Locked Dates



\- Full data: 2020-08-11 to 2026-09-30

\- Training: 2020-08-11 to 2023-12-31

\- Validation: 2024-01-01 to 2024-12-31

\- Test: 2025-01-01 to 2026-09-30



The test period must remain untouched until the strategy specification and parameters are frozen.



\## Completed Work



\- TWSQ baseline backtest verified

\- Clean baseline committed

\- Feature branch created: `feature/kraken-research-live`

\- Kraken read-only API connection verified

\- Research universe availability verified

\- Liquidity and spread ranking completed

\- Historical daily data audited

\- No duplicates, missing values or missing days found

\- Common start date established as 2020-08-11



\## Architecture Rules



\- Existing files are treated as stable interfaces.

\- Prefer adding new modules instead of repeatedly modifying established files.

\- Modify existing files only for confirmed bugs, security fixes or deliberate interface changes.

\- Central configuration must not be duplicated.

\- Strategies must remain separate from data, execution, risk and reporting code.

\- Research and live execution must use the same strategy interface.

\- Every material change requires a focused Git commit.



\## Security Rules



\- Never print, expose or commit API credentials.

\- Credentials must come from `.env`.

\- `.env` must remain ignored by Git.

\- Withdrawal and deposit permissions must never be enabled.

\- Read-only and trading keys must be separate.

\- Do not place a live order without explicit user approval.

\- Default to read-only or shadow mode.



\## Live-Risk Rules



Before live trading, implement:



\- Maximum position size

\- Maximum order size

\- Maximum daily loss

\- Maximum total exposure

\- Stale-data rejection

\- Duplicate-order prevention

\- Emergency kill switch

\- Order and balance reconciliation

\- Instrument precision and minimum-order validation



\## Research Standards



Every strategy report must include:



\- Buy-and-hold comparison

\- Returns and volatility

\- Sharpe and Sortino ratios

\- Maximum drawdown

\- Turnover

\- Trade count

\- Fees and estimated slippage

\- Train, validation and untouched-test results

\- Parameter-sensitivity analysis

\- Cross-asset consistency

\- Clear separation of hypotheses and findings



No look-ahead bias, survivorship bias or test-period parameter tuning is allowed.



\## Working Method



Before changing anything:



1\. Inspect the repository and Git status.

2\. Read the relevant implementation.

3\. State the intended change.

4\. Make the smallest coherent change.

5\. Run relevant tests or scripts.

6\. Inspect the diff.

7\. Report exactly what was verified.

8\. Commit only after verification.



Do not treat a successful program run as evidence that a strategy is profitable.



\## Immediate Next Task



Commit `22800a9` established `examples/research\_config.py` with the locked research universe and chronological dataset splits, and added the historical-data audit. The next phase is designing and implementing the reusable research experiment framework using the existing TWSQ interfaces. Do not examine locked test-period performance until the strategy specification and parameters are frozen.

