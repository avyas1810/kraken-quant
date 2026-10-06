"""Hand-calculated synthetic fixtures; no real strategy/data performance runs."""

import math
import socket

import numpy as np
import pandas as pd
import pytest

from kraken_quant.backtest import ExecutionCosts, Fill, buy_and_hold, run_backtest
from kraken_quant.config import CONFIG, utc_day
from kraken_quant.data import DAY, HistoricalSnapshot, RestrictedHistory
from kraken_quant.metrics import performance_metrics

SYMBOL, OTHER = CONFIG.universe[:2]
START = "2023-12-27"


def history(prices, start=START):
    snapshots = []
    for symbol, (opens, closes) in prices.items():
        index = pd.date_range(start, periods=len(opens), freq="D", tz="UTC")
        frame = pd.DataFrame({
            "open": opens, "close": closes,
            "high": np.maximum(opens, closes) * 1.05,
            "low": np.minimum(opens, closes) * .95, "volume": 100.,
        }, index=index)
        snapshots.append(HistoricalSnapshot.create(frame, symbol=symbol, source="synthetic",
            source_symbol=symbol.replace("/USD", "/USDT"), start=index[0], end=index[-1],
            as_of=index[-1] + DAY))
    return RestrictedHistory(snapshots)


def run(prices, strategy, **kwargs):
    count = len(next(iter(prices.values()))[0])
    options = dict(symbols=tuple(prices), split="train", start=START,
                   end=utc_day(START) + (count - 1) * DAY, initial_cash=1000.)
    options.update(kwargs)
    return run_backtest(history(prices), strategy, **options)


def test_hand_accounting_next_open_fees_slippage_and_equity():
    targets = iter([{SYMBOL: .5}, {}])
    result = run({SYMBOL: ([100., 110., 120.], [100., 120., 120.])},
                 lambda context: next(targets), costs=ExecutionCosts(.01, .01))
    # 5 bought at 111.10: 555.50 + 5.555 fee. Sell at 118.80: 594 - 5.94.
    assert result.cash.to_list() == pytest.approx([1000., 1000., 438.945, 1027.005])
    assert result.positions[SYMBOL].to_list() == pytest.approx([0., 0., 5., 0.])
    assert result.equity.to_list() == pytest.approx([1000., 1000., 1038.945, 1027.005])
    assert [fill.price for fill in result.fills] == pytest.approx([111.1, 118.8])
    assert [fill.quantity for fill in result.fills] == pytest.approx([5., -5.])
    assert result.fills[0].execution_time == utc_day(START) + DAY
    assert result.fills[0].decision_time == utc_day(START) + DAY
    metrics = result.metrics()
    assert metrics["trade_count"] == 2
    assert metrics["fees"] == pytest.approx(11.495)
    assert metrics["slippage_cost"] == pytest.approx(11.5)
    assert metrics["total_return"] == pytest.approx(.027005)
    assert metrics["turnover"] == pytest.approx(1149.5 / ((1000 + 1038.945 + 1027.005) / 3))


def test_gapped_full_weight_buy_is_scaled_to_available_cash():
    result = run({SYMBOL: ([100., 200.], [100., 200.])}, lambda context: {SYMBOL: 1.},
                 costs=ExecutionCosts(.1, .1))
    # Desired 10 units, but each costs 200 * 1.1 * 1.1 = 242 including fees.
    assert result.fills[0].quantity == pytest.approx(1000 / 242)
    assert result.cash.iloc[-1] == pytest.approx(0, abs=1e-10)
    assert result.equity.iloc[-1] == pytest.approx(200000 / 242)
    assert (result.cash >= 0).all()


def test_multiasset_buys_share_cash_proportionally_and_keep_cash_remainder():
    prices = {SYMBOL: ([10., 10.], [10., 10.]), OTHER: ([20., 20.], [20., 20.])}
    full = run(prices, lambda context: {SYMBOL: .5, OTHER: .5}, costs=ExecutionCosts(.1, 0))
    assert full.positions.iloc[-1].to_list() == pytest.approx([500 / 11, 500 / 22])
    assert full.cash.iloc[-1] == pytest.approx(0, abs=1e-10)
    partial = run(prices, lambda context: {SYMBOL: .2, OTHER: .3})
    assert partial.positions.iloc[-1].to_list() == pytest.approx([20., 15.])
    assert partial.cash.iloc[-1] == pytest.approx(500.)


def test_rotation_sells_before_buys_and_sizes_before_future_gap():
    prices = {OTHER: ([20., 20., 10.], [20., 20., 10.]),
              SYMBOL: ([10., 10., 20.], [10., 10., 20.])}
    targets = iter([{SYMBOL: 1.}, {OTHER: 1.}])
    result = run(prices, lambda context: next(targets))
    assert [(fill.symbol, fill.quantity) for fill in result.fills] == [
        (SYMBOL, 100.), (SYMBOL, -100.), (OTHER, 50.)]
    assert result.cash.iloc[-1] == pytest.approx(1500.)
    assert result.positions.iloc[-1].to_list() == pytest.approx([50., 0.])
    assert result.equity.iloc[-1] == pytest.approx(2000.)


def test_buy_and_hold_is_one_purchase_with_identical_costs():
    prices = {SYMBOL: ([100., 100., 100.], [100., 120., 80.])}
    result = buy_and_hold(history(prices), symbols=(SYMBOL,), split="train", start=START,
        end="2023-12-29", weights={SYMBOL: .5}, costs=ExecutionCosts(.01, .01))
    assert len(result.fills) == 1
    assert result.positions[SYMBOL].to_list() == pytest.approx([0., 0., 5., 5.])
    assert result.cash.iloc[-1] == pytest.approx(489.95)  # 1000 - 505 - 5.05
    assert result.equity.to_list() == pytest.approx([1000., 1000., 1089.95, 889.95])
    strategy = run(prices, lambda context: {SYMBOL: .5} if context.time.day == 28 else None,
                   costs=ExecutionCosts(.01, .01))
    pd.testing.assert_series_equal(strategy.equity, result.equity)
    assert strategy.fills == result.fills


def test_equal_weight_benchmark_default_and_no_rebalancing():
    prices = {SYMBOL: ([10., 10., 10.], [10., 10., 20.]),
              OTHER: ([20., 20., 20.], [20., 20., 10.])}
    result = buy_and_hold(history(prices), symbols=tuple(prices), split="train", start=START,
                         end="2023-12-29")
    assert [fill.quantity for fill in result.fills] == pytest.approx([50., 25.])
    assert result.equity.iloc[-1] == pytest.approx(1250.)
    assert len(result.fills) == 2


def test_metrics_hand_calculated_ratios_drawdown_turnover_and_counts():
    equity = pd.Series([1000., 1100., 990., 1089.],
                       index=pd.date_range(START, periods=4, tz="UTC"))
    fill = Fill(utc_day(START), utc_day(START) + DAY, SYMBOL, 5., 100., 100., 5., 0.)
    result = performance_metrics(equity, [fill], periods_per_year=3)
    # Returns .1, -.1, .1: sample variance 1/75, downside variance 1/300.
    assert result["sharpe"] == pytest.approx(.5)
    assert result["sortino"] == pytest.approx(1.)
    assert result["annualized_volatility"] == pytest.approx(.2)
    assert result["max_drawdown"] == pytest.approx(.1)
    assert result["total_return"] == pytest.approx(.089)
    assert result["turnover"] == pytest.approx(500 / ((1100 + 990 + 1089) / 3))
    assert result["trade_count"] == 1


def test_nonzero_risk_free_and_target_metric_conventions():
    equity = pd.Series([1000., 1100., 990., 1089.],
                       index=pd.date_range(START, periods=4, tz="UTC"))
    result = performance_metrics(equity, periods_per_year=3, daily_risk_free=.01, daily_target=.02)
    assert result["sharpe"] == pytest.approx(.35)
    assert result["sortino"] == pytest.approx(1 / 3)


def test_no_trade_and_single_day_undefined_ratios():
    for closes in ([100.], [100., 100.]):
        result = run({SYMBOL: ([100.] * len(closes), closes)}, lambda context: None)
        metrics = result.metrics()
        assert metrics["total_return"] == 0
        assert metrics["max_drawdown"] == 0
        assert metrics["trade_count"] == 0
        assert metrics["turnover"] == 0
        assert math.isnan(metrics["sharpe"])
        assert math.isnan(metrics["sortino"])


def test_initial_mark_captures_entry_cost_drawdown():
    result = run({SYMBOL: ([100., 100.], [100., 100.])}, lambda context: {SYMBOL: 1.},
                 costs=ExecutionCosts(.1, 0))
    assert result.metrics()["max_drawdown"] == pytest.approx(1 / 11)


def test_future_changes_cannot_alter_decisions_or_initial_fill_quantity():
    observations = []
    def strategy(context):
        assert context.bars[SYMBOL].index.max() + DAY == context.time
        observations.append(context.bars[SYMBOL].copy())
        return {SYMBOL: .5}
    left = run({SYMBOL: ([100., 100.], [100., 120.])}, strategy)
    right = run({SYMBOL: ([100., 200.], [100., 220.])}, strategy)
    pd.testing.assert_frame_equal(*observations)
    assert left.fills[0].quantity == right.fills[0].quantity == 5
    assert left.fills[0].price == 100
    assert right.fills[0].price == 200


def test_context_mutation_cannot_change_engine_prices_and_positions():
    def strategy(context):
        with pytest.raises(TypeError):
            context.positions[SYMBOL] = 999
        context.bars[SYMBOL].iloc[-1, :] = 999
        return {SYMBOL: .5}
    result = run({SYMBOL: ([100., 110.], [100., 120.])}, strategy)
    assert result.fills[0].quantity == 5
    assert result.fills[0].price == 110
    assert result.equity.iloc[-1] == 1050


def test_warmup_visible_but_not_scored_or_traded():
    source = history({SYMBOL: ([100.] * 4, [100.] * 4)}, start="2023-12-30")
    contexts = []
    result = run_backtest(source, lambda context: contexts.append(context) or {SYMBOL: .5},
        symbols=(SYMBOL,), split="validation", start="2024-01-01", end="2024-01-02", warmup_days=2)
    assert contexts[0].bars[SYMBOL].index[0] == utc_day("2023-12-30")
    assert contexts[0].bars[SYMBOL].index[-1] == utc_day("2024-01-01")
    assert result.equity.index[0] == utc_day("2024-01-01")
    assert len(result.equity) == 3
    assert len(result.fills) == 1
    assert result.fills[0].execution_time == utc_day("2024-01-02")


@pytest.mark.parametrize("benchmark", [False, True])
def test_test_period_access_remains_locked(benchmark):
    def must_not_run(context):
        raise AssertionError("Test strategy must never be invoked")
    function = buy_and_hold if benchmark else run_backtest
    args = () if benchmark else (must_not_run,)
    with pytest.raises(PermissionError, match="locked"):
        function(RestrictedHistory([]), *args, symbols=(SYMBOL,), split="test",
                 start=CONFIG.split("test").start, end=CONFIG.split("test").start)


def test_split_crossing_rejected_without_reading_history():
    with pytest.raises(PermissionError, match="boundaries"):
        run_backtest(RestrictedHistory([]), lambda context: None, symbols=(SYMBOL,),
            split="validation", start="2024-12-31", end="2025-01-01")


@pytest.mark.parametrize("target", [
    {SYMBOL: -1}, {SYMBOL: np.nan}, {SYMBOL: np.inf}, {SYMBOL: 1.1},
    {SYMBOL: .8, OTHER: .3}, {"unknown": .5}, {SYMBOL: True}, [.5],
])
def test_invalid_weights_rejected(target):
    with pytest.raises(ValueError):
        run({SYMBOL: ([100., 100.], [100., 100.]), OTHER: ([100., 100.], [100., 100.])},
            lambda context: target)


@pytest.mark.parametrize("cash", [0, -1, np.nan, np.inf, True])
def test_invalid_initial_funding_rejected(cash):
    with pytest.raises(ValueError):
        run({SYMBOL: ([100.], [100.])}, lambda context: None, initial_cash=cash)


@pytest.mark.parametrize("fee,slip", [(-.1, 0), (1, 0), (np.nan, 0), (0, np.inf), (0, 1), (True, 0)])
def test_invalid_cost_fractions_rejected(fee, slip):
    with pytest.raises(ValueError):
        ExecutionCosts(fee, slip)


@pytest.mark.parametrize("values", [[1000., 0], [1000., np.nan], [1000., np.inf]])
def test_invalid_equity_metrics_rejected(values):
    with pytest.raises(ValueError):
        performance_metrics(pd.Series(values, index=pd.date_range(START, periods=2, tz="UTC")))


def test_no_network_or_credential_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline backtest attempted network or credentials")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    import builtins
    monkeypatch.setattr(builtins, "open", forbidden)
    # Snapshot and engine are fully in memory; no TWSQ import/client factory.
    result = run({SYMBOL: ([100., 100.], [100., 110.])}, lambda context: {SYMBOL: .5})
    assert result.equity.iloc[-1] == 1050
