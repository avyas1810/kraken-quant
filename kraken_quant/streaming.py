"""Bounded Kraken v2 BBO collection; exchange timestamps are never replaced.

Ticker has no documented sequence field. Monotone exchange timestamps detect
regression, not packet loss; freshness and reconnect snapshots fail closed.
"""

import json
import time

import pandas as pd

from .data import utc_instant
from .market_data import KrakenMarketData
from .risk import Quote, spot_only


def connect_public(timeout):
    import websocket
    return websocket.create_connection("wss://ws.kraken.com/v2", timeout=timeout)


class WebSocketMarketData(KrakenMarketData):
    """REST completed bars/rules plus fresh public event-time top of book.

    A fresh subscription per quote avoids an unconsumed socket backlog while
    REST/strategy work runs. Timeouts and reconnects are strictly bounded.
    Heartbeats prove connection activity, never renew a market quote's time.
    """

    event_time_verified = True

    def __init__(self, *, connector=connect_public, clock=lambda: pd.Timestamp.now(tz="UTC"),
                 monotonic=time.monotonic, sleeper=time.sleep, timeout=4, attempts=3,
                 max_messages=1000, transport=None):
        if not 0 < timeout <= 5 or not 1 <= attempts <= 5 or not 1 <= max_messages <= 10000:
            raise ValueError("Bounded connection settings required")
        kwargs = {"clock": clock}
        if transport is not None:
            kwargs["transport"] = transport
        super().__init__(**kwargs)
        self.connector, self.monotonic, self.sleeper = connector, monotonic, sleeper
        self.timeout, self.attempts, self.max_messages = timeout, attempts, max_messages
        self.observation_source = "kraken_public" if connector is connect_public else "injected"
        self.last_events = {}
        self.diagnostics = dict(connections=0, reconnects=0, heartbeats=0, messages=0,
                                stale_events=0, sequence_validation="not_supported_by_ticker")

    def _quote_message(self, body, instrument):
        if not isinstance(body, dict):
            raise ValueError("Malformed websocket message")
        if body.get("method") == "subscribe" and body.get("success") is False:
            raise ValueError("Public ticker subscription rejected")
        if body.get("channel") == "heartbeat":
            self.diagnostics["heartbeats"] += 1
            return None
        if body.get("channel") == "status":
            for item in body.get("data", []):
                if item.get("system") != "online":
                    raise ValueError("Exchange system unavailable")
            return None
        if body.get("channel") != "ticker":
            return None
        if body.get("type") not in {"snapshot", "update"} or len(body.get("data", [])) != 1:
            raise ValueError("Malformed ticker envelope")
        row = body["data"][0]
        if row.get("symbol") != instrument.symbol:
            raise ValueError("Ticker symbol mismatch")
        event = utc_instant(row["timestamp"])
        now = utc_instant(self.clock())
        if event > now:
            raise ValueError("Future exchange quote")
        if event < self.last_events.get(instrument, event):
            raise ValueError("Exchange quote event time regressed")
        quote = Quote(instrument, event, row["bid"], row["ask"])
        if quote.bid >= quote.ask:
            raise ValueError("Locked or crossed top of book")
        if now - event > pd.Timedelta(seconds=5):
            self.diagnostics["stale_events"] += 1
            return None
        self.last_events[instrument] = event
        return quote

    def quote(self, instrument):
        return self.quotes([instrument])[instrument]

    def quotes(self, instruments):
        """Multiplex all selected BBOs so one handshake cannot age another pair."""
        selected = {instrument.symbol: instrument for instrument in instruments}
        if not selected:
            raise ValueError("Select at least one instrument")
        for instrument in selected.values():
            spot_only(instrument)
        for attempt in range(self.attempts):
            connection = None
            try:
                connection = self.connector(self.timeout)
                self.diagnostics["connections"] += 1
                connection.send(json.dumps(dict(method="subscribe", params=dict(
                    channel="ticker", symbol=list(selected), event_trigger="bbo", snapshot=True))))
                deadline = self.monotonic() + self.timeout
                collected = {}
                for _ in range(self.max_messages):
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("No fresh exchange BBO before deadline")
                    connection.settimeout(remaining)
                    raw = connection.recv()
                    if not raw:
                        raise ConnectionError("Public websocket closed")
                    self.diagnostics["messages"] += 1
                    body = json.loads(raw)
                    instrument = next(iter(selected.values()))
                    if isinstance(body, dict) and body.get("channel") == "ticker":
                        rows = body.get("data", [])
                        if len(rows) != 1 or rows[0].get("symbol") not in selected:
                            raise ValueError("Ticker symbol/envelope mismatch")
                        instrument = selected[rows[0]["symbol"]]
                    quote = self._quote_message(body, instrument)
                    if quote is not None:
                        collected[instrument] = quote
                        if len(collected) == len(selected):
                            now = utc_instant(self.clock())
                            if all(pd.Timedelta(0) <= now - item.timestamp <= pd.Timedelta(seconds=5)
                                   for item in collected.values()):
                                return collected
                raise TimeoutError("Public websocket message bound reached")
            except (ValueError, KeyError, TypeError):
                # Data corruption/regression is a safe halt, not a retry.
                raise
            except Exception:
                if attempt + 1 == self.attempts:
                    raise ConnectionError("Bounded public BBO collection failed") from None
                self.diagnostics["reconnects"] += 1
                self.sleeper(min(2 ** attempt, 4))
            finally:
                if connection is not None:
                    connection.close()
        raise ConnectionError("No fresh public BBO")

    def close(self):
        """Connections are closed in quote's finally block."""
