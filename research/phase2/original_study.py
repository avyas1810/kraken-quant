"""Read-only replay of hash-frozen original notebook signals and accounting.

Pickle is executable input: only the explicitly trusted original local caches,
verified against the checkpointed original manifest, may be loaded here.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .common import sha256_file, validate_frame


FROZEN = Path(__file__).resolve().parents[1] / "phase1_frozen"


def verified_pickle(path: Path, expected_hash: str):
    """Verify provenance before invoking the trusted legacy pickle loader."""
    if sha256_file(path) != expected_hash:
        raise ValueError("Original market-data hash mismatch: " + path.name)
    return pd.read_pickle(path)


def function_definitions(source: str) -> str:
    """Keep verbatim function semantics; exclude notebook experiment side effects."""
    parsed = ast.parse(source)
    nodes = [node for node in parsed.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not nodes:
        raise ValueError("Frozen accounting cell contains no function definitions")
    # Source segments preserve the original text rather than rewriting the engine.
    return "\n\n".join(ast.get_source_segment(source, node) for node in nodes)


def _utc_index(obj):
    result = obj.copy()
    if result.index.tz is not None:
        raise ValueError("Original study timestamps expected naive, explicitly interpreted as UTC")
    result.index = result.index.tz_localize("UTC")
    return result


def prepare_study(root=Path("C:/Users/avyas"), frozen_source=FROZEN / "source_cells.json") -> dict:
    """Recompute two unchanged strategies and reconcile every original net bar.

    No data downloads or writes occur. Exported original returns are never used
    as a substitute for replay reconciliation. Trial dispersion uses the 18
    correlated tried four-hour strategies, not an asserted independent universe.
    """
    root, frozen_source = Path(root), Path(frozen_source)
    original_manifest_path = root / "phase1_results" / "run_manifest.json"
    checkpoint_manifest_path = frozen_source.parent / "initial_phase1_results_run_manifest.json"
    if sha256_file(original_manifest_path) != sha256_file(checkpoint_manifest_path):
        raise ValueError("Original Phase 1 manifest differs from checkpoint")
    manifest = json.loads(checkpoint_manifest_path.read_text(encoding="utf-8"))
    if manifest["cost_bps_per_gross_dollar"] != 20 or len(manifest["universe"]) != 9:
        raise ValueError("Original research assumptions differ")
    source = json.loads(frozen_source.read_text(encoding="utf-8"))
    source_hashes = {"frozen_source_cells": sha256_file(frozen_source),
                     "original_phase1_manifest": sha256_file(original_manifest_path)}
    namespace = {"np": np, "pd": pd, "COST_BPS": 20.0}
    for name, variable in (("daily_raw.pkl", "crypto_px"), ("binance_4h_raw.pkl", "crypto_4h")):
        path = root / "research_data_cache" / name
        namespace[variable] = verified_pickle(path, manifest["data_hashes"][name])
        source_hashes[name] = manifest["data_hashes"][name]
    # Original loader normalized Binance millisecond close timestamps this way.
    namespace["crypto_4h"]["Close Time"] = pd.to_datetime(namespace["crypto_4h"]["Close Time"], utc=True).dt.tz_localize(None)
    expected_prefixes = ["daily_close=crypto_px", "# Strict complete-case panel:",
                         "CROSS-SECTIONAL MOMENTUM / REVERSAL WEIGHT CONSTRUCTION",
                         "def average_pairwise_correlation(", "# SECTION 6",
                         "# SECTION 8: Exact self-financing", "# SECTION 9: Backtest output"]
    if [cell["prefix"] for cell in source["cells"]] != expected_prefixes:
        raise ValueError("Unexpected frozen notebook cell sequence")
    for cell in source["cells"]:
        text = cell["source"]
        if hashlib.sha256(text.encode()).hexdigest() != cell["sha256"]:
            raise ValueError("Frozen notebook cell hash mismatch")
        if cell["prefix"].startswith(("# SECTION 8", "# SECTION 9")):
            text = function_definitions(text)
        exec(compile(text, "<checkpointed-phase1-cell>", "exec"), namespace)
    if namespace["daily_quality"] != manifest["daily_quality"] or namespace["four_hour_quality"] != manifest["four_hour_quality"]:
        raise ValueError("Replayed data cleaning diagnostics differ from frozen Phase 1")
    returns = namespace["returns_4h"]
    momentum = namespace["weights_4h_mom"]
    low = namespace["mom_4h_low_correlation"]
    engine = namespace["compute_transaction_costs"]
    baseline = engine(momentum, cost_bps=20., returns=returns)
    strategy = engine(low, cost_bps=20., returns=returns)
    export_path = root / "phase1_results" / "4h_net_returns.csv"
    exported = pd.read_csv(export_path, index_col=0, parse_dates=True)
    if not exported.index.equals(returns.index):
        raise ValueError("Original return export and recomputed timestamps differ")
    if exported.columns.has_duplicates or not np.isfinite(exported.to_numpy(dtype=float)).all():
        raise ValueError("Original return export contains duplicate/nonfinite data")
    errors = {}
    for name, replay in (("4h_momentum", baseline), ("4h_mom_low_correlation", strategy)):
        error = float(np.max(np.abs(replay["net_returns"].to_numpy() - exported[name].to_numpy())))
        if error > 1e-12:
            raise ValueError(f"Frozen Phase 1 return reconciliation failed: {name}, error={error}")
        errors[name] = error
    source_hashes["4h_net_returns.csv"] = sha256_file(export_path)
    for label, replay in (("strategy", strategy), ("baseline", baseline)):
        np.testing.assert_allclose(replay["gross_returns"] - replay["net_returns"],
                                   .002*replay["turnover"], rtol=1e-10, atol=1e-12,
                                   err_msg=label + " original gross/net/cost convention differs")
    frame = pd.DataFrame({"strategy_net": strategy["net_returns"], "baseline_net": baseline["net_returns"],
                          "strategy_gross": strategy["gross_returns"], "btc_return": returns["BTCUSDT"],
                          "gross_exposure": strategy["lagged_weights"].abs().sum(axis=1),
                          "turnover": strategy["turnover"],
                          "baseline_exposure": baseline["lagged_weights"].abs().sum(axis=1),
                          "baseline_turnover": baseline["turnover"],
                          "regime_active": namespace["regimes_4h"]["low_correlation"]})
    frame = validate_frame(_utc_index(frame))
    oos_mask = frame.index >= pd.Timestamp("2024-01-01", tz="UTC")
    historical = exported.loc[exported.index >= pd.Timestamp("2024-01-01")]
    trial_srs = historical.mean() / historical.std(ddof=1)
    if not np.isfinite(trial_srs.to_numpy()).all() or len(trial_srs) != 18:
        raise ValueError("Original tried-strategy dispersion requires 18 finite period Sharpes")
    train_trials = exported.loc[exported.index < pd.Timestamp("2024-01-01")]
    train_srs = train_trials.mean() / train_trials.std(ddof=1)
    if not np.isfinite(train_srs.to_numpy()).all():
        raise ValueError("Original training trial Sharpes must be finite")
    return {"frame": frame, "momentumweights": _utc_index(momentum), "returns": _utc_index(returns),
            "regime_mask": frame.regime_active.astype(bool),
            "initial_held": strategy["lagged_weights"].loc[oos_mask].iloc[0].to_numpy(copy=True),
            "source_hashes": source_hashes,
            "coverage": {"daily": namespace["daily_quality"], "four_hour": namespace["four_hour_quality"],
                         "net_reconciliation_max_absolute_error": errors,
                         "full_start": frame.index[0].isoformat(), "full_end": frame.index[-1].isoformat(),
                         "train_end": frame.index[~oos_mask][-1].isoformat(),
                         "oos_start": frame.index[oos_mask][0].isoformat(),
                         "oos_end": frame.index[-1].isoformat()},
            "trial_dispersion": float(trial_srs.std(ddof=1)),
            "trial_dispersion_train": float(train_srs.std(ddof=1)),
            "trial_dispersion_assumption": "sample standard deviation of 18 correlated tried strategies; incomplete registry, not independent trials",
            "trial_period_sharpes": {str(key): float(value) for key, value in trial_srs.items()},
            "training_trial_period_sharpes": {str(key): float(value) for key, value in train_srs.items()},
            "original_engine": engine, "namespace": namespace}
