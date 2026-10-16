"""Export exact research code cells and small provenance tables, never datasets."""

import hashlib
import json
from pathlib import Path
import shutil


PREFIXES = ("daily_close=crypto_px", "# Strict complete-case panel:",
            "CROSS-SECTIONAL MOMENTUM / REVERSAL WEIGHT CONSTRUCTION",
            "def average_pairwise_correlation(", "# SECTION 6",
            "# SECTION 8: Exact self-financing", "# SECTION 9: Backtest output")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(root, output):
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    notebook = root / "PriceData_PHASE1_GAPFIX.ipynb"
    body = json.loads(notebook.read_text(encoding="utf-8"))
    code = ["".join(c["source"]) for c in body["cells"] if c["cell_type"] == "code"]
    selected = []
    for prefix in PREFIXES:
        matches = [source for source in code if prefix in source]
        if len(matches) != 1:
            raise ValueError("Ambiguous frozen source prefix: " + prefix)
        source = matches[0]
        selected.append(dict(prefix=prefix, source=source, sha256=hashlib.sha256(source.encode()).hexdigest()))
    result = dict(notebook=notebook.name, notebook_sha256=digest(notebook), cells=selected)
    (output / "source_cells.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    phase2 = root / "PriceData_PHASE2_VALIDATION_FIXED.ipynb"
    body = json.loads(phase2.read_text(encoding="utf-8"))
    result2 = dict(notebook=phase2.name, notebook_sha256=digest(phase2),
        code_cells=["".join(c["source"]) for c in body["cells"] if c["cell_type"] == "code"])
    (output / "initial_phase2_source.json").write_text(json.dumps(result2, indent=2), encoding="utf-8")
    copied = {}
    for folder, names in (("phase1_results", ("run_manifest.json", "reconciliation_manifest.json", "reconciliation.csv",
                                             "four_hour_performance_test.csv", "four_hour_performance_train.csv")),
                          ("phase2_results", ("bootstrap_summary.csv", "btc_hac_regression.csv", "dsr_sensitivity.csv",
                                             "placebo_summary.csv", "train_oos_degradation.csv", "phase2_manifest.json"))):
        for name in names:
            source = root / folder / name
            destination = output / ("initial_" + folder + "_" + name)
            if source.stat().st_size > 100000:
                raise ValueError("Unexpectedly large provenance artifact")
            shutil.copyfile(source, destination)
            copied[str(Path(folder)/name)] = digest(source)
    (output / "discovery_manifest.json").write_text(json.dumps(dict(
        phase1_notebook_sha256=result["notebook_sha256"], phase2_notebook_sha256=result2["notebook_sha256"],
        original_artifact_hashes=copied, datasets_copied=False, notebook_outputs_copied=False), indent=2), encoding="utf-8")
    return {"source_cells": len(selected), "small_artifacts": len(copied), "datasets_copied": False}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-root", required=True)
    parser.add_argument("--output", default="research/phase1_frozen")
    args = parser.parse_args()
    print(json.dumps(freeze(args.research_root, args.output)))
