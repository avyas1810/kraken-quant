"""Public-feed shadow operation and explicitly non-prospective historical replay."""

import argparse
from dataclasses import asdict
from decimal import ROUND_CEILING, ROUND_FLOOR
import hashlib
import importlib
import json
import os
from pathlib import Path

import pandas as pd

from .config import CONFIG
from .data import DAY, canonical_json, utc_instant, validate_bars
from .evidence import bind_candidate, candidate_pipeline, qualification_evidence
from .monitoring import WebhookAlert, backup_database, health_check, restore_database
from .operational_shadow import OperationalShadowRunner
from .operations import Operations
from .ops_cli import emit, main as ops_main
from .risk import InstrumentRules, Quote, money
from .shadow_cli import FlatStrategy
from .strategy import Instrument, StrategyPipeline
from .streaming import WebSocketMarketData


def load_pipeline(spec):
    if not spec:
        return StrategyPipeline(FlatStrategy())
    module, factory = spec.split(":", 1)
    pipeline = getattr(importlib.import_module(module), factory)()
    if not isinstance(pipeline, StrategyPipeline):
        raise TypeError("Strategy factory must return StrategyPipeline")
    return pipeline


class ReplayMarketData:
    """Offline completed-history visibility with next-open simulated BBO.

    Precision/minimums are replay assumptions unless explicit rules supplied;
    this adapter is never classified as a genuine Kraken observation.
    """

    observation_source = "historical_replay"
    event_time_verified = False

    def __init__(self, frames, clock, *, spread_bps=10, rules=None):
        self.clock, self.frames, self.rule_specs = clock, {}, rules or {}
        self.spread = money(spread_bps) / 20000
        if not 0 <= self.spread < 1:
            raise ValueError("Invalid replay spread")
        for key, frame in frames.items():
            self.frames[key] = validate_bars(frame, frame.index.min(), frame.index.max(),
                                             as_of=frame.index.max() + DAY)

    def bars(self, instrument, *, start, end):
        return validate_bars(self.frames[instrument].loc[start:end], start, end, as_of=self.clock())

    def quote(self, instrument):
        now = utc_instant(self.clock())
        price = money(self.frames[instrument].loc[now.normalize(), "open"])
        tick = money(self.rule_specs.get(instrument.symbol, {}).get("price_tick", "0.00000001"))
        bid = ((price * (1 - self.spread))/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
        ask = ((price * (1 + self.spread))/tick).to_integral_value(rounding=ROUND_CEILING)*tick
        return Quote(instrument, now, bid, ask)

    def rules(self, instrument):
        spec = self.rule_specs.get(instrument.symbol, dict(quantity_step="0.00000001",
            price_tick="0.00000001", minimum_quantity="0.00000001", minimum_notional="1"))
        return InstrumentRules(timestamp=self.clock(), instrument=instrument, **spec)


def replay(frames, database, *, strategy=None, strategy_id="flat-replay-v1", fee_rate="0.004",
           spread_bps=10, rules=None, candidate=None):
    starts = {f.index.min() for f in frames.values()}
    ends = {f.index.max() for f in frames.values()}
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("Replay histories require common boundaries")
    start, end = starts.pop(), ends.pop()
    if start <= CONFIG.data_end or end <= start:
        raise PermissionError("Replay requires at least two post-locked daily bars")
    now = [start + DAY + pd.Timedelta(seconds=1)]
    clock = lambda: now[0]
    source = ReplayMarketData(frames, clock, spread_bps=spread_bps, rules=rules)
    binding = None
    pipeline = load_pipeline(strategy)
    if candidate:
        if strategy:
            raise PermissionError("Frozen candidate cannot be overridden with a strategy factory")
        pipeline, binding = candidate_pipeline(candidate, now=end + DAY)
        strategy_id = binding["candidate_sha256"]
        fee_rate = str(binding["specification"]["costs"]["fee_rate"])
        if set(binding["specification"]["universe"]) != {key.symbol for key in frames}:
            raise PermissionError("Replay universe differs from frozen candidate")
    manifest = dict(mode="historical_replay", prospective=False, spread_bps=str(spread_bps),
        rules=rules, histories={key.symbol: hashlib.sha256(frame.to_csv().encode()).hexdigest()
                               for key, frame in sorted(source.frames.items())})
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    runner = OperationalShadowRunner(pipeline, source, database,
        instruments=list(frames), start=start, strategy_id=strategy_id, fee_rate=fee_rate, clock=clock)
    try:
        if binding is not None:
            bind_candidate(runner.ops, binding)
        previous = runner.ops.value("replay_manifest")
        if previous is not None and previous != manifest:
            raise PermissionError("Replay input/configuration changed; use a new database")
        runner.ops.set_value("replay_manifest", manifest)
        events = []
        completed = runner.gateway.connection.execute("SELECT body FROM shadow_events ORDER BY rowid DESC LIMIT 1").fetchone()
        last_day = None if completed is None else utc_instant(json.loads(completed[0])["timestamp"]).normalize()
        for day in pd.date_range(start + DAY, end, freq="D"):
            # Restart resumes forward only: never apply a past day's mark to
            # the terminal portfolio or repeat an already committed decision.
            if last_day is not None and day <= last_day:
                continue
            now[0] = day + pd.Timedelta(seconds=1)
            # Deliberately controlled replay session boundary, not a heartbeat
            # continuity claim. Real unattended sessions retain the watchdog.
            runner.ops.set_value("watchdog", {"armed": False})
            events.append(runner.tick())
        runner.ops.set_value("watchdog", {"armed": False})
        return dict(mode="historical_replay", prospective=False, decisions=len(events),
                    ledger=runner.ops.ledger_check(), evidence=runner.ops.evidence())
    finally:
        runner.close()


def export_ledger(database, destination):
    """Auditable runtime JSON: durable fills, orders, equity, positions and PnL."""
    ops = Operations(database)
    try:
        ops.db.execute("BEGIN")
        ledger = ops.ledger_check()
        state = ops.inspect()
        db = ops.db
        events = [json.loads(raw) for raw, in db.execute("SELECT body FROM shadow_events ORDER BY rowid")]
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_fills'").fetchone()
        fills = [] if not exists else [json.loads(raw) for raw, in db.execute("SELECT body FROM shadow_fills ORDER BY rowid")]
        orders = [dict(zip(("order_id", "decision", "instrument", "side", "quantity", "price", "fee_rate", "status"), row))
                  for row in db.execute("SELECT * FROM reservations ORDER BY rowid")]
        origin = ops.value("ledger_origin")
        last_equity = money(events[-1]["equity"]) if events else money(origin["cash"])
        result = dict(version=1, mode="paper", live_authorized=False, state=state, ledger=ledger,
            orders=orders, fills=fills, equity=[dict(timestamp=e["timestamp"], equity=e["equity"], cash=e["cash"]) for e in events],
            total_fees=str(sum((money(f["fee"]) for f in fills), money(0))),
            marked_pnl=str(last_equity - money(origin["cash"])), evidence=ops.evidence(),
            replay_manifest=ops.value("replay_manifest"))
        db.execute("COMMIT")
        target = Path(destination)
        if target.exists():
            raise FileExistsError("Export destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(canonical_json(result) + b"\n")
        return dict(export=str(target), fills=len(fills), marked_pnl=result["marked_pnl"])
    finally:
        ops.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    diagnosis = sub.add_parser("diagnose", help="Public websocket only; no credentials or orders")
    diagnosis.add_argument("--symbol", choices=CONFIG.universe, default=CONFIG.universe[0])
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("--bars", action="append", required=True, help="SYMBOL=OHLCV.csv; UTC timestamp column")
    replay_parser.add_argument("--database", required=True)
    replay_parser.add_argument("--strategy", help="Trusted module:factory returning StrategyPipeline")
    replay_parser.add_argument("--candidate", help="Exact frozen strategy; replay remains non-prospective")
    replay_parser.add_argument("--strategy-id", default="flat-replay-v1")
    replay_parser.add_argument("--fee-rate", default="0.004")
    replay_parser.add_argument("--spread-bps", type=float, default=10)
    replay_parser.add_argument("--rules", help="JSON symbol -> quantity_step/price_tick/minimum_quantity/minimum_notional")
    for name in ("export", "backup", "restore", "health"):
        p = sub.add_parser(name)
        p.add_argument("--database", required=True)
        if name != "health":
            p.add_argument("--output", required=True)
        else:
            p.add_argument("--alert-url-env", help="Environment variable containing an explicitly configured HTTPS webhook")
            p.add_argument("--send-alert", action="store_true", help="Explicitly opt in to sending unhealthy state to the configured webhook")
        if name == "export":
            p.add_argument("--candidate")
            p.add_argument("--qualification-evidence", action="store_true")
            p.add_argument("--validation-report")
            p.add_argument("--review-report")
            p.add_argument("--robustness-report")
    # Preserve all existing operator flags without maintaining duplicate parsers.
    for name in ("once", "collect", "inspect", "halt", "resume", "recover", "evidence", "watchdog", "pin-exchange"):
        sub.add_parser(name, add_help=False)
    args, rest = parser.parse_known_args(argv)
    if args.command in {"once", "collect", "inspect", "halt", "resume", "recover", "evidence", "watchdog", "pin-exchange"}:
        return ops_main([args.command, *rest])
    if rest:
        parser.error("Unexpected arguments")
    if args.command == "diagnose":
        source = WebSocketMarketData()
        key = Instrument("kraken", args.symbol, *args.symbol.split("/"))
        try:
            quote = source.quote(key)
            emit(dict(source=source.observation_source, quote={**asdict(quote), "timestamp": quote.timestamp.isoformat(),
                "bid": str(quote.bid), "ask": str(quote.ask)}, diagnostics=source.diagnostics, live_authorized=False))
        finally:
            source.close()
    elif args.command == "replay":
        frames = {}
        for item in args.bars:
            symbol, path = item.split("=", 1)
            key = Instrument("kraken", symbol, *symbol.split("/"))
            if key in frames:
                raise ValueError("Duplicate replay symbol")
            frame = pd.read_csv(path)
            frame.index = pd.to_datetime(frame.pop("timestamp"), utc=True)
            frames[key] = frame
        rules = None if not args.rules else json.loads(Path(args.rules).read_text())
        emit(replay(frames, args.database, strategy=args.strategy, strategy_id=args.strategy_id,
                    fee_rate=args.fee_rate, spread_bps=args.spread_bps, rules=rules, candidate=args.candidate))
    elif args.command == "export":
        if args.qualification_evidence:
            if not args.candidate:
                parser.error("--qualification-evidence requires --candidate")
            emit(qualification_evidence(args.database, args.candidate, args.output,
                validation_report=args.validation_report, review_report=args.review_report,
                robustness_report=args.robustness_report))
        else:
            emit(export_ledger(args.database, args.output))
    elif args.command == "backup":
        emit(backup_database(args.database, args.output))
    elif args.command == "restore":
        emit(restore_database(args.database, args.output))
    elif args.command == "health":
        if args.send_alert and not args.alert_url_env:
            parser.error("--send-alert requires --alert-url-env")
        alert = None if not args.alert_url_env else WebhookAlert(os.environ[args.alert_url_env], enabled=args.send_alert)
        result = health_check(args.database, alert=alert)
        emit(result)
        return 0 if result["healthy"] else 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        emit(dict(alert="paper_command_failed", code=type(error).__name__))
        raise SystemExit(1) from None
