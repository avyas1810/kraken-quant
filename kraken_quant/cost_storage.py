"""Immutable, content-addressed local audit objects, not another market-data store."""
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from .asset_catalogue import _atomic_json
from .operations import ProcessLock
from .ohlcv_collector import sql_time


def payload(value):
    if is_dataclass(value):
        return payload(asdict(value))
    if isinstance(value, datetime):
        return sql_time(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k:payload(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)):
        return [payload(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(payload(value),sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


class AuditStore:
    def __init__(self,data_dir):
        self.root = Path(data_dir)/"cost_infrastructure"

    def path(self,kind,version):
        if kind not in ("fees","inputs") or not isinstance(version,str) or not re.fullmatch("[a-f0-9]{64}",version):
            raise ValueError("Invalid audit identifier")
        path = self.root/kind/(version+".json")
        if any(p.is_symlink() for p in (self.root,self.root/kind,path)):
            raise ValueError("Audit symlinks prohibited")
        return path

    def put(self,kind,value):
        body = payload(value)
        version = digest(body)
        path = self.path(kind,version)
        path.parent.mkdir(parents=True,exist_ok=True)
        lock = ProcessLock(self.root/"writer")
        try:
            if path.exists():
                if self.get(kind,version) != body:
                    raise ValueError("Immutable audit conflict")
            else:
                _atomic_json(path,body)
        finally:
            lock.close()
        return version

    def get(self,kind,version):
        value = json.loads(self.path(kind,version).read_text(encoding="utf-8"))
        if digest(value) != version or value.get("contract_version") != 1:
            raise ValueError("Audit object corrupt or unsupported")
        return value

    def versions(self,kind):
        directory = self.root/kind
        if directory.is_symlink():
            raise ValueError("Audit symlink prohibited")
        return tuple(p.stem for p in sorted(directory.glob("*.json")))
