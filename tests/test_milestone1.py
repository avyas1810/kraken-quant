from decimal import Decimal
import subprocess

import pytest

from kraken_quant import milestone1
from kraken_quant.trade_intent import TradeIntent
from kraken_quant.strategy import Instrument


KEY = Instrument("kraken", "BTC/USD", "BTC", "USD")


def test_intent_reuses_risk_contract():
    intent = TradeIntent("decision-1", KEY, "2026-10-29T00:00:00Z", "buy", "0.01")
    order = intent.priced(order_id="order-1", limit_price="100")
    assert order.instrument == KEY and order.quantity == Decimal("0.01")
    assert order.decision_key == intent.decision_key
    assert not hasattr(intent, "submit")


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity"])
def test_invalid_quantity(quantity):
    with pytest.raises(ValueError):
        TradeIntent("decision", KEY, "2026-10-29T00:00:00Z", "buy", quantity)


def test_audit_detects_tracked_secret(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text("placeholder")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".env"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixture"], check=True, capture_output=True)
    result = milestone1.repository_audit(tmp_path)
    assert result["env_tracked"] and not all(result["interfaces"].values())


def test_connectivity_sanitizes_failures(monkeypatch, capsys):
    def failure(*a, **kw):
        raise ValueError("SECRET_SENTINEL")
    monkeypatch.setattr(milestone1, "readonly_from_env", failure)
    with pytest.raises(RuntimeError, match="Read-only connectivity failed") as error:
        milestone1.check_connectivity(".env")
    assert "SECRET_SENTINEL" not in str(error.value)
    assert milestone1.main(["connectivity"]) == 1
    assert "SECRET_SENTINEL" not in capsys.readouterr().out


def test_connectivity_only_reads(monkeypatch):
    calls = []
    class Adapter:
        def permissions(self): calls.append("permissions")
        def _read(self, endpoint, params):
            calls.append(endpoint)
            return {"PRIVATE_BALANCE": "123"}
    monkeypatch.setattr(milestone1, "readonly_from_env", lambda *a, **kw: Adapter())
    result = milestone1.check_connectivity(".env")
    assert calls == ["permissions", "Balance"]
    assert result == {"authenticated": True, "read_only": True, "balance_read": True}


from dataclasses import FrozenInstanceError
import pandas as pd
from kraken_quant.credentials import readonly_from_env
from kraken_quant.trade_intent import NormalExit, PortfolioTarget, PortfolioTargetAdapter, target_weights
from kraken_quant.target_execution import size_target
from kraken_quant.strategy import StrategyContext


NOW = pd.Timestamp("2026-10-29T12:00:00Z")


def target(**kwargs):
    values = dict(decision_key="decision-1", instrument=KEY, target_weight="0.25",
                  decision_timestamp=NOW, valid_until=NOW + pd.Timedelta(hours=2))
    values.update(kwargs)
    return PortfolioTarget(**values)


@pytest.mark.parametrize("weight", [True, "NaN", "Infinity", "-Infinity", "1.01", "-1.01"])
def test_target_rejects_invalid_weights(weight):
    with pytest.raises(ValueError):
        target(target_weight=weight)


def test_target_immutable_versioned_signed_and_flat():
    item = target(target_weight="-0.5")
    assert item.version == 1 and item.target_weight == Decimal("-0.5")
    with pytest.raises(FrozenInstanceError):
        item.target_weight = 0
    with pytest.raises(ValueError, match="version"):
        target(version=2)
    with pytest.raises(ValueError, match="version"):
        target(version=True)
    assert target(target_weight=0).weight_at(now=NOW, supported_instruments=[KEY]) == 0


@pytest.mark.parametrize("now", [NOW - pd.Timedelta(seconds=1), NOW + pd.Timedelta(hours=2), NOW + pd.Timedelta(days=1)])
def test_stale_or_future_target_cannot_size(now):
    with pytest.raises(ValueError, match="stale or not yet valid"):
        size_target(target(), now=now, supported_instruments=[KEY], equity=1000,
                    current_quantity=0, market_price=100, order_id="order", limit_price=100)


def test_unsupported_instrument_cannot_size():
    with pytest.raises(ValueError, match="Unsupported instrument"):
        size_target(target(), now=NOW, supported_instruments=[], equity=1000,
                    current_quantity=0, market_price=100, order_id="order", limit_price=100)


@pytest.mark.parametrize("exit_kwargs", [dict(policy="unknown"), dict(policy="at_time"),
    dict(policy="next_target", exit_at=NOW), dict(policy="at_time", exit_at="2026-10-29")])
def test_malformed_exit(exit_kwargs):
    with pytest.raises(ValueError):
        NormalExit(**exit_kwargs)


@pytest.mark.parametrize("exit_at", [NOW, NOW + pd.Timedelta(hours=2)])
def test_exit_must_be_inside_validity(exit_at):
    with pytest.raises(ValueError, match="inside instruction validity"):
        target(normal_exit=NormalExit("at_time", exit_at))


def test_untyped_exit_rejected():
    with pytest.raises(TypeError, match="NormalExit"):
        target(normal_exit={"policy": "at_time"})


@pytest.mark.parametrize("field,value", [("decision_timestamp", "2026-10-29"),
                                         ("valid_until", NOW), ("valid_until", "NaT")])
def test_invalid_target_timestamps(field, value):
    with pytest.raises(ValueError):
        target(**{field: value})


def test_execution_sizes_from_current_equity_prices_and_holdings():
    def size(**overrides):
        kwargs = dict(now=NOW, supported_instruments=[KEY], equity="1000",
                      current_quantity="1", market_price="100", order_id="order", limit_price="101")
        kwargs.update(overrides)
        return size_target(target(), **kwargs)
    assert size().quantity == Decimal("1.5")
    assert size(equity="2000").quantity == Decimal("4")
    assert size(market_price="200").quantity == Decimal("0.25")
    assert size(current_quantity="2.5") is None
    assert size(current_quantity="3").side == "sell"
    with pytest.raises(ValueError):
        size(market_price=0)


def test_normal_exit_flattens_in_execution():
    item = target(normal_exit=NormalExit("at_time", NOW + pd.Timedelta(hours=1)))
    order = size_target(item, now=NOW + pd.Timedelta(hours=1), supported_instruments=[KEY],
                        equity=1000, current_quantity=2, market_price=100, order_id="exit", limit_price=100)
    assert order.side == "sell" and order.quantity == 2
    assert order.decision_key != item.decision_key
    assert order.decision_key.startswith(item.decision_key + ":exit:")
    assert item.weight_at(now=NOW, supported_instruments=[KEY]) == Decimal("0.25")


def test_strategy_weight_compatibility_and_risk_policy():
    class Strategy:
        def portfolio_targets(self, context): return (target(),)
    context = StrategyContext(NOW, {KEY: None}, None, {}, 1000, 1000)
    assert PortfolioTargetAdapter(Strategy()).target_weights(context) == {KEY: 0.25}
    with pytest.raises(ValueError, match="Duplicate"):
        target_weights([target(), target()], now=NOW, supported_instruments=[KEY])
    with pytest.raises(ValueError, match="long-only"):
        target_weights([target(target_weight="-0.5")], now=NOW, supported_instruments=[KEY], long_only=True)
    other = Instrument("other", "BTC/USD", "BTC", "USD")
    with pytest.raises(ValueError, match="gross"):
        target_weights([target(target_weight="0.75"), target(instrument=other, target_weight="0.75")],
                       now=NOW, supported_instruments=[KEY, other])


def test_real_loader_connectivity_without_markets_or_research_universe(tmp_path, monkeypatch):
    import ccxt
    from kraken_quant.config import CONFIG
    path = tmp_path / ".env"
    path.write_text("KRAKEN_READONLY_API_KEY=fixture-key\nKRAKEN_READONLY_API_SECRET=fixture-secret\n")
    calls = []
    class Client:
        def request(self, endpoint, api, method, params):
            calls.append(endpoint)
            assert api == "private" and method == "POST"
            if endpoint == "GetApiKeyInfo":
                return {"error": [], "result": {"permissions": ["query-funds", "query-open-trades", "query-closed-trades"]}}
            assert endpoint == "Balance"
            return {"error": [], "result": {"PRIVATE": "123"}}
        def load_markets(self): pytest.fail("Authentication must not load markets")
        def market(self, symbol): pytest.fail("Authentication must not resolve instruments")
    monkeypatch.setattr(ccxt, "kraken", lambda *args: Client())
    monkeypatch.setattr(type(CONFIG), "universe", property(lambda self: pytest.fail("Research universe accessed")), raising=False)
    result = milestone1.check_connectivity(path)
    assert result["authenticated"]
    assert calls == ["GetApiKeyInfo", "GetApiKeyInfo", "Balance"]


def test_ci_cache_uses_installed_dependency_files():
    from pathlib import Path
    text = Path(".github/workflows/ci.yml").read_text()
    cache = text.split("cache-dependency-path: |", 1)[1].split("      - run:", 1)[0]
    assert {line.strip() for line in cache.splitlines() if line.strip()} == {
        "requirements-ci.txt", "requirements-research.txt", "requirements-notebook.txt"}



def test_lazy_fee_pair_resolution_preserves_existing_interface(tmp_path, monkeypatch):
    import ccxt
    calls = []
    path = tmp_path / ".env"
    path.write_text("KRAKEN_READONLY_API_KEY=fixture-key\nKRAKEN_READONLY_API_SECRET=fixture-secret\n")
    class Client:
        def request(self, endpoint, api, method, params):
            calls.append(endpoint)
            if endpoint == "GetApiKeyInfo":
                return {"error": [], "result": {"permissions": ["query-funds", "query-open-trades", "query-closed-trades"]}}
            assert endpoint == "TradeVolume" and params == {"pair": "XXBTZUSD"}
            return {"error": [], "result": {"fees": {"XXBTZUSD": {"fee": "0.4"}}}}
        def load_markets(self): calls.append("load_markets")
        def market(self, symbol):
            assert symbol == KEY.symbol
            return dict(id="XXBTZUSD", symbol="BTC/USD", base="BTC", quote="USD", spot=True)
    monkeypatch.setattr(ccxt, "kraken", lambda *args: Client())
    adapter = readonly_from_env(path, clock=lambda: NOW, nonce=lambda: 1)
    assert "load_markets" not in calls
    assert adapter.fee(KEY).taker_fraction == Decimal("0.004")
    assert adapter.fee(KEY).taker_fraction == Decimal("0.004")
    assert calls.count("load_markets") == 1


def test_adapter_preserves_hold_quantities():
    class Strategy:
        def portfolio_targets(self, context): return None
    context = StrategyContext(NOW, {KEY: None}, None, {}, 1000, 1000)
    assert PortfolioTargetAdapter(Strategy()).target_weights(context) is None



def test_signed_target_sizes_short_only_in_execution():
    order = size_target(target(target_weight="-0.25"), now=NOW, supported_instruments=[KEY],
                        equity=1000, current_quantity=1, market_price=100, order_id="short", limit_price=100)
    assert order.side == "sell" and order.quantity == Decimal("3.5")


def test_normal_exit_immutable():
    exit_instruction = NormalExit()
    with pytest.raises(FrozenInstanceError):
        exit_instruction.policy = "at_time"
