"""Frozen candidate binding and durable paper-to-qualification evidence.

Unavailable robustness/review evidence is missing, never inferred from a software
test or backfilled with favorable numbers. No function here authorises live orders.
"""

import hashlib
import inspect
import importlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .candidate import read_candidate
from .data import canonical_json, utc_instant
from .operations import Operations
from .strategy import StrategyPipeline


def candidate_pipeline(path, *, now):
    from .strategies import BTCMomentum, MultiAssetMomentum
    record = read_candidate(path)
    spec = record["specification"]
    if utc_instant(now) < utc_instant(spec["prospective_start"]):
        raise PermissionError("Prospective paper collection has not started")
    strategies = {"btc_momentum": BTCMomentum, "multi_momentum": MultiAssetMomentum,
                  "kraken_spot_multi_momentum_v1": MultiAssetMomentum}
    root = Path(__file__).resolve().parents[1]
    for relative, expected in spec.get("source_code_hashes", {}).items():
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or source.suffix != ".py":
            raise PermissionError("Candidate source path outside repository")
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise PermissionError("Candidate source changed since preregistration")
    if spec["strategy"] in strategies:
        strategy = strategies[spec["strategy"]](**spec["parameters"])
        pipeline = StrategyPipeline(strategy)
        source = Path(inspect.getfile(type(strategy))).resolve()
    elif ":" in spec["strategy"]:
        module, name = spec["strategy"].split(":", 1)
        factory = getattr(importlib.import_module(module), name)
        source = Path(inspect.getfile(factory)).resolve()
        if not source.is_relative_to(root):
            raise PermissionError("Frozen custom factory must have repository source provenance")
        relative = source.relative_to(root).as_posix()
        if spec.get("source_code_hashes", {}).get(relative) != hashlib.sha256(source.read_bytes()).hexdigest():
            raise PermissionError("Frozen custom factory source hash required")
        strategy = factory(spec["parameters"], spec.get("seed", 17))
        pipeline = strategy if isinstance(strategy, StrategyPipeline) else StrategyPipeline(strategy)
    else:
        raise ValueError("Use a registered strategy name or hash-bound trusted module:factory")
    if not source.is_relative_to(root):
        raise PermissionError("Frozen strategy must have repository source provenance")
    relative = source.relative_to(root).as_posix()
    if spec.get("source_code_hashes", {}).get(relative) != hashlib.sha256(source.read_bytes()).hexdigest():
        raise PermissionError("Frozen strategy source hash required")
    binding = dict(candidate_sha256=record["sha256"], specification=spec,
        candidate_file_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        strategy_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        input_hashes=spec["input_hashes"], real_order_authorized=False)
    return pipeline, binding


def bind_candidate(ops, binding):
    existing = ops.value("paper_candidate")
    if existing is not None and existing != binding:
        raise PermissionError("Paper candidate binding changed; use a new database")
    ops.set_value("paper_candidate", binding)


def _artifact(path, *, candidate_sha, equity_sha):
    if path is None:
        return None, None
    target = Path(path)
    body = json.loads(target.read_text(encoding="utf-8-sig"))
    if body.get("candidate_sha256") != candidate_sha or body.get("equity_sha256") != equity_sha:
        raise PermissionError("Reviewed artifact candidate/equity identity mismatch")
    return body, dict(file=str(target.resolve()), sha256=hashlib.sha256(target.read_bytes()).hexdigest())


def qualification_evidence(database, candidate_path, output, *, validation_report=None,
                           review_report=None, robustness_report=None):
    """Export genuine observations and a daily benchmark/equity validation input.

    The equal-weight spot benchmark buys the initial observed asks with the frozen
    fee and liquidates at each observed bid, including exit fees. It is a public
    quote scenario, not a claim of actual exchange fills. Review/robustness reports
    must bind both candidate SHA and the exact exported equity CSV SHA.
    """
    record = read_candidate(candidate_path)
    spec = record["specification"]
    target = Path(output)
    equity_path = target.with_suffix(".equity.csv")
    if target.exists():
        raise FileExistsError("Qualification evidence destination exists")
    ops = Operations(database)
    try:
        ops.db.execute("BEGIN")
        ops.ledger_check()
        binding = ops.value("paper_candidate")
        if binding is None or binding["candidate_sha256"] != record["sha256"]:
            raise PermissionError("Database is not bound to this frozen candidate")
        if binding["candidate_file_sha256"] != hashlib.sha256(Path(candidate_path).read_bytes()).hexdigest():
            raise PermissionError("Candidate file differs from bound preregistration")
        prospective_start = utc_instant(spec["prospective_start"])
        all_events = [json.loads(raw) for raw, in ops.db.execute("SELECT body FROM shadow_events ORDER BY rowid")]
        events = [e for e in all_events if utc_instant(e["timestamp"]) >= prospective_start]
        samples = [json.loads(raw) for raw, in ops.db.execute("SELECT body FROM ops_samples ORDER BY timestamp")]
        genuine = [s for s in samples if s["source"] == "kraken_public"
                   and utc_instant(s["timestamp"]) >= prospective_start]
        genuine_days = {utc_instant(s["timestamp"]).normalize() for s in genuine}
        # A decision counts only with a real public observation for that UTC day.
        events = [e for e in events if utc_instant(e["timestamp"]).normalize() in genuine_days]
        origin = ops.value("ledger_origin")
        engine_manifest = ops.value("engine_manifest")
        replay_manifest = ops.value("replay_manifest")
        if replay_manifest is not None:
            # A replay cannot earn public-feed credit even if a malformed or
            # accidentally relabelled sample claims otherwise.
            genuine, events = [], []
        fault_events = ops.db.execute("SELECT count(*) FROM ops_events WHERE kind IN ('halt','fault','recovery') AND timestamp>=?",
                                      (prospective_start.isoformat(),)).fetchone()[0]
        state = ops.inspect()
        ops.db.execute("COMMIT")
    finally:
        ops.close()
    dates = [utc_instant(e["timestamp"]) for e in events]
    lifecycles = [f["order_id"] for e in events for f in e["fills"]]
    unique_days = sorted({stamp.normalize() for stamp in dates})
    expected_days = 0 if not unique_days else (unique_days[-1] - prospective_start.normalize()).days + 1
    elapsed = 0 if len(genuine) < 2 else (utc_instant(genuine[-1]["timestamp"])
                                          - utc_instant(genuine[0]["timestamp"])).total_seconds()
    heartbeat_gaps = sum(utc_instant(b["timestamp"]) - utc_instant(a["timestamp"]) > pd.Timedelta(seconds=60)
                         for a, b in zip(genuine, genuine[1:]))
    complete = 0 if not expected_days else len(unique_days)/expected_days
    prerequisites = bool(genuine) and all(s["accounting_ok"] is True and s["account_fee_verified"] is True
                                         and s["event_time_verified"] is True for s in genuine)
    reliability = (len(unique_days) >= 30 and len(set(lifecycles)) >= 20 and elapsed >= 29*86400
                   and complete >= .995 and fault_events == 0 and heartbeat_gaps == 0 and prerequisites
                   and not state["halted"])
    rows = []
    quantities = {}
    initial_cash = float(origin["cash"])
    fee = float(spec["costs"]["fee_rate"])
    if events:
        inputs = events[0]["inputs"]
        if {item["instrument"]["symbol"] for item in inputs} != set(spec["universe"]):
            raise PermissionError("Candidate/evidence universe mismatch")
        quantities = {item["instrument"]["symbol"]: initial_cash / len(inputs) / float(item["ask"]) / (1 + fee)
                      for item in inputs}
        for event in events:
            benchmark = sum(quantities[item["instrument"]["symbol"]] * float(item["bid"]) * (1 - fee)
                            for item in event["inputs"])
            rows.append(dict(timestamp=utc_instant(event["timestamp"]).normalize(),
                equity=float(event["equity"]), cash=float(event["cash"]), benchmark_equity=benchmark))
    frame = pd.DataFrame(rows, columns=["timestamp", "equity", "cash", "benchmark_equity"]).set_index("timestamp")
    payload = frame.to_csv().encode()
    if equity_path.exists() and equity_path.read_bytes() != payload:
        raise FileExistsError("Existing validation equity differs; use a new evidence output")
    target.parent.mkdir(parents=True, exist_ok=True)
    equity_path.write_bytes(payload)
    equity_sha = hashlib.sha256(payload).hexdigest()
    source = "historical_replay" if replay_manifest is not None else "prospective_public_feed"
    result = dict(schema=1, candidate_sha256=record["sha256"], source=source,
        actual_daily_decisions=len(unique_days), decision_dates_utc=[s.isoformat() for s in dates],
        completed_lifecycle_ids=lifecycles, simulated_order_lifecycles=len(set(lifecycles)),
        elapsed_seconds=elapsed, completion_fraction=complete,
        missing_days=expected_days-len(unique_days), fault_events=fault_events,
        heartbeat_gaps=heartbeat_gaps, reliability_passed=reliability,
        independent_review_passed=False, live_authorized=False, engine_manifest=engine_manifest,
        input_hashes=spec["input_hashes"], equity_sha256=equity_sha,
        durable_events_sha256=hashlib.sha256(canonical_json(all_events)).hexdigest(),
        durable_samples_sha256=hashlib.sha256(canonical_json(samples)).hexdigest(),
        equity_file=str(equity_path.resolve()), validation_artifacts={},
        limitations=["Paper fills and equal-weight benchmark are quote scenarios, not real exchange executions.",
                     "Unknown trials and unavailable robustness/review remain explicit qualification failures."])
    if len(frame):
        values = frame.equity
        returns = values.pct_change().dropna()
        result["net_return"] = float(values.iloc[-1]/initial_cash-1)
        result["benchmark_relative_return"] = float((values.iloc[-1]-frame.benchmark_equity.iloc[-1])/initial_cash)
        result["max_drawdown"] = float(-(values/pd.concat([pd.Series([initial_cash]), values.reset_index(drop=True)]).cummax().iloc[1:].to_numpy()-1).min())
        if len(returns) > 1 and returns.std(ddof=1) > 0:
            result["sharpe"] = float(returns.mean()/returns.std(ddof=1)*np.sqrt(365))
    if validation_report is not None:
        path = Path(validation_report)
        body = json.loads(path.read_text(encoding="utf-8-sig"))
        if body.get("source_sha256") != equity_sha or body.get("schema") != 1:
            raise PermissionError("Statistical validation does not match durable prospective equity")
        result["validation_artifacts"]["statistics"] = dict(file=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        result["statistics"] = body
    robustness, identity = _artifact(robustness_report, candidate_sha=record["sha256"], equity_sha=equity_sha)
    if robustness is not None:
        for key in ("positive_assets", "neighbour_parameters_positive", "round_trips",
                    "doubled_spread_slippage_return", "bootstrap_mean_excess_ci_low", "bootstrap_confidence"):
            if key in robustness:
                result[key] = robustness[key]
        result["validation_artifacts"]["robustness"] = identity
    review, identity = _artifact(review_report, candidate_sha=record["sha256"], equity_sha=equity_sha)
    if review is not None:
        result["independent_review_passed"] = review.get("passed") is True and bool(review.get("reviewer")) and validation_report is not None and robustness_report is not None
        result["validation_artifacts"]["independent_review"] = identity
    result["qualification_missing"] = [key for key in ("sharpe", "positive_assets", "neighbour_parameters_positive",
        "round_trips", "doubled_spread_slippage_return", "bootstrap_mean_excess_ci_low", "bootstrap_confidence") if key not in result]
    target.write_bytes(canonical_json(result) + b"\n")
    return dict(output=str(target), equity=str(equity_path), candidate_sha256=record["sha256"],
                actual_daily_decisions=result["actual_daily_decisions"],
                qualification_missing=result["qualification_missing"], live_authorized=False)
