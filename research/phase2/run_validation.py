"""Frozen-input Phase 2 orchestration. Missing historical inputs fail closed.

No data download, strategy creation, replacement accounting or test-release code.
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import platform
import subprocess

import numpy as np
import pandas as pd

from .common import (ANNUALISATION, COST_RATE, load_returns,
                     safe_artifact, sha256_file, validate_frame, verify_artifacts)


SEEDS = {"bootstrap": 20261014, "placebo": 20261015}
CHECKPOINT = "dfd05382fedc0a2544bec413c615fa718f384776"
TABLES = ("dsr_corrected.csv", "bootstrap_robustness.csv", "placebo_robustness.csv",
          "placebo_balance_diagnostics.csv", "btc_hac_sensitivity.csv", "train_oos_stability.csv")


def _json_bytes(body):
    return json.dumps(body, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"


def _versions():
    result = {"python": platform.python_version()}
    for package in ("numpy", "pandas", "scipy", "statsmodels"):
        result[package] = importlib.metadata.version(package)
    return result


def _source_hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob("*.py"))}


def _commit():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def _publish(output, tables, manifest):
    """Create once and refuse any different existing result."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    payloads = {}
    for name, table in tables.items():
        if np.isinf(table.select_dtypes(include="number").to_numpy(dtype=float)).any():
            raise ValueError("Infinite generated table value: " + name)
        if table.isna().any().any():
            statuses = [c for c in table.columns if c == "status" or c.endswith("_status")]
            if not statuses or table.loc[table.isna().any(axis=1), statuses].isna().any().any():
                raise ValueError("Unexplained undefined statistic: " + name)
            for _, row in table.loc[table.isna().any(axis=1), statuses].iterrows():
                if not any(str(value).startswith("undefined") or value == "no_accepted_trials" for value in row):
                    raise ValueError("Undefined value lacks an explanatory status: " + name)
        payloads[name] = table.to_csv(index=False, float_format="%.17g", na_rep="undefined").encode()
    manifest["output_hashes"] = {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()}
    payloads["phase2_manifest.json"] = _json_bytes(manifest)
    for name, payload in payloads.items():
        path = output / name
        if path.exists() and path.read_bytes() != payload:
            raise FileExistsError("Refusing to overwrite different research output: " + str(path))
    for name, payload in payloads.items():
        path = output / name
        if not path.exists():
            with path.open("xb") as handle:
                handle.write(payload)


def blocked_inventory(output):
    manifest = dict(schema_version=1, status="blocked_missing_frozen_phase1_artifacts",
        checkpoint_sha=CHECKPOINT, code_commit=_commit(), source_hashes=_source_hashes(),
        dependencies=_versions(), seeds=SEEDS, historical_results_generated=False,
        missing=["phase1_manifest.json", "nine-asset daily/four-hour market-data hashes",
                 "frozen train/OOS boundaries and return series", "original accounting replay adapter",
                 "across-trial periodic Sharpe dispersion and provenance"],
        supplied_external_estimates="unverified; never used as numerical inputs",
        old_daily_test_reader="unchanged; remains locked", conclusion="not_assessable_missing_inputs")
    _publish(output, {name: pd.DataFrame(columns=["status", "detail"]) for name in TABLES}, manifest)
    return manifest


def load_frozen_engine(root, manifest):
    path = safe_artifact(root, manifest["engine_file"])
    if path.suffix != ".py":
        raise ValueError("Frozen replay adapter must be Python")
    spec = importlib.util.spec_from_file_location("frozen_phase1_replay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "build_engine", None)):
        raise ValueError("Frozen adapter must implement build_engine(root, manifest)")
    engine = module.build_engine(Path(root).resolve(), manifest)
    if not callable(getattr(engine, "replay", None)) or not callable(getattr(engine, "simulate_filter", None)):
        raise ValueError("Frozen engine requires replay() and simulate_filter(mask)")
    return engine


def verify_replay(expected, replayed, *, tolerance=1e-12):
    replayed = validate_frame(replayed)
    if not replayed.index.equals(expected.index) or list(replayed.columns) != list(expected.columns):
        raise ValueError("Phase 1 replay index/columns differ from frozen sample")
    if not np.allclose(expected.to_numpy(float), replayed.to_numpy(float), rtol=0, atol=tolerance):
        raise ValueError("Phase 1 returns/exposures/turnover changed beyond tolerance")


def validate(root, output):
    root = Path(root)
    manifest_file = root / "phase1_manifest.json"
    if not manifest_file.is_file():
        return blocked_inventory(output)
    frozen = json.loads(manifest_file.read_text(encoding="utf-8"))
    before = verify_artifacts(root, frozen)
    manifest_hash = sha256_file(manifest_file)
    frame = load_returns(root, frozen)
    if set(frozen.get("splits", {})) != {"train", "oos"}:
        raise ValueError("Frozen train and already-inspected OOS boundaries required")
    dispersion = frozen.get("trial_sharpe_std_periodic")
    if dispersion is None or not frozen.get("trial_dispersion_provenance"):
        raise ValueError("Across-trial periodic Sharpe dispersion and provenance unavailable")
    engine = load_frozen_engine(root, frozen)
    verify_replay(frame, engine.replay())
    def verify_unchanged():
        if before != verify_artifacts(root, frozen) or sha256_file(manifest_file) != manifest_hash:
            raise ValueError("Phase 1 inputs changed during analysis")
    return analyse(frame, engine, frozen, before, manifest_hash, verify_unchanged, output)


def analyse(frame, engine, frozen, before, manifest_hash, verify_unchanged, output):
    dispersion = frozen["trial_sharpe_std_periodic"]
    from .bootstrap import paired_block_bootstrap
    from .btc_regression import btc_hac_regression
    from .dsr import deflated_sharpe
    from .placebo import run_placebos
    from .stability import stability_report

    stability_report(frame.strategy_net, frozen["splits"], exposure=frame.gross_exposure, turnover=frame.turnover)
    bounds = frozen["splits"]["oos"]
    oos = frame.loc[bounds["start"]:bounds["end"]]
    if len(oos) <= 146:
        raise ValueError("OOS sample too short for specified144-lag checks")
    dsr = pd.DataFrame(deflated_sharpe(oos.strategy_net, float(dispersion)))
    bootstrap = paired_block_bootstrap(oos[["strategy_net", "baseline_net"]].rename(
        columns={"strategy_net": "strategy", "baseline_net": "baseline"}), seed=SEEDS["bootstrap"])
    mask = oos.regime_active.astype(bool)

    def simulate(candidate):
        # Original adapter owns unchanged signals, prices, warm-up, costs/timing.
        simulated = validate_frame(engine.simulate_filter(candidate.copy(deep=True)))
        if not simulated.index.equals(mask.index):
            raise ValueError("Frozen placebo replay returned wrong sample")
        return simulated

    observed = simulate(mask)
    if not np.allclose(observed[["net_return", "gross_exposure", "turnover"]],
                       oos[["strategy_net", "gross_exposure", "turnover"]], atol=1e-12, rtol=0):
        raise ValueError("Observed filter does not reproduce frozen Phase 1 accounting")
    placebo = run_placebos(mask, simulate, seed=SEEDS["placebo"])
    for index, row in placebo["balance_diagnostics"].iterrows():
        selected = placebo["trials"][(placebo["trials"].method == row.method) & placebo["trials"].balanced]
        if not len(selected):
            placebo["balance_diagnostics"].loc[index, "status"] = "undefined_no_accepted_trials"
        placebo["balance_diagnostics"].loc[index, "observed_turnover"] = placebo["observed"]["turnover"]
        placebo["balance_diagnostics"].loc[index, "observed_exposure"] = placebo["observed"]["gross_exposure"]
        for field in ("exposure_relative_gap", "turnover_relative_gap"):
            for statistic in ("mean", "min", "max"):
                placebo["balance_diagnostics"].loc[index, "accepted_"+field+"_"+statistic] = getattr(selected[field], statistic)() if len(selected) else None
    regressions = pd.concat([btc_hac_regression(oos, strategy_column=name, btc_column="btc_return")
                            for name in ("strategy_net", "strategy_gross") if name in oos], ignore_index=True)
    periods, sensitivities, concentrations = [], [], []
    for name, exposure, turnover in (("strategy_net", "gross_exposure", "turnover"),
                                      ("baseline_net", "baseline_exposure", "baseline_turnover")):
        report = stability_report(frame[name], frozen["splits"], exposure=frame[exposure], turnover=frame[turnover])
        for table in report.values():
            table["strategy"] = name
        periods.append(report["periods"])
        sensitivities.append(report["positive_return_sensitivity"])
        concentrations.append(report["period_concentration"])
    stability_table = pd.concat(periods, ignore_index=True)
    for _, row in stability_table[stability_table.level == "split"].iterrows():
        for level in ("year", "quarter"):
            selected = stability_table[(stability_table.strategy == row.strategy) &
                (stability_table.split == row.split) & (stability_table.level == level)]
            if not np.isclose(np.prod(1 + selected.cumulative_return)-1, row.cumulative_return, atol=1e-12, rtol=1e-10):
                raise ValueError("Period returns do not reconcile to frozen split")
    activity = []
    for split, bounds in frozen["splits"].items():
        selected = frame.loc[bounds["start"]:bounds["end"]]
        activity.append(dict(split=split, observations=len(selected), active_fraction=float(selected.regime_active.mean())))
    verify_unchanged()
    manifest = dict(schema_version=1, status="historical_validation_completed", checkpoint_sha=CHECKPOINT,
        code_commit=_commit(), source_hashes=_source_hashes(), dependencies=_versions(), seeds=SEEDS,
        annualisation=ANNUALISATION, cost_per_gross_dollar=COST_RATE, phase1_manifest_sha256=manifest_hash,
        frozen_artifact_hashes=before, replay_absolute_tolerance=1e-12, splits=frozen["splits"],
        coverage=frozen.get("coverage", "not supplied; limitation"), regime_activity=activity,
        sample_start=frame.index[0].isoformat(), sample_end=frame.index[-1].isoformat(),
        oos_status="already_inspected_not_pristine", trial_dispersion_provenance=frozen["trial_dispersion_provenance"],
        gross_return_status="available" if "strategy_gross" in frame else "not_recorded_by_phase1",
        placebo=placebo["configuration"], placebo_observed=placebo["observed"],
        supplemental_checkpoint_sha="58249f2320bec4b086546b534b64ce192a0ee852",
        ess_interpretation="initial-positive-autocorrelation mean heuristic; full N is not evidence of independence",
        conclusion="requires_independent_review_of_generated_evidence")
    tables = dict(zip(TABLES, (dsr, bootstrap, placebo["summary"], placebo["balance_diagnostics"], regressions, stability_table)))
    tables["positive_return_sensitivity.csv"] = pd.concat(sensitivities, ignore_index=True)
    tables["period_concentration.csv"] = pd.concat(concentrations, ignore_index=True)
    distributions = []
    for method, group in placebo["trials"].groupby("method"):
        for metric in ("net_sharpe", "cumulative_return", "max_drawdown", "turnover", "gross_exposure"):
            values = group[metric]
            distributions.append(dict(method=method, metric=metric, observed=placebo["observed"][metric],
                trials=len(values), mean=float(values.mean()), minimum=float(values.min()), maximum=float(values.max()),
                q05=float(values.quantile(.05)), q50=float(values.quantile(.50)), q95=float(values.quantile(.95))))
    tables["placebo_distributions.csv"] = pd.DataFrame(distributions)
    if hasattr(engine, "dsr_sensitivities"):
        tables["dsr_corrected.csv"] = engine.dsr_sensitivities
        manifest["dsr_dispersion_assumptions"] = sorted(engine.dsr_sensitivities.dispersion_assumption.unique())
    if hasattr(engine, "batch_verification"):
        manifest["batch_accounting_verification"] = engine.batch_verification
    _publish(output, tables, manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", default="research_data_cache/phase1-frozen")
    parser.add_argument("--output", default="phase2_results")
    parser.add_argument("--original-research-root", help="Read-only recovered original study directory")
    args = parser.parse_args(argv)
    if args.original_research_root:
        from .original_validation import run_original
        result = run_original(args.original_research_root, args.output)
    else:
        result = validate(args.artifact_dir, args.output)
    print(json.dumps({"status": result["status"], "historical_results_generated": result["status"] == "historical_validation_completed"}))
    return 0 if result["status"] == "historical_validation_completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
