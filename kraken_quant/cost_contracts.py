"""Immutable raw-input contracts. No execution or financial calculations."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from .storage_schema import validate_pair_id, utc_timestamp

CalculationStatus = Literal["inputs_ready", "inputs_incomplete", "algorithm_not_implemented", "calculated", "calculation_failed"]


@dataclass(frozen=True)
class CostRequest:
    pair_id: str
    side: Literal["buy", "sell"]
    notional_aud: Decimal
    execution_type: Literal["maker", "taker"]
    observation_time: datetime
    model_version: str

    def __post_init__(self):
        validate_pair_id(self.pair_id)
        if self.side not in ("buy", "sell") or self.execution_type not in ("maker", "taker"):
            raise ValueError("Unrecognized side or execution type")
        if not isinstance(self.notional_aud, Decimal) or not self.notional_aud.is_finite() or self.notional_aud <= 0:
            raise ValueError("Positive finite Decimal AUD notional required")
        stamp = utc_timestamp(self.observation_time)
        if self.observation_time.utcoffset() != timedelta(0):
            raise ValueError("UTC request timestamp required")
        object.__setattr__(self, "observation_time", stamp)
        if not isinstance(self.model_version, str) or not self.model_version.strip():
            raise ValueError("Model version required")


@dataclass(frozen=True)
class FeeTier:
    label: str
    conditions: tuple[tuple[str, str], ...]
    maker_pct: Decimal | None
    taker_pct: Decimal | None


@dataclass(frozen=True)
class FeeTable:
    category: str
    tier_currency: str | None
    tiers: tuple[FeeTier, ...]


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    pair_id: str
    kraken_pair_id: str
    exchange: str
    source_url: str
    retrieved_at: datetime
    effective_at: datetime | None
    verification_status: str
    public_tables: tuple[FeeTable, ...]
    account_specific: bool
    account_maker_pct: Decimal | None
    account_taker_pct: Decimal | None
    applicable_tier: str | None
    category: str | None
    present_day_assumption: bool = False
    account_source_url: str | None = None


@dataclass(frozen=True)
class SourceReference:
    dataset: str
    pair_id: str
    timestamp: datetime
    content_hash: str
    response_id: str
    response_hash: str
    endpoint: str
    latency_seconds: Decimal | None
    timestamp_semantics: str


@dataclass(frozen=True)
class QuoteSnapshot:
    pair_id: str
    kraken_pair_id: str
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None
    ask_size: Decimal | None
    source: SourceReference


@dataclass(frozen=True)
class BookLevel:
    side: Literal["bid", "ask"]
    level: int
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class OrderBookSnapshot:
    pair_id: str
    timestamp: datetime
    levels: tuple[BookLevel, ...]
    requested_depth: int
    actual_bid_depth: int
    actual_ask_depth: int
    completeness: str
    exchange_level_update_epochs: tuple[str, ...]
    source: SourceReference


@dataclass(frozen=True)
class CandleSnapshot:
    pair_id: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_base: Decimal
    trade_count: int | None
    is_complete: bool
    source: SourceReference


@dataclass(frozen=True)
class MarketMetadata:
    pair_id: str
    kraken_pair_id: str
    base_asset: str
    quote_asset: str
    status: str
    last_seen: datetime
    min_order_qty: Decimal | None
    min_order_value: Decimal | None
    qty_precision: int | None
    price_precision: int | None


@dataclass(frozen=True)
class PreparationConfig:
    max_market_age_seconds: int = 300
    max_candle_age_seconds: int = 7200
    max_fee_age_seconds: int = 86400
    allow_present_day_fee_assumption: bool = False

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in (self.max_market_age_seconds,self.max_candle_age_seconds,self.max_fee_age_seconds)):
            raise ValueError("Positive freshness limits required")
        if type(self.allow_present_day_fee_assumption) is not bool:
            raise ValueError("Explicit boolean fee-assumption policy required")


@dataclass(frozen=True)
class InputProvenance:
    contract_version: int
    source_namespace: str
    fee_version: str | None
    metadata_hash: str | None
    sources: tuple[SourceReference, ...]
    config: PreparationConfig
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class CostModelInputs:
    request: CostRequest
    quote: QuoteSnapshot | None
    order_book: OrderBookSnapshot | None
    fee_schedule: FeeSchedule | None
    market_metadata: MarketMetadata | None
    candle: CandleSnapshot | None
    provenance: InputProvenance


@dataclass(frozen=True)
class PreparedInputs:
    inputs: CostModelInputs
    status: CalculationStatus
    missing_inputs: tuple[str, ...]
    warnings: tuple[str, ...]

    def __post_init__(self):
        if self.status not in ("inputs_ready","inputs_incomplete") or (self.status=="inputs_ready")!= (not self.missing_inputs):
            raise ValueError("Input preparation cannot claim a calculation or hide missing inputs")


@dataclass(frozen=True)
class CostResult:
    request: CostRequest
    provenance: InputProvenance
    status: CalculationStatus = "algorithm_not_implemented"
    fee_pct: Decimal | None = None
    half_spread_pct: Decimal | None = None
    additional_slippage_pct: Decimal | None = None
    total_cost_pct: Decimal | None = None
    fillability: Literal["unknown", "fillable", "partial", "unfillable"] = "unknown"
    filled_quantity: Decimal | None = None
    unfilled_quantity: Decimal | None = None
    execution_price: Decimal | None = None

    def __post_init__(self):
        if self.status not in ("inputs_ready","inputs_incomplete","algorithm_not_implemented","calculation_failed"):
            raise ValueError("Calculated results are disabled until user algorithms are approved after Milestone 10")
        if self.fillability != "unknown" or any(v is not None for v in (self.fee_pct,self.half_spread_pct,self.additional_slippage_pct,self.total_cost_pct,self.filled_quantity,self.unfilled_quantity,self.execution_price)):
            raise ValueError("Computed result values are disabled in this milestone")
