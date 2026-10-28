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
