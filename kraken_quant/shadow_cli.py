"""Run one durable shadow observation; public data only, never real orders."""

import argparse
import importlib
import json
from pathlib import Path

from .config import CONFIG
from .market_data import KrakenMarketData
from .shadow import ShadowRunner
from .strategy import Instrument, StrategyPipeline


class FlatStrategy:
    def target_weights(self, context):
        return {key: 0 for key in context.historical_data}


def main(argv=None, *, market_data=None, clock=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Persistent paper SQLite database outside the repository")
    parser.add_argument("--start", required=True, help="UTC daily history start strictly after locked data end")
    parser.add_argument("--symbol", choices=CONFIG.universe, default=CONFIG.universe[0])
    parser.add_argument("--strategy", help="Trusted module:factory returning StrategyPipeline; default is flat")
    parser.add_argument("--strategy-id", required=True, help="Frozen code/feature/parameter identity")
    parser.add_argument("--fee-rate", default="0.004", help="Paper fee fraction; scenario assumption, not fetched fee")
    args = parser.parse_args(argv)
    pipeline = StrategyPipeline(FlatStrategy())
    if args.strategy:
        module, name = args.strategy.split(":", 1)
        pipeline = getattr(importlib.import_module(module), name)()
        if not isinstance(pipeline, StrategyPipeline):
            raise TypeError("Strategy factory must return StrategyPipeline")
    base, quote = args.symbol.split("/")
    runner = ShadowRunner(pipeline, market_data or KrakenMarketData(), Path(args.database),
        instruments=[Instrument("kraken", args.symbol, base, quote)], start=args.start,
        strategy_id=args.strategy_id, fee_rate=args.fee_rate, clock=clock)
    try:
        print(json.dumps(runner.step(), sort_keys=True))
    finally:
        runner.close()


if __name__ == "__main__":
    main()
