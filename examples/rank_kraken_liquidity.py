import ccxt
import pandas as pd


from research_config import CANDIDATE_SYMBOLS

SYMBOLS = CANDIDATE_SYMBOLS


def calculate_quote_volume(ticker):
    quote_volume = ticker.get("quoteVolume")

    if quote_volume is not None:
        return float(quote_volume)

    base_volume = ticker.get("baseVolume")
    last_price = ticker.get("last")

    if base_volume is not None and last_price is not None:
        return float(base_volume) * float(last_price)

    return None


kraken = ccxt.kraken({
    "enableRateLimit": True,
})

kraken.load_markets()
tickers = kraken.fetch_tickers(SYMBOLS)

results = []

for symbol in SYMBOLS:
    ticker = tickers.get(symbol, {})

    results.append({
        "symbol": symbol,
        "last_price": ticker.get("last"),
        "base_volume_24h": ticker.get("baseVolume"),
        "estimated_usd_volume_24h": calculate_quote_volume(ticker),
        "bid": ticker.get("bid"),
        "ask": ticker.get("ask"),
    })

liquidity = pd.DataFrame(results)

liquidity["spread_bps"] = (
    (liquidity["ask"] - liquidity["bid"])
    / ((liquidity["ask"] + liquidity["bid"]) / 2)
    * 10_000
)

liquidity = liquidity.sort_values(
    by="estimated_usd_volume_24h",
    ascending=False,
)

print("\nKraken liquidity ranking:\n")
print(liquidity.to_string(index=False))

print("\nRecommended five-pair research universe:")
print(liquidity.head(5)["symbol"].tolist())