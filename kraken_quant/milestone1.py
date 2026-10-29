"""Repository metadata audit and explicit authenticated read-only connectivity."""
import argparse
import json
from pathlib import Path
import subprocess
import time

import pandas as pd

from .credentials import readonly_from_env


def repository_audit(root):
    """Inspect metadata and interface presence, never notebook or credential contents."""
    root = Path(root)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    interfaces = {
        "alpha": "twsq/alpha/base.py", "orders": "twsq/exec/order.py",
        "broker": "twsq/exec/broker.py", "research": "kraken_quant/research_execution.py",
        "strategy": "kraken_quant/strategy.py", "risk": "kraken_quant/risk.py",
        "readonly": "kraken_quant/reconciliation.py",
    }
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", ".env"]).returncode == 0
    tracked = bool(git("ls-files", "--", ".env"))
    return {"branch": git("branch", "--show-current"), "commit": git("rev-parse", "HEAD"),
            "status": git("status", "--porcelain"), "env_ignored": ignored,
            "env_tracked": tracked,
            "interfaces": {name: (root / path).is_file() for name, path in interfaces.items()},
            "live_orders_enabled": False}


def check_connectivity(env_path):
    """Authenticate, validate permissions and read balances; return no private data."""
    try:
        adapter = readonly_from_env(env_path, clock=lambda: pd.Timestamp.now(tz="UTC"),
                                    nonce=lambda: time.time_ns() // 1000)
        adapter.permissions()
        adapter._read("Balance", {"assetVersion": 1})
    except Exception:
        raise RuntimeError("Read-only connectivity failed; check dedicated credentials, permissions and network") from None
    return {"authenticated": True, "read_only": True, "balance_read": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "connectivity"))
    parser.add_argument("--root", default=".")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args(argv)
    try:
        result = repository_audit(args.root) if args.command == "audit" else check_connectivity(args.env)
    except Exception:
        print(json.dumps({"ok": False, "error": "Milestone 1 check failed; no private details logged"}))
        return 1
    print(json.dumps(result, indent=2))
    if args.command == "audit":
        return int(not result["env_ignored"] or result["env_tracked"] or not all(result["interfaces"].values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
