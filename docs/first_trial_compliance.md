# First trial compliance report

Deliverable: `notebooks/First trial.ipynb`. The original notebook is preserved
byte-for-byte from the start of this audit, including its pre-existing local
changes, which are excluded from this commit.

## Verified requirements

| Requirement | Implementation and verification |
| --- | --- |
| 1-50 assets and real ingestion | Unique 1-50 selection enforced; complete 1/3/10/50-asset experiments tested. Genuine Coinbase hourly ingestion and historical AUD FX verified, including a fresh 24-candle request without a pre-existing cache. |
| Six timeframes | Known OHLCV aggregates and full workflows tested for 4h, 8h, 16h, 24h, weekly and monthly, including calendar edges and incomplete-bar rejection. |
| Any user strategy | Shared `StrategyPipeline`, strategy factory, optional training/features and parameter interface; an independent signed strategy exercises the same engine unchanged. |
| Shared A$5,000 account | Multi-asset signed holdings, funded cash, liabilities, reversals and simultaneous funding checks reuse the existing ledger. |
| Costs before metrics | Nonzero execution costs, borrow, FX and a separate leveraged fixture's financing are deducted in both IS/OOS; gross mark PnL minus costs reconciles with net PnL. Unknown applicable cost inputs fail. |
| Separate net performance | IS/OOS net PnL, Sharpe, Sortino, drawdown, equity, fill/FIFO history and asset attribution derive from reconciled ledgers. |
| Kelly and risk | Quarter Kelly scaling verified; training-only covariance and cost-adjusted estimates; actual long/short allocations and nonzero next-timestamp targets tested under hard constraints. |
| Production handoff | `submit_recommended_weights(..., production=adapter)` calls the existing `ProductionAdapter.reserve`/`RiskGateway` on an isolated state copy. Nonzero quantity, kill controls, risk limits, mapping and FX checks tested; original gateway unchanged. Live mode rejected. |
| Standard reports | Eight standard charts, reports, costs, allocations, trades, benchmarks and immutable hashed artifacts reuse existing modules. Undefined ratios are labelled. |
| Validation and reproduction | Future observations/fits and locked final-holdout dates rejected; training sensitivity is isolated from OOS; clean kernels, explicit fixtures and deterministic repeat checks verified. |

## Test results

- Full repository suite: **374 passed** (`python -m pytest -q tests
  --basetemp=.notebook-runtime/pytest-first-trial-all`).
- Nine new cases cover nonzero signed Kelly portfolios, production-class
  dry-run integration/safety, 1-50 bounds and notebook-runner path containment.
  Existing tests cover complete asset-count/timeframe runs, costs, accounting,
  leakage, metrics, features and validation.
- The delivered notebook completed **all seven code cells from a fresh kernel**
  on genuine BTC/USD, ETH/USD and XRP/USD data (726 four-hour bars each).
- Explicit fixture mode also executes the same notebook, writing a separate
  ignored output; no provider failure silently falls back to synthetic data.
- Engineered acceptance fixtures actually hold long and short positions in
  both periods, pay execution/borrow/FX costs, and produce nonzero signed targets.
  A leveraged portfolio pays margin interest in both periods.
- Fresh public ingestion: 24 hourly BTC/USD candles, `[2024-02-28, 2024-02-29)`,
  zero missing bars, and 24 causal AUD reference valuations.

## Usage and safety boundary

```powershell
python examples/execute_research_notebook.py --notebook "notebooks/First trial.ipynb"
python examples/execute_research_notebook.py --notebook "notebooks/First trial.ipynb" --fixture
```

The notebook exposes `submit_approved_recommendations`, a thin callable wrapper.
Its production dry-run requires an existing `ProductionAdapter`, independently
reconciled USD `AccountSnapshot`, instrument-bound USD `Quote`/`InstrumentRules`
maps, explicit research-to-Kraken mapping, fresh AUD-per-USD execution FX and
fee rates. Unresolved-order and risk checks must be explicitly supplied. It never
calls `LiveEngine.submit` or changes production reservations. The separately
guarded `LiveEngine`/`KrakenTransport` exist in the repository; this notebook
does not authorize or exercise their live transport.

## Remaining limitations

- Operational historical provider/calendar support is Coinbase crypto spot,
  UTC 24/7, with AUD reporting. Fifty-asset verification uses deterministic
  inputs; fifty actual provider products were not verified. Each real product's
  eligibility/history is validated on request. Equities, ETFs, closed-market
  sessions, futures/perpetual settlement and asynchronous calendars are rejected.
- Real-data strategy defaults may remain flat under cost-adjusted Kelly. The
  separate forced-trade checks are explicitly synthetic accounting tests, not
  historical findings or evidence of profitable signals.
- Borrow/margin rates, constant impact, spreads and fees are explicit scenario
  assumptions. FX uses delayed daily references, not executable intraday quotes.
  Incomplete historical cost coverage is flagged. Real short availability and
  short broker execution are not claimed; the production spot bridge rejects
  short targets and preserves the existing tight risk limits.
- Weekly/monthly research needs enough complete history for the selected
  calibration, Kelly minimum and split. Insufficient history fails explicitly.
  Stops act at portfolio marks and flatten at the next eligible open; gaps can
  exceed limits. Custom Python strategies remain trusted code.
- The 2025-2026 final holdout remains locked. No live orders were placed.

Original notebook SHA-256 at audit start and completion:
`3c76b92b289fd71a94615747c045ee3fb55f3f57df1b1b228b6daa52544e0d56`.
