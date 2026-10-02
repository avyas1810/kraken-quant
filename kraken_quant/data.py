"""Daily UTC data validation, immutable snapshots and causal split access.

Snapshots are data-quality artifacts, not test-release authorization. No API
client or credentials are used here. A caller explicitly supplies historical bars.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .config import CONFIG, utc_day

COLUMNS = ("open", "high", "low", "close", "volume")
DAY = pd.Timedelta(days=1)


def utc_instant(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Decision/validation time must be timezone-aware")
    return stamp.tz_convert("UTC")


def validate_bars(bars, start, end, *, as_of, naive_timestamps_are_utc=False):
    """Reject bad data without filling gaps or deleting conflicting observations."""
    start, end = utc_day(start), utc_day(end)
    if start > end:
        raise ValueError("Data start exceeds end")
    if bars.empty:
        raise ValueError("No historical data returned")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("Bars require a DatetimeIndex")
    if any(column not in bars for column in COLUMNS) or bars.columns.duplicated().any():
        raise ValueError("Required unique OHLCV columns are missing")
    result = bars.loc[:, list(COLUMNS)].copy(deep=True)
    if result.index.hasnans:
        raise ValueError("Missing timestamps")
    if result.index.tz is None:
        if not naive_timestamps_are_utc:
            raise ValueError("Naive timestamps require explicit UTC acknowledgement")
        result.index = result.index.tz_localize("UTC")
    else:
        result.index = result.index.tz_convert("UTC")
    if result.index.duplicated().any():
        raise ValueError("Duplicate timestamps")
    if not result.index.equals(result.index.normalize()):
        raise ValueError("Daily bars must open at UTC midnight")
    result = result.sort_index()
    expected = pd.date_range(start, end, freq="D")
    if not result.index.equals(expected):
        raise ValueError("Missing days/boundaries or observations outside requested range")
    if (result.index + DAY > utc_instant(as_of)).any():
        raise ValueError("Uncompleted bars are not allowed")
    try:
        values = result.to_numpy(dtype="float64")
    except (TypeError, ValueError) as error:
        raise ValueError("OHLCV must be numeric") from error
    if not np.isfinite(values).all():
        raise ValueError("Missing or nonfinite OHLCV values")
    if (values[:, :4] <= 0).any():
        raise ValueError("Prices must be positive")
    if (values[:, 4] < 0).any():
        raise ValueError("Volume must be nonnegative")
    opening, high, low, close = (values[:, index] for index in range(4))
    if ((low > high) | (opening < low) | (opening > high)
            | (close < low) | (close > high)).any():
        raise ValueError("Invalid OHLC relationships")
    result = pd.DataFrame(values, index=result.index, columns=COLUMNS)
    result.index.name = "bar_open_utc"
    return result


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class SnapshotManifest:
    version: int
    symbol: str
    source: str
    source_symbol: str
    frequency: str
    start: str
    end: str
    rows: int
    sha256: str


@dataclass(frozen=True)
class HistoricalSnapshot:
    manifest: SnapshotManifest
    _payload: bytes

    def __post_init__(self):
        if not isinstance(self._payload, bytes):
            raise ValueError("Snapshot payload must be immutable bytes")
        body = json.loads(self._payload)
        if self.manifest.version != 1 or body.get("version") != 1:
            raise ValueError("Unsupported snapshot version")
        if hashlib.sha256(self._payload).hexdigest() != self.manifest.sha256:
            raise ValueError("Snapshot hash mismatch")
        metadata = {key: body[key] for key in (
            "version", "symbol", "source", "source_symbol", "frequency", "start", "end"
        )}
        expected = asdict(self.manifest)
        if any(expected[key] != value for key, value in metadata.items()):
            raise ValueError("Manifest metadata mismatch")
        if self.manifest.frequency != "1d" or len(body["bars"]) != self.manifest.rows:
            raise ValueError("Invalid daily snapshot manifest")
        if not self.manifest.source.strip() or not self.manifest.source_symbol.strip():
            raise ValueError("Source provenance is required")
        if self.manifest.symbol not in CONFIG.universe:
            raise ValueError("Symbol outside locked research universe")
        if not CONFIG.data_start <= utc_day(self.manifest.start) <= utc_day(self.manifest.end) <= CONFIG.data_end:
            raise ValueError("Snapshot outside locked full-data boundaries")
        # Revalidate restored snapshots, including their completion timestamp.
        self._frame(as_of=body["validated_as_of"])

    @classmethod
    def create(cls, bars, *, symbol, source, source_symbol, start, end, as_of,
               naive_timestamps_are_utc=False):
        clean = validate_bars(bars, start, end, as_of=as_of,
                              naive_timestamps_are_utc=naive_timestamps_are_utc)
        metadata = dict(version=1, symbol=symbol, source=source, source_symbol=source_symbol,
                        frequency="1d", start=utc_day(start).isoformat(), end=utc_day(end).isoformat())
        body = dict(metadata, validated_as_of=utc_instant(as_of).isoformat(),
                    bars=[[stamp.isoformat(), *row] for stamp, row in zip(clean.index, clean.to_numpy().tolist())])
        payload = canonical_json(body)
        manifest = SnapshotManifest(**metadata, rows=len(clean), sha256=hashlib.sha256(payload).hexdigest())
        return cls(manifest, payload)

    def _frame(self, *, as_of):
        body = json.loads(self._payload)
        rows = body["bars"]
        frame = pd.DataFrame([row[1:] for row in rows], columns=COLUMNS,
                             index=pd.DatetimeIndex([row[0] for row in rows]))
        return validate_bars(frame, self.manifest.start, self.manifest.end, as_of=as_of)

    def write(self, path):
        """Create once: never overwrite a previously recorded snapshot artifact."""
        envelope = canonical_json(dict(manifest=asdict(self.manifest), payload=json.loads(self._payload)))
        with Path(path).open("xb") as handle:
            handle.write(envelope)

    @classmethod
    def read(cls, path):
        body = json.loads(Path(path).read_bytes())
        return cls(SnapshotManifest(**body["manifest"]), canonical_json(body["payload"]))


class RestrictedHistory:
    """Application-level leakage boundary; no test-unlock switch exists in this slice."""

    def __init__(self, snapshots):
        self._snapshots = {}
        for snapshot in snapshots:
            symbol = snapshot.manifest.symbol
            if symbol in self._snapshots:
                raise ValueError("Duplicate symbol snapshot")
            self._snapshots[symbol] = snapshot

    def window(self, symbol, *, split, start, end, decision_time, warmup_days=0):
        period = CONFIG.split(split)
        if period.name == "test":
            raise PermissionError("Test-period access is locked")
        start, end = utc_day(start), utc_day(end)
        if not period.start <= start <= end <= period.end:
            raise PermissionError("Requested observations cross split boundaries")
        if isinstance(warmup_days, bool) or not isinstance(warmup_days, int) or warmup_days < 0:
            raise ValueError("Warm-up must be a nonnegative integer number of days")
        first = start - warmup_days * DAY
        if first < CONFIG.data_start:
            raise ValueError("Insufficient permitted warm-up history")
        decision = utc_instant(decision_time)
        if end + DAY > decision:
            raise PermissionError("Requested bar was not complete at decision time")
        snapshot = self._snapshots[symbol]
        frame = snapshot._frame(as_of=json.loads(snapshot._payload)["validated_as_of"])
        selected = frame.loc[first:end].copy(deep=True)
        if not selected.index.equals(pd.date_range(first, end, freq="D")):
            raise ValueError("Snapshot lacks requested history or warm-up")
        return selected
