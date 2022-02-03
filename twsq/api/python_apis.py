import ccxt
from functools import lru_cache
from .connections import get_credential
import logging 

# Third party exchange APIs

@lru_cache(16)
def get_api(exch: str):
    if exch == "Kraken":
        return instantiate_ccxt_kraken()
    elif exch=='Binance':
        return instantiate_ccxt_binance()
    else:
        raise ValueError(f"Unsupported exchange <{exch}>")

def instantiate_ccxt_kraken():
    # This API key is set to allow for nonce window of 3000
    # Or 3 seconds, since ccxt uses milliseconds as nonce

    api = ccxt.kraken(
        {
            "apiKey": get_credential("Kraken", "key"),
            "secret": get_credential("Kraken", "secret"),
            # "enableRateLimit": True,
            "rateLimit": 50,  # compared to default of 3000; need to be careful
        }
    )

    api.fetch_currencies()
    api.loadMarkets()


    logging.debug("Instantiated Kraken API via ccxt")

    return api

def instantiate_ccxt_binance():
    # This API key is set to allow for nonce window of 3000
    # Or 3 seconds, since ccxt uses milliseconds as nonce

    api = ccxt.binance(
        {
            "apiKey": get_credential("Binance", "key"),
            "secret": get_credential("Binance", "secret"),
            # "enableRateLimit": True,
            "rateLimit": 50,  # compared to default of 3000; need to be careful
        }
    )

    api.fetch_currencies()
    api.loadMarkets()
    
    logging.debug("Instantiated Binance API via ccxt")
    
    return api
