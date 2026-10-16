# Infrastructure log

Maintained from 2026-10-16. This is the cumulative project record, reconstructed from Git history, component sources and milestone documents. Historical statements distinguish implemented capability from operational evidence. Update the component map and append a dated entry after each material change. Never include credentials, account payloads or raw private responses.

## Objective and design

Build a reusable historical-research -> backtest -> validation -> Kraken shadow -> risk-controlled live system around the existing TWSQ framework. Keep strategy decisions separate from data, accounting, risk and exchange transport. Prefer small adapters and reuse over rewrites. One supervised local process and SQLite are sufficient; no distributed services are required.

There are two separate configurations: the executable Kraken framework uses five daily USD spot markets (BTC, ETH, XRP, SOL, ADA), while the recovered empirical study uses nine assets and four-hour Binance USDT bars. The study is not already a Kraken strategy. `examples/research_config.py` is the sole daily universe/split authority. Do not substitute the nine-asset study into that configuration.

## Historical milestones

| Date | Commit | Delivered |
| --- | --- | --- |
| 2022-02-03 | cba828e | Original shared TWSQ codebase. Licensed baseline, not independently publishable project code. |
| 2026-09-09 | 3ac6623 | Baseline checkpoint before Kraken work. |
| 2026-09-15 | 5ccdb6e | Secure read-only Kraken connectivity example. |
| 2026-10-01 | b941ea6 | Liquidity/spread-based research-universe selection. |
| 2026-10-02 | 6edf3e8, 22800a9, b5c0493 | Operating brief, central dates/universe, availability and daily-history audit. |
| 2026-10-02 | e1a7b81 | Immutable snapshots, validation, restricted history and offline Alpha bridge. |
| 2026-10-06 | 341cba9 | Funded backtest accounting, transaction costs, metrics and benchmarks. |
| 2026-10-08 | 073aeb7 | Signed research execution, immutable experiment registry, durable risk gateway and non-submitting production boundary. |
| 2026-10-08 | def04c4 | Public Kraken market data, persistent spot paper runner, mocked tests and CI. |
| 2026-10-14 | dfd0538 | Preserved Phase 1 and interrupted operational implementation. Preservation did not approve production. |
| 2026-10-14 | 58249f2 | Preserved discovered original nine-asset research source/manifests. |
| 2026-10-16 | Working tree | Existing Phase 2 analysis and tests are pending verification/commit; historical report is stale. |

## Component map

| Component | Responsibility and boundaries |
| --- | --- |
| `twsq/` | Existing Alpha, broker, order and runner contracts. Stable licensed framework; legacy transport/recovery are not production approved. |
| `examples/research_config.py` | Five-asset USD universe; full 2020-08-11..2026-09-30, train through 2023, validation 2024, test 2025..2026-09-30. |
| `examples/check_research_universe.py`, `rank_kraken_liquidity.py`, `audit_historical_data.py` | Availability/liquidity/spread preparation and historical integrity audit. No profitability inference. |
| `kraken_quant/config.py` | Frozen validated import of central configuration; no duplicate universe definition. |
| `data.py` | UTC daily OHLCV validation, canonical provenance-hashed snapshots, create-once storage, decision-time copies, locked-test rejection. Does not interpolate gaps. |
| `features.py` | Causal momentum feature fixture; extensibility point, not a selected live model. |
| `synthetic.py` | Offline TWSQ Alpha integration; isolated from credentials and live brokerage. |
| `strategy.py` | Instrument identity, per-venue histories, strategy context and shared target-weight pipeline. Arbitrary Python feature objects supported; callers remain responsible for causal fitting. |
| `backtest.py` | Funded long-only research core using completed decisions and subsequent execution. |
| `research_execution.py` | Signed long/short research ledger, funding constraints, explicit costs/financing, next-bar execution and same-assumption benchmarks. Simulated borrowing is not Kraken spot permission. |
| `metrics.py` | Returns, risk ratios, volatility, drawdown, turnover and related reporting. Undefined ratios remain undefined. |
| `experiment.py` | Preregistered hypotheses/parameters/features/seeds; immutable trial records and train/validation reports with hashes. Rejects locked test access. |
| `risk.py` | Decimal instrument rules, precision/minimum checks, funded spot sizing, freshness, pending reservations, duplicate identities, persistent kill and hard dollar caps. SQLite transactions serialize approval. |
| `boundary.py` | Strategy-to-order evaluation; spot long-only validation. Production `submit` is deliberately unimplemented. No exchange order transport. |
| `market_data.py` | Public Kraken OHLC/Ticker/AssetPairs normalization. Completed daily bars only; rejects insufficient coverage and slow requests. Ticker timestamps are local observations. |
| `shadow.py`, `shadow_cli.py` | Persistent daily paper decisions, sell-before-buy fills at bid/ask, fees, immutable identity and crash/deduplication protection. Full fills only; no exchange orders. |
| `operational_data.py` | Public Spread event-time quotes, five-second freshness and regression rejection. Retains normal OHLC/instrument reader. |
| `operations.py` | OS process lock; durable operator events, ledger replay, halt/recover/resume, persistent nonces, heartbeat/watchdog and shadow-evidence counters. Operator inspection initializes schema; it is not a strictly read-only DB interface. |
| `operational_shadow.py` | Supervises shadow ticks, ledger checks, quote-marked risk, UTC baseline handling, engine/dependency identity, optional private read-only mirror and fee observations. Stops on discrepancy. |
| `reconciliation.py` | Whitelisted read-only permissions/endpoints, account-specific fee snapshot, paginated trade-ID checks, coherent balances/open orders and pinned-account comparison. Observes an unchanged real account; does not reconcile live submitted intents or partial fills. |
| `credentials.py` | Explicit opt-in .env loader for dedicated read-only key, separate from trading key; redacted failures and account fingerprint. Never load or print credentials for reporting. |
| `ops_cli.py` | `once`, `collect`, `inspect`, `halt`, `resume`, `recover`, `evidence`, `watchdog`, `pin-exchange`. Default flat safety strategy; collector is a supervised foreground process. |
| `research/phase1_frozen/` | Selected original source cells and provenance tables, excluding raw datasets/notebook output dumps. |
| `research/phase2/` | Frozen-study statistical layer. Detailed responsibilities are in the research log. |
| `tests/`, `.github/workflows/ci.yml` | Synthetic/offline contract, accounting, research, risk and operational checks. Test success verifies software, not alpha or a real account. |
| `.shadow-runtime/`, `.phase2-cache/`, `.env` | Local operational state, computational caches and secrets; excluded from publication. Existing public-safety DB is historical rehearsal state, not proof of a current collector. |

## Event and persistence flow

Historical: source -> audited immutable snapshot -> authorized split/history -> features -> target weights -> next-event accounting/costs -> ledger -> metrics/benchmark -> registered artifact hashes.

Shadow: completed Kraken bars + event-time quote/rules -> pipeline -> long-only targets -> durable risk reservation -> simulated bid/ask fill -> atomic paper ledger -> replay reconciliation -> monitored equity/baseline -> structured evidence. Real-account observations are a separate mirror and never made equal to paper balances.

Live: no implemented submission path. A future path needs durable intent -> signed transport -> acknowledged/UNKNOWN state -> deduplicated fills -> order/trade/balance reconciliation -> terminal reservation release. Never retry an uncertain submission without reconciliation.

## Current controls and limitations

- Spot caps: $20/order, $40/asset, $100 aggregate exposure, $2 daily loss; pending costs/exposure included. No shorting or leverage in Kraken mode.
- Locked daily test access remains rejected. Research signed positions and nine-asset Binance results cannot bypass it.
- Quotes must be fresh; event-time public spread data is stricter than locally timestamped ticker observations.
- OS process lock, persistent identity/reservations and replay checks exist. A changed engine requires a new evidence DB.
- Read-only private account mirror exists, but real key permissions/account fees have not been verified in this session.
- Existing architecture proposes 30 daily decisions, 20 paper lifecycles, >=99.5% completion and successful fault drills before tiny-live consideration. Flat observations cannot manufacture trading evidence.
- Partial fills, live order recovery, exchange dead-man transport, external alert delivery, market-depth/latency execution and complete append-only payload archiving remain incomplete.
- Promotion criteria are proposals, not retroactively tuned gates. Failed research criteria cannot be waived by infrastructure success.

## Verification and change log

### 2026-10-16 - Baseline inventory and logging established

Read Git history, existing milestone documents and operational sources. Active branch `research/phase2-statistical-validation`, baseline HEAD `58249f2`; Phase 2 sources/results/tests and CI changes were already uncommitted. No files were discarded. Phase 2 result integrity command passed for nine tables (1,999 bootstrap draws and 999 placebo trials/method). No new trading, credential access or collector start occurred. Two cumulative logs created before implementation changes, as requested. Next: verify full suite, refresh stale reports, inspect operational readiness and preserve a reviewable checkpoint.

### 2026-10-16 - Verification, reproduction and operational audit

Initial full suite:245 passed, one offline Alpha subprocess cold-import timeout at30 seconds. Isolated test passed; only that startup limit changed to120 seconds, keeping every credential/network/security assertion. Full rerun:246 passed in55.34 seconds. No production source changes were necessary.

Read-only audit of historical public-safety DB:one complete cycle and one sample on2026-10-08; supervision stopped. First new public-only one-shot (`public-review-20261016.sqlite`) halted with ValueError and left an incomplete cycle, no fills/observation. Subsequent public diagnostics verified15 completed bars (October1..15), valid metadata and quote event age1.32 seconds; an earlier diagnostic rejected stale/future quote data. Failure is retained, not automatically cleared or counted as passing evidence.

Added current README, operations runbook and completion register. The runbook corrects the older overnight-holdings limitation:current code has previous-close midnight baseline handling, covered by existing tests. Read-only account mirror, operational modules and fault checks already existed; they are not newly invented functionality. No private key/account access or real submission occurred. Remaining live transport and sustained evidence are explicitly open.

### 2026-10-16 - Independent publication boundary

Prepared a local correction in the separate publication repository, copying only current derived statistics and public-facing research documentation. Preserved original artifacts with superseded labels. Implementation source and runtime/secret files remain excluded. No push or remote repository change was made.

### 2026-10-16 - Public freshness limitation confirmed

A separate acceptance attempt and a diagnostic attempt also halted before a completed decision. Public-only diagnostics isolated the repeated failure to Spread event freshness, not missing daily history or metadata. One sampled full recent-spread response had a latest event16.07 seconds old; the documented incremental request over the last5 seconds returned no observations. No freshness threshold was relaxed, no synthetic timestamp was substituted, and no failed database was reset. There is no unattended collector running. Reliable event-time public collection remains an operational blocker requiring further feed review (potentially a reviewed streaming adapter), not another passing test claim.

Official schema reviewed: https://docs.kraken.com/api-reference/market-data/get-recent-spreads . It defines Spread as recent top-of-book updates, not a guarantee of a fresh event on every HTTP poll. Added CI verification of current result artifacts; source code remains unchanged except the offline test startup budget.

### 2026-10-16 - Final verification before checkpoint

Compiled71 tracked/new Python sources into an external temporary bytecode cache. Both repositories passed diff whitespace checks. `.env`, all operational rehearsal DBs and computational caches remain ignored. Full implementation suite246 passed; publication suite3 passed. Reviewed result integrity passed. No unattended collector, account access, live order or remote publication occurred. Focused local checkpoints are being created for the timeout fix, completed Phase2/reporting slice and independent publication correction. Remaining deliverables are tracked in `completion_register.md`, with research/feed/elapsed-time/account/live-implementation requirements still open.

### 2026-10-16 - Cross-platform artifact integrity correction

Staged-blob inspection found that the pending `text eol=lf` rule normalized CRLF CSV bytes, invalidating manifest hashes after checkout. Replaced text conversion with `-text` for hash-frozen results and recovered source/provenance. Re-staged preserved original bytes; the recovered source content and data are unchanged. Statistical Python retains explicit LF. The independent publication uses the same exact-byte rule for its copied reconciled tables. Verification now includes staged Git blobs, not just the working tree.

### 2026-10-16 - Checkout verification

Verified the current9-table manifest and original recovered source/manifest identities from a fresh staged checkout. Frozen-artifact staging differences are line endings only (`git diff --cached --ignore-space-at-eol` is empty for the recovered corpus). CRLF is explicitly treated as a line ending by whitespace checks. No inference values or source semantics changed.

### 2026-10-16 - Local checkpoint receipts

- `a670029`:verified offline Alpha startup timeout correction.
- `1587c70`:completed pending Phase2 implementation/results, cumulative logs, report/runbook/register and frozen-byte packaging.
- Independent publication `8fec265`:corrected0.37 headline, current report/results and explicit legacy labels.

These are local commits; no remote push occurred. This task has not completed the original end-to-end trading objective. Open requirements are reliable fresh quote collection, a qualified Kraken-compatible frozen candidate, independent review, real account verification, sustained prospective/shadow evidence, live transport/lifecycle implementation and explicit first-order approval. The current paper correction is Markdown; legacy PDF/DOCX and charts remain labeled archival. Continue from this register rather than recreating completed modules or treating the current candidate as approved.
