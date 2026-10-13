"""Explicit .env-only read credential loader. Never loads a trading secret."""

import hashlib
from pathlib import Path

from .reconciliation import READ_ENDPOINTS, ReadOnlyKraken


def readonly_from_env(path, *, clock, nonce):
    # Import only on explicit private-account opt-in. No environment fallback.
    from dotenv import dotenv_values
    import ccxt

    values = dotenv_values(Path(path), interpolate=False)
    key, secret = values.get("KRAKEN_READONLY_API_KEY"), values.get("KRAKEN_READONLY_API_SECRET")
    if not key or not secret:
        raise ValueError("Dedicated read-only credentials missing from .env")
    if values.get("KRAKEN_TRADING_API_KEY") == key:
        raise PermissionError("Read-only and trading keys must be separate")
    client = ccxt.kraken({"apiKey": key, "secret": secret, "enableRateLimit": True})
    client.nonce = nonce
    fingerprint = hashlib.sha256(key.encode()).hexdigest()

    def request(endpoint, params):
        if endpoint not in READ_ENDPOINTS:
            raise PermissionError("Trading/funding requests prohibited")
        return client.request(endpoint, "private", "POST", params)

    adapter = ReadOnlyKraken(request, fingerprint, clock=clock)
    adapter.permissions()  # Fail closed before querying other private data.
    # ccxt's verified public market map binds internal IDs to canonical symbols.
    try:
        client.load_markets()
        from .strategy import Instrument
        from .config import CONFIG
        adapter.pair_ids = {Instrument("kraken", symbol, *symbol.split("/")): client.market(symbol)["id"]
                            for symbol in CONFIG.universe}
    except Exception:
        raise RuntimeError("Public instrument identity lookup failed") from None
    return adapter
