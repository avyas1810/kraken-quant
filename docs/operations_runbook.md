# Kraken shadow operator runbook

Updated 2026-10-16. Scope: supervised paper observations and optional read-only exchange mirror. No command in this runbook submits a real order. See the infrastructure log for component details and the research report for why the historical candidate is not eligible for live promotion.

## Preparation and identity

1. Run the full tests and result verification. Pin the repository revision, dependency versions, strategy source/parameters/features and cost assumptions before beginning evidence collection.
2. Default `flat-safety-v1` is an operational data/health probe. It cannot satisfy twenty order lifecycles or demonstrate strategy returns.
3. Use an ignored local `.shadow-runtime` database for private implementation work, or an explicitly chosen local directory outside published material. Never reuse a database for a different engine, dependency set, strategy, start date, funding or fees.
4. Start must be a UTC daily boundary strictly after 2026-09-30. REST history is limited; missing warm-up halts. Do not silently shorten required history.
5. Synchronize system time. Event-time quotes must be <=5 seconds old, metadata must be current, and completed daily bars must cover the requested range. A genuine stale-data halt is a successful protective control, not a passing collection run.

## Public-only observation and supervised collection

From the implementation repository:

```powershell
python -m kraken_quant.ops_cli once --database .shadow-runtime/paper-new.sqlite --start 2026-10-01T00:00:00Z --strategy-id flat-safety-v1
python -m kraken_quant.ops_cli evidence --database .shadow-runtime/paper-new.sqlite
```

After reviewing the initial observation, use the foreground collector with the same identity:

```powershell
python -m kraken_quant.ops_cli collect --database .shadow-runtime/paper-new.sqlite --start 2026-10-01T00:00:00Z --strategy-id flat-safety-v1 --duration-days 7
```

Keep the process supervised and the machine awake. It polls around every20 seconds and decides once per completed daily bar. Watchdog timeout is60 seconds. The OS lock prevents a second collector/operator recovery process from owning the DB. Structured JSON goes to the console; no external alert destination or background service is configured. Stopping supervision disarms its watchdog and records the stop; it does not produce sustained evidence. An external supervisor is still needed to notice a process/machine that has died.

A real frozen candidate must be provided through `--strategy module:factory`, with a matching immutable `--strategy-id`. Trusted Python plugins are not sandboxed. Do not plug the signed nine-asset study directly into the spot runner. Changes to candidate code outside the engine package require a new strategy identity and database.

## Halt, inspect, recover, resume

```powershell
python -m kraken_quant.ops_cli halt --database .shadow-runtime/paper-new.sqlite --reason 'operator stop'
python -m kraken_quant.ops_cli inspect --database .shadow-runtime/paper-new.sqlite
python -m kraken_quant.ops_cli recover --database .shadow-runtime/paper-new.sqlite --reason 'reviewed paper-only interrupted cycle'
python -m kraken_quant.ops_cli resume --database .shadow-runtime/paper-new.sqlite --reason 'ledger and input problem reviewed'
```

Inspect contains local paper financial state; do not publish its raw output. These operator commands can initialize operational schema, so use direct SQLite `mode=ro` for a strict audit. Never edit cash, positions, kill flags or reservation rows manually.

Recovery first halts and replays the ledger. Uncommitted paper reservations become terminal while retaining identity; incomplete cycles become abandoned and are skipped, never retried. Committed fills remain. This is safe only because shadow has no exchange submit transport. It is not a valid recovery algorithm for UNKNOWN real orders. A corrupt ledger cannot be resumed. Resume starts a new evidence window; fault history is retained in the audit trail.

Held positions are marked against the previous completed UTC close for the daily baseline, persisted once per day. Monitoring never resets the same-day loss baseline. The original early shadow milestone's statement that all overnight holdings halt is superseded by this implementation; failure to establish the current baseline still halts.

## Optional private read-only mirror

No credentials are needed for public observation. To add private observation, use dedicated `.env` variables `KRAKEN_READONLY_API_KEY` and `KRAKEN_READONLY_API_SECRET`, separate from any trading key. Never print the file. The loader verifies the allowed read privileges and rejects unknown/trading/funding privileges. Key/account configuration and dedicated-account status still require real verification; a mocked test is insufficient.

Initialize public paper state, then explicitly accept a quiet dedicated real-account baseline:

```powershell
python -m kraken_quant.ops_cli pin-exchange --database .shadow-runtime/paper-new.sqlite --env-file .env --start 2026-10-01T00:00:00Z --reason 'dedicated read-only account baseline reviewed'
```

Run later `once`/`collect` with the same explicit `--env-file .env`. Once pinned, the observer cannot silently be disabled. Actual account fees and a fresh coherent private snapshot are checked. Real balances/orders/trades are compared to the pinned real mirror, never to paper fills. Unexpected account activity halts. Do not repin to conceal a discrepancy. This mirror does not implement live submitted-order lifecycle reconciliation.

## Evidence and promotion

Review `evidence` plus fault/recovery events. Synthetic samples never count as real market observations. Proposed operational thresholds:30 consecutive daily decisions, at least20 paper fills/lifecycles, >=99.5% scheduled completion, no unexplained discrepancies/duplicates, no heartbeat gaps/faults in the reviewed window, verified event times/account fees and successful recovery drills. The evidence counter's paper-fill count is only a proxy; inspect lifecycle records separately. `live_authorized` is always false.

The current research candidate also fails proposed research thresholds (Sharpe >=0.5, drawdown <=15%, positive absolute uncertainty evidence). Passing operational checks cannot override that failure. No live enablement flag implements transport. Before a first tiny live order, submitted-order reconciliation/partial fills/UNKNOWN recovery, dead-man timer, real account checks, monitored drills, a qualified candidate and explicit user approval are required.

## Current rehearsal records

- 2026-10-08 `public-safety.sqlite`: one completed flat daily decision, one public observation, supervision stopped. No sustained gate evidence.
- 2026-10-16 `public-review-20261016.sqlite`: first fresh flat attempt halted with `ValueError`, no completed cycle. Subsequent public diagnostics showed valid15-bar coverage and an intermittent stale/future quote rejection. Preserve the failed DB; do not treat it as a successful run or automatically clear it. Review follow-up observations in the infrastructure log.
