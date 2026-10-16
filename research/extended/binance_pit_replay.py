"""Replay the frozen 4H momentum research on an ex-ante liquidity cohort.

The cohort is selected using the official Binance 2021-08 USDT quote-volume
archive, before the evaluation sample. Raw archives stay outside this repo.
Missing bars are never forward-filled; an unexplained post-listing data exit is
marked at -99.9% once as a deliberately severe terminal-recovery sensitivity.
This exploratory study reuses already-inspected historical dates; it is not a
pristine holdout or Kraken executable strategy.
"""
from __future__ import annotations

import argparse
import ast
import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import zipfile

import numpy as np
import pandas as pd

from research.phase2.common import sha256_file
from research.extended.universe import AssetHistory


COHORT = (
    "BTC", "ETH", "ADA", "DOGE", "XRP", "BNB", "SOL", "DOT", "AXS", "MATIC",
    "C98", "LUNA", "AVAX", "ICP", "FIL", "ALICE", "LINK", "ETC", "TLM",
    "VET", "IOTX", "EOS", "LTC", "THETA", "SHIB", "CHZ", "ATA", "FTT",
    "FTM", "RUNE",
)
PERIODS_PER_YEAR = 2190
COST_BPS = 20.0
LOOKBACK = 180
BINANCE_S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/"
BINANCE_CDN = "https://data.binance.vision/"
EXCLUDED_BASES = frozenset({"BUSD", "USDC", "USDP", "TUSD", "DAI", "FDUSD", "EUR", "AUD", "GBP", "TRY", "BRL", "RUB", "UAH", "BIDR", "IDRT", "NGN"})


def _get(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "kraken-quant-research/1.0"}), timeout=45) as response:
        return response.read()


def _exists(url: str) -> bool:
    try:
        with urlopen(Request(url, method="HEAD", headers={"User-Agent": "kraken-quant-research/1.0"}), timeout=20):
            return True
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def _path_component(value: str) -> str:
    return quote(value, safe="")


def _archive_symbols() -> list[str]:
    """List Binance monthly spot-market directories via the official S3 index."""
    symbols, token = [], None
    while True:
        query = {"list-type": "2", "delimiter": "/", "prefix": "data/spot/monthly/klines/"}
        if token:
            query["continuation-token"] = token
        import xml.etree.ElementTree as ET
        root = ET.fromstring(_get(BINANCE_S3 + "?" + urlencode(query)))
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        symbols.extend(node.find("s:Prefix", ns).text.rstrip("/").rsplit("/", 1)[-1]
                       for node in root.findall("s:CommonPrefixes", ns))
        next_node = root.find("s:NextContinuationToken", ns)
        if next_node is None:
            return sorted(set(symbol for symbol in symbols if symbol.endswith("USDT")))
        token = next_node.text


def _verified_download(url: str) -> tuple[bytes, str]:
    """Download an archive and verify Binance's official SHA-256 sidecar."""
    content = _get(url)
    sidecar = _get(url + ".CHECKSUM").decode("ascii").split()
    if not sidecar:
        raise ValueError("Empty official checksum sidecar")
    expected = sidecar[0].lower()
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise ValueError("Official archive checksum mismatch: " + url)
    return content, actual


def _quote_volume(zip_bytes: bytes) -> tuple[float, int]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise ValueError("Expected one CSV in monthly kline archive")
        rows = csv.reader(io.TextIOWrapper(archive.open(names[0]), encoding="utf-8"))
        parsed = [row for row in rows if len(row) >= 8]
    return sum(float(row[7]) for row in parsed), len(parsed)


def select_august_cohort(records: list[dict]) -> list[dict]:
    """Select a reproducible top-30 historical cohort, with no current-list filter."""
    eligible = [row for row in records if row["bars"] == 31 and
                row["symbol"] not in EXCLUDED_BASES and
                not row["symbol"].endswith(("UP", "DOWN", "BULL", "BEAR"))]
    return sorted(eligible, key=lambda row: (-row["quote_volume_usdt"], row["symbol"]))[:30]


def download_official_cohort(cache: Path, output: Path) -> dict:
    """Download the official 2021-08 selection snapshot and cohort 4H archives."""
    cache, output = Path(cache), Path(output)
    cache.mkdir(parents=True, exist_ok=True); output.mkdir(parents=True, exist_ok=True)
    markets = _archive_symbols()
    def has_daily_snapshot(symbol):
        encoded = _path_component(symbol)
        return symbol if _exists(
            f"{BINANCE_CDN}data/spot/monthly/klines/{encoded}/1d/{encoded}-1d-2021-08.zip") else None
    with ThreadPoolExecutor(max_workers=32) as pool:
        daily_files = [symbol for symbol in pool.map(has_daily_snapshot, markets) if symbol]
    def daily(symbol):
        encoded = _path_component(symbol)
        url = f"{BINANCE_CDN}data/spot/monthly/klines/{encoded}/1d/{encoded}-1d-2021-08.zip"
        data, digest = _verified_download(url)
        quote_volume, bars = _quote_volume(data)
        return {"symbol": symbol.removesuffix("USDT"), "pair": symbol, "bars": bars,
                "quote_volume_usdt": quote_volume, "zip_sha256": digest,
                "official_checksum_sha256": digest}
    with ThreadPoolExecutor(max_workers=32) as pool:
        records = list(pool.map(daily, daily_files))
    selected = select_august_cohort(records)
    excluded = []
    rows = []
    chosen = {row["pair"] for row in selected}
    eligible_sorted = [row for row in sorted(
        (r for r in records if r["bars"] == 31 and r["symbol"] not in EXCLUDED_BASES and
         not r["symbol"].endswith(("UP", "DOWN", "BULL", "BEAR"))),
        key=lambda r: (-r["quote_volume_usdt"], r["symbol"]))]
    rank = {row["pair"]: i + 1 for i, row in enumerate(eligible_sorted)}
    for row in records:
        is_excluded = (row["bars"] != 31 or row["symbol"] in EXCLUDED_BASES or
                       row["symbol"].endswith(("UP", "DOWN", "BULL", "BEAR")))
        rows.append({**row, "liquidity_rank": rank.get(row["pair"]),
                     "excluded_by_rule": is_excluded, "selected": row["pair"] in chosen})
    fields = ["liquidity_rank", "symbol", "pair", "bars", "quote_volume_usdt",
              "excluded_by_rule", "selected", "zip_sha256", "official_checksum_sha256"]
    selection_path = output / "universe_selection_aug2021.csv"
    with selection_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: row["pair"]))
    selection_manifest = {
        "source": "Binance official monthly 1d spot klines; August 2021",
        "listing_symbols_discovered": len(markets), "historically_traded_quote_symbols": len(records),
        "excluded_base_rules": sorted(EXCLUDED_BASES) + ["symbols ending UP/DOWN/BULL/BEAR"],
        "selection_method": "top 30 eligible USDT spot bases by August quote-asset volume; 31 daily bars; lexical tie-break",
        "selected_assets": [row["symbol"] for row in selected],
        "daily_archive_inputs": {row["pair"]: row["zip_sha256"] for row in records},
        "checksums_verified": len(records),
    }
    (output / "universe_selection_manifest.json").write_text(
        json.dumps(selection_manifest, indent=2) + "\n", encoding="utf-8")
    months = []
    for year in range(2021, 2027):
        for month in range(1, 13):
            if (year, month) >= (2021, 8) and (year, month) <= (2026, 6):
                months.append(f"{year}-{month:02d}")
    jobs = [(row["symbol"], row["pair"], month) for row in selected for month in months]
    def monthly(job):
        asset, pair, month = job
        name = f"{pair}-4h-{month}.zip"
        rel = f"{pair}/{name}"
        encoded = _path_component(pair)
        url = f"{BINANCE_CDN}data/spot/monthly/klines/{encoded}/4h/{_path_component(name)}"
        path = cache / pair / name
        if path.exists():
            try:
                expected = _get(url + ".CHECKSUM").decode("ascii").split()[0].lower()
                actual = sha256_file(path)
                if actual == expected:
                    return rel, actual
            except HTTPError as exc:
                if exc.code == 404:
                    return rel, None
                raise
        try:
            data, digest = _verified_download(url)
        except HTTPError as exc:
            if exc.code == 404:
                return rel, None
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return rel, digest
    with ThreadPoolExecutor(max_workers=48) as pool:
        monthly_results = list(pool.map(monthly, jobs))
    checksums = {relative: digest for relative, digest in monthly_results if digest}
    (cache / "archive_checksums.json").write_text(
        json.dumps(checksums, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"markets_discovered": len(markets), "daily_snapshots": len(records),
            "cohort_assets": len(selected), "monthly_requests": len(jobs),
            "verified_4h_files": len(checksums), "missing_4h_files": len(jobs)-len(checksums)}


def close_time_index(open_timestamps) -> pd.DatetimeIndex:
    """Convert Binance 4H open times (ms or us) to exact UTC close labels."""
    values = pd.Series(pd.to_numeric(pd.Series(open_timestamps), errors="raise").astype("int64"))
    micros = values.ge(100_000_000_000_000)
    stamps = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns, UTC]")
    stamps.loc[~micros] = pd.to_datetime(
        values.loc[~micros] + 4 * 60 * 60 * 1000 - 1, unit="ms", utc=True)
    stamps.loc[micros] = pd.to_datetime(
        values.loc[micros] + 4 * 60 * 60 * 1_000_000 - 1000, unit="us", utc=True)
    if stamps.isna().any():
        raise ValueError("Unable to convert Binance kline timestamps")
    return pd.DatetimeIndex(stamps)


def load_archive(root: Path, *, start="2021-08-01", end="2026-06-30"):
    """Load validated official monthly 4H zip files for the frozen cohort."""
    root = Path(root)
    checksum_path = root / "archive_checksums.json"
    if not checksum_path.exists():
        raise FileNotFoundError("Official .CHECKSUM verification receipt required in archive_checksums.json")
    expected_hashes = json.loads(checksum_path.read_text(encoding="utf-8"))
    records: dict[str, pd.DataFrame] = {}
    archive_hashes: dict[str, str] = {}
    for asset in COHORT:
        pair = asset + "USDT"
        chunks = []
        for path in sorted((root / pair).glob(f"{pair}-4h-*.zip")):
            digest = sha256_file(path)
            relative = str(path.relative_to(root)).replace("\\", "/")
            expected = expected_hashes.get(relative)
            if expected != digest:
                raise ValueError(f"Official Binance .CHECKSUM mismatch or missing receipt: {relative}")
            archive_hashes[relative] = digest
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                if len(names) != 1:
                    raise ValueError(f"Expected one CSV in {path.name}")
                chunk = pd.read_csv(zf.open(names[0]), header=None)
            if chunk.shape[1] < 8:
                raise ValueError(f"Malformed Binance kline: {path.name}")
            bars = pd.DataFrame({
                "open_ms": pd.to_numeric(chunk.iloc[:, 0], errors="raise").astype("int64"),
                "close": pd.to_numeric(chunk.iloc[:, 4], errors="raise"),
                "base_volume": pd.to_numeric(chunk.iloc[:, 5], errors="raise"),
                "quote_volume": pd.to_numeric(chunk.iloc[:, 7], errors="raise"),
            })
            if (bars.close <= 0).any() or (bars[["base_volume", "quote_volume"]] < 0).any().any():
                raise ValueError(f"Invalid kline values: {path.name}")
            chunks.append(bars)
        if not chunks:
            raise FileNotFoundError(f"No official 4H history for {pair}")
        data = pd.concat(chunks, ignore_index=True).sort_values("open_ms")
        if data.open_ms.duplicated().any():
            for _, duplicate_rows in data.groupby("open_ms", sort=False):
                if len(duplicate_rows) > 1 and len(duplicate_rows.drop_duplicates()) != 1:
                    raise ValueError(f"Conflicting duplicate 4H bars for {pair}")
            data = data.drop_duplicates(subset="open_ms", keep="first")
        # Binance changed public archive timestamps from milliseconds to
        # microseconds; infer units only from the unambiguous epoch magnitude.
        data["timestamp"] = close_time_index(data.open_ms)
        data = data.set_index("timestamp").drop(columns="open_ms")
        end_stamp = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
        records[asset] = data.loc[pd.Timestamp(start, tz="UTC"):end_stamp]
    index = pd.date_range(pd.Timestamp(start, tz="UTC") + pd.Timedelta(hours=4) - pd.Timedelta(milliseconds=1),
                          pd.Timestamp(end, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59, milliseconds=999),
                          freq="4h")
    close = pd.DataFrame({a: records[a].close.reindex(index) for a in COHORT}, index=index)
    base_volume = pd.DataFrame({a: records[a].base_volume.reindex(index) for a in COHORT}, index=index)
    quote_volume = pd.DataFrame({a: records[a].quote_volume.reindex(index) for a in COHORT}, index=index)
    # A symbol that disappears and later reappears can represent a re-listing,
    # ticker reuse, chain migration or a data hole. Without an authenticated
    # instrument-identity bridge, fail closed and stop that selected asset at
    # its first gap instead of silently joining two instruments.
    for asset in COHORT:
        observed = close[asset].notna()
        first = observed[observed].index[0]
        gap_after_listing = (~observed) & (close.index > first)
        if gap_after_listing.any():
            first_gap = gap_after_listing[gap_after_listing].index[0]
            close.loc[first_gap:, asset] = np.nan
            base_volume.loc[first_gap:, asset] = np.nan
            quote_volume.loc[first_gap:, asset] = np.nan
    hashes_by_asset = {}
    histories = {}
    for asset in COHORT:
        keys = [k for k in archive_hashes if k.startswith(asset + "USDT/")]
        aggregate = hashlib.sha256("".join(archive_hashes[k] for k in sorted(keys)).encode()).hexdigest()
        valid = close[asset].dropna()
        if valid.empty:
            raise ValueError(f"Empty cohort history: {asset}")
        first, last = valid.index[0], valid.index[-1]
        hashes_by_asset[asset] = {"source_sha256": aggregate, "files": len(keys)}
        histories[asset] = AssetHistory(asset, first, last + pd.Timedelta(hours=4),
                                        first, "Binance official monthly spot klines; listing/delisting proxied by first/last archived bar", aggregate)
    return close, base_volume, quote_volume, histories, archive_hashes, hashes_by_asset


def momentum_weights(close: pd.DataFrame, lookback=LOOKBACK, min_assets=3) -> pd.DataFrame:
    """Frozen cross-sectional tanh-z momentum, with strict complete history."""
    past = close.pct_change(lookback, fill_method=None)
    complete = close.notna().rolling(lookback + 1, min_periods=lookback + 1).sum().eq(lookback + 1)
    signal = past.where(complete)
    signal = signal.where(signal.notna().sum(axis=1).ge(min_assets))
    demeaned = signal.sub(signal.mean(axis=1), axis=0)
    z = demeaned.div(demeaned.std(axis=1).replace(0, np.nan), axis=0).clip(-3, 3)
    scaled = np.tanh(z)
    weights = scaled.div(scaled.abs().sum(axis=1).replace(0, np.nan), axis=0)
    return weights.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _frozen_functions():
    """Load only hash-checked original function definitions, no notebook effects."""
    source_path = Path(__file__).resolve().parents[1] / "phase1_frozen" / "source_cells.json"
    repo_root = Path(__file__).resolve().parents[2]
    manifest_path = repo_root / "phase2_results" / "reviewed_20261016" / "phase2_manifest.json"
    phase2_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256_file(source_path) != phase2_manifest["frozen_artifact_hashes"]["frozen_source_cells"]:
        raise ValueError("Frozen Phase 1 source-cell artifact hash mismatch")
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    env = {"np": np, "pd": pd, "COST_BPS": COST_BPS}
    for cell_index in (3, 5):
        cell = raw["cells"][cell_index]
        source = cell["source"]
        actual = hashlib.sha256(source.encode()).hexdigest()
        if actual != cell["sha256"]:
            raise ValueError("Frozen Phase 1 function source hash mismatch")
        tree = ast.parse(source)
        definitions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        exec("\n\n".join(ast.get_source_segment(source, node) for node in definitions), env)
    return env


def replay(close: pd.DataFrame, base_volume: pd.DataFrame, *, terminal_return=-0.999) -> dict:
    if not close.index.equals(base_volume.index) or not close.columns.equals(base_volume.columns):
        raise ValueError("Close and volume panels must align")
    if not np.isfinite(terminal_return) or not -1 < terminal_return <= 0:
        raise ValueError("Terminal recovery sensitivity must lie in (-100%, 0%]")
    returns = close.pct_change(fill_method=None)
    gap = close.isna() & close.notna().shift(1).fillna(False)
    accounting_returns = returns.copy()
    accounting_returns[gap] = terminal_return
    accounting_returns = accounting_returns.fillna(0.0)
    # Historical state uses only observed returns. It never treats absent bars
    # as zero-return assets; zero is used only in the accounting ledger while
    # an unavailable asset has a zero target, with a terminal shock on exit.
    env = _frozen_functions()
    weights = momentum_weights(close)
    regimes = env["construct_regime_variables"](
        returns=returns, volume=base_volume, vol_window=180,
        regime_window=540, corr_window=180)
    low_corr = weights.mul(regimes["low_correlation"].fillna(0), axis=0)
    baseline_tc = env["compute_transaction_costs"](weights, COST_BPS, accounting_returns)
    filtered_tc = env["compute_transaction_costs"](low_corr, COST_BPS, accounting_returns)
    return {"weights": weights, "low_corr_weights": low_corr,
            "returns": accounting_returns, "regimes": regimes,
            "baseline": baseline_tc, "low_corr": filtered_tc,
            "gap_events": gap.sum().to_dict()}


def metrics(returns: pd.Series, turnover: pd.Series, active: pd.Series) -> dict:
    values = returns.to_numpy(dtype=float)
    wealth = np.r_[1.0, np.cumprod(1.0 + values)]
    if not np.isfinite(wealth).all():
        raise ValueError("Nonfinite portfolio wealth")
    vol = float(np.std(values, ddof=1))
    if vol <= 0:
        raise ValueError("Sharpe undefined for zero volatility")
    return {"observations": len(values), "sharpe_sqrt2190_rf0": float(np.sqrt(PERIODS_PER_YEAR) * np.mean(values) / vol),
            "cumulative_return": float(wealth[-1] - 1),
            "maximum_drawdown": float(np.min(wealth / np.maximum.accumulate(wealth) - 1)),
            "turnover_sum": float(turnover.sum()), "low_correlation_filter_active_fraction": float(active.mean())}


def run(cache: Path, output: Path) -> dict:
    output = Path(output)
    selection_manifest_path = output / "universe_selection_manifest.json"
    selection_table_path = output / "universe_selection_aug2021.csv"
    if not selection_manifest_path.exists() or not selection_table_path.exists():
        raise FileNotFoundError("Hash-verified August 2021 selection table and manifest are required")
    selection_manifest = json.loads(selection_manifest_path.read_text(encoding="utf-8"))
    selection_table = pd.read_csv(selection_table_path)
    if selection_table.pair.duplicated().any() or len(selection_table) != selection_manifest.get("historically_traded_quote_symbols"):
        raise ValueError("August 2021 selection table is incomplete or duplicated")
    expected_daily = selection_manifest.get("daily_archive_inputs", {})
    for row in selection_table.itertuples():
        if expected_daily.get(row.pair) != row.zip_sha256 or row.zip_sha256 != row.official_checksum_sha256:
            raise ValueError("August 2021 input hash/checksum mismatch: " + row.pair)
    eligible = selection_table.loc[~selection_table.excluded_by_rule.astype(bool)].sort_values(
        ["quote_volume_usdt", "symbol"], ascending=[False, True])
    ranked_cohort = tuple(eligible.symbol.head(30))
    if ranked_cohort != COHORT or tuple(selection_manifest.get("selected_assets", ())) != COHORT:
        raise ValueError("Frozen cohort differs from the point-in-time liquidity ranking")
    close, volume, quote_volume, histories, hashes, by_asset = load_archive(cache)
    rows = []
    result = None
    for terminal_return in (-0.999, 0.0):
        result = replay(close, volume, terminal_return=terminal_return)
        oos = result["returns"].index >= pd.Timestamp("2024-01-01", tz="UTC")
        train = (result["returns"].index >= pd.Timestamp("2021-10-01", tz="UTC")) & ~oos
        for split, mask in (("train", train), ("oos_inspected", oos)):
            for name in ("baseline", "low_corr"):
                tc = result[name]
                rows.append({"split": split, "strategy": name,
                             "terminal_return_assumption": terminal_return, **metrics(
                    tc["net_returns"].loc[mask], tc["turnover"].loc[mask],
                    result["regimes"]["low_correlation"].loc[mask].fillna(0).astype(bool))})
    output.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(rows)
    table.to_csv(output / "expanded_universe_metrics.csv", index=False, float_format="%.12g")
    summary = {"schema_version": 1, "study_status": "exploratory_historical_already_inspected",
               "source": "Binance official monthly spot klines; August 2021 quote-volume selected static cohort",
               "selection": {"as_of": "2021-08-31 UTC", "criterion": "top 30 USDT spot crypto base assets by 2021-08 sum quote-asset volume; exclude fiat/stable/leveraged bases", "assets": list(COHORT),
                             "selection_manifest_sha256": sha256_file(selection_manifest_path),
                             "selection_table_sha256": sha256_file(selection_table_path)},
               "period": {"train": ["2021-10-01", "2023-12-31"], "oos_inspected": ["2024-01-01", "2026-06-30"]},
               "method": {"lookback_4h": 180, "cost_bps_gross_traded_notional": COST_BPS,
                          "annualisation": PERIODS_PER_YEAR, "risk_free_rate": 0.0,
                          "delisting_sensitivity": "reported at -99.9% terminal loss and 0% additional loss after first unexplained gap; neither is a verified recovery return",
                          "short_financing": "excluded as in frozen original; signed Binance research only"},
               "archive_hashes": hashes,
               "official_checksum_receipt_sha256": sha256_file(Path(cache) / "archive_checksums.json"),
               "replay_source_sha256": sha256_file(Path(__file__)),
               "frozen_phase1_cells_sha256": sha256_file(Path(__file__).resolve().parents[1] / "phase1_frozen" / "source_cells.json"),
               "asset_provenance": by_asset,
               "effective_history": {a: {"first_observation_in_research_window": str(h.listing), "termination_after_first_unexplained_gap": str(h.delisting)} for a,h in histories.items()},
               "internal_gap_events": result["gap_events"],
               "output_sha256": hashlib.sha256((output / "expanded_universe_metrics.csv").read_bytes()).hexdigest()}
    (output / "expanded_universe_manifest.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return {"metrics": table.to_dict(orient="records"), "assets": len(COHORT), "bar_count": len(close),
            "internal_gap_events": result["gap_events"], "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path, help="Directory for verified monthly ZIPs, pair subfolders, and checksums")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="Acquire official Binance public archives before replay")
    args = parser.parse_args()
    if args.download:
        print(json.dumps({"download": download_official_cohort(args.cache, args.output)}, indent=2))
    print(json.dumps(run(args.cache, args.output), indent=2))


if __name__ == "__main__":
    main()
