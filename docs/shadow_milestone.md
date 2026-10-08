# Public market data and durable spot shadow trading

This slice adds `market_data.py`, `shadow.py`, `shadow_cli.py`, mocked tests and
GitHub Actions. All existing TWSQ and research modules remain unchanged.
`ProductionAdapter.submit` remains unimplemented. Repository privacy and history
are unchanged; this milestone is committed locally, not pushed.

## Setup and operation

Use Python 3.10 and `python -m pip install -r requirements-ci.txt` for the complete
test environment. The shadow modules themselves need only existing pandas/numpy
and the Python standard library. No credentials or `.env` loading are needed:
the adapter permits only public OHLC, Ticker and AssetPairs GET requests.

Run one observation with a persistent database **outside the repository**:

```powershell
python -m kraken_quant.shadow_cli --database C:/Users/avyas/shadow-paper.sqlite --start 2026-10-01T00:00:00Z --strategy-id flat-v1
```

The default strategy stays flat. `--symbol` selects a symbol from the central
configuration. `--strategy trusted_module:factory` loads a trusted, zero-argument
factory returning the existing `StrategyPipeline`, including arbitrary custom
features. Such Python plugins are not sandboxed: review them for external data,
network and credential access. Use an immutable code/parameter/feature hash as
`--strategy-id`; change identity or configuration only by starting a new database.
Never use a shadow database as an exchange account or production gateway database.

The CLI runs once and prints structured JSON. Schedule it once per completed UTC
daily bar, retaining the same database. A repeated completed decision is a no-op.
Failures halt the cycle: inspect the database and reconcile paper state before any
operator recovery. There is deliberately no automatic reset or bypass command.
Close connections with `runner.close()` when embedding the runner. The public HTTP
transport and clock are injected; tests use neither real Kraken nor credentials.

## Data and order-intent flow

1. Claim a durable daily cycle transactionally, preventing concurrent/restarted
   workers from replaying the same decision.
2. Request daily OHLC with explicit `since`, interval1440 and `assetVersion=1`.
   Match the exact venue/instrument; discard Kraken's explicitly uncommitted final
   row. Select requested boundaries and reuse `validate_bars`: exact UTC daily
   coverage, unique timestamps, complete bars, finite positive prices, nonnegative
   volume and valid OHLC. Reject faults; do not interpolate or silently deduplicate.
3. Fetch bid/ask and instrument increments/minimums/status. Ticker lacks exchange
   event time: timestamp the **local request start**, reject request durations over
   five seconds and recheck quote freshness after collecting all inputs. This is
   observation time, not proof of order-book event freshness. System UTC must be
   synchronized. Fetch enough data within freshness bounds or halt.
4. Give completed-history copies, paper balances/equity and arbitrary feature
   objects to the same `StrategyPipeline`. The spot `ShadowAdapter` rejects shorts
   and gross weight above1. No exchange logic enters strategies.
5. Convert weights to target quantities at the last completed close. Process sells
   before buys. `RiskGateway` checks precision/minimums, cash, long-only holdings,
   instrument status, duplicate identity, kill state and its existing dollar caps.
   Approved intents are simulated at the observed ask/bid with a configured fee.
   No method calls an exchange trading/funding endpoint.
6. Atomically persist each paper fill, cash/positions and terminal reservation;
   retain reservation identities forever. Persist cycle completion, input-history
   hashes, quotes/rules and structured events. An incomplete cycle or unresolved
   reservation halts on restart, including crashes between approval and accounting.

Only **new observations strictly after CONFIG.data_end** can be requested. The
locked historical reader has no release switch and remains unchanged. This runner
does not backtest, tune, calculate strategy metrics or examine test performance.
The API may return extra rows; they are excluded before any strategy receives them.

## Accounting and risk mathematics

At decision time, let B be paper USD cash, q_i nonnegative base quantity, b_i bid,
a_i ask, C_i last completed close, w_i target weight and f configured fee fraction.
Bid-marked equity is

\[E=B+\sum_i q_i b_i.\]

Target quantity and requested change are

\[q_i^*=w_iE/C_i,\qquad \Delta q_i=q_i^*-q_i.\]

Require w_i >=0 and sum(w_i)<=1. A buy uses p=a_i, a sell p=b_i. Quantity is floored
to exchange step h: Q=h floor(|Delta q_i|/h); limits are tick-rounded by the existing
gateway. The paper fill is permitted only within the approved price bound. With
d=+1 for buys and -1 for sells,

\[F=fQp,\qquad B'=B-dQp-F,\qquad q_i'=q_i+dQ.\]

Example: B=100, q=0, C=100, w=.1, bid100, ask100.1, h=.001 and f=.01. Target and
rounded buy are .1 units, notional10.01, fee.1001. Cash becomes89.8899 and bid-marked
equity99.8899. The .1101 reduction represents spread and fee, not a strategy loss
estimate. Future paper marks can change equity without trades. An oversized target
is rejected rather than automatically split into multiple orders to bypass caps.

Existing caps remain $20/order, $40/asset, $100 total exposure and $2 daily loss.
With E_0 the persisted UTC day-start baseline, current marked loss is L=E_0-E.
The gateway rejects L>=2, or L plus pending/new adverse execution costs >=2.
Pending buys reserve cash including fees and exposure; pending sells never create
buying power before paper accounting. Daily baseline initializes only while flat.
**Held positions crossing a UTC day boundary halt for operator reconciliation**;
this intentionally avoids inventing a midnight equity baseline after downtime.
Continuous overnight operation awaits durable midnight mark/baseline handling.

## Verification and limits

Run `python -m pytest -q tests`. This includes all automated synthetic suites;
`examples/test_kraken_connection.py` is an interactive network example, not a CI test.
CI installs pinned dependencies, runs the full test directory and compiles every
tracked Python source into a temporary bytecode directory. It has read-only GitHub
permissions, no repository secrets and no persisted checkout credentials. Hosted
Actions can run only after the owner later pushes this local commit.

New tests cover completed OHLC, wrong instrument/errors, duplicates/gaps/invalid
values, locked-date rejection before request, request latency, unfinished bars,
metadata normalization, hand accounting, restart/deduplication, shorts/leverage/
order cap, stale quotes, kill/crash halts, UTC rollover, immutable configuration,
CLI with mocked data and forbidden network, and submission refusal. Existing suites
retain daily-loss/exposure/minimum/duplicate and locked-reader tests.

This is a full-fill paper rehearsal, not an exchange execution simulator or evidence
of profitability. Fee default .004 is a disclosed scenario, not an account-specific
Kraken fee claim. No latency fills, partial fills, book depth, additional slippage,
FX, private balances or exchange reconciliation exist. History hashes detect input
changes but do not archive the full market payload; append-only archival and learned
feature provenance are future work. SQLite is for a local supervised process,
not distributed high availability. OHLC REST retains at most720 entries; absent
requested warm-up bars halt, requiring a future archive rather than silent trimming.

Before tiny live trading: midnight/restart recovery and operator tooling, quote
stream event-time/depth capture, private order/trade/balance reconciliation including
UNKNOWN submissions and deduplicated fills, account fee/permission checks, separate
read-only/trading keys without funding rights, monitored kill/dead-man drills,
sustained reviewed shadow evidence, frozen research promotion gates and explicit
user approval. Live transport remains deliberately absent.

Official source schemas:
[OHLC](https://docs.kraken.com/api-reference/market-data/get-ohlc-data),
[Ticker](https://docs.kraken.com/api-reference/market-data/get-ticker-information),
[AssetPairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs).
