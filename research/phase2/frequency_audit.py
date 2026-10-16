"""Reconcile frozen four-hour returns with compounded UTC-daily Sharpe.

The input is a verified Phase 1 export. This audit never edits the export or
the locked OOS; it labels the OOS as already inspected and records its hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _sharpe(values: np.ndarray, annualisation: int) -> float:
    if len(values) < 2 or not np.isfinite(values).all():
        raise ValueError("At least two finite returns are required")
    sigma = float(np.std(values, ddof=1))
    if sigma <= 0:
        raise ValueError("Sharpe is undefined for zero volatility")
    # The frozen study has no risk-free series; this explicitly uses RF=0.
    return float(np.sqrt(annualisation) * np.mean(values) / sigma)


def audit(frame: pd.DataFrame, *, timestamp: str, columns: list[str],
          oos_start: str = "2024-01-01", source_sha256: str | None = None) -> dict:
    if timestamp not in frame or not columns or any(c not in frame for c in columns):
        raise ValueError("Timestamp and requested return columns must exist")
    stamps = pd.to_datetime(frame[timestamp], utc=True, errors="raise")
    if stamps.isna().any() or stamps.duplicated().any() or not stamps.is_monotonic_increasing:
        raise ValueError("Unique ordered UTC timestamps required")
    raw = frame[columns].apply(pd.to_numeric, errors="raise")
    if not np.isfinite(raw.to_numpy(dtype=float)).all() or (raw <= -1).any().any():
        raise ValueError("Returns must be finite and greater than -100%")
    data = raw.copy()
    data.index = pd.DatetimeIndex(stamps)
    oos = data.loc[data.index >= pd.Timestamp(oos_start, tz="UTC")]
    if len(oos) == 0 or (oos.index.to_series().diff().dropna() != pd.Timedelta(hours=4)).any():
        raise ValueError("OOS must be a nonempty contiguous four-hour sample")
    counts = pd.Series(1, index=oos.index).groupby(oos.index.floor("D")).sum()
    if (counts != 6).any():
        raise ValueError("Each OOS UTC date must contain exactly six four-hour returns")
    daily = (1.0 + oos).groupby(oos.index.floor("D")).prod() - 1.0
    if not np.isfinite(daily.to_numpy(dtype=float)).all() or (daily <= -1).any().any():
        raise ValueError("Daily compounded returns are invalid")
    records = []
    for column in columns:
        bar = oos[column].to_numpy(dtype=float)
        days = daily[column].to_numpy(dtype=float)
        bar_total = float(np.prod(1.0 + bar) - 1.0)
        day_total = float(np.prod(1.0 + days) - 1.0)
        if not np.isclose(bar_total, day_total, rtol=0, atol=1e-12):
            raise AssertionError("Compounding four-hour returns by UTC day changed total wealth")
        records.append({
            "return_series": column,
            "oos_start_utc": oos.index[0].isoformat(),
            "oos_end_utc": oos.index[-1].isoformat(),
            "four_hour_observations": int(len(oos)),
            "utc_days": int(len(daily)),
            "four_hour_sharpe_sqrt_2190_rf0": _sharpe(bar, 2190),
            "daily_compounded_sharpe_sqrt_252_rf0": _sharpe(days, 252),
            "daily_compounded_sharpe_sqrt_365_rf0": _sharpe(days, 365),
            "compounded_total_return": day_total,
            "wealth_reconciliation_abs_error": abs(bar_total - day_total),
            "risk_free_rate": 0.0,
            "daily_aggregation": "compound six consecutive UTC four-hour net returns per UTC calendar date",
            "status": "historical_already_inspected_not_pristine_holdout",
        })
    return {"schema_version": 1, "source_sha256": source_sha256,
            "oos_start_rule": oos_start, "input_rows": len(frame), "results": records}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="Hash-verified frozen Phase 1 CSV")
    parser.add_argument("--timestamp", default="Close Time")
    parser.add_argument("--columns", nargs="+", default=["4h_mom_low_correlation"])
    parser.add_argument("--oos-start", default="2024-01-01")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    digest = hashlib.sha256(args.csv.read_bytes()).hexdigest()
    result = audit(pd.read_csv(args.csv), timestamp=args.timestamp, columns=args.columns,
                   oos_start=args.oos_start, source_sha256=digest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "verified", "output": str(args.output),
                      "results": result["results"]}, indent=2))


if __name__ == "__main__":
    main()
