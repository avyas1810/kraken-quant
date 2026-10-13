"""Event-time spread quotes for Kraken paper execution; public endpoints only."""

import json
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd

from .data import utc_instant
from .market_data import KrakenMarketData, public_get
from .risk import Quote


def operational_public_get(endpoint, params):
    if endpoint != "Spread":
        return public_get(endpoint, params)
    url = "https://api.kraken.com/0/public/Spread?" + urlencode(params)
    with urlopen(url, timeout=10) as response:
        return json.load(response)


class EventTimeMarketData(KrakenMarketData):
    def __init__(self, transport=operational_public_get, clock=lambda: pd.Timestamp.now(tz="UTC")):
        super().__init__(transport, clock)
        self.observation_source = "kraken_public" if transport is operational_public_get else "injected"
        self.event_time_verified = True
        self._last = {}

    def quote(self, instrument):
        rows, _, finished = self._fetch("Spread", instrument)
        if not rows or any(len(row) != 3 for row in rows):
            raise ValueError("Missing/malformed event-time spread")
        stamps = [pd.Timestamp(row[0], unit="s", tz="UTC") for row in rows]
        if stamps != sorted(stamps):
            raise ValueError("Out-of-order spread events")
        event = utc_instant(stamps[-1])
        if not pd.Timedelta(0) <= finished - event <= pd.Timedelta(seconds=5):
            raise ValueError("Stale/future exchange quote event")
        if instrument in self._last and event < self._last[instrument]:
            raise ValueError("Quote event time regressed")
        self._last[instrument] = event
        return Quote(instrument, event, rows[-1][1], rows[-1][2])
