"""Synthetic-only research, shared-interface and production-boundary contracts."""

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import socket

import numpy as np
import pandas as pd
import pytest

from kraken_quant.boundary import ProductionAdapter, ShadowAdapter
from kraken_quant.config import CONFIG, utc_day
from kraken_quant.data import DAY, HistoricalSnapshot
from kraken_quant.experiment import ParameterRegistry, run_experiment
from kraken_quant.research_execution import ResearchCosts, ResearchPolicy, research_buy_and_hold, run_research
from kraken_quant.risk import AccountSnapshot, InstrumentRules, OrderIntent, Quote, RiskGateway
from kraken_quant.strategy import Instrument, StrategyPipeline, VenueHistory

START = utc_day("2023-12-29")
NOW = pd.Timestamp("2024-01-03T12:00:00Z")
KEY = Instrument("kraken", CONFIG.universe[0], *CONFIG.universe[0].split("/"))
OTHER = Instrument("kraken", CONFIG.universe[1], *CONFIG.universe[1].split("/"))


def snapshot(opens, closes, *, symbol=CONFIG.universe[0], venue="kraken", start=START):
    index = pd.date_range(start, periods=len(opens), tz="UTC")
    frame = pd.DataFrame(dict(open=opens, close=closes, volume=100.,
        high=np.maximum(opens, closes) * 1.05, low=np.minimum(opens, closes) * .95), index=index)
    return HistoricalSnapshot.create(frame, symbol=symbol, source=venue, source_symbol=symbol,
        start=index[0], end=index[-1], as_of=index[-1] + DAY)


class Fixed:
    def __init__(self, weight):
        self.weight = weight
    def target_weights(self, context):
        return {key: self.weight for key in context.historical_data}


class Sequence:
    def __init__(self, weights):
        self.weights = iter(weights)
    def target_weights(self, context):
        return next(self.weights)


def simulate(strategy, *, opens=(100., 100., 100.), closes=(100., 100., 100.), **kwargs):
    source = VenueHistory([snapshot(opens, closes)])
    return run_research(source, StrategyPipeline(strategy), split="train", start=START,
                        end=START + (len(opens) - 1) * DAY, **kwargs)


def account(*, cash="200", positions=None, baseline=None, timestamp=NOW):
    positions = {} if positions is None else positions
    equity = Decimal(cash) + sum((Decimal(q) * 100 for q in positions.values()), Decimal(0))
    return AccountSnapshot(timestamp, NOW.normalize(), cash, positions,
                           equity if baseline is None else baseline)


def quotes(keys=(KEY,), *, timestamp=NOW, bid="100", ask="100"):
    return {key: Quote(key, timestamp, bid, ask) for key in keys}


def rules(key=KEY, **changes):
    return replace(InstrumentRules(Decimal("0.001"), Decimal("0.01"),
        Decimal("0.001"), Decimal("1"), NOW, key), **changes)


def intent(*, id="order-1", decision="decision-1", key=KEY, side="buy", qty="0.1", price="100", **changes):
    return OrderIntent(id, decision, key, side, qty, price, **changes)


def reserve(gateway, request=None, **changes):
    args = dict(account=account(), quotes=quotes(), rules=rules(), now=NOW)
    args.update(changes)
    return gateway.reserve(intent() if request is None else request, **args)


def context():
    source = VenueHistory([snapshot([100.], [100.], start=utc_day("2024-01-02"))])
    frame = source.window(KEY, split="validation", start="2024-01-02", end="2024-01-02", decision_time=NOW)
    return dict(timestamp=NOW, historical_data={KEY: frame}, positions={KEY: 0.}, cash=200., equity=200.)


def factory(parameters, seed):
    return Fixed(parameters["weight"])


def feature_factory(parameters, seed):
    # A custom object factory is deliberately not restricted to numeric arrays.
    def build(data, timestamp):
        return {"nodes": tuple(data), "seed": seed, "timestamp": timestamp}
    return build


def test_short_hand_accounting_spread_slippage_fees_and_flatten():
    result = simulate(Sequence([{KEY: -.5}, {}]), opens=(100., 100., 80.), closes=(100., 90., 80.),
                      costs=ResearchCosts(fee_rate=.01, spread_rate=.02, slippage_rate=.01))
    # Sell 5 @98, fee4.90: cash1485.10; close90 -> equity1035.10.
    # Cover5 @81.60, fee4.08: cash/equity1073.02.
    assert result.cash.to_list() == pytest.approx([1000., 1000., 1485.1, 1073.02])
    assert result.positions[KEY].to_list() == pytest.approx([0., 0., -5., 0.])
    assert result.equity.to_list() == pytest.approx([1000., 1000., 1035.1, 1073.02])
    assert [fill.quantity for fill in result.fills] == [-5., 5.]
    assert [fill.price for fill in result.fills] == pytest.approx([98., 81.6])
    metrics = result.metrics()
    assert metrics["fees"] == pytest.approx(8.98)
    assert metrics["spread_cost"] == pytest.approx(9.)
    assert metrics["slippage_cost"] == pytest.approx(9.)
    assert metrics["trade_count"] == 2
    assert metrics["turnover"] == pytest.approx(898 / ((1000 + 1035.1 + 1073.02) / 3))


def test_long_next_bar_gap_and_flat_hand_accounting():
    result = simulate(Sequence([{KEY: .5}, {}]), opens=(100., 110., 120.), closes=(100., 120., 120.),
                      costs=ResearchCosts(fee_rate=.01, slippage_rate=.01))
    assert result.equity.to_list() == pytest.approx([1000., 1000., 1038.945, 1027.005])
    assert result.fills[0].timestamp == START + DAY
    assert result.fills[0].reference_price == 110
    assert result.fills[0].quantity == 5


def test_signed_rotation_and_research_borrow_financing():
    result = simulate(Sequence([{KEY: -.5}, {KEY: .5}]))
    assert [fill.quantity for fill in result.fills] == pytest.approx([-5., 5., 5.])
    assert result.positions[KEY].iloc[-1] == 5
    financed = simulate(Sequence([{KEY: -.5}, None]), costs=ResearchCosts(short_borrow_rate=.01))
    assert financed.financing.to_list() == pytest.approx([0., 0., 5., 5.])
    assert financed.equity.iloc[-1] == pytest.approx(990.)
    assert financed.metrics()["financing_cost"] == pytest.approx(10.)


def test_explicit_nonproduction_leverage_and_cash_borrowing_only():
    result = simulate(Fixed(1.5), policy=ResearchPolicy(2., True), costs=ResearchCosts(cash_borrow_rate=.01))
    assert result.cash.iloc[2] == pytest.approx(-505.)
    assert result.equity.iloc[2] == pytest.approx(995.)
    with pytest.raises(ValueError, match="gross"):
        simulate(Fixed(1.5))


def test_collateral_budget_prevents_short_sale_pyramiding_and_stops_insolvency():
    result = simulate(Sequence([{KEY: -1.}, None]), costs=ResearchCosts(fee_rate=.01))
    assert abs(result.fills[0].quantity) == pytest.approx(1000 / 101)
    assert abs(result.positions[KEY].iloc[-1]) * 100 <= result.equity.iloc[-1] + 1e-6
    with pytest.raises(ArithmeticError, match="margin"):
        simulate(Sequence([{KEY: -.5}, None]), closes=(100., 400., 100.))


def test_long_short_cross_asset_funded_equity():
    source = VenueHistory([snapshot([100.] * 3, [100., 110., 110.]),
        snapshot([100.] * 3, [100., 90., 90.], symbol=CONFIG.universe[1])])
    result = run_research(source, StrategyPipeline(Sequence([{KEY: .25, OTHER: -.25}, None])),
                         split="train", start=START, end=START + 2 * DAY)
    assert result.cash.iloc[-1] == 1000
    assert result.equity.iloc[-1] == 1050
    assert result.positions.iloc[-1].to_dict() == {KEY: 2.5, OTHER: -2.5}


def test_benchmark_uses_shared_engine_and_costs_without_rebalancing():
    source = VenueHistory([snapshot([100.] * 3, [100., 120., 80.])])
    result = research_buy_and_hold(source, weights={KEY: .5}, split="train", start=START,
        end=START + 2 * DAY, costs=ResearchCosts(fee_rate=.01, slippage_rate=.01))
    assert result.equity.to_list() == pytest.approx([1000., 1000., 1089.95, 889.95])
    assert len(result.fills) == 1


@pytest.mark.parametrize("component", ["signed", "benchmark"])
def test_research_test_period_inaccessible(component):
    source = VenueHistory([])
    args = dict(split="test", start=CONFIG.split("test").start, end=CONFIG.split("test").end)
    function = run_research if component == "signed" else research_buy_and_hold
    positional = (StrategyPipeline(Fixed(.5)),) if component == "signed" else ()
    with pytest.raises(PermissionError):
        function(source, *positional, **args)


def test_arbitrary_graph_and_functor_features_are_causal_and_copied():
    class GraphFunctor:
        def __init__(self, data):
            self.nodes = tuple(data)
            self.edges = {(key, key): float(frame.close.iloc[-1]) for key, frame in data.items()}
        def transform(self, function):
            return {edge: function(value) for edge, value in self.edges.items()}
    observed = []
    def features(data, timestamp):
        return GraphFunctor(data)
    class GraphStrategy:
        def target_weights(self, ctx):
            observed.append(ctx.features.transform(lambda price: price / 200))
            ctx.historical_data[KEY].iloc[-1, 0] = 999
            return {KEY: .5}
    for last_open, last_close in ((100., 120.), (150., 180.)):
        source = VenueHistory([snapshot([100., last_open], [100., last_close])])
        run_research(source, StrategyPipeline(GraphStrategy(), features), split="train", start=START, end=START + DAY)
    assert observed[0] == observed[1] == {(KEY, KEY): .5}
    future = context()
    future["historical_data"][KEY].index += 2 * DAY
    with pytest.raises(PermissionError):
        StrategyPipeline(Fixed(.5)).evaluate(**future)


def test_multi_venue_fields_and_mixed_quotes_rejection():
    source = VenueHistory([snapshot([100.] * 2, [100.] * 2), snapshot([100.] * 2, [100.] * 2, venue="other")])
    assert len(source.instruments) == 2
    frame = source.window(KEY, split="train", start=START, end=START, decision_time=START + DAY)
    assert set(("venue", "symbol", "base", "quote", "timestamp", "data_type")) <= set(frame)
    assert frame.venue.iloc[0] == "kraken"
    assert frame.data_type.iloc[0] == "ohlcv"
    original = snapshot([100.] * 2, [100.] * 2, venue="binance")
    # Create a correctly labeled USDT source with the same locked research symbol.
    bars = original._frame(as_of=START + 2 * DAY)
    usdt = HistoricalSnapshot.create(bars, symbol=CONFIG.universe[0], source="binance",
        source_symbol=CONFIG.universe[0].replace("/USD", "/USDT"), start=START, end=START + DAY, as_of=START + 2 * DAY)
    with pytest.raises(ValueError, match="FX"):
        run_research(VenueHistory([snapshot([100.] * 2, [100.] * 2), usdt]), StrategyPipeline(Fixed(.25)),
                     split="train", start=START, end=START + DAY)


def test_shared_strategy_interface_backtest_shadow_and_enabled_dry_production():
    pipeline = StrategyPipeline(Fixed(.1))
    result = simulate(Fixed(.1))
    assert result.positions[KEY].iloc[-1] == 1
    assert ShadowAdapter(pipeline).evaluate(**context()) == {KEY: .1}
    gateway = RiskGateway()
    try:
        adapter = ProductionAdapter(pipeline, gateway)
        with pytest.raises(PermissionError, match="disabled"):
            adapter.evaluate(**context())
        enabled = ProductionAdapter(pipeline, gateway, enabled=True)
        assert enabled.evaluate(**context()) == {KEY: .1}
        with pytest.raises(NotImplementedError, match="No live transport"):
            enabled.submit()
    finally:
        gateway.close()


@pytest.mark.parametrize("weight", [-.5, 1.1, np.nan])
def test_production_rejects_shorts_leverage_and_nonfinite_weights(weight):
    gateway = RiskGateway()
    try:
        with pytest.raises(ValueError):
            ProductionAdapter(StrategyPipeline(Fixed(weight)), gateway, enabled=True).evaluate(**context())
    finally:
        gateway.close()


def test_shadow_signed_decisions_cannot_submit_and_spot_rehearsal_rejects_shorts():
    adapter = ShadowAdapter(StrategyPipeline(Fixed(-.5)))
    assert adapter.evaluate(**context()) == {KEY: -.5}
    with pytest.raises(PermissionError, match="no submission"):
        adapter.submit(intent())
    with pytest.raises(ValueError):
        ShadowAdapter(StrategyPipeline(Fixed(-.5)), spot_rehearsal=True).evaluate(**context())


@pytest.fixture
def gateway():
    result = RiskGateway()
    yield result
    result.close()


def test_precision_minimums_and_funded_approved_order(gateway):
    approved = reserve(gateway, intent(qty="0.10999", price="100.009"))
    assert approved.quantity == Decimal("0.109")
    assert approved.limit_price == Decimal("100.00")
    with pytest.raises(ValueError):
        reserve(gateway, intent(id="tiny", decision="tiny", qty="0.001"))
    with pytest.raises(ValueError, match="another market"):
        reserve(gateway, intent(id="wrong", decision="wrong"), rules=rules(OTHER))


@pytest.mark.parametrize("change", ["leverage", "borrowing", "derivatives", "venue"])
def test_order_boundary_rejects_nonspot_or_borrowed_requests(gateway, change):
    if change == "leverage":
        request = intent(leverage="2")
    elif change == "borrowing":
        request = intent(borrowing=True)
    else:
        request = intent(key=replace(KEY, market_type="future") if change == "derivatives" else replace(KEY, venue="other"))
    with pytest.raises(ValueError):
        reserve(gateway, request)


def test_production_short_balance_rejection(gateway):
    with pytest.raises(ValueError, match="base balance"):
        reserve(gateway, intent(side="sell"))
    with pytest.raises(ValueError, match="borrowing"):
        reserve(gateway, account=replace(account(), borrowing=True))


def test_order_asset_and_total_caps(gateway):
    with pytest.raises(ValueError, match="20"):
        reserve(gateway, intent(qty="0.201"))
    with pytest.raises(ValueError, match="40"):
        reserve(gateway, account=account(positions={KEY: ".35"}))
    keys = tuple(Instrument("kraken", symbol, *symbol.split("/")) for symbol in CONFIG.universe)
    holdings = {key: ".25" for key in keys[:4]}
    with pytest.raises(ValueError, match="100"):
        reserve(gateway, account=account(positions=holdings), quotes=quotes(keys))


def test_daily_loss_and_pending_cost_budget(gateway):
    with pytest.raises(ValueError, match="daily-loss"):
        reserve(gateway, account=account(cash="198", baseline="200"))
    reserve(gateway, intent(fee_rate="0.05"), account=account(cash="199", baseline="200"))
    with pytest.raises(ValueError, match="daily-loss"):
        reserve(gateway, intent(id="second", decision="second", fee_rate="0.05"),
                account=account(cash="199", baseline="200"))


def test_stale_future_data_and_offline_metadata_rejected(gateway):
    for timestamp in (NOW - pd.Timedelta(seconds=6), NOW + pd.Timedelta(seconds=1)):
        with pytest.raises(ValueError, match="quote"):
            reserve(gateway, quotes=quotes(timestamp=timestamp))
    with pytest.raises(ValueError, match="account"):
        reserve(gateway, account=account(timestamp=NOW - pd.Timedelta(seconds=61)))
    with pytest.raises(ValueError, match="metadata"):
        reserve(gateway, rules=rules(status="offline"))


def test_pending_orders_reserve_cash_positions_and_exposure(gateway):
    reserve(gateway, account=account(cash="15"))
    with pytest.raises(ValueError, match="cash"):
        reserve(gateway, intent(id="second", decision="second"), account=account(cash="15"))
    gateway.release_after_reconciliation("order-1")
    reserve(gateway, intent(id="sell1", decision="sell1", side="sell"), account=account(positions={KEY: ".15"}))
    with pytest.raises(ValueError, match="base balance"):
        reserve(gateway, intent(id="sell2", decision="sell2", side="sell"), account=account(positions={KEY: ".15"}))


def test_pending_buy_limit_premium_counts_toward_exposure(gateway):
    # Premium .95 is below the daily-loss budget; exposure is 20 + 19.95 + 1.
    reserve(gateway, intent(qty="0.19", price="105"))
    with pytest.raises(ValueError, match="40"):
        reserve(gateway, intent(id="next", decision="next", qty="0.01"),
                account=account(positions={KEY: ".2"}))


def test_duplicate_ids_decisions_and_kill_persist_across_restart(tmp_path):
    path = tmp_path / "risk.sqlite"
    first = RiskGateway(path)
    reserve(first)
    first.kill()
    first.close()
    second = RiskGateway(path)
    try:
        with pytest.raises(PermissionError, match="kill"):
            reserve(second, intent(id="new", decision="new"))
        second.clear_kill()
        second.release_after_reconciliation("order-1")
        for request in (intent(), intent(id="new", decision="decision-1"), intent(id="order-1", decision="new")):
            with pytest.raises(ValueError, match="Duplicate"):
                reserve(second, request)
    finally:
        second.close()


def test_concurrent_connections_share_reservations_and_failed_validation_rolls_back(tmp_path):
    path = tmp_path / "risk.sqlite"
    first, second = RiskGateway(path), RiskGateway(path)
    try:
        reserve(first, account=account(cash="15"))
        with pytest.raises(ValueError, match="cash"):
            reserve(second, intent(id="second", decision="second"), account=account(cash="15"))
        approved = reserve(second, intent(id="second", decision="second"))
        assert approved.order_id == "second"
    finally:
        first.close()
        second.close()


def test_experiment_registry_reproducibility_and_paper_table(tmp_path):
    registry = ParameterRegistry(tmp_path / "registry")
    trial = registry.register("synthetic", "fixed", {"weight": .25}, code_revision="fixture", seed=7)
    assert trial == registry.register("synthetic", "fixed", {"weight": .25}, code_revision="fixture", seed=7)
    source = VenueHistory([snapshot([100.] * 6, [100.] * 6)])
    windows = {"train": (START, START + 2 * DAY), "validation": ("2024-01-01", "2024-01-03")}
    first = run_experiment(trial, source, factory, windows=windows)
    second = run_experiment(trial, source, factory, windows=windows)
    assert first.run_id == second.run_id
    assert first.manifest == second.manifest
    pd.testing.assert_frame_equal(first.table, second.table)
    assert first.table.split.to_list() == ["train", "validation"]
    assert {"sharpe", "sortino", "max_drawdown", "turnover", "trade_count", "benchmark_total_return"} <= set(first.table)
    path = first.write(tmp_path / "artifacts")
    manifest = json.loads((path / "manifest.json").read_bytes())
    assert manifest["inputs"]["test_access"] == "locked"
    assert hashlib.sha256((path / "paper_table.csv").read_bytes()).hexdigest() == manifest["paper_table_sha256"]
    with pytest.raises(FileExistsError):
        first.write(tmp_path / "artifacts")
    changed = registry.register("synthetic", "fixed", {"weight": -.25}, code_revision="fixture", seed=7)
    assert run_experiment(changed, source, factory, windows=windows).run_id != first.run_id


def test_experiment_test_lock_and_registry_budget(tmp_path):
    registry = ParameterRegistry(tmp_path / "registry", max_trials_per_hypothesis=1)
    trial = registry.register("h", "fixed", {"weight": .25}, code_revision="fixture")
    with pytest.raises(ValueError, match="budget"):
        registry.register("h", "fixed", {"weight": .5}, code_revision="fixture")
    def never(*args):
        raise AssertionError("Test access must reject before strategy construction")
    with pytest.raises(PermissionError, match="test"):
        run_experiment(trial, VenueHistory([]), never, windows={"test": ("2025-01-01", "2025-01-02")})


def test_experiment_custom_feature_identity_and_mutation_rejection(tmp_path):
    registry = ParameterRegistry(tmp_path / "registry")
    registered = registry.register("feature", "fixed", {"weight": .25}, code_revision="fixture", feature_id="custom-graph")
    missing = registry.register("missing", "fixed", {"weight": .25}, code_revision="fixture")
    source = VenueHistory([snapshot([100.] * 6, [100.] * 6)])
    windows = {"train": (START, START + 2 * DAY), "validation": ("2024-01-01", "2024-01-03")}
    result = run_experiment(registered, source, factory, windows=windows, feature_factory=feature_factory)
    assert json.loads(result.manifest)["inputs"]["feature_factory_sha256"]
    with pytest.raises(ValueError, match="identity"):
        run_experiment(missing, source, factory, windows=windows, feature_factory=feature_factory)
    result.table.loc[0, "total_return"] = 99
    with pytest.raises(ValueError, match="changed"):
        result.write(tmp_path / "artifacts")


def test_milestone_has_no_network_or_submission_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network connection attempted")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    result = simulate(Fixed(-.25))
    assert result.positions[KEY].iloc[-1] == -2.5
    with pytest.raises(PermissionError):
        ShadowAdapter(StrategyPipeline(Fixed(.25))).submit()
