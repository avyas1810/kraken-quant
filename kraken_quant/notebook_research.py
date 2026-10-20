"""Offline inputs for the research notebook; no credentials or exchange clients."""

from pathlib import Path

import numpy as np
import pandas as pd

from .config import CONFIG
from .data import DAY, HistoricalSnapshot
from .strategy import VenueHistory


def synthetic_history(seed=7):
    """Deterministic mechanics fixture, never historical performance evidence."""
    index = pd.date_range('2023-10-01', '2024-02-29', tz='UTC')
    rng = np.random.default_rng(seed)
    snapshots = []
    for symbol in CONFIG.universe:
        close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, len(index))))
        opening = np.r_[close[0], close[:-1]]
        frame = pd.DataFrame(dict(open=opening,
            high=np.maximum(opening, close) * 1.005,
            low=np.minimum(opening, close) * 0.995,
            close=close, volume=rng.uniform(100, 1000, len(index))), index=index)
        snapshots.append(HistoricalSnapshot.create(frame, symbol=symbol,
            source='synthetic', source_symbol=symbol, start=index[0], end=index[-1],
            as_of=index[-1] + DAY))
    return VenueHistory(snapshots)


def load_local_history(root, directory):
    """Load validated snapshots only from inside this repository, before test."""
    root = Path(root).resolve()
    directory = (root / directory).resolve()
    if not directory.is_relative_to(root):
        raise ValueError('Snapshot directory must be inside the repository')
    paths = sorted(directory.glob('*.json'))
    if not paths:
        raise ValueError('No snapshot JSON files found')
    snapshots = []
    for path in paths:
        if not path.resolve().is_relative_to(root):
            raise ValueError('Snapshot files must be inside the repository')
        snapshot = HistoricalSnapshot.read(path)
        if pd.Timestamp(snapshot.manifest.end) > CONFIG.split('validation').end:
            raise PermissionError('Notebook inputs must exclude the locked test period')
        snapshots.append(snapshot)
    history = VenueHistory(snapshots)
    if {key.symbol for key in history.instruments} != set(CONFIG.universe):
        raise ValueError('Snapshots must cover the configured research universe')
    return history
