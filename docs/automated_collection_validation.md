# Automated collection validation ? 10 November 2026

This report distinguishes verified collection/storage from pending research admission.
It does not certify profitability, a dense price grid, execution research readiness,
or uninterrupted operation through a physically observed reboot/logoff.

## Windows task

`KrakenQuant-OHLCV-6Hourly` has S4U (no stored password), its original six-hour
TimeTrigger, LogonTrigger, BootTrigger (two-minute delay), StartWhenAvailable,
IgnoreNew overlap policy and a two-hour execution limit. Next observed trigger:
10 November 2026, 18:00 Australia/Sydney. The project virtual environment and
local wrapper are retained; code is pinned to an immutable Git worktree.

The post-administrator 9 November run `20261109T000518Z-37147c12` completed with
exit 0: fourteen public OHLC requests, fourteen successful markets, 149 inserted
candles and fourteen matching checkpoints. SQLite integrity passed, foreign-key
errors were zero, frozen partitions and the original DuckDB database were unchanged.
The S4U batch process ran in Windows Session 0. Anonymous Kraken HTTPS works
under this configuration. A physical logged-out/reboot test was not forced.

The expanded 10 November run `20261110T041454Z-f576f939` collected all 64 markets
successfully with zero per-market errors. Its inventory grew from 9,689,626 to
9,733,060 candles (43,434 inserted). The parent was deliberately stopped during
slow research validation; this is NOT reported as a completed successful expanded
task. Windows retained the old read-only validation grandchild and denied
interactive termination. It must finish naturally or be cleaned up by an
administrator before a clean end-to-end research-admission rerun is certified.
Future workers have their own UTC deadline, independent of the scheduled parent
and Windows virtual-environment launcher. No permission bypass is used.

Machine sleep had previously delayed a run by over a day. UTC wall deadlines now
supplement monotonic request budgets; expired budgets are not called network
errors. A sleeping/offline PC cannot continuously collect market data. Missed-run
recovery cannot restore old OHLC via repeated `since` requests; complete public
trade replay is separately bounded and audited.

## Catalogue and actual storage

The latest discovery returned 1,814 source records: 1,431 classified standard
spot records, zero explicitly non-spot exclusions and 383 unresolved class/venue
records. SQLite retains 1,470 historically seen identities; absent markets are not
deleted or arbitrarily marked delisted. Fourteen verified online AUD markets remain.
There are 1,219 verified active candidates across AUD, USD, USDT, USDC and EUR.
Sixty-four are enrolled: 14 AUD, 27 USD, 17 EUR, 4 USDT and 2 USDC.

Fifty newly enrolled non-AUD pairs have actual June?September 2026 archive data,
with source-native filename identity checks, SHA-256, ZIP CRC, raw slices and
immutable import receipts. This is four months, NOT a claim of one/two years
of populated history. Routine archive work and public-trade repair yield to
incremental collection under explicit budgets.

Approved original source: `Kraken_OHLCVT_Full.zip`, 9,546,553,626 bytes.
SHA-256: `bc273e9027470b07d790a7fa4df9bb43f0dfc527c20c6ecae48402e123e7b1c7`.
This existing approved archive was reused rather than downloaded again.

After the separate AUD repair pass there are **9,734,186 genuine OHLCV rows**
across 64 pairs. Earliest UTC: **2020-06-16T21:40:00Z**; latest UTC:
**2026-11-10T04:23:00Z**. All 64 checkpoints match actual persisted maxima;
SQLite integrity is `ok`, foreign-key errors zero. Existing store validation
passed for 1,162 partitions. Quote rows: 18,996; order-book snapshots: 18,996;
order-book level rows: 191,896. No calculated cost records exist.

## Gaps and research scope

All original 28 warning intervals across 14 AUD markets were replayed through
Kraken public Trades: 4,638 observed traded minutes, 54,197 verified no-trade
minutes, zero unknown intervals within those requested windows. Thirteen additional
AUD windows after sleep were replayed and independently admitted by M12:
1,181 observed intervals and 10,822 verified no-trade intervals, zero unknowns.
These are interval classification counts, not all newly inserted rows; overlap
is deduplicated. No absent candle alone is proof of no trading. Proof knowledge
times are actual receipts, preserving historical decision-time exclusion.

The queue retains historical evidence. Of 100 queued windows, 87 non-AUD windows
are still pending, spanning parts of 30 September?9 November 2026. They are
`MISSING_OR_UNVERIFIED`, not delisted, fabricated no-trade, or proven unrecoverable.
They exceed current OHLC retention. Public Trades may recover them; bounded jobs
preserve progress and reasons. Their individual ranges are stored locally in
`data/automated_collection/verification/pending-gap-windows.csv` and the JSON queue.
All queued AUD windows are complete, but this does not certify every historical
AUD minute outside those audited windows.

M13 is a distinct admission gate: the original 64-market FIXED_WINDOW request is
retained, and a separately disclosed count-qualified proposal is checked jointly.
No assets are silently dropped from the original request. Study dates remain
[2026-07-01, 2026-10-01) UTC, hourly continuity grid over actual minute observations,
EVENT_TIME, complete-source evidence required, verified no-trade allowed, 60/20/20
calendar proportions, minimum phase observations 1000/250/250 and durations
30/10/10 days. This is not STRICT_GRID or received-at-the-time online readiness.
The target of fifty jointly admitted markets remains **unverified** while the
real validation worker is pending. Candidate/enrollment counts are not admission.
Existing frozen plans are not modified; no strategy-performance evaluation occurs.

## Audit and viewer

The original viewer database and all three original immutable partition manifests
match their baseline SHA-256 values. User notebook changes remain outside commits.
A separate read-only expanded catalogue is served at `http://localhost:4214`;
HTTP returned 200 and SQL verified all 64 pairs/9,734,186 rows, including BTC-USD
and ETH-EUR. The original viewer at port 4213 is unchanged. New viewer SQL remains
local and is not canonical market storage.

Local evidence (ignored by Git):
- `.shadow-runtime/automated-final-audit.json`
- `.shadow-runtime/automated-parquet-audit.json`
- `.shadow-runtime/gap-continuity-audit.json`
- `.shadow-runtime/resume-gap-continuity-audit.json`
- `data/automated_collection/runs/` and public source receipts
- `data/automated_collection/verification/market-inventory.csv`

Focused regression: 71 passed. Full regression and final-head GitHub Actions
results are reported in the PR. An earlier root pytest run discovered a pre-existing
interactive account example and attempted read-only authentication (InvalidNonce).
No orders were submitted or secrets printed. Default pytest discovery is now
limited to the offline tests directory; scheduled collection uses public endpoints.

## Remaining acceptance work

- Clean exit-0 expanded task and joint 50-market M13 admission after old-worker cleanup.
- Resolve the 87 pending non-AUD windows; no full-to-present completeness claim.
- Optional physical logged-out reboot verification by the operator.
- More historical archive months for new markets if a longer study is requested.
- Do not merge or infer live-trading permission from this infrastructure.
