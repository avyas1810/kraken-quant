# Milestone 1: repository audit, trade intents and read-only Kraken

Run `python -m kraken_quant.milestone1 audit` to inspect Git state, required interfaces and credential exclusion without reading notebooks, strategy results or secrets. A tracked `.env`, missing interface or unignored `.env` fails the check. This is an interface inventory, not a security certification.

The existing TWSQ alpha, order, broker and accounting infrastructure remains intact. The shared `StrategyPipeline` and target-weight strategies remain supported. `IntentStrategy` optionally emits immutable, venue-labelled `TradeIntent` objects using the same `StrategyContext`. A trade intent describes a positive base quantity and buy/sell direction. `priced()` translates it into the existing `risk.OrderIntent`; it grants no execution authority. Market freshness, precision, sizing, duplicate prevention and reservation remain the responsibility of the existing risk/execution layers.

Run `python -m kraken_quant.milestone1 connectivity` explicitly to authenticate with dedicated `KRAKEN_READONLY_API_KEY` and `KRAKEN_READONLY_API_SECRET` entries in ignored `.env`. There is no fallback to generic/trading credentials. The existing loader rejects a matching trading key and checks Kraken key permissions before account reads. It allows only query permissions and rejects trading, funding and unknown permissions. The existing read adapter restricts private requests to an explicit read endpoint allowlist. No legacy broker or order socket is instantiated.

The connectivity command checks permission metadata, public instrument identity and authenticated balances. Output contains success flags only; balances, keys and exchange exceptions are suppressed. Provision a separate key with query funds, open trades and closed trades permissions; never enable trading, deposits or withdrawals for it. Authentication alone is not evidence of strategy profitability or live readiness. No live-order enablement is added.

Existing modified/untracked notebooks and `notebooks/environment_snapshots/` are outside this implementation. No locked test-period data is evaluated.
