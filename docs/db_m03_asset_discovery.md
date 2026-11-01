# Database Milestone 3: Kraken asset discovery and catalogue population

## Scope and verified base

PR #3 was verified actually merged into review-base/milestone1-20261029 before branch creation: merge commit `3c4561639cc4433b9fd5df7e78cddf9db2eb7ea1`, merged 2026-11-01T06:50:48Z. The updated base contains Database Milestone 1 environment/configuration and Milestone 2 SQLite/Parquet storage. Existing notebooks, credentials, research artifacts and execution databases are preserved. Only assets rows in the existing metadata.sqlite and ignored public discovery receipts are populated.

No OHLCV, quote/order-book observations, liquidity screening, cost modelling or trading is implemented. The five-table SQLite schema remains version 1, unchanged. No second database is created.

## Reused infrastructure

- `db_environment.load_config` locates the existing data directory and .env configuration without mutation.
- `metadata_store.connect_metadata` validates application ID, schema version, exact DDL and foreign keys. Discovery requires initialization; it never initializes or repairs a store.
- `market_data.public_get` remains the bounded urllib HTTP client. Its public endpoint allowlist is deliberately extended with Assets; no new client or credentials are introduced.
- `operations.ProcessLock` serializes catalogue writers. Existing streaming/strategy/risk/execution interfaces remain unchanged.
- No existing exchange-to-canonical catalogue mapper was found in the application modules. The new resolver joins exchange-provided metadata; it does not use the locked research universe, arbitrary X/Z prefix removal or guessed ticker replacements.

## Endpoints and complete source scope

The discovery command issues six public GET requests, with no pair or country filter:

1. `/0/public/AssetPairs?aclass_base=currency&execution_venue=international,bitnomial_exchange`
2. `/0/public/Assets?aclass=currency`
3. `/0/public/Assets?aclass=currency&assetVersion=1`
4. The same three requests for class `tokenized_asset`.

The [AssetPairs documentation](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs) describes the currency/tokenized_asset and execution-venue filters. The default endpoint alone excludes additional classes/venues, so discovery requests them explicitly. [Assets](https://docs.kraken.com/api-reference/market-data/get-asset-info) supplies native keys, canonical display keys and altname identities. Default/native keys are preserved; assetVersion=1 supplies display identities, not permission to overwrite native IDs.

Responses must be objects with error=[] and an object result. Empty currency metadata is a mandatory failure; an empty optional tokenized response is allowed. HTTP timeouts/exceptions, exchange errors and malformed envelopes fail the run before persistence. The existing transport timeout is 10 seconds per request. There are no silent retries or private/trading requests.

Every returned pair record is assigned exactly one outcome: safely classified/normalized spot, explicitly non-spot, or unresolved. Source totals must equal the sum. Explicit derivative instrument_type metadata is excluded as non-spot; absent such evidence, unsupported classes/venues are unresolved rather than guessed non-spot. Standard currency/currency pairs on the international venue are supported when native/display identity and wsname agree. Margin leverage options on an ordinary spot pair do not turn it into a derivative.

## Identifier resolution

For each native base/quote ID, locate its Assets row, require the matching asset class and nonempty altname, and join that (class,altname) to exactly one assetVersion=1 record. Its display key is the canonical asset, which must fit the approved uppercase alphanumeric 1..20-character representation. Verify pair.wsname against the original asset aliases, then form BASE-QUOTE. No slicing of native pair names is used.

Examples from exchange metadata: XXBT -> altname XBT -> canonical BTC; XETH -> ETH; XXDG -> XDG -> DOGE; ZAUD -> AUD. An asset literally named XNEW remains XNEW, not NEW. Additional aliases are resolved using the same metadata join. Duplicate display mappings, missing/invalid metadata, wsname disagreements, invalid constraints and duplicate canonical/native pair keys are explicitly unresolved. All sides of a source collision are withheld; first/last input order cannot choose a winner.

An existing SQLite canonical ID cannot be reassigned to another native pair, nor can a native pair move to a different canonical ID. Either conflict aborts the whole database transaction. first_seen never changes. Original native base, quote, aliases, class, venue and all extra pair fields are retained in public audit receipts; native pair ID is stored in assets.kraken_pair_id. No unapproved schema fields are added.

## Field-by-field SQLite mapping

| SQLite field | Source / parsing | Missing / invalid behavior |
|---|---|---|
| pair_id | Exchange-resolved canonical base + '-' + quote | Unresolved if ambiguous or not representable |
| kraken_pair_id | AssetPairs object key, verbatim | Required; never rewritten to a sample identifier |
| base_asset | Native base -> Assets altname -> canonical display key | Required and unique |
| quote_asset | Same mapping for native quote | AUD matching uses this field, not pair-name substrings |
| status | Nonempty pair.status, verbatim | Missing status is unresolved; no invented online default |
| first_seen | Collector UTC observation time, fixed microseconds | New rows only; previous value preserved |
| last_seen | Most recent successful collector observation time | Advances only for confirmed records; clock regression fails |
| min_order_qty | pair.ordermin through exact Decimal parsing to fixed decimal TEXT | Missing/null -> NULL; float, negative, nonfinite or unsupported precision -> unresolved |
| min_order_value | pair.costmin, in pair quote currency | Same exact/null rules; not automatically AUD for other quotes |
| qty_precision | pair.lot_decimals, integer 0..18 | Missing/null -> NULL; not a quantity increment |
| price_precision | pair.pair_decimals, integer 0..18 | Missing/null -> NULL; not a tick size |

Decimal parsing never passes through binary floats or rounds: up to 20 integer and 18 fractional digits are representable in the approved schema. Exponents are converted exactly to fixed decimal text if representable. Price tick size, lot name (including unit5), lot multiplier, cost_decimals, fee-volume currency and venue/class metadata are not fabricated from precision. They remain in the raw public receipt.

### Proposed schema extensions ? not implemented

A reviewed migration would be needed to represent venue/instrument-class-qualified market identity and tokenized identity provenance without collisions, and to store native base/quote IDs, actual quantity increments, tick_size, lot_multiplier and cost_decimals as first-class columns. A later catalogue-run/unresolved-record table could replace filesystem receipts. Current code does not alter schema v1 or assume those fields are present. No screening thresholds are proposed here.

## Transactions, repeatability and missing markets

Discovery validates the existing database before requesting APIs. One writer lock covers fetch/parse, SQL transaction and receipt publication. BEGIN IMMEDIATE serializes updates. All qualifying assets are inserted/updated in one transaction, preserving first_seen and foreign-key references; identity conflicts, regressing clocks or database/write failures roll back the entire SQL update. Same-content/same-timestamp repeats avoid SQL writes. Later observations update last_seen without duplicates. Optional fields that become unknown are stored as NULL, not an old/default value.

Previously stored records absent from the response are retained with their last confirmed status and last_seen. Absence is not evidence of delisting. The report separately lists not_observed_latest. Online AUD reporting requires status=online and confirmation in the latest consistent discovery; raw stored-online SQL counts are also exposed. The inactive report bucket means non-online/restricted status, not necessarily delisted: post_only, for example, is preserved rather than relabelled.

## Public audit receipts and report consistency

`<data_dir>/catalogue_discovery/<run_id>.json` contains immutable public source metadata, native/display asset mappings, source identities, per-record unresolved/exclusion reasons, counts and a catalogue-content fingerprint. It is staged/fsynced/atomically replaced before SQL COMMIT. A failed SQL commit may leave an unselected orphan receipt, never an inconsistent partial catalogue. latest.json is atomically published after COMMIT.

SQLite and filesystem receipt publication are not one atomic transaction. If publication fails after SQL commit, the complete SQL update can already be present; the command fails and a retry is safe. Reports compare the receipt fingerprint to the actual SQL rows and return missing_or_stale/nonzero if evidence is missing or inconsistent. A stale receipt never supplies fabricated empty resolution results. Readers can retry a transient publication race. Storage root and audit directory are trusted local filesystem locations; audit receipts remain ignored generated data, not a second database.

## Commands and exit codes

```powershell
.venv-db/Scripts/python.exe -m kraken_quant.asset_catalogue discover --summary
.venv-db/Scripts/python.exe -m kraken_quant.asset_catalogue report --summary
```

Both accept --root and --data-dir. Full report output (without --summary) includes every persisted asset; summary omits the full catalogue but retains AUD records and resolution records. Reports use SQLite plus local public receipts, with no API requests. Required database initialization is the approved `python -m kraken_quant.metadata_store init` command; discovery never runs it automatically.

Discovery exits: 0 = all returned records classified/resolved or explicitly excluded; 2 = safely persisted qualifying records with unresolved source records (partial discovery); 1 = request, schema, identity, persistence or receipt failure. Report exits 0 when the database and receipt agree and integrity is ok, even if its explicit resolution list is nonempty; otherwise 1. Private exceptions/credential values are never printed. Partial discovery is never reported as complete.

## Actual Kraken observations ? not synthetic

First actual run: `2026-11-01T07:07:41.032874Z`. Second actual run: `2026-11-01T07:09:01.303719Z`. Both discovery CLI commands returned **2** because broader-class/venue resolution remains incomplete; the report command returned **0**.

| Audit measure | First run | Second run |
|---|---:|---:|
| source_records | 1847 | 1847 |
| classified_spot | 1464 | 1464 |
| excluded_non_spot | 0 | 0 |
| unresolved_count | 383 | 383 |
| inserted | 1464 | 0 |
| updated | 0 | 1464 |
| unchanged | 0 | 0 |
| persisted | 1464 | 1464 |
| previously_stored | 0 | 1464 |
| retained_previous | 0 | 1464 |
| total_assets | 1464 | 1464 |

API scope: currency including all documented venues returned 1493; tokenized_asset returned 354. Reconciliation: **1847 = 1464 persisted + 0 explicitly non-spot + 383 unresolved**. All 1464 prior first_seen values were preserved; last_seen advanced for all confirmed pairs. No historical pair was deleted.

### Required SQL audit

```sql
SELECT COUNT(*) AS total_assets FROM assets;
SELECT COUNT(*) AS aud_pairs FROM assets WHERE quote_asset = 'AUD';
SELECT COUNT(*) AS online_aud_pairs FROM assets WHERE quote_asset = 'AUD' AND status = 'online';
SELECT pair_id, kraken_pair_id, base_asset, quote_asset, status,
       min_order_qty, min_order_value, qty_precision, price_precision
FROM assets WHERE quote_asset = 'AUD' ORDER BY pair_id LIMIT 20;
```

Results: total_assets=1464; aud_pairs=14; online_aud_pairs=14; integrity_check=ok; foreign_key_check=[]. Other metadata tables remain empty; no eligibility decisions, ingestion checkpoints, collection states or quality records were manufactured.

| pair_id | kraken_pair_id | base_asset | quote_asset | status | min_order_qty | min_order_value | qty_precision | price_precision |
|---|---|---|---|---|---|---|---|---|
| ADA-AUD | ADAAUD | ADA | AUD | online | 20 | 1 | 8 | 5 |
| AUDX-AUD | AUDXAUD | AUDX | AUD | online | 7 | 1 | 8 | 4 |
| BCH-AUD | BCHAUD | BCH | AUD | online | 0.018 | 1 | 8 | 2 |
| BTC-AUD | XBTAUD | BTC | AUD | online | 0.00006 | 1 | 8 | 1 |
| DOGE-AUD | XDGAUD | DOGE | AUD | online | 50 | 1 | 8 | 7 |
| ETH-AUD | ETHAUD | ETH | AUD | online | 0.002 | 1 | 8 | 2 |
| EUR-AUD | EURAUD | EUR | AUD | online | 4 | 1 | 8 | 5 |
| LINK-AUD | LINKAUD | LINK | AUD | online | 0.35 | 1 | 8 | 3 |
| LTC-AUD | LTCAUD | LTC | AUD | online | 0.08 | 1 | 8 | 2 |
| PEPE-AUD | PEPEAUD | PEPE | AUD | online | 1100000 | 1 | 5 | 9 |
| SOL-AUD | SOLAUD | SOL | AUD | online | 0.05 | 1 | 8 | 2 |
| USDC-AUD | USDCAUD | USDC | AUD | online | 5 | 1 | 8 | 4 |
| USDT-AUD | USDTAUD | USDT | AUD | online | 5 | 1 | 8 | 4 |
| XRP-AUD | XRPAUD | XRP | AUD | online | 4 | 1 | 8 | 5 |

These are actual SQLite rows, not mocked fixture values. In particular the live BTC/AUD native pair key was XBTAUD and ETH/AUD was ETHAUD; they were not rewritten to illustrative legacy keys XXBTZAUD/XETHZAUD. BTC-AUD, ETH-AUD and ADA-AUD demonstrate real persisted normalized identities.

### Actual unresolved source records

The complete list below is retained rather than silently dropped. Tokenized records require an explicit asset-class/identity policy; the live Assets responses also contain paired SPV/display aliases. Bitnomial records belong to a separate venue/order-book context and cannot be folded into the same BASE-QUOTE identity without review. They are **not asserted non-spot**. See [Kraken margin order-book documentation](https://support.kraken.com/articles/margin-order-book). No schema migration or alias guess was made to force these records into assets.

| Native pair ID | Native base | Native quote | Reason |
|---|---|---|---|
| AAVEUSD:BTNL | AAVE | ZUSD | execution_venue_requires_resolution |
| ADAUSD:BTNL | ADA | ZUSD | execution_venue_requires_resolution |
| ALGOUSD:BTNL | ALGO | ZUSD | execution_venue_requires_resolution |
| AVAXUSD:BTNL | AVAX | ZUSD | execution_venue_requires_resolution |
| BCHUSD:BTNL | BCH | ZUSD | execution_venue_requires_resolution |
| CRVUSD:BTNL | CRV | ZUSD | execution_venue_requires_resolution |
| DOTUSD:BTNL | DOT | ZUSD | execution_venue_requires_resolution |
| ETHUSD:BTNL | XETH | ZUSD | execution_venue_requires_resolution |
| HBARUSD:BTNL | HBAR | ZUSD | execution_venue_requires_resolution |
| HYPEUSD:BTNL | HYPE | ZUSD | execution_venue_requires_resolution |
| LINKUSD:BTNL | LINK | ZUSD | execution_venue_requires_resolution |
| LTCUSD:BTNL | XLTC | ZUSD | execution_venue_requires_resolution |
| NEARUSD:BTNL | NEAR | ZUSD | execution_venue_requires_resolution |
| PAXGUSD:BTNL | PAXG | ZUSD | execution_venue_requires_resolution |
| PENGUUSD:BTNL | PENGU | ZUSD | execution_venue_requires_resolution |
| PEPEUSD:BTNL | PEPE | ZUSD | execution_venue_requires_resolution |
| RENDERUSD:BTNL | RENDER | ZUSD | execution_venue_requires_resolution |
| SHIBUSD:BTNL | SHIB | ZUSD | execution_venue_requires_resolution |
| SOLUSD:BTNL | SOL | ZUSD | execution_venue_requires_resolution |
| SUIUSD:BTNL | SUI | ZUSD | execution_venue_requires_resolution |
| TRXUSD:BTNL | TRX | ZUSD | execution_venue_requires_resolution |
| UNIUSD:BTNL | UNI | ZUSD | execution_venue_requires_resolution |
| USDCUSD:BTNL | USDC | ZUSD | execution_venue_requires_resolution |
| WLDUSD:BTNL | WLD | ZUSD | execution_venue_requires_resolution |
| XBTUSD:BTNL | XXBT | ZUSD | execution_venue_requires_resolution |
| XDGUSD:BTNL | XXDG | ZUSD | execution_venue_requires_resolution |
| XLMUSD:BTNL | XXLM | ZUSD | execution_venue_requires_resolution |
| XRPUSD:BTNL | XXRP | ZUSD | execution_venue_requires_resolution |
| ZECUSD:BTNL | XZEC | ZUSD | execution_venue_requires_resolution |
| AAOISPVUSD | AAOIx | ZUSD | asset_class_requires_resolution |
| AAOIxUSD | AAOIx | ZUSD | asset_class_requires_resolution |
| AAPLSPVUSD | AAPLx | ZUSD | asset_class_requires_resolution |
| AAPLxUSD | AAPLx | ZUSD | asset_class_requires_resolution |
| ABBVSPVUSD | ABBVx | ZUSD | asset_class_requires_resolution |
| ABBVxUSD | ABBVx | ZUSD | asset_class_requires_resolution |
| ABTSPVUSD | ABTx | ZUSD | asset_class_requires_resolution |
| ABTxUSD | ABTx | ZUSD | asset_class_requires_resolution |
| ACNSPVUSD | ACNx | ZUSD | asset_class_requires_resolution |
| ACNxUSD | ACNx | ZUSD | asset_class_requires_resolution |
| ADBESPVUSD | ADBEx | ZUSD | asset_class_requires_resolution |
| ADBExUSD | ADBEx | ZUSD | asset_class_requires_resolution |
| AMATSPVUSD | AMATx | ZUSD | asset_class_requires_resolution |
| AMATxUSD | AMATx | ZUSD | asset_class_requires_resolution |
| AMBRSPVUSD | AMBRx | ZUSD | asset_class_requires_resolution |
| AMBRxUSD | AMBRx | ZUSD | asset_class_requires_resolution |
| AMDSPVUSD | AMDx | ZUSD | asset_class_requires_resolution |
| AMDxUSD | AMDx | ZUSD | asset_class_requires_resolution |
| AMZNSPVUSD | AMZNx | ZUSD | asset_class_requires_resolution |
| AMZNxUSD | AMZNx | ZUSD | asset_class_requires_resolution |
| ANETSPVUSD | ANETx | ZUSD | asset_class_requires_resolution |
| ANETxUSD | ANETx | ZUSD | asset_class_requires_resolution |
| APLDSPVUSD | APLDx | ZUSD | asset_class_requires_resolution |
| APLDxUSD | APLDx | ZUSD | asset_class_requires_resolution |
| APPSPVUSD | APPx | ZUSD | asset_class_requires_resolution |
| APPxUSD | APPx | ZUSD | asset_class_requires_resolution |
| ARMSPVUSD | ARMx | ZUSD | asset_class_requires_resolution |
| ARMxUSD | ARMx | ZUSD | asset_class_requires_resolution |
| ASMLSPVUSD | ASMLx | ZUSD | asset_class_requires_resolution |
| ASMLxUSD | ASMLx | ZUSD | asset_class_requires_resolution |
| ASTSSPVUSD | ASTSx | ZUSD | asset_class_requires_resolution |
| ASTSxUSD | ASTSx | ZUSD | asset_class_requires_resolution |
| AVGOSPVUSD | AVGOx | ZUSD | asset_class_requires_resolution |
| AVGOxUSD | AVGOx | ZUSD | asset_class_requires_resolution |
| AZNSPVUSD | AZNx | ZUSD | asset_class_requires_resolution |
| AZNxUSD | AZNx | ZUSD | asset_class_requires_resolution |
| BACSPVUSD | BACx | ZUSD | asset_class_requires_resolution |
| BACxUSD | BACx | ZUSD | asset_class_requires_resolution |
| BESPVUSD | BEx | ZUSD | asset_class_requires_resolution |
| BExUSD | BEx | ZUSD | asset_class_requires_resolution |
| BITXSPVUSD | BITXx | ZUSD | asset_class_requires_resolution |
| BITXxUSD | BITXx | ZUSD | asset_class_requires_resolution |
| BKRSPVUSD | BKRx | ZUSD | asset_class_requires_resolution |
| BKRxUSD | BKRx | ZUSD | asset_class_requires_resolution |
| BMNRSPVUSD | BMNRx | ZUSD | asset_class_requires_resolution |
| BMNRxUSD | BMNRx | ZUSD | asset_class_requires_resolution |
| BOTSPVUSD | BOTx | ZUSD | asset_class_requires_resolution |
| BOTxUSD | BOTx | ZUSD | asset_class_requires_resolution |
| BRK.BSPVUSD | BRK.Bx | ZUSD | asset_class_requires_resolution |
| BRK.BxUSD | BRK.Bx | ZUSD | asset_class_requires_resolution |
| BSPSPVUSD | BSPx | ZUSD | asset_class_requires_resolution |
| BSPxUSD | BSPx | ZUSD | asset_class_requires_resolution |
| BTBTSPVUSD | BTBTx | ZUSD | asset_class_requires_resolution |
| BTBTxUSD | BTBTx | ZUSD | asset_class_requires_resolution |
| BTGOSPVUSD | BTGOx | ZUSD | asset_class_requires_resolution |
| BTGOxUSD | BTGOx | ZUSD | asset_class_requires_resolution |
| CBRSSPVUSD | CBRSx | ZUSD | asset_class_requires_resolution |
| CBRSxUSD | CBRSx | ZUSD | asset_class_requires_resolution |
| CEGSPVUSD | CEGx | ZUSD | asset_class_requires_resolution |
| CEGxUSD | CEGx | ZUSD | asset_class_requires_resolution |
| CLSKSPVUSD | CLSKx | ZUSD | asset_class_requires_resolution |
| CLSKxUSD | CLSKx | ZUSD | asset_class_requires_resolution |
| CMCSASPVUSD | CMCSAx | ZUSD | asset_class_requires_resolution |
| CMCSAxUSD | CMCSAx | ZUSD | asset_class_requires_resolution |
| COINSPVUSD | COINx | ZUSD | asset_class_requires_resolution |
| COINxUSD | COINx | ZUSD | asset_class_requires_resolution |
| COPXSPVUSD | COPXx | ZUSD | asset_class_requires_resolution |
| COPXxUSD | COPXx | ZUSD | asset_class_requires_resolution |
| CORZSPVUSD | CORZx | ZUSD | asset_class_requires_resolution |
| CORZxUSD | CORZx | ZUSD | asset_class_requires_resolution |
| CRCLSPVUSD | CRCLx | ZUSD | asset_class_requires_resolution |
| CRCLxUSD | CRCLx | ZUSD | asset_class_requires_resolution |
| CRMSPVUSD | CRMx | ZUSD | asset_class_requires_resolution |
| CRMxUSD | CRMx | ZUSD | asset_class_requires_resolution |
| CRWDSPVUSD | CRWDx | ZUSD | asset_class_requires_resolution |
| CRWDxUSD | CRWDx | ZUSD | asset_class_requires_resolution |
| CRWVSPVUSD | CRWVx | ZUSD | asset_class_requires_resolution |
| CRWVxUSD | CRWVx | ZUSD | asset_class_requires_resolution |
| CSCOSPVUSD | CSCOx | ZUSD | asset_class_requires_resolution |
| CSCOxUSD | CSCOx | ZUSD | asset_class_requires_resolution |
| CVXSPVUSD | CVXx | ZUSD | asset_class_requires_resolution |
| CVXxUSD | CVXx | ZUSD | asset_class_requires_resolution |
| DAXSPVUSD | DAXx | ZUSD | asset_class_requires_resolution |
| DAXxUSD | DAXx | ZUSD | asset_class_requires_resolution |
| DELLSPVUSD | DELLx | ZUSD | asset_class_requires_resolution |
| DELLxUSD | DELLx | ZUSD | asset_class_requires_resolution |
| DFDVSPVUSD | DFDVx | ZUSD | asset_class_requires_resolution |
| DFDVxUSD | DFDVx | ZUSD | asset_class_requires_resolution |
| DHRSPVUSD | DHRx | ZUSD | asset_class_requires_resolution |
| DHRxUSD | DHRx | ZUSD | asset_class_requires_resolution |
| ENHASPVUSD | ENHAx | ZUSD | asset_class_requires_resolution |
| ENHAxUSD | ENHAx | ZUSD | asset_class_requires_resolution |
| ETNSPVUSD | ETNx | ZUSD | asset_class_requires_resolution |
| ETNxUSD | ETNx | ZUSD | asset_class_requires_resolution |
| EWGSPVUSD | EWGx | ZUSD | asset_class_requires_resolution |
| EWGxUSD | EWGx | ZUSD | asset_class_requires_resolution |
| EWQSPVUSD | EWQx | ZUSD | asset_class_requires_resolution |
| EWQxUSD | EWQx | ZUSD | asset_class_requires_resolution |
| EWUSPVUSD | EWUx | ZUSD | asset_class_requires_resolution |
| EWUxUSD | EWUx | ZUSD | asset_class_requires_resolution |
| EWYSPVUSD | EWYx | ZUSD | asset_class_requires_resolution |
| EWYxUSD | EWYx | ZUSD | asset_class_requires_resolution |
| FAAASPVUSD | FAAAx | ZUSD | asset_class_requires_resolution |
| FAAAxUSD | FAAAx | ZUSD | asset_class_requires_resolution |
| FDXFSPVUSD | FDXFx | ZUSD | asset_class_requires_resolution |
| FDXFxUSD | FDXFx | ZUSD | asset_class_requires_resolution |
| FEZSPVUSD | FEZx | ZUSD | asset_class_requires_resolution |
| FEZxUSD | FEZx | ZUSD | asset_class_requires_resolution |
| FGDLSPVUSD | FGDLx | ZUSD | asset_class_requires_resolution |
| FGDLxUSD | FGDLx | ZUSD | asset_class_requires_resolution |
| FLBLSPVUSD | FLBLx | ZUSD | asset_class_requires_resolution |
| FLBLxUSD | FLBLx | ZUSD | asset_class_requires_resolution |
| FLQMSPVUSD | FLQMx | ZUSD | asset_class_requires_resolution |
| FLQMxUSD | FLQMx | ZUSD | asset_class_requires_resolution |
| FSMLSPVUSD | FSMLx | ZUSD | asset_class_requires_resolution |
| FSMLxUSD | FSMLx | ZUSD | asset_class_requires_resolution |
| GDXSPVUSD | GDXx | ZUSD | asset_class_requires_resolution |
| GDXxUSD | GDXx | ZUSD | asset_class_requires_resolution |
| GEVSPVUSD | GEVx | ZUSD | asset_class_requires_resolution |
| GEVxUSD | GEVx | ZUSD | asset_class_requires_resolution |
| GLDSPVUSD | GLDx | ZUSD | asset_class_requires_resolution |
| GLDxUSD | GLDx | ZUSD | asset_class_requires_resolution |
| GLXYSPVUSD | GLXYx | ZUSD | asset_class_requires_resolution |
| GLXYxUSD | GLXYx | ZUSD | asset_class_requires_resolution |
| GMESPVUSD | GMEx | ZUSD | asset_class_requires_resolution |
| GMExUSD | GMEx | ZUSD | asset_class_requires_resolution |
| GOOGLSPVUSD | GOOGLx | ZUSD | asset_class_requires_resolution |
| GOOGLxUSD | GOOGLx | ZUSD | asset_class_requires_resolution |
| GSSPVUSD | GSx | ZUSD | asset_class_requires_resolution |
| GSxUSD | GSx | ZUSD | asset_class_requires_resolution |
| HIMSSPVUSD | HIMSx | ZUSD | asset_class_requires_resolution |
| HIMSxUSD | HIMSx | ZUSD | asset_class_requires_resolution |
| HONSPVUSD | HONx | ZUSD | asset_class_requires_resolution |
| HONxUSD | HONx | ZUSD | asset_class_requires_resolution |
| HOODSPVUSD | HOODx | ZUSD | asset_class_requires_resolution |
| HOODxUSD | HOODx | ZUSD | asset_class_requires_resolution |
| HUTSPVUSD | HUTx | ZUSD | asset_class_requires_resolution |
| HUTxUSD | HUTx | ZUSD | asset_class_requires_resolution |
| IBMSPVUSD | IBMx | ZUSD | asset_class_requires_resolution |
| IBMxUSD | IBMx | ZUSD | asset_class_requires_resolution |
| IEMGSPVUSD | IEMGx | ZUSD | asset_class_requires_resolution |
| IEMGxUSD | IEMGx | ZUSD | asset_class_requires_resolution |
| IJRSPVUSD | IJRx | ZUSD | asset_class_requires_resolution |
| IJRxUSD | IJRx | ZUSD | asset_class_requires_resolution |
| INTCSPVUSD | INTCx | ZUSD | asset_class_requires_resolution |
| INTCxUSD | INTCx | ZUSD | asset_class_requires_resolution |
| IQMSPVUSD | IQMx | ZUSD | asset_class_requires_resolution |
| IQMxUSD | IQMx | ZUSD | asset_class_requires_resolution |
| IRENSPVUSD | IRENx | ZUSD | asset_class_requires_resolution |
| IRENxUSD | IRENx | ZUSD | asset_class_requires_resolution |
| ITASPVUSD | ITAx | ZUSD | asset_class_requires_resolution |
| ITAxUSD | ITAx | ZUSD | asset_class_requires_resolution |
| IWMSPVUSD | IWMx | ZUSD | asset_class_requires_resolution |
| IWMxUSD | IWMx | ZUSD | asset_class_requires_resolution |
| JAAASPVUSD | JAAAx | ZUSD | asset_class_requires_resolution |
| JAAAxUSD | JAAAx | ZUSD | asset_class_requires_resolution |
| JMKESPVUSD | JMKEx | ZUSD | asset_class_requires_resolution |
| JMKExUSD | JMKEx | ZUSD | asset_class_requires_resolution |
| JNJSPVUSD | JNJx | ZUSD | asset_class_requires_resolution |
| JNJxUSD | JNJx | ZUSD | asset_class_requires_resolution |
| JPMSPVUSD | JPMx | ZUSD | asset_class_requires_resolution |
| JPMxUSD | JPMx | ZUSD | asset_class_requires_resolution |
| JPSTSPVUSD | JPSTx | ZUSD | asset_class_requires_resolution |
| JPSTxUSD | JPSTx | ZUSD | asset_class_requires_resolution |
| KLACSPVUSD | KLACx | ZUSD | asset_class_requires_resolution |
| KLACxUSD | KLACx | ZUSD | asset_class_requires_resolution |
| KOSPVUSD | KOx | ZUSD | asset_class_requires_resolution |
| KOxUSD | KOx | ZUSD | asset_class_requires_resolution |
| KRAQSPVUSD | KRAQx | ZUSD | asset_class_requires_resolution |
| KRAQxUSD | KRAQx | ZUSD | asset_class_requires_resolution |
| LINSPVUSD | LINx | ZUSD | asset_class_requires_resolution |
| LINxUSD | LINx | ZUSD | asset_class_requires_resolution |
| LITESPVUSD | LITEx | ZUSD | asset_class_requires_resolution |
| LITExUSD | LITEx | ZUSD | asset_class_requires_resolution |
| LLYSPVUSD | LLYx | ZUSD | asset_class_requires_resolution |
| LLYxUSD | LLYx | ZUSD | asset_class_requires_resolution |
| LNGSPVUSD | LNGx | ZUSD | asset_class_requires_resolution |
| LNGxUSD | LNGx | ZUSD | asset_class_requires_resolution |
| LRCXSPVUSD | LRCXx | ZUSD | asset_class_requires_resolution |
| LRCXxUSD | LRCXx | ZUSD | asset_class_requires_resolution |
| MARASPVUSD | MARAx | ZUSD | asset_class_requires_resolution |
| MARAxUSD | MARAx | ZUSD | asset_class_requires_resolution |
| MCDSPVUSD | MCDx | ZUSD | asset_class_requires_resolution |
| MCDxUSD | MCDx | ZUSD | asset_class_requires_resolution |
| MDTSPVUSD | MDTx | ZUSD | asset_class_requires_resolution |
| MDTxUSD | MDTx | ZUSD | asset_class_requires_resolution |
| METASPVUSD | METAx | ZUSD | asset_class_requires_resolution |
| METAxUSD | METAx | ZUSD | asset_class_requires_resolution |
| MOOSPVUSD | MOOx | ZUSD | asset_class_requires_resolution |
| MOOxUSD | MOOx | ZUSD | asset_class_requires_resolution |
| MRKSPVUSD | MRKx | ZUSD | asset_class_requires_resolution |
| MRKxUSD | MRKx | ZUSD | asset_class_requires_resolution |
| MRNASPVUSD | MRNAx | ZUSD | asset_class_requires_resolution |
| MRNAxUSD | MRNAx | ZUSD | asset_class_requires_resolution |
| MRVLSPVUSD | MRVLx | ZUSD | asset_class_requires_resolution |
| MRVLxUSD | MRVLx | ZUSD | asset_class_requires_resolution |
| MSFTSPVUSD | MSFTx | ZUSD | asset_class_requires_resolution |
| MSFTxUSD | MSFTx | ZUSD | asset_class_requires_resolution |
| MSTRSPVUSD | MSTRx | ZUSD | asset_class_requires_resolution |
| MSTRxUSD | MSTRx | ZUSD | asset_class_requires_resolution |
| MUSPVUSD | MUx | ZUSD | asset_class_requires_resolution |
| MUxUSD | MUx | ZUSD | asset_class_requires_resolution |
| NBISSPVUSD | NBISx | ZUSD | asset_class_requires_resolution |
| NBISxUSD | NBISx | ZUSD | asset_class_requires_resolution |
| NETSPVUSD | NETx | ZUSD | asset_class_requires_resolution |
| NETxUSD | NETx | ZUSD | asset_class_requires_resolution |
| NFLXSPVUSD | NFLXx | ZUSD | asset_class_requires_resolution |
| NFLXxUSD | NFLXx | ZUSD | asset_class_requires_resolution |
| NLRSPVUSD | NLRx | ZUSD | asset_class_requires_resolution |
| NLRxUSD | NLRx | ZUSD | asset_class_requires_resolution |
| NVDASPVUSD | NVDAx | ZUSD | asset_class_requires_resolution |
| NVDAxUSD | NVDAx | ZUSD | asset_class_requires_resolution |
| NVOSPVUSD | NVOx | ZUSD | asset_class_requires_resolution |
| NVOxUSD | NVOx | ZUSD | asset_class_requires_resolution |
| OKLOSPVUSD | OKLOx | ZUSD | asset_class_requires_resolution |
| OKLOxUSD | OKLOx | ZUSD | asset_class_requires_resolution |
| ONDSSPVUSD | ONDSx | ZUSD | asset_class_requires_resolution |
| ONDSxUSD | ONDSx | ZUSD | asset_class_requires_resolution |
| OPENSPVUSD | OPENx | ZUSD | asset_class_requires_resolution |
| OPENxUSD | OPENx | ZUSD | asset_class_requires_resolution |
| ORCLSPVUSD | ORCLx | ZUSD | asset_class_requires_resolution |
| ORCLxUSD | ORCLx | ZUSD | asset_class_requires_resolution |
| PALLSPVUSD | PALLx | ZUSD | asset_class_requires_resolution |
| PALLxUSD | PALLx | ZUSD | asset_class_requires_resolution |
| PANWSPVUSD | PANWx | ZUSD | asset_class_requires_resolution |
| PANWxUSD | PANWx | ZUSD | asset_class_requires_resolution |
| PEPSPVUSD | PEPx | ZUSD | asset_class_requires_resolution |
| PEPxUSD | PEPx | ZUSD | asset_class_requires_resolution |
| PFESPVUSD | PFEx | ZUSD | asset_class_requires_resolution |
| PFExUSD | PFEx | ZUSD | asset_class_requires_resolution |
| PGSPVUSD | PGx | ZUSD | asset_class_requires_resolution |
| PGxUSD | PGx | ZUSD | asset_class_requires_resolution |
| PLSPVUSD | PLx | ZUSD | asset_class_requires_resolution |
| PLTRSPVUSD | PLTRx | ZUSD | asset_class_requires_resolution |
| PLTRxUSD | PLTRx | ZUSD | asset_class_requires_resolution |
| PLxUSD | PLx | ZUSD | asset_class_requires_resolution |
| PMSPVUSD | PMx | ZUSD | asset_class_requires_resolution |
| PMxUSD | PMx | ZUSD | asset_class_requires_resolution |
| PPLTSPVUSD | PPLTx | ZUSD | asset_class_requires_resolution |
| PPLTxUSD | PPLTx | ZUSD | asset_class_requires_resolution |
| PWRSPVUSD | PWRx | ZUSD | asset_class_requires_resolution |
| PWRxUSD | PWRx | ZUSD | asset_class_requires_resolution |
| PYPLSPVUSD | PYPLx | ZUSD | asset_class_requires_resolution |
| PYPLxUSD | PYPLx | ZUSD | asset_class_requires_resolution |
| QQQSPVUSD | QQQx | ZUSD | asset_class_requires_resolution |
| QQQxUSD | QQQx | ZUSD | asset_class_requires_resolution |
| QURESPVUSD | QUREx | ZUSD | asset_class_requires_resolution |
| QURExUSD | QUREx | ZUSD | asset_class_requires_resolution |
| RBLXSPVUSD | RBLXx | ZUSD | asset_class_requires_resolution |
| RBLXxUSD | RBLXx | ZUSD | asset_class_requires_resolution |
| RCATSPVUSD | RCATx | ZUSD | asset_class_requires_resolution |
| RCATxUSD | RCATx | ZUSD | asset_class_requires_resolution |
| RIOTSPVUSD | RIOTx | ZUSD | asset_class_requires_resolution |
| RIOTxUSD | RIOTx | ZUSD | asset_class_requires_resolution |
| SATASPVUSD | SATAx | ZUSD | asset_class_requires_resolution |
| SATAxUSD | SATAx | ZUSD | asset_class_requires_resolution |
| SBETSPVUSD | SBETx | ZUSD | asset_class_requires_resolution |
| SBETxUSD | SBETx | ZUSD | asset_class_requires_resolution |
| SCHFSPVUSD | SCHFx | ZUSD | asset_class_requires_resolution |
| SCHFxUSD | SCHFx | ZUSD | asset_class_requires_resolution |
| SGOVSPVUSD | SGOVx | ZUSD | asset_class_requires_resolution |
| SGOVxUSD | SGOVx | ZUSD | asset_class_requires_resolution |
| SHAZSPVUSD | SHAZx | ZUSD | asset_class_requires_resolution |
| SHAZxUSD | SHAZx | ZUSD | asset_class_requires_resolution |
| SKHYSPVUSD | SKHYx | ZUSD | asset_class_requires_resolution |
| SKHYxUSD | SKHYx | ZUSD | asset_class_requires_resolution |
| SLVSPVUSD | SLVx | ZUSD | asset_class_requires_resolution |
| SLVxUSD | SLVx | ZUSD | asset_class_requires_resolution |
| SMCISPVUSD | SMCIx | ZUSD | asset_class_requires_resolution |
| SMCIxUSD | SMCIx | ZUSD | asset_class_requires_resolution |
| SMHSPVUSD | SMHx | ZUSD | asset_class_requires_resolution |
| SMHxUSD | SMHx | ZUSD | asset_class_requires_resolution |
| SMRSPVUSD | SMRx | ZUSD | asset_class_requires_resolution |
| SMRxUSD | SMRx | ZUSD | asset_class_requires_resolution |
| SNDKSPVUSD | SNDKx | ZUSD | asset_class_requires_resolution |
| SNDKxUSD | SNDKx | ZUSD | asset_class_requires_resolution |
| SOXLSPVUSD | SOXLx | ZUSD | asset_class_requires_resolution |
| SOXLxUSD | SOXLx | ZUSD | asset_class_requires_resolution |
| SOXXSPVUSD | SOXXx | ZUSD | asset_class_requires_resolution |
| SOXXxUSD | SOXXx | ZUSD | asset_class_requires_resolution |
| SPCESPVUSD | SPCEx | ZUSD | asset_class_requires_resolution |
| SPCExUSD | SPCEx | ZUSD | asset_class_requires_resolution |
| SPCXSPVUSD | SPCXx | ZUSD | asset_class_requires_resolution |
| SPCXxUSD | SPCXx | ZUSD | asset_class_requires_resolution |
| SPYSPVUSD | SPYx | ZUSD | asset_class_requires_resolution |
| SPYxUSD | SPYx | ZUSD | asset_class_requires_resolution |
| STRCSPVUSD | STRCx | ZUSD | asset_class_requires_resolution |
| STRCxUSD | STRCx | ZUSD | asset_class_requires_resolution |
| TBLLSPVUSD | TBLLx | ZUSD | asset_class_requires_resolution |
| TBLLxUSD | TBLLx | ZUSD | asset_class_requires_resolution |
| TERSPVUSD | TERx | ZUSD | asset_class_requires_resolution |
| TERxUSD | TERx | ZUSD | asset_class_requires_resolution |
| TESPVUSD | TEx | ZUSD | asset_class_requires_resolution |
| TExUSD | TEx | ZUSD | asset_class_requires_resolution |
| TMOSPVUSD | TMOx | ZUSD | asset_class_requires_resolution |
| TMOxUSD | TMOx | ZUSD | asset_class_requires_resolution |
| TMUSSPVUSD | TMUSx | ZUSD | asset_class_requires_resolution |
| TMUSxUSD | TMUSx | ZUSD | asset_class_requires_resolution |
| TONXSPVUSD | TONXx | ZUSD | asset_class_requires_resolution |
| TONXxUSD | TONXx | ZUSD | asset_class_requires_resolution |
| TQQQSPVUSD | TQQQx | ZUSD | asset_class_requires_resolution |
| TQQQxUSD | TQQQx | ZUSD | asset_class_requires_resolution |
| TSLASPVUSD | TSLAx | ZUSD | asset_class_requires_resolution |
| TSLAxUSD | TSLAx | ZUSD | asset_class_requires_resolution |
| TSMSPVUSD | TSMx | ZUSD | asset_class_requires_resolution |
| TSMxUSD | TSMx | ZUSD | asset_class_requires_resolution |
| TWSTSPVUSD | TWSTx | ZUSD | asset_class_requires_resolution |
| TWSTxUSD | TWSTx | ZUSD | asset_class_requires_resolution |
| UBERSPVUSD | UBERx | ZUSD | asset_class_requires_resolution |
| UBERxUSD | UBERx | ZUSD | asset_class_requires_resolution |
| UNHSPVUSD | UNHx | ZUSD | asset_class_requires_resolution |
| UNHxUSD | UNHx | ZUSD | asset_class_requires_resolution |
| URASPVUSD | URAx | ZUSD | asset_class_requires_resolution |
| URAxUSD | URAx | ZUSD | asset_class_requires_resolution |
| USARSPVUSD | USARx | ZUSD | asset_class_requires_resolution |
| USARxUSD | USARx | ZUSD | asset_class_requires_resolution |
| USPXSPVUSD | USPXx | ZUSD | asset_class_requires_resolution |
| USPXxUSD | USPXx | ZUSD | asset_class_requires_resolution |
| UUUUSPVUSD | UUUUx | ZUSD | asset_class_requires_resolution |
| UUUUxUSD | UUUUx | ZUSD | asset_class_requires_resolution |
| VCXSPVUSD | VCXx | ZUSD | asset_class_requires_resolution |
| VCXxUSD | VCXx | ZUSD | asset_class_requires_resolution |
| VGKSPVUSD | VGKx | ZUSD | asset_class_requires_resolution |
| VGKxUSD | VGKx | ZUSD | asset_class_requires_resolution |
| VIDASPVUSD | VIDAx | ZUSD | asset_class_requires_resolution |
| VIDAxUSD | VIDAx | ZUSD | asset_class_requires_resolution |
| VOOSPVUSD | VOOx | ZUSD | asset_class_requires_resolution |
| VOOxUSD | VOOx | ZUSD | asset_class_requires_resolution |
| VRTSPVUSD | VRTx | ZUSD | asset_class_requires_resolution |
| VRTxUSD | VRTx | ZUSD | asset_class_requires_resolution |
| VSPVUSD | Vx | ZUSD | asset_class_requires_resolution |
| VTISPVUSD | VTIx | ZUSD | asset_class_requires_resolution |
| VTIxUSD | VTIx | ZUSD | asset_class_requires_resolution |
| VTSPVUSD | VTx | ZUSD | asset_class_requires_resolution |
| VTxUSD | VTx | ZUSD | asset_class_requires_resolution |
| VUGSPVUSD | VUGx | ZUSD | asset_class_requires_resolution |
| VUGxUSD | VUGx | ZUSD | asset_class_requires_resolution |
| VXUSSPVUSD | VXUSx | ZUSD | asset_class_requires_resolution |
| VXUSxUSD | VXUSx | ZUSD | asset_class_requires_resolution |
| VxUSD | Vx | ZUSD | asset_class_requires_resolution |
| WBDSPVUSD | WBDx | ZUSD | asset_class_requires_resolution |
| WBDxUSD | WBDx | ZUSD | asset_class_requires_resolution |
| WENSPVUSD | WENx | ZUSD | asset_class_requires_resolution |
| WENxUSD | WENx | ZUSD | asset_class_requires_resolution |
| WULFSPVUSD | WULFx | ZUSD | asset_class_requires_resolution |
| WULFxUSD | WULFx | ZUSD | asset_class_requires_resolution |
| XLESPVUSD | XLEx | ZUSD | asset_class_requires_resolution |
| XLExUSD | XLEx | ZUSD | asset_class_requires_resolution |
| XOMSPVUSD | XOMx | ZUSD | asset_class_requires_resolution |
| XOMxUSD | XOMx | ZUSD | asset_class_requires_resolution |
| XOPSPVUSD | XOPx | ZUSD | asset_class_requires_resolution |
| XOPxUSD | XOPx | ZUSD | asset_class_requires_resolution |
| YLDESPVUSD | YLDEx | ZUSD | asset_class_requires_resolution |
| YLDExUSD | YLDEx | ZUSD | asset_class_requires_resolution |

## Tests and boundary

```powershell
.venv-db/Scripts/python.exe -m pytest -q tests/test_asset_catalogue.py
.venv-db/Scripts/python.exe -m pytest -q tests
$env:KRAKEN_CATALOGUE_SMOKE = '1'
.venv-db/Scripts/python.exe -m pytest -q tests/test_asset_catalogue.py::test_real_public_discovery
Remove-Item Env:KRAKEN_CATALOGUE_SMOKE
```

Mocked tests cover complete class/venue requests, BTC/ETH/DOGE/REP aliases and literal X-prefixed assets, collisions, unknown classes, missing metadata, optional nulls, decimal fidelity, repeat/status updates, disappeared records, foreign keys, rollback, receipt-publication interruption, read-only reporting, no private endpoints and no research-universe filtering. The opt-in real integration test uses a temporary database and does not alter the configured catalogue.

The implemented standard currency spot catalogue is populated and verified, but broader discovery remains **PARTIAL** due to the explicitly unresolved source records above. No complete-all-instruments claim is made. These records require independent identity/venue/schema review; no subsequent milestone, historical ingestion, order-book collection, cost modelling, screening thresholds or live orders have been implemented. Existing notebook changes remain untouched.

## Local test receipt

Focused tests: **42 passed, 1 skipped** (`python -m pytest -q tests/test_asset_catalogue.py`). Full offline regression: **574 passed, 3 skipped** (`python -m pytest -q tests`). Opt-in actual public catalogue integration: **1 passed** (`KRAKEN_CATALOGUE_SMOKE=1`, test_real_public_discovery). Offline skips are the two opted-in public-network tests plus Windows symlink privilege. All tests are successful; live discovery remains partial (exit 2) because of the documented 383 unresolved source records, not a test failure. Final parser validation reproduced the exact actual receipt counts without new database changes. Compilation passed.
