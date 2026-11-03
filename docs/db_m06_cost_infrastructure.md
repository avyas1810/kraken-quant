# Database Milestone 6: transaction-cost input infrastructure

**All mathematical cost algorithms are reserved for user implementation after Milestone 10.**

This milestone prepares auditable raw inputs. It does not provide an operational mathematical cost model. There are no spread, book-walking, slippage, market-impact, VWAP, total-cost, calibration, fee-tier-selection or execution-optimization algorithms.

## Verified base and reuse

PR #6 was verified actually merged into `review-base/milestone1-20261029` at `2e4f65c4190501f827c9a701f9a51d2f768665b6`; merge ancestry was verified before branching. Milestones 1?5, AGENTS.md, SQLite/Arrow schemas, monthly storage, catalogue discovery, OHLCV/book collectors and read-only authentication were inspected.

Existing `collection_universe`, validated `connect_metadata`, `MonthlyParquetStore`, collector parsing and source receipts, configuration, atomic JSON writer and process locks are reused. No collector or market-data dataset is duplicated. Public transport gains only a fixed official fee-document URL and opt-in exact JSON numeric parsing; existing callers retain their defaults. Daily research bars, research configuration, execution, credentials and existing Parquet schemas are unchanged.

## Official fee acquisition

Sources:

- `GET https://api.kraken.com/0/public/AssetPairs`, requested with stored native pair IDs, `assetVersion=1`, `info=fees`. Original complete response and pair-specific maker/taker threshold arrays are retained. Missing arrays remain unknown and empty arrays remain empty.
- `GET https://www.kraken.com/features/fee-schedule`, using a standard browser User-Agent through the existing public transport. Original HTML is retained. Table categories come from source DOM identifiers, not inferred asset classifications. Every parsed maker/taker table retains every row and every original header/value condition, including alternate qualification measures. Percentage text is parsed as exact decimal percentage points; no conversion to monetary costs or fractions occurs. Signed maker rebates are preserved.
- Optional authenticated `POST /0/private/TradeVolume`, using the existing dedicated `.env` read-only adapter and its permission gate. It returns observed account fee fields separately from the public tier schedule. No order, balance, amendment, cancellation or funding request is required by this component.

References: [AssetPairs](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs), [official schedule](https://www.kraken.com/features/fee-schedule), [TradeVolume](https://docs.kraken.com/api-reference/account-data/get-trade-volume).

The existing adapter verifies `query-funds`, `query-open-trades`, and `query-closed-trades`, permits only its known read permissions, and rejects unsafe/unknown permissions before further private queries. Those are the repository's conservative adapter requirements; this milestone does not loosen them or claim they are the server's minimum TradeVolume permissions. The dedicated key is never shared with trading. A monotone local nonce file is isolated from execution databases. `--public-only` deliberately omits authentication. Authentication failures retain a distinct unavailable status and never fall back to an assumed account volume or zero fee.

Public/API schedules do not establish account applicability. No lowest tier, optimal tier, maker/taker choice or category is selected. Where AssetPairs omits pair-specific tiers, all public table facts are exposed without automatically assigning a category to a pair. Old campaigns and product-specific tables remain source facts with their original document context, not recommended/applicable rates. In particular, a retained zero-fee row is never a default fee. Account rates, if actually returned and validated, are kept separately; full private payloads remain local and are excluded from public reports.

## Fee storage and time semantics

`data/cost_infrastructure/fees/<sha256>.json` contains contract version 1, source namespace, source URLs, full public payload/HTML, all parsed categories and conditions, canonical/native pair mapping, retrieval timestamp, unknown effective timestamp, authenticated availability and optional complete private TradeVolume result. Fees are not stored in a new market database. All generated files remain ignored.

The identifier hashes canonical content including retrieval time. Historical versions are never overwritten. Reads verify the hash and version; publication uses the existing temporary-file/atomic-replace helper under an advisory writer lock. A failed publication cannot expose a partially written object. Private account volume/fingerprint is not emitted by `fees report` or committed to Git. Fee rates are never defaulted to zero.

Retrieval time and effective time are separate. An observed schedule with unknown effective time does not prove historical applicability. Historical preparation selects only fee observations known by the request time. If none exists, it reports `historical_fee_unavailable`. The operator can explicitly enable `allow_present_day_fee_assumption`; the selected version, actual retrieval time and assumption warning are then recorded. This is not a historical fee reconstruction. Freshness limits are explicit configuration, not financial model parameters. Stale schedules are reported distinctly.

## Immutable interfaces

`cost_contracts.py` defines frozen dataclasses:

- `CostRequest`: canonical pair, buy/sell, exact positive finite Decimal AUD notional, maker/taker request type, aware UTC observation time, model version. Pair existence/AUD identity is checked by the preparation service. Requests do not imply quantities or executable orders.
- `FeeTier`, `FeeTable`, `FeeSchedule`: complete source conditions, percentage-point rates, public categories, version, timestamps, optional account observations and applicability limitations.
- `QuoteSnapshot`: bid/ask, optional exact quantities, native identity, local observation time and source reference.
- `OrderBookSnapshot` / `BookLevel`: all observed levels, both sides, requested/returned depths, source completeness label and exchange-level last-update epochs.
- `CandleSnapshot`: unmodified completed OHLCV fields and source reference.
- `MarketMetadata`: native/canonical identities, last observation, minimum quantity/notional and quantity/price precision. Unknown constraints remain NULL; decimal precision is not represented as a tick/quantity increment.
- `InputProvenance`, `PreparationConfig`, `CostModelInputs`, `PreparedInputs`: immutable assembly, freshness policy, source namespace, dependency hashes, assumptions and structured missing-input/warning lists.
- `CostResult`: request identity, observation/model version, provenance, optional fee/half-spread/slippage/total percentages, fillability, filled/unfilled quantities, execution price and calculation status. All computed values must remain None and fillability unknown in this milestone.

Example sizes A$100/A$1,000/A$5,000 are request examples, configurable through `--sizes-aud`; they are not fixed mathematical parameters. No trade quantities or costs are computed.

## Input lookup and provenance

Preparation is offline and deterministic for fixed dependencies:

1. Validate the catalogue and requested AUD market; capture exact small metadata. Metadata observed after the historical request is explicitly unavailable for that request.
2. Locate a persisted quote at/before request time and a complete book with exactly the same observation timestamp.
3. Require matching source response identifiers and exact quote/book top-level consistency. Validate all book levels with the existing collector validator.
4. Reparse the original public response, verify exact persisted values, and require response receipt time at/before the historical request. A request-start timestamp alone does not authorize use of a response received later.
5. Locate the latest completed OHLCV candle whose closing time is at/before the request, and verify its existing collector receipt. No aggregation, averaging or zero filling occurs.
6. Attach the full observed fee facts and explicit applicability/freshness warnings.
7. Return `inputs_ready` or `inputs_incomplete` with structured codes. `inputs_ready` means raw inputs are assembled, not that a fee tier is known, an algorithm exists or a trade is executable.

Quote/book receipt references preserve endpoint, response ID/hash, observation timestamp semantics and measured collection latency. Books retain the source's requested/actual depths, snapshot label and all available level-update epochs. REST has no trustworthy whole-snapshot event timestamp. OHLCV receipts did not record request latency in Milestone 4, so that field remains unknown, not invented. Source-provenance namespaces prevent test fixtures from being used as real exchange inputs.

Missing/stale quote, book, OHLCV, fees, constraints/provenance, mismatched timestamps/values and future observations are reported. Unknown account tier/effective time/category are explicit warnings. No research eligibility or collection readiness is inferred. Persisting incomplete preparation appends idempotent, open `costs_1m` input-quality issues to existing `data_quality_issues`; no calculated costs or market rows are written. Existing issues are never automatically resolved or deleted.

## Input manifests and reproducibility

`data/cost_infrastructure/inputs/<sha256>.json` is an immutable version-1 manifest holding the request, small observed metadata snapshot, policy, status, warnings and references. Quote/book/candle arrays are not copied into every manifest. Each reference contains its logical timestamp, exact selected-row content hash, source response path/hash and semantics. The fee source is pinned by immutable version.

`inputs load --manifest <id>` reads those pinned records, validates dependencies and reconstructs the original bundle. It never selects newer observations or refreshes fees. Monthly Parquet append/replacement does not invalidate unchanged logical records. Changed/missing records, fee objects or source receipts produce explicit dependency failures rather than substitutions. Metadata is copied because SQL assets are updated in place; its content hash is validated. Source parse caching is keyed by the full receipt content hash, so changed source bytes cannot reuse stale parsing.

Reproducibility requires retaining referenced Parquet rows, source receipts and fee versions. Local deletion/correction can make a manifest unavailable; no backup/archive service is claimed. Data/schema contract version is independent of the requested future model version. Immutable object publication and SQLite quality appends are separate operations, not a distributed transaction; retrying is idempotent.

## Reserved algorithm interfaces

`CostAlgorithms` declares typed `CostModelInputs -> CostResult` methods:

| Method | Status |
|---|---|
| calculate_spread_cost | NOT_IMPLEMENTED |
| calculate_book_execution | NOT_IMPLEMENTED |
| calculate_slippage | NOT_IMPLEMENTED |
| calculate_market_impact | NOT_IMPLEMENTED |
| calculate_total_cost | NOT_IMPLEMENTED |

Every method raises `NotImplementedError("Algorithm reserved for user implementation after Milestone 10")`. Tests exercise each exception and inspect that each method contains only the raise. No textbook formula, simplified assumption, zero result, book walk or mocked production calculation is provided. Arithmetic in infrastructure concerns timestamps, bounds and identifiers only.

Calculation statuses are `inputs_ready`, `inputs_incomplete`, `algorithm_not_implemented`, `calculated`, `calculation_failed`. `calculated` is reserved for the future contract and rejected by current result construction. Any computed result field or known fillability is also rejected. Prepared-input contracts can only claim the two input statuses. There is no result writer.

## Existing costs_1m compatibility

The schema remains version 1 and is not migrated. Future mapping:

| Existing column | Future result source |
|---|---|
| pair_id | request.pair_id |
| timestamp | request.observation_time |
| side | request.side |
| order_size_aud | request.notional_aud |
| fee_type | request.execution_type |
| fee_pct | result.fee_pct |
| half_spread_pct | result.half_spread_pct |
| slippage_pct | result.additional_slippage_pct |
| total_cost_pct | result.total_cost_pct |
| model_version | request.model_version |

The existing schema cannot represent fillability, filled/unfilled quantities, execution price, calculation status or complete provenance links. Proposed future extension: an explicitly migrated result metadata sidecar/table keyed by full result identity, with those fields and an input-manifest reference. No extension is implemented here. Existing schema total/component checks remain untouched; no component or total is calculated or written in Milestone 6. Model algorithm design remains reserved through Milestones 6?10.

## Commands

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.cost_infrastructure fees refresh
.venv-db/Scripts/python.exe -m kraken_quant.cost_infrastructure fees refresh --public-only
.venv-db/Scripts/python.exe -m kraken_quant.cost_infrastructure fees report
.venv-db/Scripts/python.exe -m kraken_quant.cost_infrastructure inputs prepare --pair BTC-AUD --side buy --notional-aud 1000 --execution-type taker
.venv-db/Scripts/python.exe -m kraken_quant.cost_infrastructure inputs report --all-aud --sizes-aud 100 1000 5000
.venv-db/Scripts/python.exe -m kraken_quant.cost_infrastructure inputs load --manifest <sha256>
.venv-db/Scripts/python.exe -m kraken_quant.cost_infrastructure algorithms status
```

Established `--root`/`--data-dir` configuration is reused. Historical requests use `--observation-time <UTC ISO timestamp>`. The explicit assumption flag is `--allow-present-day-fee-assumption`; it is never silently enabled. Freshness flags are `--max-market-age-seconds`, `--max-candle-age-seconds`, `--max-fee-age-seconds`. Reports/status/load perform no network requests or writes. `inputs prepare` writes only its manifest and missing-input quality records, and returns nonzero for incomplete inputs. Algorithms status exits successfully while reporting all components unavailable.

## Actual Kraken audit

Actual Kraken fee sources were retrieved on **2026-11-03**. The final fee snapshot version is `62e29fa6aba2d4b477399b77643c0e903602e4b35f5ec162ac34ca45c60f0bba`. AssetPairs returned empty maker/taker arrays for all **14** requested markets. The official page was successfully retained and parsed into **8 source-category tables / 88 complete tier rows**, including alternate qualification columns. All source tables remain unassigned to account/pair applicability; no tier is selected. The initial and final fee versions are both retained.

The configured dedicated read-only adapter was attempted and failed during its authenticated request. Final status: **unavailable_readonly_adapter_auth_or_permissions**. No account-applicable maker/taker rate or volume is assumed. Credentials were neither printed nor changed. Tests separately validate successful guarded TradeVolume acquisition and private report redaction.

Existing market inputs were observed on 2026-11-01. Actual historical preparation as-of **2026-11-01T09:46:47.662510+00:00** was performed for every AUD pair. Strict preparation correctly reports `historical_fee_unavailable` for all 14: today's schedule was not known at those observations. With the explicit recorded present-day fee assumption, all 14 bundles are structurally ready. This is raw-input readiness only, not historically verified fees or a cost model. Current-time preparation correctly reports stale market inputs rather than inventing updates.

### Actual all-AUD availability

| Pair | Quote | Book | Public fee source | Strict historical bundle | Explicit-assumption bundle | Missing strict input |
|---|---|---|---|---|---|---|
| ADA-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| AUDX-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| BCH-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| BTC-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| DOGE-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| ETH-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| EUR-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| LINK-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| LTC-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| PEPE-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| SOL-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| USDC-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| USDT-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |
| XRP-AUD | True | True | Official page + retained AssetPairs | inputs_incomplete | inputs_ready | historical_fee_unavailable |

Every explicit-assumption bundle warns that account tier, automatic pair category and effective time are unknown. No fee is silently treated as applicable. All 383 unresolved catalogue records remain unchanged.

### Actual BTC/AUD and ETH/AUD request validation

#### BTC-AUD

Native pair: `XBTAUD`. Quote/book timestamp: `2026-11-01T09:46:22.652593Z`. Source: `https://api.kraken.com/0/public/Depth`. Response ID: `book_collection/responses/BTC-AUD/79ae103ea27b41ad8b1f2c806ec71360.json`; response hash `c9c11650a5d63f6be6231e5a46245a6e3e34a684d92be416c86ff8a52571cec5`. Collection latency: **0.383752 seconds**. Semantics: `local_request_start_UTC`. Requested/returned depth: **10 / 10 bid / 10 ask**. Completeness: `complete_returned_L2_top_depth`. All source level-update epochs are preserved in the loaded immutable book and original receipt.

| Bid | Ask | Bid size | Ask size |
|---|---|---|---|
| 110916.200000000000000000 | 110953.900000000000000000 | 0.001000000000000000 | 0.001000000000000000 |

Complete real returned snapshot; prices AUD/base and quantities base units:

| Side | Level | Price | Quantity |
|---|---|---|---|
| ask | 1 | 110953.900000000000000000 | 0.001000000000000000 |
| ask | 2 | 110954.000000000000000000 | 0.010000000000000000 |
| ask | 3 | 110965.000000000000000000 | 0.019000000000000000 |
| ask | 4 | 110987.200000000000000000 | 0.320000000000000000 |
| ask | 5 | 111014.800000000000000000 | 0.011000000000000000 |
| ask | 6 | 111014.900000000000000000 | 0.038000000000000000 |
| ask | 7 | 111053.500000000000000000 | 0.026000000000000000 |
| ask | 8 | 111053.600000000000000000 | 0.100000000000000000 |
| ask | 9 | 111084.300000000000000000 | 0.038000000000000000 |
| ask | 10 | 111150.400000000000000000 | 1.487000000000000000 |
| bid | 1 | 110916.200000000000000000 | 0.001000000000000000 |
| bid | 2 | 110916.100000000000000000 | 0.001000000000000000 |
| bid | 3 | 110916.000000000000000000 | 0.790000000000000000 |
| bid | 4 | 110915.700000000000000000 | 0.083000000000000000 |
| bid | 5 | 110909.000000000000000000 | 0.028000000000000000 |
| bid | 6 | 110895.800000000000000000 | 0.007000000000000000 |
| bid | 7 | 110895.200000000000000000 | 0.010000000000000000 |
| bid | 8 | 110836.000000000000000000 | 0.067000000000000000 |
| bid | 9 | 110835.900000000000000000 | 0.030000000000000000 |
| bid | 10 | 110833.000000000000000000 | 0.026000000000000000 |

Observed exchange constraints (not order increments):

| Minimum qty | Minimum AUD notional | Qty precision | Price precision | Metadata observed UTC |
|---|---|---|---|---|
| 0.00006 | 1 | 8 | 1 | 2026-11-01T07:09:01.303719Z |

Latest eligible completed real candle:

| UTC timestamp | Open | High | Low | Close | Base volume | Trades | Complete |
|---|---|---|---|---|---|---|---|
| 2026-11-01T08:28:00.000000Z | 110835.400000000000000000 | 110835.700000000000000000 | 110835.400000000000000000 | 110835.700000000000000000 | 0.001290360000000000 | 2 | True |

Fee source retrieved `2026-11-03T03:31:52.636870Z`; effective time unknown; account maker/taker rates NULL. Present-day assumption explicitly **True**. No percentage is selected from the full schedule.

| AUD request size | Side | Execution request | Status | Manifest |
|---|---|---|---|---|
| 100 | buy | maker | inputs_ready | 49f2bd76addfd6bd0e9718a75c61d0e5b0473b76fc65d7944a005f99f78d67e4 |
| 100 | buy | taker | inputs_ready | 39ef1a25440979835adf42dcb41ea9e05f6374ab8d8e40f363ddabb308eaae63 |
| 100 | sell | maker | inputs_ready | 9c78e424c51efdc5b59cacd5ecd66b1ca06b835e6539a3de86fb97db016a82df |
| 100 | sell | taker | inputs_ready | 4afe354861371a8c46350d2efbe65677fc2f40e19538cbe28d95ae2d758a00bf |
| 1000 | buy | maker | inputs_ready | 1e86a6a40bf4b03ef0c365dc48efce92d7d1d2c95632a8e843b5ee47adb7a38d |
| 1000 | buy | taker | inputs_ready | d9fb8025fe50085fbbd43ba47a8bfa537c6c59a2c2366b2b023ce1620feaea7f |
| 1000 | sell | maker | inputs_ready | a13546ac758a0781d85e40fe56bc9d4922728206923a699d3acc41076b85538b |
| 1000 | sell | taker | inputs_ready | d1e08745fde0c32879ff0ede39692326d1d089da3e11a82d46d9da8c7a0e7eac |
| 5000 | buy | maker | inputs_ready | 7c35f309d956b5217e577f36dc9344d4471d00286c8f5e75e460442afac9dbc6 |
| 5000 | buy | taker | inputs_ready | 333943adebfb99f6f2a45e0870dd5e0365a491243f086e2450645b4c21186b36 |
| 5000 | sell | maker | inputs_ready | 56483766942ff2e81d9b3089526ef6d09e5f9da98f86fce9df2c26dd4f10d953 |
| 5000 | sell | taker | inputs_ready | 1df31d515ec59be70c099d10292da7a7914c9850add9f76a533da4ef18e43632 |

#### ETH-AUD

Native pair: `ETHAUD`. Quote/book timestamp: `2026-11-01T09:46:25.720001Z`. Source: `https://api.kraken.com/0/public/Depth`. Response ID: `book_collection/responses/ETH-AUD/bb41cc22ac364a0588fc0ad68e30cf5d.json`; response hash `4d615b075fc9f816a068d5efe3e68268bace130754a783a8378358aa640e4420`. Collection latency: **0.515462 seconds**. Semantics: `local_request_start_UTC`. Requested/returned depth: **10 / 10 bid / 10 ask**. Completeness: `complete_returned_L2_top_depth`. All source level-update epochs are preserved in the loaded immutable book and original receipt.

| Bid | Ask | Bid size | Ask size |
|---|---|---|---|
| 3228.430000000000000000 | 3230.090000000000000000 | 2.506000000000000000 | 0.200000000000000000 |

Complete real returned snapshot; prices AUD/base and quantities base units:

| Side | Level | Price | Quantity |
|---|---|---|---|
| ask | 1 | 3230.090000000000000000 | 0.200000000000000000 |
| ask | 2 | 3230.410000000000000000 | 11.001000000000000000 |
| ask | 3 | 3231.490000000000000000 | 2.506000000000000000 |
| ask | 4 | 3232.890000000000000000 | 0.696000000000000000 |
| ask | 5 | 3232.920000000000000000 | 0.864000000000000000 |
| ask | 6 | 3234.520000000000000000 | 12.461000000000000000 |
| ask | 7 | 3234.550000000000000000 | 0.877000000000000000 |
| ask | 8 | 3234.560000000000000000 | 3.404000000000000000 |
| ask | 9 | 3236.080000000000000000 | 0.620000000000000000 |
| ask | 10 | 3236.090000000000000000 | 1.199000000000000000 |
| bid | 1 | 3228.430000000000000000 | 2.506000000000000000 |
| bid | 2 | 3228.420000000000000000 | 20.511000000000000000 |
| bid | 3 | 3228.120000000000000000 | 0.200000000000000000 |
| bid | 4 | 3227.830000000000000000 | 0.031000000000000000 |
| bid | 5 | 3224.020000000000000000 | 2.296000000000000000 |
| bid | 6 | 3224.010000000000000000 | 10.998000000000000000 |
| bid | 7 | 3223.050000000000000000 | 0.776000000000000000 |
| bid | 8 | 3222.610000000000000000 | 0.515000000000000000 |
| bid | 9 | 3222.600000000000000000 | 3.404000000000000000 |
| bid | 10 | 3222.450000000000000000 | 0.063000000000000000 |

Observed exchange constraints (not order increments):

| Minimum qty | Minimum AUD notional | Qty precision | Price precision | Metadata observed UTC |
|---|---|---|---|---|
| 0.002 | 1 | 8 | 2 | 2026-11-01T07:09:01.303719Z |

Latest eligible completed real candle:

| UTC timestamp | Open | High | Low | Close | Base volume | Trades | Complete |
|---|---|---|---|---|---|---|---|
| 2026-11-01T08:28:00.000000Z | 3223.050000000000000000 | 3223.050000000000000000 | 3223.050000000000000000 | 3223.050000000000000000 | 0E-18 | 0 | True |

Fee source retrieved `2026-11-03T03:31:52.636870Z`; effective time unknown; account maker/taker rates NULL. Present-day assumption explicitly **True**. No percentage is selected from the full schedule.

| AUD request size | Side | Execution request | Status | Manifest |
|---|---|---|---|---|
| 100 | buy | maker | inputs_ready | 1d577be4e101ec97c2a109316a9f6e5256a4ad70f08c22c015fc14ee8052beb8 |
| 100 | buy | taker | inputs_ready | f7596e82b486860278bf990995a060d9cca5ee095764e1219527148dfe72a92a |
| 100 | sell | maker | inputs_ready | 62db1e6578ac19162f05dc20b2f04367da9ff9a34d99fd274a94b0538af3511b |
| 100 | sell | taker | inputs_ready | bbc963fe68bfa1ac5544a09644d438079f7c64a0fad88f181de8a55a31513bf4 |
| 1000 | buy | maker | inputs_ready | 7a699253051eec3bcbfd47590a8a11a4d6ada40848b9b22b826ee5abe69c77f8 |
| 1000 | buy | taker | inputs_ready | 15cc4527ab78d0fdcedd5d57bd53d7d13755c547571d9ed4e911f4b66856b379 |
| 1000 | sell | maker | inputs_ready | 6be012e5a14fa97506d145c4e6c891bae143b3c9152b4578c563088bb58178af |
| 1000 | sell | taker | inputs_ready | f781cfa946ce87456d38c27f7c6eb2cf9ecebae296035865b631869c5dffb567 |
| 5000 | buy | maker | inputs_ready | 543caf5fb5645211bf7b03928e090c9a6238b99f2b980d1f665e10f87e5c9de6 |
| 5000 | buy | taker | inputs_ready | 903a7b14f14ef6ad6ec9916a28400e6cda1b776fd0305b58a3e110ebefa676d4 |
| 5000 | sell | maker | inputs_ready | e1d61c4cd077686f825080a91c458a4a5646ac47ca44e268235d9fcaf396dfff |
| 5000 | sell | taker | inputs_ready | f8ee5978dcd44e87f585c9d2556cfc5c0742f5bcc847c4133f7d7b52ee977b44 |

All **24** actual BTC/ETH manifests were prepared twice, returned deterministic identifiers, reloaded, and compared exactly to their original bundles. Notionals are requests only. No quantity, execution price, spread or cost was produced. SQLite integrity_check: **ok**. Calculated cost Parquet files: **0**. No `costs_1m` observations were created. Source quality history is preserved; this successful explicit-assumption audit added no missing-input issues.

### Actual algorithm status

| Algorithm | Status |
|---|---|
| spread_cost | NOT_IMPLEMENTED |
| book_execution | NOT_IMPLEMENTED |
| slippage | NOT_IMPLEMENTED |
| market_impact | NOT_IMPLEMENTED |
| total_cost | NOT_IMPLEMENTED |


## Final operational CLI evidence

The current-time BTC-AUD A$1,000 input preparation returned exit **1**, `inputs_incomplete`, with `stale_ohlcv` and `stale_quote_and_orderbook`. It persisted one incomplete manifest and two open input-quality issues, not market-data corrections or calculated costs. Historical BTC preparation with the explicit present-day-fee assumption returned exit **0**, `inputs_ready`, and its existing deterministic manifest. `inputs report --all-aud --sizes-aud 100 1000 5000` at the documented historical as-of with the explicit assumption returned exit **0**, **42 input-availability rows**; fee reporting returned both retained versions. All report commands remained offline/read-only. There are now **25 manifests: 24 inputs_ready and 1 inputs_incomplete**, and zero calculated costs.

## Verification receipt

- Focused offline suite: **64 passed, 1 skipped** (opt-in official public integration).
- Full offline regression: **715 passed, 6 skipped**. Skips are five opt-in public integrations (environment, catalogue, OHLCV, book, cost inputs) and privileged Windows symlink creation.
- Opt-in real public fee/persisted-input integration: **1 passed**.
- Credential-value scan passed without printing any values.
- Preservation manifest verified **24,328 preexisting files unchanged**, excluding only the authorized existing transport file and metadata.sqlite input-quality additions. Existing notebooks, credentials, research/execution data, previous market partitions and source receipts remain unchanged.
- Compilation and generated-file exclusion checks passed. No SQLite, Parquet, fee payload or input manifest is tracked in Git.
- Each mathematical method remains a single explicit NotImplementedError raise. No fabricated or calculated result has been persisted.
