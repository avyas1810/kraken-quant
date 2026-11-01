"""Versioned storage contracts, independent of strategy and research universes."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re

SCHEMA_VERSION = 1
DECIMAL_PRECISION, DECIMAL_SCALE = 38, 18
DATASETS = ("prices_1m", "quotes_1m", "orderbook", "costs_1m")
LOGICAL_KEYS = {
    "prices_1m": ("pair_id", "timestamp"),
    "quotes_1m": ("pair_id", "timestamp"),
    "orderbook": ("pair_id", "timestamp", "side", "level"),
    "costs_1m": ("pair_id", "timestamp", "side", "order_size_aud", "fee_type", "model_version"),
}


def validate_pair_id(pair_id):
    if not isinstance(pair_id, str) or re.fullmatch(r"[A-Z0-9]{1,20}-[A-Z0-9]{1,20}", pair_id) is None:
        raise ValueError("pair_id must be canonical BASE-QUOTE (uppercase alphanumeric assets)")
    return pair_id


def validate_dataset(dataset):
    if dataset not in DATASETS:
        raise ValueError("Unsupported storage dataset")
    return dataset


def utc_timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware datetime")
    # Pandas Timestamp is a datetime subclass; do not silently truncate nanoseconds.
    if getattr(value, "nanosecond", 0):
        raise ValueError("Storage timestamps have microsecond precision")
    return value.astimezone(timezone.utc)


def decimal_value(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Decimal fields require exact Decimal, integer or decimal string; floats rejected")
    try:
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError("Nonfinite decimal")
        # Arrow enforces decimal128(38,18) representability, without rounding.
        import pyarrow as pa
        pa.scalar(result, type=pa.decimal128(DECIMAL_PRECISION, DECIMAL_SCALE))
    except (InvalidOperation, OverflowError, ValueError) as error:
        raise ValueError("Decimal does not fit decimal128(38,18)") from error
    return result


def dataset_schema(dataset):
    import pyarrow as pa
    validate_dataset(dataset)
    decimal = pa.decimal128(DECIMAL_PRECISION, DECIMAL_SCALE)
    fields = [pa.field("pair_id", pa.string(), nullable=False, metadata={"unit": "identifier"}),
              pa.field("timestamp", pa.timestamp("us", tz="UTC"), nullable=False,
                       metadata={"unit": "UTC observation time; prices: minute-bar open"})]
    def field(name, dtype, unit, nullable=False):
        fields.append(pa.field(name, dtype, nullable=nullable, metadata={"unit": unit}))
    if dataset == "prices_1m":
        for name in ("open", "high", "low", "close"):
            field(name, decimal, "quote currency per base asset")
        field("volume_base", decimal, "base asset units")
        field("trade_count", pa.int64(), "trades in minute", nullable=True)
        field("is_complete", pa.bool_(), "completed bar flag")
    elif dataset == "quotes_1m":
        for name in ("bid", "ask"):
            field(name, decimal, "quote currency per base asset")
        for name in ("bid_size", "ask_size"):
            field(name, decimal, "base asset units", nullable=True)
    elif dataset == "orderbook":
        field("side", pa.string(), "bid or ask")
        field("level", pa.int32(), "one-based book level")
        field("price", decimal, "quote currency per base asset")
        field("quantity", decimal, "base asset units")
    else:
        field("side", pa.string(), "buy or sell")
        field("order_size_aud", decimal, "AUD notional")
        field("fee_type", pa.string(), "fee classification supplied by source/model")
        for name in ("fee_pct", "half_spread_pct", "slippage_pct", "total_cost_pct"):
            field(name, decimal, "percentage points (0.4 means 0.4%)", nullable=True)
        field("model_version", pa.string(), "cost-model identifier")
    return pa.schema(fields, metadata={"schema_version": str(SCHEMA_VERSION), "dataset": dataset,
                     "logical_key": json.dumps(LOGICAL_KEYS[dataset])})


def table_from_records(dataset, records):
    import pyarrow as pa
    schema = dataset_schema(dataset)
    cleaned = []
    for record in records:
        if not isinstance(record, dict) or set(record) - set(schema.names):
            raise ValueError("Record has unknown fields or is not a mapping")
        row = {}
        for field in schema:
            value = record.get(field.name)
            if value is None:
                if not field.nullable:
                    raise ValueError(f"Required field missing: {field.name}")
            elif pa.types.is_decimal(field.type):
                value = decimal_value(value)
            elif pa.types.is_timestamp(field.type):
                value = utc_timestamp(value)
            elif pa.types.is_integer(field.type):
                if type(value) is not int:
                    raise ValueError("Integer fields require integers")
            elif pa.types.is_boolean(field.type):
                if type(value) is not bool:
                    raise ValueError("Boolean fields require booleans")
            elif not isinstance(value, str):
                raise ValueError("String fields require strings")
            row[field.name] = value
        cleaned.append(row)
    table = pa.Table.from_pylist(cleaned, schema=schema)
    validate_table(dataset, table)
    return table


def validate_table(dataset, table):
    import pyarrow as pa
    schema = dataset_schema(dataset)
    if not isinstance(table, pa.Table) or not table.schema.equals(schema, check_metadata=True):
        raise ValueError("Parquet schema mismatch (types, nullability, units or version)")
    table.validate(full=True)
    for field in schema:
        if not field.nullable and table[field.name].null_count:
            raise ValueError("Null in required field")
    for row in table.to_pylist():
        validate_pair_id(row["pair_id"])
        stamp = utc_timestamp(row["timestamp"])
        if dataset == "prices_1m":
            if stamp.second or stamp.microsecond:
                raise ValueError("Price bar timestamp must open on an exact minute")
            if any(row[name] <= 0 for name in ("open", "high", "low", "close")) or row["volume_base"] < 0:
                raise ValueError("Invalid price or volume")
            if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]) or row["low"] > row["high"]:
                raise ValueError("Inconsistent OHLC bounds")
            if row["trade_count"] is not None and row["trade_count"] < 0:
                raise ValueError("Negative trade count")
        elif dataset == "quotes_1m":
            if row["bid"] <= 0 or row["ask"] < row["bid"]:
                raise ValueError("Invalid or crossed quote")
            if any(row[name] is not None and row[name] < 0 for name in ("bid_size", "ask_size")):
                raise ValueError("Negative quote size")
        elif dataset == "orderbook":
            if row["side"] not in ("bid", "ask") or row["level"] < 1 or row["price"] <= 0 or row["quantity"] < 0:
                raise ValueError("Invalid order-book observation")
        else:
            if row["side"] not in ("buy", "sell") or row["order_size_aud"] <= 0:
                raise ValueError("Invalid cost side or notional")
            if not row["fee_type"].strip() or not row["model_version"].strip():
                raise ValueError("Cost classification and model version required")
            parts = [row[name] for name in ("fee_pct", "half_spread_pct", "slippage_pct")]
            if row["half_spread_pct"] is not None and row["half_spread_pct"] < 0:
                raise ValueError("Half spread must be nonnegative")
            if any(value is None for value in parts):
                if row["total_cost_pct"] is not None:
                    raise ValueError("Unknown cost component requires unknown total")
            else:
                from decimal import localcontext
                with localcontext() as context:
                    context.prec = 80
                    if row["total_cost_pct"] != sum(parts, Decimal(0)):
                        raise ValueError("Total cost must equal fee + half spread + slippage")
    return table
