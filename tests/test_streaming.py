import json

import pandas as pd
import pytest

from kraken_quant.streaming import WebSocketMarketData
from kraken_quant.strategy import Instrument

KEY = Instrument("kraken", "BTC/USD", "BTC", "USD")
NOW = pd.Timestamp("2026-10-16T01:00:00Z")


def message(timestamp=NOW, bid=100, ask=101, symbol="BTC/USD"):
    return dict(channel="ticker", type="update", data=[dict(symbol=symbol, timestamp=timestamp.isoformat(), bid=bid, ask=ask)])


class Socket:
    def __init__(self, rows):
        self.rows, self.sent, self.closed = iter(rows), [], False
    def send(self, body): self.sent.append(json.loads(body))
    def settimeout(self, seconds): assert seconds > 0
    def recv(self):
        try: row = next(self.rows)
        except StopIteration: raise TimeoutError()
        return json.dumps(row)
    def close(self): self.closed = True


def source(rows):
    socket = Socket(rows)
    return WebSocketMarketData(connector=lambda timeout: socket, clock=lambda: NOW,
                               sleeper=lambda _: None, attempts=1), socket


def test_event_time_bbo_heartbeat_and_cleanup():
    feed, socket = source([dict(channel="heartbeat"), message()])
    quote = feed.quote(KEY)
    assert quote.timestamp == NOW
    assert socket.closed
    assert socket.sent[0]["params"]["event_trigger"] == "bbo"
    assert feed.observation_source == "injected"
    assert feed.diagnostics["heartbeats"] == 1


@pytest.mark.parametrize("row", [message(bid=102), message(bid=101), message(symbol="ETH/USD"),
                                   message(timestamp=NOW+pd.Timedelta(seconds=1)),
                                   dict(method="subscribe", success=False)])
def test_invalid_data_halts_without_retry(row):
    feed, socket = source([row])
    with pytest.raises(ValueError): feed.quote(KEY)
    assert socket.closed
    assert feed.diagnostics["connections"] == 1


def test_heartbeat_cannot_refresh_stale_event():
    feed, socket = source([message(NOW-pd.Timedelta(seconds=6)), dict(channel="heartbeat")])
    with pytest.raises(ConnectionError): feed.quote(KEY)
    assert feed.diagnostics["stale_events"] == 1
    assert socket.closed


def test_regression_halts():
    feed, _ = source([message()])
    feed.quote(KEY)
    with pytest.raises(ValueError, match="regressed"):
        feed._quote_message(message(NOW-pd.Timedelta(seconds=1)), KEY)


def test_bounded_reconnect_lost_connection():
    sockets = [Socket([]), Socket([message()])]
    sleep = []
    feed = WebSocketMarketData(connector=lambda timeout: sockets.pop(0), clock=lambda: NOW,
                               sleeper=sleep.append)
    assert feed.quote(KEY).timestamp == NOW
    assert feed.diagnostics["reconnects"] == 1
    assert sleep == [1]


def test_bounded_failure_no_infinite_retry():
    calls = []
    def fail(timeout):
        calls.append(timeout)
        raise TimeoutError()
    feed = WebSocketMarketData(connector=fail, sleeper=lambda _: None)
    with pytest.raises(ConnectionError): feed.quote(KEY)
    assert len(calls) == 3


def test_multiplex_five_assets_one_connection():
    from kraken_quant.config import CONFIG
    keys = [Instrument("kraken", symbol, *symbol.split("/")) for symbol in CONFIG.universe]
    feed, socket = source([message(symbol=key.symbol) for key in keys])
    quotes = feed.quotes(keys)
    assert set(quotes) == set(keys)
    assert feed.diagnostics["connections"] == 1
    assert socket.sent[0]["params"]["symbol"] == [key.symbol for key in keys]
    assert socket.closed
