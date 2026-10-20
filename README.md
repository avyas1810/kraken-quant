# Kraken Quant

A research-to-Kraken project built around the existing TWSQ framework. Research, data validation, backtesting, persistent paper trading and risk controls are implemented. **No live order transport is implemented and no strategy is approved for live trading.**

## Current research finding

The reconciled nine-asset four-hour low-correlation momentum study has inspected-OOS Sharpe **0.37**, cumulative net return **+12.01%** and maximum drawdown **-30.04%**. It improves on a losing baseline, but absolute uncertainty intervals include zero and net BTC-adjusted alpha is insignificant. The older published 0.75 Sharpe is superseded.

This signed Binance/USDT study is separate from the five-asset daily Kraken/USD spot framework. It cannot be deployed as a spot strategy without a separately frozen, validated specification. Existing inspected OOS cannot be presented as untouched.

## Project records

- [Infrastructure log](docs/infrastructure_log.md): cumulative history, every component, event/persistence flow, verification and readiness limitations.
- [Research log](docs/research_log.md): original question, data/configurations, recovered accounting, diagnostics, findings and remaining research gates.
- [Current statistical report](docs/phase2_statistical_report.md).
- [Current reviewed results](phase2_results/reviewed_20261016/README.md).
- [Operator runbook](docs/operations_runbook.md).
- [Remaining-work register](docs/completion_register.md).
- [Architecture and proposed promotion gates](docs/architecture.md).

## Verification

The audited [First trial notebook](notebooks/First%20trial.ipynb) preserves the
original research notebook and adds automatically executed, nonzero long/short
acceptance portfolios and an isolated dry-run through the existing production
risk boundary. Run it from a fresh kernel with:

```powershell
python examples/execute_research_notebook.py --notebook "notebooks/First trial.ipynb"
```

Add `--fixture` for offline verification. See [its compliance report](docs/first_trial_compliance.md)
for tested scope and limitations. No live orders are submitted.

The [Quant Kraken research notebook](notebooks/quant_kraken_research.ipynb)
retrieves genuine Coinbase crypto spot history by default. Configure any supported
asset selection, one of six timeframes, a strategy factory and explicit costs,
then run all cells. Its shared AUD portfolio ledger supports costed long/short
research scenarios, covariance Kelly, hard risk limits, chronological validation,
net dashboards and historical next-allocation recommendations. Unsupported
asset classes/calendars and missing material costs fail explicitly. Final holdout
access remains locked; live routing is not implemented.

Install `requirements-notebook.txt` alongside the test/research requirements.
Run `python examples/execute_research_notebook.py` for a clean-kernel real-data
run, or add `--fixture` for explicit offline integration verification. Fixtures
never replace failed provider requests. Data caches, trial registrations,
immutable ledger exports and charts stay in ignored `.notebook-runtime/`.
See [the framework contract and limitations](docs/general_research_framework.md).

Use Python 3.10 with dependencies in `requirements-ci.txt` and `requirements-research.txt`.

```powershell
python -m pytest -q tests
python -m research.phase2.verify_results --results phase2_results/reviewed_20261016
```

For full historical reproduction from the original verified local corpus, see [the input contract](docs/phase2_input_contract.md). Raw data is not redistributed. Do not download replacement data and call it an exact reproduction.

## Paper operations

The default strategy is flat. A one-shot public observation needs no key and submits no exchange order:

```powershell
python -m kraken_quant.ops_cli once --database .shadow-runtime/paper-new.sqlite --start 2026-10-01T00:00:00Z --strategy-id flat-safety-v1
```

Use a new database for a changed engine/strategy/configuration. A halt requires inspection and documented recovery; never weaken freshness or replay checks to force a successful observation. Follow the runbook before using the supervised collector.

## Source and account boundaries

This checkout contains the existing licensed TWSQ framework. Keep its source private unless redistribution is explicitly permitted. The separate independent publication repository contains earlier research artifacts that are now marked superseded. No automatic publication or live trading occurs here. `.env`, raw caches and runtime databases remain ignored. Read-only observation and trading credentials must be separate, without funding permissions.
