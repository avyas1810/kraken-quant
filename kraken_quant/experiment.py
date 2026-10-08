"""Registered train/validation experiments; no test release or automatic tuning."""

from dataclasses import asdict, dataclass
import hashlib
from importlib.metadata import version
import inspect
import json
import math
from pathlib import Path
import platform

import pandas as pd

from .config import CONFIG, utc_day
from .data import canonical_json
from .research_execution import ResearchCosts, ResearchPolicy, research_buy_and_hold, run_research
from .strategy import StrategyPipeline


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class RegisteredTrial:
    trial_id: str
    payload: bytes

    def __post_init__(self):
        if not isinstance(self.payload, bytes) or digest(self.payload) != self.trial_id:
            raise ValueError("Trial registration hash mismatch")

    @property
    def specification(self):
        return json.loads(self.payload)


class ParameterRegistry:
    """Create-once parameter records; repeated registration is idempotent."""

    def __init__(self, directory, *, max_trials_per_hypothesis=20):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.maximum = max_trials_per_hypothesis
        if not isinstance(self.maximum, int) or isinstance(self.maximum, bool) or self.maximum < 1:
            raise ValueError("Trial budget must be a positive integer")

    def register(self, hypothesis, strategy_id, parameters, *, code_revision,
                 feature_id="none", seed=0, input_hashes=None):
        if not all(isinstance(v, str) and v.strip() for v in (hypothesis, strategy_id, code_revision, feature_id)):
            raise ValueError("Hypothesis, strategy, feature and code identity are required")
        if not isinstance(parameters, dict) or isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("Parameters must be JSON mappings and seed a nonnegative integer")
        payload = canonical_json(dict(hypothesis=hypothesis, strategy_id=strategy_id,
            parameters=parameters, code_revision=code_revision, feature_id=feature_id,
            seed=seed, input_hashes={} if input_hashes is None else input_hashes))
        trial = RegisteredTrial(digest(payload), payload)
        path = self.directory / f"{trial.trial_id}.json"
        if path.exists():
            if path.read_bytes() != payload:
                raise ValueError("Registered trial was altered")
            return trial
        previous = [json.loads(p.read_bytes()) for p in self.directory.glob("*.json")]
        if sum(record["hypothesis"] == hypothesis for record in previous) >= self.maximum:
            raise ValueError("Preregistered hypothesis trial budget exhausted")
        with path.open("xb") as handle:
            handle.write(payload)
        return trial


@dataclass(frozen=True)
class ExperimentResult:
    run_id: str
    manifest: bytes
    table: pd.DataFrame

    def write(self, directory):
        """Create-once artifacts. Hash table bytes in the manifest before publishing."""
        table_bytes = self.table.to_csv(index=False, lineterminator="\n").encode()
        if digest(table_bytes) != json.loads(self.manifest)["paper_table_sha256"]:
            raise ValueError("Paper table changed after experiment completion")
        path = Path(directory) / self.run_id
        path.mkdir(parents=True, exist_ok=False)
        with (path / "manifest.json").open("xb") as handle:
            handle.write(self.manifest)
        with (path / "paper_table.csv").open("xb") as handle:
            handle.write(table_bytes)
        return path


def run_experiment(trial, history, strategy_factory, *, windows=None, feature_factory=None,
                   costs=None, policy=None, initial_cash=1000.0, warmup_days=0):
    # Reject test requests before constructing strategy/features or reading data.
    windows = {split.name: (split.start, split.end) for split in CONFIG.splits[:2]} if windows is None else dict(windows)
    if set(windows) != {"train", "validation"}:
        raise PermissionError("Experiments require training and validation only; test remains locked")
    for name, (start, end) in windows.items():
        period = CONFIG.split(name)
        if not period.start <= utc_day(start) <= utc_day(end) <= period.end:
            raise PermissionError("Experiment windows cross locked split boundaries")
    spec = trial.specification
    if feature_factory is not None and spec["feature_id"] == "none":
        raise ValueError("Custom features require a registered feature identity")
    costs = ResearchCosts() if costs is None else costs
    policy = ResearchPolicy() if policy is None else policy
    identity = dict(schema=1, trial_id=trial.trial_id, trial=spec,
        strategy_factory_sha256=digest(inspect.getsource(strategy_factory).encode()),
        feature_factory_sha256=None if feature_factory is None else digest(inspect.getsource(feature_factory).encode()),
        engine_sha256={name: digest((Path(__file__).parent / name).read_bytes())
                       for name in ("research_execution.py", "strategy.py", "metrics.py", "data.py", "config.py", "experiment.py")},
        environment=dict(python=platform.python_version(), pandas=version("pandas"), numpy=version("numpy")),
        snapshots=[dict(instrument=asdict(key), manifest=asdict(manifest)) for key, manifest in sorted(history.manifests.items())],
        windows={name: [utc_day(a).isoformat(), utc_day(b).isoformat()] for name, (a, b) in sorted(windows.items())},
        costs=asdict(costs), policy=asdict(policy), initial_cash=initial_cash, warmup_days=warmup_days,
        test_access="locked", fitting_policy="external learned artifacts must be training-only and hash-registered")
    run_id = digest(canonical_json(identity))
    rows = []
    for split in ("train", "validation"):
        # Fresh instances prevent fitted/stateful validation-to-training feedback.
        parameters = trial.specification["parameters"]
        strategy = strategy_factory(parameters, spec["seed"])
        features = None if feature_factory is None else feature_factory(parameters, spec["seed"])
        args = dict(split=split, start=windows[split][0], end=windows[split][1],
                    costs=costs, policy=policy, initial_cash=initial_cash, warmup_days=warmup_days)
        result = run_research(history, StrategyPipeline(strategy, features), **args)
        benchmark = research_buy_and_hold(history, **args)
        row = dict(trial_id=trial.trial_id, hypothesis=spec["hypothesis"], strategy=spec["strategy_id"],
                   parameters=canonical_json(parameters).decode(), split=split, seed=spec["seed"],
                   start=utc_day(args["start"]).isoformat(), end=utc_day(args["end"]).isoformat())
        row.update(result.metrics())
        row.update({f"benchmark_{key}": value for key, value in benchmark.metrics().items()})
        row["benchmark_relative_return"] = row["total_return"] - row["benchmark_total_return"]
        rows.append(row)
    table = pd.DataFrame(rows)
    metrics = [{key: None if isinstance(value, float) and not math.isfinite(value) else value
                for key, value in row.items()} for row in rows]
    table_bytes = table.to_csv(index=False, lineterminator="\n").encode()
    manifest = canonical_json(dict(inputs=identity, run_id=run_id, rows=metrics,
                                   paper_table_sha256=digest(table_bytes)))
    return ExperimentResult(run_id, manifest, table)
