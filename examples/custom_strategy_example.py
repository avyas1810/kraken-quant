from kraken_quant.strategy import StrategyPipeline

class CustomMomentum:
    def __init__(self, lookback=20, weight=0.05):
        if not isinstance(lookback,int) or lookback < 1 or not 0 <= weight <= 0.2:
            raise ValueError('Invalid lookback or conservative spot weight')
        self.lookback,self.weight=lookback,float(weight)

    def target_weights(self, context):
        targets={}
        for instrument,bars in context.historical_data.items():
            closes=bars['close']
            positive=len(closes)>self.lookback and closes.iloc[-1]>closes.iloc[-1-self.lookback]
            targets[instrument]=self.weight if instrument.base=='BTC' and positive else 0.0
        return targets

def backtest_factory(parameters, seed):
    return CustomMomentum(**parameters)

def paper_factory():
    return StrategyPipeline(CustomMomentum())

def paper_demo_factory():
    """Three-day software-replay demonstration; distinct from research candidate."""
    return StrategyPipeline(CustomMomentum(lookback=3))
