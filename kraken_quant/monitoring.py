"""Read-only health, opt-in HTTPS alerts, and consistent SQLite backups."""

import json
from pathlib import Path
import sqlite3
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .operations import Operations


class WebhookAlert:
    """Caller explicitly supplies an HTTPS destination; no automatic discovery.

    Payloads contain only enumerated health fields, never account/exception data.
    """

    def __init__(self, url, *, enabled=False, opener=urlopen):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("HTTPS alert destination without embedded credentials required")
        self.url, self.enabled, self.opener = url, bool(enabled), opener

    def send(self, health):
        payload = {key: health[key] for key in ("healthy", "halted", "ledger_ok", "watchdog_healthy")}
        if not self.enabled:
            return {"sent": False, "reason": "disabled"}
        request = Request(self.url, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener(request, timeout=5) as response:
                if not 200 <= response.status < 300:
                    raise ConnectionError("Alert endpoint rejected payload")
        except Exception:
            raise ConnectionError("External alert delivery failed") from None
        return {"sent": True}


def health_check(database, *, clock=None, alert=None):
    """Watchdog deliberately persists a halt on expiry; ledger faults also halt."""
    kwargs = {} if clock is None else {"clock": clock}
    ops = Operations(database, **kwargs)
    try:
        integrity = ops.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        ledger_ok = False
        try:
            ops.ledger_check()
            ledger_ok = integrity
        except Exception:
            ops.halt("health_ledger_failure")
        watchdog = ops.watchdog()
        state = ops.inspect()
        result = dict(healthy=ledger_ok and watchdog["healthy"] and not state["halted"],
                      halted=state["halted"], ledger_ok=ledger_ok,
                      watchdog_healthy=watchdog["healthy"], watchdog_armed=watchdog["armed"])
        if alert is not None and not result["healthy"]:
            result["alert"] = alert.send(result)
        return result
    finally:
        ops.close()


def backup_database(database, destination):
    """SQLite online backup API, not an unsafe copy of an open database.

    Backups are runtime/private artifacts and must remain outside version control.
    Never overwrite a previous recovery point.
    """
    source, target = Path(database).resolve(), Path(destination).resolve()
    if not source.is_file() or target.exists() or source == target:
        raise ValueError("Existing source and new destination required")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as read:
        with sqlite3.connect(target) as write:
            read.backup(write)
            if write.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup failed integrity check")
    return {"backup": str(target), "integrity": "ok"}


def restore_database(backup, destination):
    """Restore to a NEW file, verify ledger, and require reviewed manual resume.

    Existing databases are never overwritten. Stop the former writer first;
    use its final backup, not an older portfolio checkpoint.
    """
    result = backup_database(backup, destination)
    ops = Operations(destination)
    try:
        ops.halt("restored_database_requires_review")
        ops.set_value("watchdog", {"armed": False})
        ledger = ops.ledger_check()
        ops.event("restore", ledger=ledger)
        return dict(restored=result["backup"], integrity="ok", halted=True, ledger=ledger)
    finally:
        ops.close()
