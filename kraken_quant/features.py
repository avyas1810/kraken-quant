"""One causal feature for interface verification, not strategy evaluation."""


def momentum(bars, lookback=1):
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
        raise ValueError("Lookback must be a positive integer")
    return bars["close"] / bars["close"].shift(lookback) - 1
