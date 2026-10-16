import json
import sqlite3

import pytest

from kraken_quant.monitoring import WebhookAlert, backup_database, health_check, restore_database
from kraken_quant.operations import Operations
from kraken_quant.paper_cli import replay
from test_paper_platform import frames


def test_backup_consistency_and_no_overwrite(tmp_path):
    db, backup = tmp_path/"paper.sqlite", tmp_path/"backup.sqlite"
    replay(frames(), db)
    assert backup_database(db, backup)["integrity"] == "ok"
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT count(*) FROM shadow_events").fetchone()[0] == 4
    with pytest.raises(ValueError): backup_database(db, backup)


def test_health_stopped_and_corrupt_ledger_halts(tmp_path):
    db = tmp_path/"paper.sqlite"
    replay(frames(), db)
    assert not health_check(db)["healthy"]
    with sqlite3.connect(db) as connection:
        state = json.loads(connection.execute("SELECT body FROM shadow_state").fetchone()[0])
        state["cash"] = "999"
        connection.execute("UPDATE shadow_state SET body=?", (json.dumps(state),))
    health = health_check(db)
    assert not health["ledger_ok"]
    assert health["halted"]


def test_restore_new_file_halted_until_review(tmp_path):
    db, restored = tmp_path/"paper.sqlite", tmp_path/"restored.sqlite"
    replay(frames(), db)
    result = restore_database(db, restored)
    assert result["halted"]
    ops = Operations(restored)
    try:
        assert ops.inspect()["halted"]
        assert ops.ledger_check()["fills"] == 0
        ops.resume("restoration drill and final backup verified")
        assert not ops.inspect()["halted"]
    finally:
        ops.close()


def test_alert_explicit_optin_sanitized_and_failure():
    payload = dict(healthy=False, halted=True, ledger_ok=True, watchdog_healthy=False, secret="never-send")
    calls = []
    class Response:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def opener(request, timeout):
        calls.append(json.loads(request.data))
        return Response()
    alert = WebhookAlert("https://example.invalid/health", opener=opener)
    assert not alert.send(payload)["sent"]
    assert calls == []
    alert.enabled = True
    assert alert.send(payload)["sent"]
    assert "secret" not in calls[0]
    def failure(*args, **kwargs): raise RuntimeError("secret")
    alert.opener = failure
    with pytest.raises(ConnectionError, match="delivery failed"): alert.send(payload)
    with pytest.raises(ValueError): WebhookAlert("http://example.invalid")
