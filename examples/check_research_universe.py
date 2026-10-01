import ccxt
import pandas as pd


assets = [
    "BTC", "ETH", "SOL", "XRP", "ADA",
    "DOGE", "LINK", "DOT", "AVAX", "LTC"
]

kraken = ccxt.kraken({"enableRateLimit": True})
binance = ccxt.binance({"enableRateLimit": True})

kraken.load_markets()
binance.load_markets()

results = []

for asset in assets:
    kraken_symbol = f"{asset}/USD"
    binance_symbol = f"{asset}/USDT"

    results.append({
        "asset": asset,
        "kraken_live_pair": kraken_symbol,
        "available_on_kraken": kraken_symbol in kraken.markets,
        "binance_backtest_pair": binance_symbol,
        "available_on_binance": binance_symbol in binance.markets,
        "usable": (
            kraken_symbol in kraken.markets
            and binance_symbol in binance.markets
        ),
    })

universe = pd.DataFrame(results)

print(universe.to_string(index=False))
print("\nUsable assets:")
print(universe.loc[universe["usable"], "asset"].tolist())