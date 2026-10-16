"""Independent small-artifact integrity and reconciliation checks."""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .common import sha256_file


def verify_results(directory):
    directory = Path(directory)
    manifest = json.loads((directory/"phase2_manifest.json").read_text())
    if manifest["status"] != "historical_validation_completed":
        raise ValueError("Historical validation not completed")
    tables = {}
    for name, expected in manifest["output_hashes"].items():
        path = directory/name
        if sha256_file(path) != expected:
            raise ValueError("Generated table hash mismatch: " + name)
        table = pd.read_csv(path, na_values=["undefined"])
        if np.isinf(table.select_dtypes(include="number").to_numpy(float)).any():
            raise ValueError("Infinite result")
        for _, row in table[table.isna().any(axis=1)].iterrows():
            status = [row[col] for col in table.columns if col == "status" or col.endswith("_status")]
            if not any(str(value).startswith("undefined") or value == "no_accepted_trials" for value in status):
                raise ValueError("Unexplained missing result: " + name)
        tables[name] = table
    bootstrap = tables["bootstrap_robustness.csv"]
    if set(bootstrap.block_length) != {12,36,72,144} or not bootstrap.replications.eq(1999).all():
        raise ValueError("Bootstrap budget/specifications differ")
    regression = tables["btc_hac_sensitivity.csv"]
    if set(regression.hac_lag) != {12,36,72,144}:
        raise ValueError("HAC lag specifications incomplete")
    balance = tables["placebo_balance_diagnostics.csv"]
    if not balance.trials.eq(999).all():
        raise ValueError("Placebo budget incomplete")
    periods = tables["train_oos_stability.csv"]
    for _, row in periods[periods.level == "split"].iterrows():
        for level in ("year", "quarter"):
            selected = periods[(periods.strategy == row.strategy) & (periods.split == row.split) & (periods.level == level)]
            if not np.isclose(np.prod(1+selected.cumulative_return)-1, row.cumulative_return, atol=1e-12, rtol=1e-10):
                raise ValueError("Period accounting does not reconcile")
    return dict(status="verified", tables=len(tables), bootstrap_replications=1999,
                placebo_trials_per_method=999, historical_data_fabricated=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="phase2_results")
    args = parser.parse_args()
    print(json.dumps(verify_results(args.results), sort_keys=True))
