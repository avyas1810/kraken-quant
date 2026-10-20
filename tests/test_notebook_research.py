"""Notebook input boundaries and deterministic mechanics, without historical data."""

import pytest

from kraken_quant.config import CONFIG
from kraken_quant.notebook_research import load_local_history, synthetic_history


def test_synthetic_inputs_reproduce_hashes_and_preserve_universe():
    first, second = synthetic_history(), synthetic_history()
    assert first.manifests == second.manifests
    assert {key.symbol for key in first.instruments} == set(CONFIG.universe)
    assert all(key.venue == 'synthetic' for key in first.instruments)
    assert first.manifests != synthetic_history(seed=8).manifests


def test_notebook_local_inputs_reject_external_or_empty_directory(tmp_path):
    with pytest.raises(ValueError, match='inside'):
        load_local_history(tmp_path, tmp_path.parent)
    with pytest.raises(ValueError, match='No snapshot'):
        load_local_history(tmp_path, '.')


def test_notebook_local_snapshot_roundtrip(tmp_path):
    from kraken_quant.data import HistoricalSnapshot
    history = synthetic_history()
    for key, (reader, symbol) in history._readers.items():
        frame = reader.window(symbol, split='train', start='2023-10-01',
            end='2023-12-31', decision_time='2024-01-01T00:00:00Z')
        snapshot = HistoricalSnapshot.create(frame, symbol=symbol, source=key.venue,
            source_symbol=symbol, start='2023-10-01', end='2023-12-31',
            as_of='2024-01-01T00:00:00Z')
        snapshot.write(tmp_path / (key.base + '.json'))
    assert len(load_local_history(tmp_path, '.').instruments) == len(CONFIG.universe)


def test_notebook_rejects_snapshot_extending_into_test(tmp_path):
    import pandas as pd
    from kraken_quant.data import HistoricalSnapshot
    # Synthetic boundary fixture only; no historical test observations.
    day = CONFIG.split('test').start
    frame = pd.DataFrame(dict(open=[100.], high=[101.], low=[99.],
                              close=[100.], volume=[10.]), index=pd.DatetimeIndex([day]))
    symbol = CONFIG.universe[0]
    HistoricalSnapshot.create(frame, symbol=symbol, source='synthetic',
        source_symbol=symbol, start=day, end=day,
        as_of=day + pd.Timedelta(days=1)).write(tmp_path / 'boundary.json')
    with pytest.raises(PermissionError, match='locked test'):
        load_local_history(tmp_path, '.')
