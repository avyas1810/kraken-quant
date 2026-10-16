"""Small causal spot strategies shared by historical and paper execution."""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class BTCMomentum:
    lookback: int = 30
    budget: float = 0.8
    def __post_init__(self):
        if self.lookback < 2 or not 0 < self.budget <= 1:
            raise ValueError('lookback >= 2 and budget in (0,1] required')
    def target_weights(self, context):
        result = {}
        for key, frame in context.historical_data.items():
            close = frame.close
            result[key] = self.budget if key.base == 'BTC' and len(close) > self.lookback and close.iloc[-1] > close.iloc[-1-self.lookback] else 0.0
        return result

@dataclass(frozen=True)
class MultiAssetMomentum:
    lookback: int = 30
    regime_days: int = 60
    volatility_days: int = 20
    annual_vol_limit: float = 1.0
    budget: float = 0.8
    def __post_init__(self):
        if min(self.lookback, self.regime_days, self.volatility_days) < 2 or not math.isfinite(self.annual_vol_limit) or self.annual_vol_limit <= 0 or not 0 < self.budget <= 1:
            raise ValueError('Invalid momentum parameters')
    def target_weights(self, context):
        histories = context.historical_data
        btc = next((f.close for k, f in histories.items() if k.base == 'BTC'), None)
        allowed = btc is not None and len(btc) >= self.regime_days and btc.iloc[-1] > btc.iloc[-self.regime_days:].mean()
        selected = []
        if allowed:
            for key, frame in histories.items():
                close = frame.close
                if len(close) > max(self.lookback, self.volatility_days) and close.iloc[-1] > close.iloc[-1-self.lookback] and close.pct_change().iloc[-self.volatility_days:].std(ddof=1) * math.sqrt(365) <= self.annual_vol_limit:
                    selected.append(key)
        return {key: self.budget / len(selected) if key in selected else 0.0 for key in histories}

def btc_factory(parameters, seed=0):
    return BTCMomentum(**parameters)

def multi_factory(parameters, seed=0):
    return MultiAssetMomentum(**parameters)

def paper_btc_factory():
    """Small-exposure demonstration; distinct from any frozen candidate."""
    from .strategy import StrategyPipeline
    return StrategyPipeline(BTCMomentum(budget=0.1))

def paper_multi_factory():
    """Small-exposure demonstration; never represents frozen0.8-budget candidate."""
    from .strategy import StrategyPipeline
    return StrategyPipeline(MultiAssetMomentum(budget=0.1))
