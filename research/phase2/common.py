"""Strict four-hour sample contracts and frozen artifact verification."""

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd


ANNUALISATION = 365 * 6
COST_RATE = 0.002
STUDY_ASSETS = frozenset({"BTC", "ETH", "BNB", "ADA", "LINK", "XRP", "XLM", "DOGE", "AVAX"})
RETURN_COLUMNS = ("strategy_net", "baseline_net", "strategy_gross", "btc_return")
REQUIRED_COLUMNS = RETURN_COLUMNS + ("gross_exposure", "turnover", "baseline_exposure", "baseline_turnover", "regime_active")


def validate_frame(frame):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("Nonempty aligned frame required")
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError("Timezone-aware UTC timestamps required")
    if str(frame.index.tz) != "UTC" or frame.index.hasnans or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("Unique ordered UTC timestamps required")
    if frame.columns.has_duplicates or not np.isfinite(frame.to_numpy(dtype=float)).all():
        raise ValueError("Duplicate columns, missing or nonfinite data")
    if len(frame) > 1 and not (frame.index.to_series().diff().iloc[1:] == pd.Timedelta(hours=4)).all():
        raise ValueError("Missing four-hour observations or irregular alignment")
    return frame.copy(deep=True)


def safe_artifact(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path.suffix not in {".csv", ".parquet", ".json", ".py"}:
        raise ValueError("Artifact must be an allowed relative research file")
    return path


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifacts(root, manifest):
    if manifest.get("schema_version") != 1 or manifest.get("study_status") != "historical_already_inspected":
        raise ValueError("Explicit frozen historical study status required; synthetic results prohibited")
    universe = manifest.get("universe", [])
    if len(universe) != 9 or set(universe) != STUDY_ASSETS:
        raise ValueError("This study requires the stated nine-asset universe")
    if manifest.get("annualisation") != ANNUALISATION or manifest.get("cost_per_gross_dollar") != COST_RATE:
        raise ValueError("Frozen four-hour annualisation/cost assumptions differ")
    if manifest.get("return_timestamp") != "bar_end_utc" or manifest.get("accounting") != "exact_self_financing_drift_aware":
        raise ValueError("Frozen accounting/timing provenance required")
    files = manifest.get("files", {})
    if not files or manifest.get("returns_file") not in files or manifest.get("engine_file") not in files:
        raise ValueError("Frozen returns and accounting source hashes required")
    if not manifest.get("data_files") or any(name not in files for name in manifest["data_files"]):
        raise ValueError("Frozen market-data hashes required")
    checked = {}
    for name, expected in files.items():
        actual = sha256_file(safe_artifact(root, name))
        if actual != expected:
            raise ValueError("Frozen artifact hash mismatch: " + name)
        checked[name] = actual
    return checked


def load_returns(root, manifest):
    path = safe_artifact(root, manifest["returns_file"])
    frame = pd.read_csv(path)
    if "timestamp" not in frame:
        raise ValueError("Returns require explicit timestamp column")
    stamps = frame.pop("timestamp")
    if any(pd.Timestamp(value).tzinfo is None for value in stamps):
        raise ValueError("Naive CSV timestamps are not permitted")
    frame.index = pd.DatetimeIndex(pd.to_datetime(stamps, utc=True))
    if any(name not in frame for name in REQUIRED_COLUMNS if name != "strategy_gross"):
        raise ValueError("Frozen return/exposure/turnover/filter columns missing")
    frame = validate_frame(frame.loc[:, [name for name in REQUIRED_COLUMNS if name in frame]])
    if (frame.loc[:, [name for name in RETURN_COLUMNS if name in frame]] <= -1).any().any():
        raise ValueError("Insolvent returns unsupported")
    if (frame.loc[:, ("gross_exposure", "turnover", "baseline_exposure", "baseline_turnover")] < 0).any().any():
        raise ValueError("Negative exposure or turnover")
    if not frame.regime_active.isin([0, 1]).all():
        raise ValueError("Regime activity must be binary")
    # Do not invent gross-net identities: exact self-financing costs can interact
    # with intrabar returns/drift. Cost handling belongs to the hash-frozen engine.
    return frame
