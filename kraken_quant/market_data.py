"""Credential-free Kraken public REST data, with injected transport and clock."""

import json
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd

from .config import CONFIG
from .data import COLUMNS, DAY, utc_instant, validate_bars
from .risk import InstrumentRules, Quote, money, spot_only


def public_get(endpoint, params):
    if endpoint not in {"OHLC", "Ticker", "AssetPairs"}:
        raise PermissionError("Only public market-data endpoints are allowed")
    url = "https://api.kraken.com/0/public/" + endpoint + "?" + urlencode(params)
    with urlopen(url, timeout=10) as response:
        return json.load(response)


class KrakenMarketData:
    def __init__(self, transport=public_get, clock=lambda: pd.Timestamp.now(tz="UTC")):
        self.transport, self.clock = transport, clock

    def _fetch(self, endpoint, instrument, **params):
        spot_only(instrument)
        started = utc_instant(self.clock())
        body = self.transport(endpoint, {"pair": instrument.symbol, "assetVersion": 1, **params})
        finished = utc_instant(self.clock())
        if finished < started or finished - started > pd.Timedelta(seconds=5):
            raise ValueError("Market-data request latency/clock invalid")
        if not isinstance(body, dict) or body.get("error") != []:
            raise ValueError("Kraken public request failed")
        result = body["result"]
        if instrument.symbol not in result:
            raise ValueError("Kraken response instrument mismatch")
        return result[instrument.symbol], started, finished

    def bars(self, instrument, *, start, end):
        start, end = utc_instant(start), utc_instant(end)
        if start != start.normalize() or end != end.normalize() or start > end:
            raise ValueError("Expected ordered daily UTC boundaries")
        if start <= CONFIG.data_end:
            raise PermissionError("Live history must be strictly after the locked dataset")
        if end + DAY > utc_instant(self.clock()):
            raise ValueError("Requested bar is unfinished")
        rows, _, finished = self._fetch("OHLC", instrument, interval=1440, since=int(start.timestamp()))
        if not rows:
            raise ValueError("No Kraken OHLC data")
        # Kraken explicitly declares its final row uncommitted, even at boundaries.
        committed = rows[:-1]
        if any(len(row) != 8 for row in committed):
            raise ValueError("Malformed OHLC row")
        frame = pd.DataFrame([[r[1], r[2], r[3], r[4], r[6]] for r in committed],
            columns=COLUMNS, index=pd.to_datetime([r[0] for r in committed], unit="s", utc=True))
        frame = frame.loc[(frame.index >= start) & (frame.index <= end)]
        frame = validate_bars(frame, start, end, as_of=finished)
        for name, value in (("venue", instrument.venue), ("symbol", instrument.symbol),
                            ("base", instrument.base), ("quote", instrument.quote), ("data_type", "ohlcv")):
            frame[name] = value
        frame["timestamp"] = frame.index
        return frame

    def quote(self, instrument):
        row, started, _ = self._fetch("Ticker", instrument)
        # REST ticker has no exchange event time: conservative local request-start time.
        return Quote(instrument, started, row["b"][0], row["a"][0])

    def rules(self, instrument):
        row, started, _ = self._fetch("AssetPairs", instrument)
        if row["base"] != instrument.base or row["quote"] != instrument.quote:
            raise ValueError("Metadata base/quote mismatch")
        return InstrumentRules(money(10) ** -int(row["lot_decimals"]),
            money(row["tick_size"]), money(row["ordermin"]), money(row["costmin"]),
            started, instrument, row["status"])
