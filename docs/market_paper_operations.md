# Public market data, paper execution and operational recovery

## Components and safety boundaries

`streaming.WebSocketMarketData` inherits completed daily REST OHLC and instrument
rules from `KrakenMarketData`, replacing REST Spread execution quotes with Kraken
WebSocket v2 ticker, subscribed with `event_trigger="bbo"`. The exchange's RFC3339
ticker timestamp is retained exactly. A quote older than five seconds is not
executable; heartbeats never refresh it. Future, regressing, mismatched, locked or
crossed quotes halt. Ticker does not expose a documented sequence number; this
adapter cannot prove packet completeness. Fresh subscription snapshots, monotone
event times and bounded collection provide the available controls. An initial
stale snapshot can be followed by a genuine fresh BBO update within the deadline.
Protocol: [Kraken's official v2 ticker specification](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/ticker).

Each quote uses a fresh connection closed in `finally`, preventing unread REST or
strategy-time socket backlogs. At most three connection attempts, four seconds
per collection and 1/2 second reconnect backoffs are permitted. Each connection
has a 1,000-message bound. A missing heartbeat/update or disconnected feed cannot
produce a fabricated quote. All transport failures become enumerated safe errors.
`ops_cli` defaults to this feed. Injected transports remain labelled `injected`.

`OperationalShadowRunner` retains the existing single-writer process lock,
`ShadowRunner` strategy adapter, `RiskGateway`, persistent reservations, orders,
fills, cash/positions, midnight baseline, engine manifest, kill switch, ledger
reconciliation and watchdog. It has no exchange order-submission transport.
The default strategy is flat. A trusted `module:factory` must return the actual
`StrategyPipeline`; custom strategies cannot silently bypass production risk.

`paper_cli.ReplayMarketData` drives that same durable paper engine from daily UTC
OHLCV CSVs after the locked dataset. The strategy sees completed prior-day bars;
simulated fills use the following open with explicitly configured half-spread.
Precision/minimums are scenario assumptions unless a rules JSON is supplied.
The entire history hash and replay assumptions are pinned to the database.
Replays resume forward, never replay committed decisions. They are labelled
`historical_replay`, with `event_time_verified=False`, and never count as genuine
prospective evidence. This adapter neither unlocks nor reads the original test
dataset. Research-period backtests use the separate research interfaces.

`monitoring.health_check` checks SQLite integrity, exact ledger reconstruction and
watchdog. Ledger faults or expired armed watchdogs persist a halt. An intentionally
stopped process is unhealthy, even if its ledger is sound. `WebhookAlert` is an
opt-in HTTPS adapter: only four health booleans are sent, never private responses,
credentials, account balances, raw exception messages or destination URLs.
No external alert recipient has been configured or contacted during development.

Selected assets are multiplexed into one subscription/connection for each BBO
batch, rather than sequential handshakes that could age the first pair's quote.
Instrument metadata is collected before executable quotes. A regression test
delays five metadata lookups by 15 seconds and verifies that execution still
receives newly collected quotes without weakening freshness.

`backup_database` uses SQLite's online backup API and verifies integrity; existing
destinations are never overwritten. `restore_database` restores into a new file,
checks the ledger and persists a kill switch requiring manual reviewed resume.
Backups, exports and databases are private runtime artifacts, not Git artifacts.

## Tested Windows PowerShell commands

Run from the repository root with dependencies installed. These commands submit
no exchange orders. The public operations require internet access and actual
exchange availability; their prices and timestamps necessarily vary.

```powershell
python -m pytest tests/test_streaming.py tests/test_paper_platform.py tests/test_monitoring.py tests/test_operations.py -q
python -m kraken_quant.paper_cli diagnose --symbol BTC/USD
python -m kraken_quant.paper_cli once --database .shadow-runtime/public-flat.sqlite --start 2026-10-01T00:00:00Z --strategy-id flat-safety-v1
python -m kraken_quant.paper_cli collect --database .shadow-runtime/public-flat.sqlite --start 2026-10-01T00:00:00Z --strategy-id flat-safety-v1 --duration-days 30
```

Expected diagnostic: `source="kraken_public"`, positive bid/ask with bid < ask,
authentic recent quote timestamp, `live_authorized=false`. Expected flat decision:
`status="processed"`, cash/equity 100 and no fills. Same-day repeats return
`already_processed`. `collect` checks risk and marks every approximately 20 seconds,
decides once daily and exits with an audited halt on any failure. Graceful duration
completion disarms the watchdog. Ctrl+C closes the writer lock; the watchdog stays
armed so a subsequent health check can identify an unexpected stop.

```powershell
python -m kraken_quant.paper_cli inspect --database .shadow-runtime/public-flat.sqlite
python -m kraken_quant.paper_cli evidence --database .shadow-runtime/public-flat.sqlite
python -m kraken_quant.paper_cli health --database .shadow-runtime/public-flat.sqlite
python -m kraken_quant.paper_cli halt --database .shadow-runtime/public-flat.sqlite --reason "operator emergency drill"
python -m kraken_quant.paper_cli recover --database .shadow-runtime/public-flat.sqlite --reason "paper-only crash inspected"
python -m kraken_quant.paper_cli resume --database .shadow-runtime/public-flat.sqlite --reason "ledger, inputs and faults reviewed"
python -m kraken_quant.paper_cli export --database .shadow-runtime/public-flat.sqlite --output .shadow-runtime/paper-export.json
python -m kraken_quant.paper_cli backup --database .shadow-runtime/public-flat.sqlite --output .shadow-runtime/paper-backup.sqlite
python -m kraken_quant.paper_cli restore --database .shadow-runtime/paper-backup.sqlite --output .shadow-runtime/restored-paper.sqlite
```

`health` returns exit 0 only when the ledger, armed heartbeat and kill switch are
healthy; exit 1 is expected for a stopped collector. Exports include durable
orders/fills, positions/cash, sampled decision equity, marked PnL, total fees and
evidence. Export/backup/restore paths must be new. Stop the original writer before
restoring and select its final consistent backup. Inspect the restored state,
recover incomplete paper-only cycles if present, then explicitly resume. Never
run the original and restored databases concurrently as separate instances of one
prospective experiment. Engine changes require a new evidence database; inspect
and preserve the previous experiment rather than rewriting its manifest.

Replay inputs have `timestamp,open,high,low,close,volume` columns with complete
daily UTC observations and common boundaries. At least two days after 2026-09-30
are required. This command assumes a supplied authentic post-lock CSV:

```powershell
python -m kraken_quant.paper_cli replay --bars BTC/USD=.shadow-runtime/BTC_USD.csv --database .shadow-runtime/replay.sqlite --strategy-id flat-replay-v1 --fee-rate 0.004 --spread-bps 10
```

Add `--strategy trusted_module:factory` for an actual pipeline; add repeated
`--bars ETH/USD=...` for a common-boundary multi-asset replay. Real-feed `collect`
accepts `--symbols BTC/USD ETH/USD XRP/USD SOL/USD ADA/USD` and the same strategy
factory option. The strategy must respect the fixed $20/order, $40/asset,
$100/total and $2/daily-loss gates; no threshold is weakened for a candidate.

Optional external alerts must be deliberately configured by the operator. Store
the HTTPS recipient in an environment variable (not committed configuration),
then use `health --alert-url-env KRAKEN_HEALTH_WEBHOOK --send-alert` alongside the
normal `--database` option. Omitting `--send-alert` performs no network delivery.
The independent health process must be scheduled separately from the collector
so it can detect collector death; delivery failures return nonzero for supervision.

Linux uses identical Python commands; activate the environment with
`source .venv/bin/activate` instead of PowerShell's activation script. Process locks
use `flock` on Linux and non-blocking `msvcrt.locking` on Windows.

## Candidate binding and qualification evidence

Use `collect --candidate candidate.json` with the normal database/start options
to instantiate the exact frozen strategy, parameters, selected universe and fee
scenario. No `--strategy` override is accepted. Public collection cannot start
before `prospective_start`. Registered strategy names are supported, as are
trusted `module:factory(parameters,seed)` factories returning a strategy or
pipeline; custom factories require their actual repository source SHA in the
preregistration. Candidate source hashes are verified before collection. The
database binds the full specification, raw input identities, candidate file SHA,
strategy source SHA and existing engine manifest. Mutation requires a new
experiment rather than quietly rebasing evidence. Replay accepts the same
`--candidate` option but remains categorically non-prospective.

```powershell
python -m kraken_quant.paper_cli export --database .shadow-runtime/candidate.sqlite --candidate candidate.json --qualification-evidence --output .shadow-runtime/candidate-evidence.json
```

This creates top-level qualification input plus `candidate-evidence.equity.csv`.
Actual dates, decision counts, unique completed fill identities, elapsed time,
missing days, faults and reliability are derived from durable observations.
Replay produces zero actual prospective decisions. Equity-derived net return,
Sharpe where estimable and nonnegative drawdown magnitude are calculated directly.
The benchmark is an equal-weight, fully funded spot buy-and-hold quote scenario:
initial observed asks plus the frozen fee, then marked at observed bids net of
exit fee. The CSV has daily UTC boundaries for the daily validation interface.

After sufficient actual observations, run the implemented daily validation
command on that CSV and re-export to a new destination with
`--validation-report statistics.json`. The report's `source_sha256` must match the
exact CSV bytes. Optional `--robustness-report robustness.json` and
`--review-report review.json` must carry both `candidate_sha256` and
`equity_sha256`. Robustness must independently establish positive-asset count,
neighbor parameter consistency, round trips, doubled-cost performance and the
95% bootstrap mean-excess lower bound. Review requires an identified reviewer,
`passed=true`, and both bound statistics and robustness artifacts. Reports are
preserved by file SHA. Missing data/metrics/review remain explicit qualification
failures; no default success or synthetic substitute is generated. Finally pass
the exported evidence to the actual `platform_cli qualify` command. It never
authorises live trading.

## Real observations and acceptance status

On 2026-10-16, the credential-free BTC/USD diagnostic obtained exchange timestamp
`2026-10-16T05:09:33.557550Z`, bid 82192.5 and ask 82192.6 with one connection,
three messages, zero stale messages and zero reconnects. A real flat shadow tick
then processed the 2026-10-15 completed history with quote event time
`2026-10-16T05:10:12.197319Z`, bid 82213.8 and ask 82213.9. The public metadata
reported quantity step 1e-8, tick 0.1, minimum BTC 0.00006 and notional USD 0.5.
There were no fills, no order submissions and no faults. This demonstrates the
Spread-event blocker is resolved under the observed feed conditions; it is not
sustained reliability or strategy performance evidence.

The integrated five-asset multiplex tick also passed at
`2026-10-16T05:16:29.844059Z`, with authentic BBO timestamps between
`05:16:27.034124Z` and `05:16:29.279852Z`, one completed decision, zero fills and
zero faults. The earlier sequential-connection experiment safely halted on stale
data; metadata-first multiplex collection fixed that integration failure.

The gate remains at least 30 actual daily decisions, 20 simulated fill lifecycles,
29 days elapsed, >=99.5% daily completion, no >60-second heartbeat gaps, no fault
events, and verified account fees and event timestamps. Accelerated replay never
counts. Account fees remain unverified without a dedicated read-only key. The
diagnostic database is not a production evidence stream and engine changes after
that observation intentionally require a fresh database. No collector is left
running and no real order is authorised by these tools.
