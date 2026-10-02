"""Synthetic checks only: no historical strategy-performance evaluation."""

from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from examples import research_config as locked
from kraken_quant.config import CONFIG, Split, utc_day
from kraken_quant.data import HistoricalSnapshot, RestrictedHistory, validate_bars
from kraken_quant.features import momentum


def bars(start="2023-12-29", end="2024-01-03"):
    index = pd.date_range(start, end, freq="D", tz="UTC")
    return pd.DataFrame({"open": 100., "high": 102., "low": 98.,
                         "close": 101., "volume": 10.}, index=index)


def snapshot(frame=None):
    frame = bars() if frame is None else frame
    return HistoricalSnapshot.create(
        frame, symbol=CONFIG.universe[0], source="synthetic", source_symbol="BTC/USDT",
        start=frame.index.min(), end=frame.index.max(), as_of="2024-01-04T00:00:00Z",
    )


def validate(frame):
    return validate_bars(frame, "2023-12-29", "2024-01-03", as_of="2024-01-04T00:00:00Z")


def test_config_imports_locked_authority_and_is_immutable():
    assert CONFIG.universe == tuple(locked.RESEARCH_UNIVERSE)
    assert CONFIG.data_start == utc_day(locked.DATA_START)
    assert CONFIG.data_end == utc_day(locked.DATA_END)
    for name, start, end in (
        ("train", locked.TRAIN_START, locked.TRAIN_END),
        ("validation", locked.VALIDATION_START, locked.VALIDATION_END),
        ("test", locked.TEST_START, locked.TEST_END),
    ):
        assert CONFIG.split(name) == Split(name, start, end)
    with pytest.raises(FrozenInstanceError):
        CONFIG.universe = ()


def test_splits_are_non_overlapping_and_consecutive():
    for previous, following in zip(CONFIG.splits, CONFIG.splits[1:]):
        assert previous.end < following.start
        assert previous.end + pd.Timedelta(days=1) == following.start
    overlap = (CONFIG.splits[0], Split("validation", CONFIG.splits[0].end,
                                     locked.VALIDATION_END), CONFIG.splits[2])
    with pytest.raises(ValueError, match="non-overlapping"):
        replace(CONFIG, splits=overlap)


@pytest.mark.parametrize("changes", [
    {"universe": ()}, {"universe": ("BTC/USD", "BTC/USD")},
    {"universe": ("BTC/USDT",)}, {"data_start": "2020-08-12"},
    {"splits": tuple(reversed(CONFIG.splits))},
])
def test_invalid_configuration_rejected(changes):
    with pytest.raises(ValueError):
        replace(CONFIG, **changes)


@pytest.mark.parametrize("value", [pd.NaT, "2024-01-01T01:00:00Z"])
def test_invalid_calendar_dates_rejected(value):
    with pytest.raises(ValueError):
        utc_day(value)


def test_test_split_locked_even_without_snapshot():
    with pytest.raises(PermissionError, match="locked"):
        RestrictedHistory([]).window(CONFIG.universe[0], split="test",
                                     start=locked.TEST_START, end=locked.TEST_START,
                                     decision_time="2025-01-02T00:00:00Z")


@pytest.mark.parametrize("split,start,end", [
    ("train", "2023-12-31", "2024-01-01"),
    ("validation", "2024-12-31", "2025-01-01"),
    ("validation", "2023-12-31", "2024-01-01"),
])
def test_cross_split_requests_rejected(split, start, end):
    with pytest.raises(PermissionError, match="boundaries"):
        RestrictedHistory([snapshot()]).window(CONFIG.universe[0], split=split,
            start=start, end=end, decision_time="2026-10-01T00:00:00Z")


def test_valid_warmup_uses_prior_training_bars_and_returns_copy():
    original = snapshot()
    history = RestrictedHistory([original])
    args = dict(split="validation", start="2024-01-01", end="2024-01-02",
                decision_time="2024-01-03T00:00:00Z", warmup_days=2)
    result = history.window(CONFIG.universe[0], **args)
    assert result.index[0] == utc_day("2023-12-30")
    assert result.index[-1] == utc_day("2024-01-02")
    result.iloc[0, 0] = 999
    assert history.window(CONFIG.universe[0], **args).iloc[0, 0] == 100


@pytest.mark.parametrize("warmup", [-1, True, 1.5])
def test_invalid_warmup_rejected(warmup):
    with pytest.raises(ValueError, match="Warm-up"):
        RestrictedHistory([snapshot()]).window(CONFIG.universe[0], split="validation",
            start="2024-01-01", end="2024-01-02", decision_time="2024-01-03T00:00:00Z",
            warmup_days=warmup)


def test_warmup_before_full_start_rejected():
    with pytest.raises(ValueError, match="warm-up"):
        RestrictedHistory([]).window(CONFIG.universe[0], split="train",
            start=locked.TRAIN_START, end=locked.TRAIN_START,
            decision_time="2020-08-12T00:00:00Z", warmup_days=1)


def test_missing_warmup_in_snapshot_rejected():
    with pytest.raises(ValueError, match="lacks"):
        RestrictedHistory([snapshot()]).window(CONFIG.universe[0], split="validation",
            start="2024-01-01", end="2024-01-02", decision_time="2024-01-03T00:00:00Z",
            warmup_days=4)


def test_future_or_uncompleted_bar_access_rejected():
    with pytest.raises(PermissionError, match="not complete"):
        RestrictedHistory([snapshot()]).window(CONFIG.universe[0], split="validation",
            start="2024-01-01", end="2024-01-02", decision_time="2024-01-02T12:00:00Z")


@pytest.mark.parametrize("damage", [
    "empty", "first", "last", "middle", "duplicate", "missing_column", "missing_value",
    "infinity", "zero", "negative_price", "negative_volume", "low_high", "open_high",
    "close_low", "timestamp", "missing_timestamp", "extra_date",
])
def test_invalid_data_rejected(damage):
    frame = bars()
    if damage == "empty":
        frame = frame.iloc[:0]
    elif damage in ("first", "last", "middle"):
        frame = frame.drop(frame.index[{"first": 0, "last": -1, "middle": 2}[damage]])
    elif damage == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]])
    elif damage == "missing_column":
        frame = frame.drop(columns="volume")
    elif damage == "timestamp":
        frame.index += pd.Timedelta(hours=1)
    elif damage == "missing_timestamp":
        frame.index = pd.DatetimeIndex([pd.NaT, *frame.index[1:]])
    elif damage == "extra_date":
        frame = bars("2023-12-28", "2024-01-03")
    else:
        column, value = {
            "missing_value": ("volume", np.nan), "infinity": ("close", np.inf),
            "zero": ("open", 0), "negative_price": ("close", -1),
            "negative_volume": ("volume", -1), "low_high": ("low", 103),
            "open_high": ("open", 103), "close_low": ("close", 97),
        }[damage]
        frame.loc[frame.index[0], column] = value
    with pytest.raises(ValueError):
        validate(frame)


def test_completed_bar_exact_boundary_and_uncompleted_rejection():
    validate(bars())
    with pytest.raises(ValueError, match="Uncompleted"):
        validate_bars(bars(), "2023-12-29", "2024-01-03", as_of="2024-01-03T23:59:59Z")


def test_naive_source_requires_explicit_utc_acknowledgement():
    frame = bars()
    frame.index = frame.index.tz_localize(None)
    with pytest.raises(ValueError, match="Naive"):
        validate(frame)
    clean = validate_bars(frame, "2023-12-29", "2024-01-03",
                          as_of="2024-01-04T00:00:00Z", naive_timestamps_are_utc=True)
    assert str(clean.index.tz) == "UTC"
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_bars(clean, "2023-12-29", "2024-01-03", as_of="2024-01-04")


def test_validation_sorts_without_mutating_source_and_allows_zero_volume():
    frame = bars().iloc[::-1].copy()
    frame["volume"] = 0
    clean = validate(frame)
    assert clean.index.is_monotonic_increasing
    assert not frame.index.is_monotonic_increasing
    assert (clean.volume == 0).all()


def test_causal_feature_unchanged_by_future_bar_changes():
    frame = bars()
    altered = frame.copy()
    altered.loc[utc_day("2024-01-03"), ["open", "high", "low", "close"]] *= 5
    pd.testing.assert_series_equal(momentum(frame).loc[:"2024-01-02"],
                                   momentum(altered).loc[:"2024-01-02"])
    args = dict(split="validation", start="2024-01-01", end="2024-01-02",
                decision_time="2024-01-03T00:00:00Z", warmup_days=1)
    left = RestrictedHistory([snapshot(frame)]).window(CONFIG.universe[0], **args)
    right = RestrictedHistory([snapshot(altered)]).window(CONFIG.universe[0], **args)
    pd.testing.assert_series_equal(momentum(left), momentum(right))


def test_snapshot_hash_is_deterministic_and_input_is_copied():
    frame = bars()
    first = snapshot(frame)
    assert first.manifest == snapshot(frame.iloc[::-1]).manifest
    frame.iloc[0, 4] += 1
    assert first.manifest.sha256 != snapshot(frame).manifest.sha256
    assert first.manifest == snapshot().manifest
    assert first.manifest.sha256 != HistoricalSnapshot.create(bars(),
        symbol=CONFIG.universe[0], source="another-source", source_symbol="BTC/USDT",
        start="2023-12-29", end="2024-01-03", as_of="2024-01-04T00:00:00Z").manifest.sha256
    with pytest.raises(FrozenInstanceError):
        first.manifest.rows = 1


def test_snapshot_roundtrip_create_once_and_tamper_rejection(tmp_path):
    original = snapshot()
    path = tmp_path / "snapshot.json"
    original.write(path)
    restored = HistoricalSnapshot.read(path)
    assert restored == original
    with pytest.raises(FileExistsError):
        original.write(path)
    body = json.loads(path.read_bytes())
    body["payload"]["bars"][0][-1] += 1
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        HistoricalSnapshot.read(path)
    with pytest.raises(ValueError, match="metadata mismatch"):
        replace(original, manifest=replace(original.manifest, source="forged"))


def test_unknown_symbol_and_duplicate_snapshots_rejected():
    with pytest.raises(ValueError, match="outside locked"):
        HistoricalSnapshot.create(bars(), symbol="DOGE/USD", source="synthetic",
            source_symbol="DOGE/USDT", start="2023-12-29", end="2024-01-03",
            as_of="2024-01-04T00:00:00Z")
    with pytest.raises(ValueError, match="Duplicate symbol"):
        RestrictedHistory([snapshot(), snapshot()])


def test_synthetic_alpha_injected_without_network_or_credentials(tmp_path):
    # Fresh interpreter: guards cover import as well as prepare/rebalance. Legacy
    # settings lookup receives FileNotFoundError without opening any real file.
    script = r'''
import builtins, io, os, socket
from pathlib import Path
original_open, original_io_open = builtins.open, io.open
original_path_open = Path.open
blocked_settings = []
def guarded(original):
    def open_file(file, *args, **kwargs):
        try:
            name = Path(file).name
        except TypeError:
            return original(file, *args, **kwargs)
        if name == "settings.yml":
            blocked_settings.append(name)
            raise FileNotFoundError("Offline test: legacy credentials unavailable")
        if name == ".env":
            raise AssertionError("Credential file access")
        return original(file, *args, **kwargs)
    return open_file
builtins.open = guarded(original_open)
io.open = guarded(original_io_open)
Path.open = guarded(original_path_open)
def forbidden(*args, **kwargs):
    raise AssertionError("Network/API/credential access")
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
original_getenv = os.getenv
def getenv(key, *args):
    if any(word in key.upper() for word in ("KRAKEN", "BINANCE", "SECRET", "API_KEY")):
        return forbidden()
    return original_getenv(key, *args)
os.getenv = getenv
import pandas as pd
from kraken_quant.config import CONFIG
from kraken_quant.data import HistoricalSnapshot, RestrictedHistory
from kraken_quant.synthetic import SyntheticAlpha, SyntheticBroker
import twsq.api.connections as connections
import twsq.api.python_apis as clients
import requests
connections.get_credential = forbidden
clients.get_credential = forbidden
clients.get_api = forbidden
requests.sessions.Session.request = forbidden
assert blocked_settings, "Expected legacy import to attempt missing settings lookup"
frame = pd.DataFrame(dict(open=[100., 100.], high=[102., 102.], low=[98., 98.],
                          close=[100., 101.], volume=[10., 10.]),
                     index=pd.date_range("2023-12-30", periods=2, tz="UTC"))
snap = HistoricalSnapshot.create(frame, symbol=CONFIG.universe[0], source="synthetic",
    source_symbol="BTC/USDT", start="2023-12-30", end="2023-12-31",
    as_of="2024-01-01T00:00:00Z")
broker = SyntheticBroker(RestrictedHistory([snap]), decision_time="2024-01-01T00:00:00Z")
alpha = SyntheticAlpha(broker, freq="1d")
assert alpha.broker is broker
assert broker.alphas[alpha.name] is alpha
alpha.prepare()
alpha.rebalance()
assert abs(alpha.signal - .01) < 1e-12
try:
    broker.create_order()
except RuntimeError:
    pass
else:
    raise AssertionError("Submission must be disabled")
print("Offline Alpha injection passed")
'''
    environment = {key: value for key, value in os.environ.items()
                   if not any(word in key.upper() for word in ("KRAKEN", "BINANCE", "SECRET", "API_KEY"))}
    environment["TWSQROOT"] = str(tmp_path / "empty_twsq")
    # Ignore legacy tracked bytecode; exercise the current TWSQ source files.
    environment["PYTHONPYCACHEPREFIX"] = str(tmp_path / "fresh_bytecode")
    result = subprocess.run([sys.executable, "-c", script], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "Offline Alpha injection passed" in result.stdout
