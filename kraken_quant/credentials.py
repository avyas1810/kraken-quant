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
    # Authentication and balances do not need a research universe or market map.
    # Resolve only a caller-requested fee instrument, lazily.
    def resolve_pair(instrument):
        try:
            client.load_markets()
            market = client.market(instrument.symbol)
            if (instrument.venue != "kraken" or instrument.market_type != "spot"
                    or not market.get("spot") or market.get("symbol") != instrument.symbol
                    or market.get("base") != instrument.base or market.get("quote") != instrument.quote):
                raise ValueError("Instrument identity mismatch")
            return market["id"]
        except Exception:
            raise RuntimeError("Public instrument identity lookup failed") from None
    adapter.pair_resolver = resolve_pair
    return adapter
