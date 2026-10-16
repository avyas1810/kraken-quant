"""Reproduce the recovered study with hash-verified original Phase 1 inputs."""

import hashlib
import json
from pathlib import Path
import pickle

import numpy as np
import pandas as pd

from .common import sha256_file
from .dsr import deflated_sharpe
from .frozen_accounting import batch_frozen_accounting
from .original_study import FROZEN, prepare_study
from .placebo import clustered_mask


def original_hashes(root):
    root = Path(root)
    paths = {"daily_raw.pkl": root/"research_data_cache/daily_raw.pkl",
             "binance_4h_raw.pkl": root/"research_data_cache/binance_4h_raw.pkl",
             "4h_net_returns.csv": root/"phase1_results/4h_net_returns.csv",
             "original_phase1_manifest": root/"phase1_results/run_manifest.json",
             "frozen_source_cells": FROZEN/"source_cells.json"}
    return {name: sha256_file(path) for name, path in paths.items()}


class OriginalAdapter:
    def __init__(self, prepared, cache, *, seed, trials=999):
        self.prepared, self.cache, self.seed, self.trials = prepared, Path(cache), seed, trials
        self.cache.mkdir(parents=True, exist_ok=True)
        self.oos = prepared["frame"].loc["2024-01-01":]
        self.mask = self.oos.regime_active.astype(bool)
        self.lookup, self.results = None, None
        rows = []
        for assumption, sigma in (("recorded_18_training_strategies_correlated_incomplete", prepared["trial_dispersion_train"]),
                                   ("recorded_18_oos_strategies_correlated_incomplete", prepared["trial_dispersion"]),
                                   ("iid_gaussian_null_reference_not_empirical_registry", 1/np.sqrt(len(self.oos)-1))):
            table = pd.DataFrame(deflated_sharpe(self.oos.strategy_net, sigma))
            table["dispersion_assumption"] = assumption
            rows.append(table)
        self.dsr_sensitivities = pd.concat(rows, ignore_index=True)
        self.batch_verification = {}

    def replay(self):
        return self.prepared["frame"].copy(deep=True)

    def _batch(self):
        rng = np.random.default_rng(self.seed)
        candidates = [self.mask]
        for method in ("cluster", "shift"):
            for _ in range(self.trials):
                candidate = clustered_mask(self.mask, rng) if method == "cluster" else pd.Series(
                    np.roll(self.mask.to_numpy(), rng.integers(1, len(self.mask))), index=self.mask.index)
                candidates.append(candidate)
        masks = np.asarray([candidate.to_numpy(bool) for candidate in candidates])
        mask_digest = hashlib.sha256(masks.tobytes()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps(dict(hashes=self.prepared["source_hashes"], seed=self.seed,
            trials=self.trials, accounting_source=sha256_file(Path(__file__).with_name("frozen_accounting.py"))), sort_keys=True).encode()).hexdigest()
        path = self.cache / ("null-accounting-"+fingerprint+".npz")
        integrity_path = path.with_suffix(".integrity.json")
        if path.exists():
            if not integrity_path.exists():
                raise ValueError("Unsealed existing null cache cannot be trusted")
            integrity = json.loads(integrity_path.read_text())
            if integrity != {"sha256": sha256_file(path), "mask_sha256": mask_digest}:
                raise ValueError("Cached accounting hash or placebo mask definition changed")
            with np.load(path, allow_pickle=False) as saved:
                results = {name: saved[name] for name in ("net_return", "gross_return", "turnover", "gross_exposure", "trading_costs")}
        else:
            print(f"Evaluating frozen-equation batch:{len(masks)} observed/null masks", flush=True)
            results = batch_frozen_accounting(self.prepared["momentumweights"].loc[self.oos.index],
                self.prepared["returns"].loc[self.oos.index], masks, initial_held=self.prepared["initial_held"], cost_bps=20)
            np.savez(path, **results)
            integrity_path.write_text(json.dumps({"sha256": sha256_file(path), "mask_sha256": mask_digest}, sort_keys=True))
        for values in results.values():
            if values.shape != masks.shape or not np.isfinite(values).all():
                raise ValueError("Invalid cached null accounting")
        reference = {"net_return": self.oos.strategy_net, "gross_return": self.oos.strategy_gross,
                     "turnover": self.oos.turnover, "gross_exposure": self.oos.gross_exposure}
        errors = {name: float(np.max(np.abs(results[name][0] - series.to_numpy()))) for name, series in reference.items()}
        if any(error > 1e-12 for error in errors.values()):
            raise ValueError("Batched observed accounting differs from original Phase 1: " + str(errors))
        if not np.allclose(results["gross_return"]-results["trading_costs"], results["net_return"], atol=1e-12, rtol=0):
            raise ValueError("Original gross/net cost identity changed")
        if not np.allclose(results["trading_costs"], .002*results["turnover"], atol=1e-12, rtol=0):
            raise ValueError("Original20bp traded-notional cost identity changed")
        self.batch_verification = dict(observed_max_absolute_errors=errors, absolute_tolerance=1e-12,
            solver_iterations=55, source="same frozen Phase 1 equations; only independent trial axis vectorised",
            trials_including_observed=len(masks), cache_sha256=sha256_file(path))
        self.batch_verification["mask_sha256"] = mask_digest
        self.lookup = {mask.tobytes(): index for index, mask in reversed(list(enumerate(masks)))}
        self.results = results

    def simulate_filter(self, candidate):
        if not candidate.index.equals(self.mask.index):
            raise ValueError("Original study mask index changed")
        if self.results is None:
            self._batch()
        row = self.lookup[candidate.to_numpy(bool).tobytes()]
        return pd.DataFrame({name: self.results[name][row] for name in ("net_return", "gross_exposure", "turnover")}, index=self.mask.index)


def run_original(root, output, *, cache=".phase2-cache"):
    from .run_validation import SEEDS, analyse, verify_replay
    root, cache = Path(root), Path(cache)
    before = original_hashes(root)
    checkpointed = json.loads((FROZEN/"initial_phase1_results_run_manifest.json").read_text())
    for name, expected in checkpointed["data_hashes"].items():
        if before[name] != expected:
            raise ValueError("Original data changed before replay")
    prepared_path = cache/"prepared_study.pkl"
    if prepared_path.exists():
        # This is our own ignored local preparation cache, not arbitrary market
        # input. Every original artifact hash must still match its preparation.
        prepared = pickle.loads(prepared_path.read_bytes())
        if prepared["source_hashes"] != before:
            raise ValueError("Prepared replay is stale; original inputs changed")
    else:
        prepared = prepare_study(root)
        prepared = {k:v for k,v in prepared.items() if k not in ("namespace", "original_engine")}
        cache.mkdir(parents=True, exist_ok=True)
        with prepared_path.open("xb") as handle:
            pickle.dump(prepared, handle, protocol=5)
    frame = prepared["frame"]
    # Independently compare cached replay to the original frozen CSV on every
    # invocation; comparing a cache to its own copy would be tautological.
    original = pd.read_csv(root/"phase1_results/4h_net_returns.csv", index_col=0, parse_dates=True)
    original.index = original.index.tz_localize("UTC")
    if not original.index.equals(frame.index):
        raise ValueError("Prepared sample differs from original exported timestamps")
    for actual, reference in (("strategy_net", "4h_mom_low_correlation"), ("baseline_net", "4h_momentum")):
        if not np.allclose(frame[actual], original[reference], atol=1e-12, rtol=0):
            raise ValueError("Prepared returns differ from original Phase 1 export")
    bounds = prepared["coverage"]
    frozen = dict(splits={"train": {"start": bounds["full_start"], "end": bounds["train_end"]},
                          "oos": {"start": bounds["oos_start"], "end": bounds["oos_end"]}},
        trial_sharpe_std_periodic=prepared["trial_dispersion_train"],
        trial_dispersion_provenance=prepared["trial_dispersion_assumption"], coverage=prepared["coverage"])
    adapter = OriginalAdapter(prepared, cache, seed=SEEDS["placebo"])
    verify_replay(frame, adapter.replay())
    def unchanged():
        if original_hashes(root) != before:
            raise ValueError("Original Phase 1 artifacts changed during analysis")
    return analyse(frame, adapter, frozen, before, before["original_phase1_manifest"], unchanged, output)
