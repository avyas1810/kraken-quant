"""Shadow inspection/recovery and supervised collection. No order submission."""

import argparse
import importlib
import json
from pathlib import Path
import time

import pandas as pd

from .config import CONFIG
from .credentials import readonly_from_env
from .data import utc_instant
from .evidence import bind_candidate, candidate_pipeline
from .streaming import WebSocketMarketData
from .operational_shadow import OperationalShadowRunner, pin_exchange_baseline
from .operations import Operations, ProcessLock
from .shadow_cli import FlatStrategy
from .strategy import Instrument, StrategyPipeline


def emit(body):
    print(json.dumps(body, sort_keys=True), flush=True)


def main(argv=None, *, market_data=None, clock=lambda: pd.Timestamp.now(tz="UTC"), sleeper=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["once", "collect", "inspect", "halt", "resume", "recover", "evidence", "watchdog", "pin-exchange"])
    parser.add_argument("--database", required=True)
    parser.add_argument("--start", help="Daily UTC history start after the locked dataset")
    parser.add_argument("--symbol", choices=CONFIG.universe, default=CONFIG.universe[0])
    parser.add_argument("--symbols", nargs="+", choices=CONFIG.universe, help="Explicit multi-asset spot universe")
    parser.add_argument("--strategy-id", default="flat-safety-v1")
    parser.add_argument("--strategy", help="Trusted module:factory -> StrategyPipeline")
    parser.add_argument("--candidate", help="Hash-sealed candidate; binds exact strategy, parameters, universe and fee scenario")
    parser.add_argument("--fee-rate", help="Scenario fee if private read-only adapter is not configured; default0.004")
    parser.add_argument("--env-file", help="Explicit opt-in to dedicated read-only credentials; never a trading key")
    parser.add_argument("--reason", help="Audited operator reason for state-changing controls")
    parser.add_argument("--duration-days", type=float, default=7)
    args = parser.parse_args(argv)
    database = Path(args.database)
    if args.command in {"once", "collect"}:
        if not args.start:
            parser.error("--start required")
        if not 0 < args.duration_days <= 365:
            parser.error("--duration-days must be in (0,365]")
        database.parent.mkdir(parents=True, exist_ok=True)
        pipeline = StrategyPipeline(FlatStrategy())
        binding = None
        fee_rate = args.fee_rate or "0.004"
        if args.candidate:
            if args.strategy:
                parser.error("--candidate cannot be overridden with --strategy")
            pipeline, binding = candidate_pipeline(args.candidate, now=clock())
            frozen_fee = str(binding["specification"]["costs"]["fee_rate"])
            if args.fee_rate is not None and float(args.fee_rate) != float(frozen_fee):
                parser.error("--fee-rate differs from frozen candidate")
            fee_rate = frozen_fee
            args.strategy_id = binding["candidate_sha256"]
        if args.strategy:
            module, factory = args.strategy.split(":", 1)
            pipeline = getattr(importlib.import_module(module), factory)()
        symbols = binding["specification"]["universe"] if binding else (args.symbols or [args.symbol])
        if binding and args.symbols and set(args.symbols) != set(symbols):
            parser.error("--symbols differs from frozen candidate")
        keys = [Instrument("kraken", symbol, *symbol.split("/")) for symbol in symbols]
        source = market_data or WebSocketMarketData(clock=clock)
        runner = OperationalShadowRunner(pipeline, source, database,
            instruments=keys, start=args.start, strategy_id=args.strategy_id, fee_rate=fee_rate, clock=clock)
        try:
            if binding is not None:
                bind_candidate(runner.ops, binding)
            if args.env_file:
                runner.readonly = readonly_from_env(args.env_file, clock=clock, nonce=runner.ops.nonce)
            deadline = time.monotonic() + args.duration_days * 86400
            while True:
                try:
                    emit(runner.tick())
                except Exception as error:
                    # Exception messages may include private server information.
                    emit({"mode": "shadow", "alert": "halted", "code": type(error).__name__})
                    return 1
                if args.command == "once" or time.monotonic() >= deadline:
                    break
                sleeper(min(20, max(0, deadline - time.monotonic())))
            runner.ops.set_value("watchdog", {"armed": False})
            runner.ops.event("supervision_stopped", reason="requested_duration_completed")
            emit(runner.ops.evidence())
            return 0
        finally:
            runner.close()
            if hasattr(source, "close"):
                source.close()
    lock = None
    if args.command in {"resume", "recover", "pin-exchange"}:
        lock = ProcessLock(database)
    ops = Operations(database, clock=clock)
    try:
        if args.command == "inspect": emit(ops.inspect())
        elif args.command == "evidence": emit(ops.evidence())
        elif args.command == "watchdog":
            result = ops.watchdog()
            emit(result)
            return 0 if result["healthy"] else 1
        elif args.command in {"halt", "resume", "recover"}:
            if not args.reason: parser.error("--reason required")
            result = getattr(ops, args.command)(args.reason)
            emit({"command": args.command, "result": result, "halted": ops.inspect()["halted"]})
        elif args.command == "pin-exchange":
            if not args.reason or not args.env_file or not args.start:
                parser.error("--reason, --env-file and --start required")
            if utc_instant(args.start) <= CONFIG.data_end:
                parser.error("Exchange observation start must be after the locked dataset")
            observer = readonly_from_env(args.env_file, clock=clock, nonce=ops.nonce)
            pin_exchange_baseline(ops, observer, since=args.start, reason=args.reason)
            emit({"command": "pin-exchange", "accepted": True})
        return 0
    finally:
        ops.close()
        if lock is not None: lock.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        emit({"alert": "operator_command_failed", "code": type(error).__name__})
        raise SystemExit(1) from None
