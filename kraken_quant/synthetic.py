"""Offline proof of the existing Alpha's broker-object injection.

TWSQ import has legacy settings/path side effects. Import this module only in
an isolated TWSQROOT with no credentials (the test additionally blocks reads).
No legacy runner, API client, order route or portfolio performance is invoked.
"""

from twsq.alpha import Alpha

from .config import CONFIG
from .features import momentum


class SyntheticBroker:
    is_backtesting = True

    def __init__(self, history, *, decision_time):
        self.alphas = {}
        self.history = history
        self.ts = decision_time
        self.pricing = self

    def get_lastn_bars(self, symbol, num_bars, freq, lag=0):
        from .data import DAY, utc_instant
        if freq != "1d" or num_bars < 1 or lag < 0:
            raise ValueError("Synthetic broker requires positive daily history")
        end = utc_instant(self.ts).normalize() - (lag + 1) * DAY
        start = end - (num_bars - 1) * DAY
        return self.history.window(symbol, split="train", start=start, end=end,
                                   decision_time=self.ts)

    def create_order(self, *args, **kwargs):
        raise RuntimeError("Synthetic broker cannot submit orders")

    def route(self, *args, **kwargs):
        raise RuntimeError("Synthetic broker cannot submit orders")


class SyntheticAlpha(Alpha):
    def prepare(self):
        self.signal = None

    def rebalance(self):
        bars = self.get_lastn_bars(CONFIG.universe[0], 2, "1d")
        self.signal = float(momentum(bars).iloc[-1])
