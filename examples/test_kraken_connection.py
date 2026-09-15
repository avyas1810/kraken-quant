import os

import ccxt
from dotenv import load_dotenv


load_dotenv()

api_key = os.getenv("KRAKEN_API_KEY")
api_secret = os.getenv("KRAKEN_API_SECRET")

if not api_key or not api_secret:
    raise RuntimeError("Kraken credentials were not found in .env")

kraken = ccxt.kraken({
    "apiKey": api_key,
    "secret": api_secret,
    "enableRateLimit": True,
})

kraken.load_markets()
kraken.fetch_balance()

print("Kraken read-only connection successful.")
print(f"Markets loaded: {len(kraken.markets)}")