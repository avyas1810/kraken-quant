# Research platform usage

Run from the implementation repository root with Python dependencies installed. These commands were executed on Windows PowerShell; module invocations are identical on Linux. Create-once output paths must not already exist.

## Complete executable demonstrations

```powershell
python -m examples.run_btc_momentum --demo --output .phase2-cache/btc-demo-final
python -m examples.run_multi_momentum --demo --output .phase2-cache/multi-demo-final
python -m research.extended.validation --equity .phase2-cache/btc-demo-final/equity.csv --trial-dispersion 0.05 --replications 199 --output .phase2-cache/btc-demo-validation.json
```

Expected: JSON explicitly says `SYNTHETIC SOFTWARE DEMO; NOT EMPIRICAL EVIDENCE`. Both examples produce executed fills and equity, trades, positions, metrics and hashed manifests. Demo Sharpe/return is not research evidence. Trial dispersion0.05 is an illustrative assumption for the demo, not an estimated effective-trial model.

## Real historical data and custom strategies

Daily CSV filenames are `BTC_USD.csv`, `ETH_USD.csv`, `XRP_USD.csv`, `SOL_USD.csv`, `ADA_USD.csv`. Columns: `timestamp,open,high,low,close,volume`; timestamps explicitly denote daily UTC opens. Missing/duplicate days, invalid prices and OHLC relationships are rejected. No fill-forward occurs. Use training or authorised validation windows only; test is locked before data access. `--venue binance` identifies source instruments as USDT; it does not convert USDT into USD.

```powershell
[ordered]@{lookback=30;budget=0.8} | ConvertTo-Json | Set-Content -Encoding utf8 .phase2-cache/custom-parameters.json
python -m kraken_quant.platform_cli backtest --data-dir C:/Data/authorised-bars --venue kraken --factory kraken_quant.strategies:btc_factory --parameters-file .phase2-cache/custom-parameters.json --start 2022-01-01 --end 2022-12-31 --split train --fee 0.0026 --spread 0.001 --slippage 0.0005 --output .phase2-cache/custom-backtest
```

The real-CSV path above is operator-specific; the equivalent generated CSV fixture and custom factory command run in `tests/test_platform_workflow.py`. No authentic Kraken historical dataset is bundled. User-defined `module:function` factories receive `(parameters, seed)` and return a strategy implementing `target_weights(context)`. Use only `context.historical_data`, `context.positions`, `context.cash`, `context.equity` and causal features. `BTCMomentum`/`MultiAssetMomentum` provide complete working code, and `StrategyPipeline` is the same interface used by paper trading. Arbitrary Python is not a security sandbox; externally fitted models must have training-only provenance and frozen hashes.

Execution uses completed bars, next-open fills, funded position changes, fees/spread/slippage, gross exposure and cash constraints. Result exports include cash, equity, daily PnL, returns and buy/hold benchmark, quantities, reference/fill prices and each cost. Performance reports include return, volatility, Sharpe, Sortino, drawdown, turnover, executed order count and fees. Trades mean executed fills, not round trips.

## Statistical review and original empirical receipts

```powershell
python -m research.phase2.verify_results --results phase2_results/reviewed_20261016
python -m research.extended.trials --returns C:/Users/avyas/phase1_results/4h_net_returns.csv --output .phase2-cache/trial-audit-new
python -m research.extended.audit --root C:/Users/avyas --output .phase2-cache/independent-audit-new
python -m research.extended.kraken_candidate --trusted-cache C:/Users/avyas/MyTWSQ/data/Binance --output .phase2-cache/kraken-candidate-new --code-revision YOUR_VERIFIED_GIT_SHA
python -m pytest tests/test_platform_workflow.py tests/test_candidate_protocol.py tests/test_extended_research.py -q
```

First returns `status:verified,tables:9`. Original audit requires original hash-verified local files, not arbitrary pickle downloads. Candidate evaluation reads training only and never opens validation/test for optimisation. Its Binance proxy training drawdown fails promotion. The frozen four-hour statistical CLI remains `research.phase2.run_validation`; use `docs/phase2_input_contract.md` for exact recovered inputs and bootstrap/placebo regeneration options. Daily `research.extended.validation` exports block bootstrap, DSR and benchmark HAC; the benchmark need not be BTC, so output is labelled benchmark HAC.

## Freeze and qualify

```powershell
python -m kraken_quant.platform_cli freeze --spec candidate_spec.json --output candidate_frozen.json
python -m kraken_quant.platform_cli qualify --candidate research/extended/results_kraken_candidate/candidate.json --evidence prospective_evidence.json --output qualification.json
```

Supply operator-authored JSON. Freeze required fields: strategy,parameters,features,costs,risk_limits,input_hashes (SHA256),code_revision,prospective_start (future UTC),universe; evaluation_split=train or validation. Created record seals a UTC frozen_at and unchanged acceptance rules. Existing frozen files cannot be overwritten.

Qualification evidence requires candidate_sha256,sharpe,max_drawdown (positive magnitude),actual_daily_decisions,simulated_order_lifecycles,source=prospective_public_feed,reliability_passed,independent_review_passed,decision_dates_utc and completed_lifecycle_ids. At least30 unique real dates after the frozen start and20 distinct completed simulated lifecycle IDs are mandatory. Accelerated replay and training results produce an explicitly failed report. The report never authorises live trading. Review `docs/research_audit.md` and the operating SOP for account controls and separate approval.


PowerShell5.1 operators should use --parameters-file to avoid native executable JSON quoting differences. Parameters-file and inline parameters are mutually exclusive. Optional --feature-factory module:function receives parameters and seed and returns the feature builder used by StrategyPipeline. Factory source hashes and Git revision are recorded.

Paper entry points kraken_quant.strategies:paper_btc_factory and kraken_quant.strategies:paper_multi_factory return complete pipelines with0.1 gross budget. They are operational demonstrations distinct from the frozen0.8-budget research candidate, and cannot inherit its registration or evidence.

Qualification additionally requires positive net/benchmark-relative return,3positive assets,2positive neighbouring parameters,30round trips,doubled spread/slippage nonnegative return,95%bootstrap lower excessmean>0,99.5%completion,zero fault/gaps and29elapsed days. Exact fields and immutable thresholds are defined in kraken_quant/candidate.py. Daily validation reports mean-excess bootstrap confidence intervals explicitly; none replaces missing prospective evidence.


To freeze the tutorial directly from a completed custom-strategy backtest manifest, run `python -m examples.freeze_operator_demo --manifest .phase2-cache/custom-demo/manifest.json --output .phase2-cache/tutorial-candidate.json`. It verifies output hashes, seals source/input identities and a future UTC start, prints the candidate SHA, and marks research_only=true/live_authorized=false. Such a tutorial can never qualify for live promotion even if subsequent scalars look favourable.
