"""Venue-neutral target-weight strategies and arbitrary causal feature objects."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math
from types import MappingProxyType
from typing import Any, Protocol

import pandas as pd

from .data import DAY, HistoricalSnapshot, RestrictedHistory, utc_instant


@dataclass(frozen=True, order=True)
class Instrument:
    venue: str
    symbol: str
    base: str
    quote: str
    market_type: str = "spot"

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in asdict(self).values()):
            raise ValueError("Instrument fields must be nonempty strings")
        if self.symbol != f"{self.base}/{self.quote}":
            raise ValueError("Instrument symbol must match base/quote")


class VenueHistory:
    """Per-venue wrappers preserve the locked reader, even for identical symbols."""

    def __init__(self, snapshots):
        self._readers, self._manifests = {}, {}
        for snapshot in snapshots:
            if not isinstance(snapshot, HistoricalSnapshot):
                raise TypeError("Expected validated HistoricalSnapshot")
            manifest = snapshot.manifest
            base, quote = manifest.source_symbol.split("/")
            key = Instrument(manifest.source.lower(), manifest.source_symbol, base, quote)
            if key in self._readers:
                raise ValueError("Duplicate venue/instrument snapshot")
            if base != manifest.symbol.split("/")[0]:
                raise ValueError("Source base does not match research instrument")
            self._readers[key] = (RestrictedHistory([snapshot]), manifest.symbol)
            self._manifests[key] = manifest

    @property
    def instruments(self):
        return tuple(sorted(self._readers))

    @property
    def manifests(self):
        return MappingProxyType(self._manifests.copy())

    def window(self, instrument, **kwargs):
        reader, research_symbol = self._readers[instrument]
        frame = reader.window(research_symbol, **kwargs)
        frame["venue"] = instrument.venue
        frame["symbol"] = instrument.symbol
        frame["base"] = instrument.base
        frame["quote"] = instrument.quote
        frame["timestamp"] = frame.index
        frame["data_type"] = "ohlcv"
        return frame


@dataclass(frozen=True)
class StrategyContext:
    timestamp: pd.Timestamp
    historical_data: Mapping
    features: Any
    positions: Mapping
    cash: float
    equity: float


class Strategy(Protocol):
    def target_weights(self, context: StrategyContext):
        """Mapping[Instrument, signed weight], or None to hold quantities."""
        ...


class StrategyPipeline:
    """Same interface in research, shadow and production; no exchange logic.

    Feature builder receives only completed-history copies and may return any
    object (graph, functor, neural tensor, etc.). External artifacts must separately
    satisfy provenance/training-only fitting; arbitrary Python is not sandboxed.
    """

    def __init__(self, strategy, feature_builder=None):
        if not callable(getattr(strategy, "target_weights", None)):
            raise TypeError("Strategy must implement target_weights(context)")
        self.strategy, self.feature_builder = strategy, feature_builder

    def evaluate(self, *, timestamp, historical_data, positions, cash, equity, bar_ends=None):
        timestamp = utc_instant(timestamp)
        copied = {}
        for key, frame in historical_data.items():
            if not isinstance(key, Instrument) or frame.empty:
                raise ValueError("Expected nonempty instrument history")
            if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
                raise ValueError("History timestamps must be timezone-aware")
            ends = frame.index + DAY if bar_ends is None else pd.DatetimeIndex(bar_ends[key])
            if len(ends) != len(frame) or ends.tz is None or (ends <= frame.index).any():
                raise ValueError("Invalid completed-bar boundaries")
            if (ends > timestamp).any():
                raise PermissionError("Feature/strategy history contains future or unfinished bars")
            copied[key] = frame.copy(deep=True)
        features = None
        if self.feature_builder is not None:
            features = self.feature_builder(MappingProxyType({k: v.copy(deep=True) for k, v in copied.items()}), timestamp)
        context = StrategyContext(timestamp, MappingProxyType(copied), features,
            MappingProxyType(dict(positions)), float(cash), float(equity))
        return self.strategy.target_weights(context)


def validate_weights(target, instruments, *, long_only=False, max_gross=1.0):
    if not math.isfinite(max_gross) or max_gross <= 0:
        raise ValueError("Gross budget must be positive and finite")
    if not isinstance(target, Mapping) or set(target) - set(instruments):
        raise ValueError("Targets must map selected instruments to weights")
    values = {}
    for key in instruments:
        raw = target.get(key, 0)
        if isinstance(raw, bool):
            raise ValueError("Boolean weights are invalid")
        value = float(raw)
        if not math.isfinite(value) or (long_only and value < 0):
            raise ValueError("Production weights must be finite and long-only" if long_only else "Nonfinite weight")
        values[key] = value
    if math.fsum(abs(w) for w in values.values()) > max_gross + 1e-12:
        raise ValueError("Target gross exposure exceeds budget; production leverage is prohibited")
    return values
