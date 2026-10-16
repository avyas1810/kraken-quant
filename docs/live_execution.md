# Kraken live execution: operator reference

## Status and security boundary

`kraken_quant.live` implements a durable Kraken spot order lifecycle. It is disabled by default. No real trading, funding transfer or withdrawal has been authorised by the completion mandate. No private live order was sent while developing or testing these modules. Tests use a deterministic exchange and an in-memory HTTP endpoint that verifies HMAC signatures.

The current research candidate fails qualification. Custom candidate specifications must include `factory` (or a `module:function` strategy), optional `feature_factory`, explicit `seed` for stochastic factories, and `source_code_hashes` for every strategy/feature module and its relevant dependencies. Built-in `btc_momentum`/`multi_momentum` names resolve to their existing repository factories. Live execution refuses missing or changed frozen source. A tested execution implementation does not qualify a strategy or establish profitable live performance. Actual account acceptance requires dedicated credentials, genuine prospective observations, independently reviewed qualification and a separate operator authorisation.

Supported execution is deliberately narrow: the configured five Kraken USD spot assets, long-only funded balances, price-bounded limit IOC orders, one unsettled order at a time, and fees requested in quote currency (`fciq`). No margin, borrowing, conditional orders, account transfers or funding endpoints exist. IOC limits minimise standing-order exposure. Partial fills are supported; unresolved or UNKNOWN intents block new orders.

## Components

- `live.py / KrakenTransport`: .env-only separate trading credentials; exact permission allowlist; private REST endpoint allowlist; SHA256 API-key fingerprint; HMAC-SHA512 signatures; persisted increasing nonce; >=1.1 seconds between requests; 15-second HTTP timeout; no HTTP retries; redacted exception codes. Public market metadata uses credential-free ccxt. Use one process/database per key and do not share the key with another program.
- `live.py / LiveEngine`: exclusive `ProcessLock`; existing `RiskGateway` and Decimal spot models; durable intents and deterministic UUID client IDs; persistent SUBMITTING/UNKNOWN/acknowledged/exchange terminal states; audit event codes; persistent daily-loss baseline; emergency local kill; reconciliation; cancel and dead-man control.
- `live_sim.py / SimulatedKraken`: Kraken-shaped order/trade/balance responses; success, partial-open and partial-canceled fills, rejection, insufficient funds, timeout before acceptance, lost acknowledgement, cancellations and dead-man expiry. No network connection.
- `live_strategy.py / FrozenLiveStrategy`: verifies the hash-sealed candidate, all frozen source files, explicit strategy/feature factories, seed and parameters; invokes the same causal `StrategyPipeline` used in research; converts long-only target weights into candidate-bound deterministic daily order IDs and bounded order intents. The engine only accepts real submissions through this verified runner and enforces any stricter frozen exposure/position/daily-loss limits.
- `live_cli.py`: local inspection/kill, account observation, real gated execution, bounded monitoring and simulation demo. Credentials and exchange responses are not printed; balances and order identifiers are private account data and should remain local.

## Durable recovery and accounting

Immediately before the actual HTTP send, after throttle and nonce generation, the transport rechecks approval expiry and invokes the engine guard to recheck the persistent kill and quote/account/rule ages. A slow dead-man request cannot turn a fresh risk decision into a stale wire submission. A blocked local send is recorded as not submitted, halted and reconciled before reservation release.

Before a submission, complete read-only reconciliation establishes current balances and a stable complete history window. Historical pages must retain the same count and contain no repeated IDs. A second balance/open-order observation must match the first; otherwise the process halts. Initialisation requires a dedicated account with no active orders or trades within the origin window.

Every intent is risk-reserved before its client UUID and SUBMITTING state are persisted. A crash in the narrow gap leaves an orphan reservation and halts on restart. A crash after persistence becomes UNKNOWN. Neither case resubmits automatically. AddOrder is called once only. A definitive AddOrder rejection can release the reservation only after successful reconciliation; a failed dead-man request remains conservatively UNKNOWN.

Reconciliation locates lost acknowledgements using `cl_ord_id` across open and paginated closed orders. It binds instrument aliases, side, quantity, limit and exchange ID, deduplicates immutable exchange trade IDs, checks execution limits and quantity totals, applies actual reported trade cost and fee, and compares resulting balances with the origin account. Reported cost rounding is bounded by the precision of the exchange-reported cost. Quote-fee charges above the reserved fee budget halt. Use a conservative fee fraction above the observed taker fee to budget rounding. Unknown fee currencies, external orders, external balance changes and unsupported assets fail closed rather than being silently rebased.

Reservations remain conservative through partial fills and are released only when a terminal order, its fills and balances reconcile. Missing known orders, mutated/disappearing fills, incomplete history or changed balances halt. UNKNOWN absent from complete history remains reserved: contact Kraken/independently investigate instead of guessing that submission failed. Historical identity is retained after terminal completion so the same strategy decision cannot be traded again.

Default risk limits remain the existing $20/order, $40/asset, $100 total exposure, $2 daily loss, quotes <=5 seconds old and account snapshots <=60 seconds old. Real submission uses public metadata for quantity step, price tick, minimums and market status. The CLI refreshes genuine WebSocket BBO event-time quotes after slower account/fee requests; timestamps are never manufactured. The current day's baseline is frozen. On a new UTC day, record a genuine midnight equity mark with `day-baseline`; a restart never resets a loss baseline automatically.

## Safe, executable local commands

Run from the repository root after installing `requirements-ci.txt` and `requirements-research.txt`.

```powershell
python -m pytest tests/test_live_execution.py -q
python -m kraken_quant.live_cli --help
```

Expected: 30 passing focused tests (additional tests may be added), and the implemented command options.

Use a new database name for each demonstration. Existing evidence is never overwritten:

```powershell
python -m kraken_quant.live_cli simulate --database .shadow-runtime/live-demo-new.sqlite
python -m kraken_quant.live_cli inspect --database .shadow-runtime/live-demo-new.sqlite
python -m kraken_quant.live_cli kill --database .shadow-runtime/live-demo-new.sqlite
```

Expected demo: `mode=SIMULATION_ONLY`, `real_order_submissions=0`, `simulated_submissions=2`, `fills=2`, one canceled partial order and one closed lost-acknowledgement order, USD cash `984.94000`, BTC `0.15`, and `prospective_daily_decisions=0`. Restart and fill deduplication are exercised. Kill subsequently reports `halted=true`. The same commands work in Linux shells.

## Credentials and actual account verification

The ignored `.env` file must contain distinct `KRAKEN_READONLY_API_KEY`/`KRAKEN_READONLY_API_SECRET` and `KRAKEN_TRADING_API_KEY`/`KRAKEN_TRADING_API_SECRET`. This loader reads the explicit file and does not silently fall back to process environment. Never print the file or commit it.

The dedicated trading key must have exactly `query-funds`, `query-open-trades`, `query-closed-trades`, `modify-trades`, `close-trades`. Funding, withdrawal, deposit or unknown permissions are rejected. Restricted query windows and expired keys are rejected. The separate read-only verification procedures remain in the operations manual. Verify legal account identity and account ownership in Kraken itself; an API-key fingerprint is a key identity, not a legal identity.

The following implemented commands require actual credentials and network access, but do not enable real submission:

```powershell
python -m kraken_quant.live_cli account-check --database .shadow-runtime/live.sqlite --env-file .env
python -m kraken_quant.live_cli reconcile --database .shadow-runtime/live.sqlite --env-file .env
python -m kraken_quant.live_cli resume --database .shadow-runtime/live.sqlite --env-file .env
```

Expected account-check: current local account mirror, `account_fingerprint`, safe `verified_permissions`, and no order submission. Resume first reconciles and refuses unresolved UNKNOWN/orphan state. A stable mirror must be established before any enablement.

## Separate approval and immutable evidence

Approval and evidence files are operator-owned private runtime artifacts, never automatically generated by this CLI. The completion request does not constitute them. Qualification is recomputed from the reviewed files each time real mutation is attempted.

The eligibility JSON contains:

- `artifact_directory`: an absolute directory containing reviewed evidence files.
- `candidate_file`, `candidate_file_sha256`: frozen candidate file path and SHA256 of its raw bytes.
- `candidate_sha256`: `read_candidate(candidate_file)['sha256']`, the canonical frozen specification hash (distinct from the raw file hash).
- `qualification_file`, `qualification_sha256`: path and raw-byte SHA256 of the qualification report.
- `shadow_export_file`, `shadow_export_sha256`: path and raw-byte SHA256 of the prospective evidence JSON accepted by `qualify_candidate`.
- `qualified=true`, `reliability_gate_passed=true`, `actual_daily_decisions>=30`, `simulated_order_lifecycles>=20`, `elapsed_seconds>=2505600`, `completion_fraction>=0.995`, `fault_events=0`, `missing_days=0`, `evidence_type=prospective_public_feed`.

The prospective file must satisfy the unchanged candidate requirements: actual dated consecutive daily decisions after preregistration, distinct completed lifecycle IDs, return/drawdown/relative/cross-asset/parameter/round-trip/stress/bootstrap/reliability and independent-review gates. Counts and candidate identity must agree with the eligibility summary. Replay is rejected. The stored qualification report must exactly equal a fresh `qualify_candidate` computation and be qualified for review with no failure reasons. Artifact paths and hashes bind content, not independent truth: the operator/reviewer must verify provenance and authenticity; a locally authored JSON is not exchange-certified evidence.

The approval JSON contains `action=AUTHORISE_KRAKEN_SPOT_LIVE_ORDERS`, a nonempty `operator`, `account_fingerprint`, the same `candidate_sha256`, `evidence_sha256` (SHA256 of `canonical_json(eligibility)`), `approved_at` and `expires_at` in timezone-aware ISO format. The authorisation window must be active and no longer than 24 hours. It is an explicit operator control, not a third-party digital signature. File permissions and review protect it. No approval is included in this repository.

After separately obtaining valid approval and evidence, this read-only validation command is available:

```powershell
python -m kraken_quant.live_cli validate-approval --approval .shadow-runtime/live-approval.json --eligibility .shadow-runtime/live-eligibility.json --account-fingerprint <verified-key-fingerprint>
```

Replace the fingerprint with the account-check result. Expected only when every requirement passes: `approval_valid=true`, `orders_submitted=0`. No current project evidence qualifies.

## Gated live commands: not authorised during this iteration

The following interfaces are implemented and covered using simulated inputs. Their actual account outcomes are unverified until credentials, qualified evidence and separate authorisation exist.

Use `strategy-once` for real candidate execution. It loads the candidate named in reviewed eligibility, verifies its source hashes, selects its frozen factory/parameters, loads completed public daily bars strictly after the locked data boundary, reconciles the dedicated account, obtains genuine BBO events and runs the shared strategy interface. By default it executes at most one unprocessed candidate/asset/day decision per call. `--max-orders` may be set from 1 through the frozen universe size to process remaining assets sequentially: sells first, then buys, with reconciliation and a fresh account/BBO before each order. UNKNOWN or any unsettled lifecycle stops the rebalance. Output includes `remaining_rebalances` and `residual_adjustments`; a bounded $20 adjustment may leave a target residual until a later daily decision, and rerunning never trades the same asset/day twice. IDs retain the frozen candidate and UTC decision date. Target adjustments are bounded to $20 per order; all global and stricter frozen risk limits still apply. Observed taker fees include a conservative fraction buffer of 0.0001 for rounding. No parameter override or manual intent is permitted for real execution. The legacy `submit --intent` interface cannot bypass the frozen runner and fails closed for real transport.

```powershell
python -m kraken_quant.live_cli strategy-once --database .shadow-runtime/live.sqlite --env-file .env --history-start 2026-10-01T00:00:00Z --max-orders 5 --day-start-equity 1000 --approval .shadow-runtime/live-approval.json --eligibility .shadow-runtime/live-eligibility.json --enable-live-orders
python -m kraken_quant.live_cli cancel --database .shadow-runtime/live.sqlite --env-file .env --order-id <existing-local-order-id> --approval .shadow-runtime/live-approval.json --eligibility .shadow-runtime/live-eligibility.json --enable-live-orders
python -m kraken_quant.live_cli watch --database .shadow-runtime/live.sqlite --env-file .env --seconds 60 --approval .shadow-runtime/live-approval.json --eligibility .shadow-runtime/live-eligibility.json --enable-live-orders
python -m kraken_quant.live_cli deadman --database .shadow-runtime/live.sqlite --env-file .env --approval .shadow-runtime/live-approval.json --eligibility .shadow-runtime/live-eligibility.json --enable-live-orders
```

`1000` is an example of an independently recorded daily equity mark, not a value to copy for another account. Replace the existing order ID with the inspected local ID. Strategy-once reconciles before risk reservation and after the acknowledgement. Cancellation does not assume there were no fills; it reconciles before release. Watch is bounded to 1..3600 seconds, reconciles and refreshes the 60-second exchange dead-man timer approximately every 20 seconds while healthy. A network failure, expired approval, persistent kill or reconciliation failure halts locally. Watch checks the persistent kill before refreshing the exchange timer; it cannot keep a killed process alive remotely. The exchange timer remains armed on abnormal shutdown and cancels remaining open orders. All submissions are IOC even without a separate watch process.

To record an independently observed UTC-midnight equity for a later day:

```powershell
python -m kraken_quant.live_cli day-baseline --database .shadow-runtime/live.sqlite --day-start-equity <recorded-equity> --observed-at <UTC-midnight-ISO-timestamp>
```

This refuses to overwrite the already frozen current-day baseline. It does not manufacture an observation.

## Emergency actions and recovery

1. Run `python -m kraken_quant.live_cli kill --database .shadow-runtime/live.sqlite`. This is entirely local, requires no credentials, persists the kill and blocks new risk reservations. Its atomic database write deliberately bypasses the order-writer ProcessLock, so it remains available while a writer/watch process is running.
2. Inspect orders with the local inspect command. Stop the supervised strategy process. If its process lock is held, stop it first; the OS releases the advisory lock on exit/crash. Do not run concurrent order writers or delete lock/database files to bypass protection.
3. Cancel known open orders through the separately authorised cancel interface or directly in Kraken's trusted UI. Local kill alone does not promise exchange cancellation. The 60-second exchange dead-man timer is the independent safety backstop when armed.
4. Run read-only reconcile with working credentials. Do not release UNKNOWN reservations manually or create a new database to evade them. Independently investigate an absent ambiguous order through Kraken support/history.
5. Resolve external balance/history discrepancies and unsupported assets outside the automated process, preserving audit evidence. Do not rebase a database with active/UNKNOWN orders.
6. Resume only after successful reconciliation and independent resolution. Approval must still be current before any real mutation.

For database lock errors, stop competing processes before retrying read-only observation. For stale quotes, verify system UTC clock, WebSocket availability, event timestamps and market status; never replace event time with local now or increase the risk freshness threshold. For API rate/errors, this adapter throttles but deliberately does not blindly retry writes. Reduce process contention and investigate the exchange status. Restore a consistent private runtime database from a verified backup only with stopped processes, then reconcile before resuming; a missing live ledger is not a reason to submit again.

## Validation and primary specifications

Focused tests cover success, restart/idempotence, partial fills/cancellation, explicit rejection, timeout before acceptance, lost acknowledgement, UNKNOWN blocking, kill/funds/order/exposure/loss/stale rejection, process exclusion, concurrent-writer emergency kill, orphan crash recovery, dead-man expiry, delayed-deadman and post-throttle stale-quote prevention, real metadata observation-time retention/refresh, frozen shared-strategy-to-fill integration, future-history/source-change rejection, immutable daily baseline, funding permission rejection, increasing nonces, HMAC signatures, redaction, evidence-bound qualification, accelerated-evidence rejection and CLI simulation/inspection/kill. An integrated in-memory HTTP test verifies signed requests through partial fills and real engine reconciliation. Separate approval is mocked in that simulated transport test; evidence/default-disable boundaries are tested independently. Tests and accelerated demonstration are engineering evidence only.

Primary Kraken documentation checked during implementation:

- https://docs.kraken.com/exchange/guides/rest/authentication
- https://docs.kraken.com/api-reference/trading/add-order
- https://docs.kraken.com/api-reference/trading/cancel-all-orders-after-x
- https://docs.kraken.com/api-reference/account-data/get-api-key-info
- https://docs.kraken.com/api-reference/account-data/get-closed-orders
- https://docs.kraken.com/api-reference/account-data/get-trades-history

No live readiness claim substitutes for real credential/account verification, fresh prospective qualification, approved recipients/supervision, or explicit first-order approval.
