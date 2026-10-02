import pandas as pd

from research_config import RESEARCH_UNIVERSE, DATA_START, DATA_END
from twsq.data import BinancePrices


START_DATE = DATA_START
END_DATE = DATA_END
expected_dates = pd.date_range(start=START_DATE, end=END_DATE, freq="D")

prices = BinancePrices()
results = []
failures = []

for symbol in RESEARCH_UNIVERSE:
    bars = prices.get_bars(
        symbol=symbol,
        freq="1d",
        start_ts=START_DATE,
        end_ts=END_DATE,
    )

    bars = bars.sort_index()

    duplicate_rows = int(bars.index.duplicated().sum())
    missing_values = int(bars.isna().sum().sum())

    if bars.empty:
        first_date = None
        last_date = None
        missing_days = len(expected_dates)
        missing_start = True
        missing_end = True
        failures.append(f"{symbol}: no data returned for {START_DATE} to {END_DATE}")
    else:
        first_date = bars.index.min()
        last_date = bars.index.max()

        observed_dates = pd.DatetimeIndex(
            bars.index.normalize().unique()
        )

        missing_days = len(
            expected_dates.difference(observed_dates)
        )
        missing_start = expected_dates[0] not in observed_dates
        missing_end = expected_dates[-1] not in observed_dates

        if duplicate_rows or missing_values or missing_days:
            failures.append(
                f"{symbol}: duplicate_rows={duplicate_rows}, "
                f"missing_values={missing_values}, missing_days={missing_days}, "
                f"missing_start={missing_start}, missing_end={missing_end}"
            )

    results.append({
        "symbol": symbol,
        "rows": len(bars),
        "first_date": first_date,
        "last_date": last_date,
        "duplicate_rows": duplicate_rows,
        "missing_values": missing_values,
        "missing_days": missing_days,
        "missing_start": missing_start,
        "missing_end": missing_end,
    })

audit = pd.DataFrame(results)

print("\nHistorical data audit:\n")
print(audit.to_string(index=False))

if failures:
    raise SystemExit("Historical data audit FAILED:\n" + "\n".join(failures))

print(f"\nHistorical data audit PASSED for every symbol: {START_DATE} to {END_DATE}.")
