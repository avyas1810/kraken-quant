"""Public Kraken catalogue discovery; no strategy universe, trading or ingestion."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .db_environment import load_config
from .metadata_store import connect_metadata
from .operations import ProcessLock
from .storage_schema import utc_timestamp, validate_pair_id

ASSET_CLASSES = ("currency", "tokenized_asset")
VENUES = "international,bitnomial_exchange"
COLUMNS = ("pair_id", "kraken_pair_id", "base_asset", "quote_asset", "status", "first_seen", "last_seen",
           "min_order_qty", "min_order_value", "qty_precision", "price_precision")


class DiscoveryError(ValueError):
    pass


def _result(body, *, allow_empty=False):
    if not isinstance(body, dict) or body.get("error") != [] or not isinstance(body.get("result"), dict):
        raise DiscoveryError("Invalid public Kraken response or exchange error")
    if not body["result"] and not allow_empty:
        raise DiscoveryError("Empty mandatory public metadata response")
    return body["result"]


def _request(transport, endpoint, params):
    if endpoint not in ("AssetPairs", "Assets"):
        raise DiscoveryError("Only public catalogue endpoints are permitted")
    try:
        return _result(transport(endpoint, params), allow_empty=params.get("aclass_base", params.get("aclass")) == "tokenized_asset")
    except DiscoveryError:
        raise
    except Exception:
        raise DiscoveryError("Public catalogue request failed; private error details suppressed") from None


def fetch_sources(transport=None):
    if transport is None:
        from .market_data import public_get
        transport = public_get
    sources, native_assets, display_assets = [], {}, {}
    for asset_class in ASSET_CLASSES:
        pairs = _request(transport, "AssetPairs", {"aclass_base": asset_class, "execution_venue": VENUES})
        native = _request(transport, "Assets", {"aclass": asset_class})
        display = _request(transport, "Assets", {"aclass": asset_class, "assetVersion": 1})
        sources.append({"asset_class": asset_class, "pairs": pairs})
        native_assets[asset_class], display_assets[asset_class] = native, display
    return sources, native_assets, display_assets


def _symbol(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Z0-9]{1,20}", value) is None:
        raise DiscoveryError("unrepresentable_canonical_asset")
    return value


def _asset(native_id, asset_class, native, display):
    """Join by exchange-provided (class, altname), never by stripping X/Z prefixes."""
    if not isinstance(native_id, str) or not native_id.strip():
        raise DiscoveryError("missing_native_asset_identity")
    row = native.get(asset_class, {}).get(native_id)
    if (not isinstance(row, dict) or row.get("aclass") != asset_class
            or not isinstance(row.get("altname"), str) or not row["altname"].strip()):
        raise DiscoveryError("missing_native_asset_identity")
    candidates = [(key, item) for key, item in display.get(asset_class, {}).items()
                  if isinstance(item, dict) and item.get("aclass") == asset_class and item.get("altname") == row["altname"]]
    if len(candidates) != 1:
        raise DiscoveryError("ambiguous_display_asset_identity")
    return _symbol(candidates[0][0]), row["altname"]


def _decimal(raw):
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
        raise DiscoveryError("invalid_decimal_constraint")
    try:
        number = Decimal(raw)
        if not number.is_finite() or number < 0:
            raise ValueError()
        if number.as_tuple().exponent < -18 or number.adjusted() >= 20:
            raise ValueError()
        text = format(number, "f")
        integer, _, fraction = text.partition(".")
        if len(integer) > 20 or len(fraction) > 18:
            raise ValueError()
        return text
    except (InvalidOperation, ValueError):
        raise DiscoveryError("unrepresentable_decimal_constraint") from None


def _precision(raw):
    if raw is None:
        return None
    if type(raw) is not int or not 0 <= raw <= 18:
        raise DiscoveryError("invalid_decimal_precision")
    return raw


def parse_sources(sources, native_assets, display_assets, observed_at):
    observed_at = utc_timestamp(observed_at).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    parsed, unresolved, excluded = [], [], []
    for source in sources:
        for native_pair, row in source["pairs"].items():
            identity = {"kraken_pair_id": native_pair, "requested_asset_class": source["asset_class"]}
            if isinstance(row, dict):
                identity.update({key: row.get(key) for key in ("base", "quote", "aclass_base", "aclass_quote", "execution_venue", "wsname")})
            try:
                if not isinstance(native_pair, str) or not native_pair or not isinstance(row, dict):
                    raise DiscoveryError("malformed_pair_record")
                # Only explicit derivative metadata may be classified non-spot.
                kind = row.get("instrument_type", "spot")
                if kind in ("future", "futures", "perpetual", "swap", "option"):
                    excluded.append({**identity, "reason": "explicit_non_spot_instrument"})
                    continue
                if kind != "spot":
                    raise DiscoveryError("ambiguous_instrument_type")
                if row.get("aclass_base") != "currency" or row.get("aclass_quote") != "currency":
                    raise DiscoveryError("asset_class_requires_resolution")
                if source["asset_class"] != "currency":
                    raise DiscoveryError("asset_class_response_mismatch")
                if row.get("execution_venue") != "international":
                    raise DiscoveryError("execution_venue_requires_resolution")
                base, base_alias = _asset(row.get("base"), "currency", native_assets, display_assets)
                quote, quote_alias = _asset(row.get("quote"), "currency", native_assets, display_assets)
                if row.get("wsname") != base_alias + "/" + quote_alias:
                    raise DiscoveryError("pair_asset_identity_mismatch")
                pair_id = validate_pair_id(base + "-" + quote)
                status = row.get("status")
                if not isinstance(status, str) or not status.strip():
                    raise DiscoveryError("missing_trading_status")
                parsed.append({"pair_id": pair_id, "kraken_pair_id": native_pair, "base_asset": base,
                    "quote_asset": quote, "status": status, "first_seen": observed_at, "last_seen": observed_at,
                    "min_order_qty": _decimal(row.get("ordermin")), "min_order_value": _decimal(row.get("costmin")),
                    "qty_precision": _precision(row.get("lot_decimals")), "price_precision": _precision(row.get("pair_decimals")),
                    "source_identity": identity})
            except (DiscoveryError, ValueError, TypeError) as error:
                reason = str(error) if isinstance(error, DiscoveryError) else "invalid_pair_identity"
                unresolved.append({**identity, "reason": reason})
    canonical, native = Counter(row["pair_id"] for row in parsed), Counter(row["kraken_pair_id"] for row in parsed)
    accepted = []
    for row in parsed:
        if canonical[row["pair_id"]] > 1 or native[row["kraken_pair_id"]] > 1:
            unresolved.append({**row["source_identity"], "pair_id": row["pair_id"], "reason": "identifier_collision"})
        else:
            accepted.append(row)
    total = sum(len(source["pairs"]) for source in sources)
    if total != len(accepted) + len(excluded) + len(unresolved):
        raise DiscoveryError("Source record reconciliation failed")
    return {"observed_at": observed_at, "source_records": total, "classified_spot": len(accepted),
            "excluded_non_spot": len(excluded), "unresolved_count": len(unresolved),
            "records": accepted, "excluded": excluded, "unresolved": unresolved}


def _rows(db):
    return [dict(zip(COLUMNS, row)) for row in db.execute("SELECT " + ",".join(COLUMNS) + " FROM assets ORDER BY pair_id")]


def _fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _atomic_json(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".catalogue-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(content, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def discover(data_dir, *, transport=None, clock=lambda: datetime.now(timezone.utc)):
    # Validate the existing store BEFORE network requests; never create a database.
    with connect_metadata(data_dir, readonly=True):
        pass
    lock = ProcessLock(Path(data_dir) / "catalogue-discovery")
    try:
        sources, native, display = fetch_sources(transport)
        result = parse_sources(sources, native, display, clock())
        receipt = {**result, "run_id": uuid.uuid4().hex, "schema_version": 1,
                   "source_requests": [{"asset_class": s["asset_class"], "records": len(s["pairs"])} for s in sources],
                   "raw_sources": sources, "native_assets": native, "display_assets": display}
        with connect_metadata(data_dir) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = _rows(db)
            by_pair = {row["pair_id"]: row for row in previous}
            by_native = {row["kraken_pair_id"]: row for row in previous}
            inserted = updated = unchanged = 0
            for row in result["records"]:
                old, old_native = by_pair.get(row["pair_id"]), by_native.get(row["kraken_pair_id"])
                if ((old and old["kraken_pair_id"] != row["kraken_pair_id"]) or
                    (old_native and old_native["pair_id"] != row["pair_id"])):
                    raise DiscoveryError("Existing catalogue identity conflict; entire update rolled back")
                if old and result["observed_at"] < old["last_seen"]:
                    raise DiscoveryError("Observation clock regressed; entire update rolled back")
                values = [row[name] for name in COLUMNS]
                if old:
                    values[COLUMNS.index("first_seen")] = old["first_seen"]
                    if all(old[name] == value for name,value in zip(COLUMNS,values)):
                        unchanged += 1
                        continue
                    updated += 1
                else:
                    inserted += 1
                db.execute("INSERT INTO assets (" + ",".join(COLUMNS) + ") VALUES (" + ",".join("?" for _ in COLUMNS) + ") "
                    "ON CONFLICT(pair_id) DO UPDATE SET " + ",".join(name+"=excluded."+name for name in COLUMNS if name not in ("pair_id", "first_seen")), values)
            current = _rows(db)
            observed = {row["kraken_pair_id"] for row in result["records"]}
            receipt.update(inserted=inserted, updated=updated, unchanged=unchanged, persisted=len(result["records"]),
                previously_stored=len(previous), retained_previous=len(previous),
                absent_retained=[row["pair_id"] for row in previous if row["kraken_pair_id"] not in observed],
                total_assets=len(current), catalogue_sha256=_fingerprint(current))
            # Prepare immutable evidence before COMMIT. Orphan receipts are never selected.
            audit_dir = Path(data_dir) / "catalogue_discovery"
            receipt_path = audit_dir / (receipt["run_id"] + ".json")
            if receipt_path.exists():
                raise DiscoveryError("Discovery receipt identifier collision")
            _atomic_json(receipt_path, receipt)
        # A crash here cannot corrupt SQL; report detects a stale/missing receipt fingerprint.
        _atomic_json(audit_dir / "latest.json", receipt)
        return {key:value for key,value in receipt.items() if key not in ("records", "raw_sources", "native_assets", "display_assets")}
    finally:
        lock.close()


def catalogue_report(data_dir):
    with connect_metadata(data_dir, readonly=True) as db:
        assets = _rows(db)
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    receipt_path = Path(data_dir) / "catalogue_discovery" / "latest.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else None
    current = bool(receipt and receipt.get("catalogue_sha256") == _fingerprint(assets))
    aud = [row for row in assets if row["quote_asset"] == "AUD"]
    observed = receipt["observed_at"] if current else None
    online = [row for row in aud if row["status"] == "online" and row["last_seen"] == observed]
    inactive = [row for row in aud if row["status"] != "online"]
    unresolved = receipt.get("unresolved", []) if current else None
    return {"total_assets": len(assets), "aud_pairs": len(aud), "stored_online_aud_pairs": sum(r["status"] == "online" for r in aud),
            "observed_online_aud_pairs": len(online), "inactive_aud_pairs": len(inactive), "integrity_check": integrity,
            "audit_status": "current" if current else "missing_or_stale", "all_spot_pairs": assets,
            "aud_spot_pairs": aud, "online_aud_spot_pairs": online, "inactive_aud_spot_pairs": inactive,
            "requires_resolution": unresolved, "not_observed_latest": receipt.get("absent_retained", []) if current else None,
            "latest_discovery": {k:receipt[k] for k in ("observed_at", "source_records", "classified_spot", "excluded_non_spot", "unresolved_count", "persisted", "inserted", "updated", "unchanged", "retained_previous")} if current else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("discover", "report"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--data-dir")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.root, data_dir=args.data_dir)
        result = discover(config.data_dir) if args.command == "discover" else catalogue_report(config.data_dir)
        if args.summary:
            result = {k:v for k,v in result.items() if k not in ("all_spot_pairs", "online_aud_spot_pairs", "inactive_aud_spot_pairs")}
        print(json.dumps(result, indent=2))
        if args.command == "discover":
            return 2 if result["unresolved_count"] else 0
        return int(result["integrity_check"] != "ok" or result["audit_status"] != "current")
    except Exception:
        print(json.dumps({"error": "Catalogue operation failed; check public connectivity, initialized schema and identity conflicts. Private details suppressed."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
