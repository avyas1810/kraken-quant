import pandas as pd

from research_config import RESEARCH_UNIVERSE, DATA_START, DATA_END
from twsq.data import BinancePrices


START_DATE = DATA_START
END_DATE = DATA_END

prices = BinancePrices()
results = []

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
        missing_days = None
    else:
        first_date = bars.index.min()
        last_date = bars.index.max()

        expected_dates = pd.date_range(
            start=first_date.normalize(),
            end=last_date.normalize(),
            freq="D",
        )

        observed_dates = pd.DatetimeIndex(
            bars.index.normalize().unique()
        )

        missing_days = len(
            expected_dates.difference(observed_dates)
        )

    results.append({
        "symbol": symbol,
        "rows": len(bars),
        "first_date": first_date,
        "last_date": last_date,
        "duplicate_rows": duplicate_rows,
        "missing_values": missing_values,
        "missing_days": missing_days,
    })

audit = pd.DataFrame(results)

print("\nHistorical data audit:\n")
print(audit.to_string(index=False))
