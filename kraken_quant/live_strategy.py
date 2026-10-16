"""Frozen shared-strategy decisions to funded Kraken spot order intents."""
import hashlib
import importlib
from pathlib import Path
import uuid
import pandas as pd

from .candidate import read_candidate
from .config import CONFIG
from .data import utc_instant
from .risk import OrderIntent, RiskGateway, money, spot_only
from .strategy import StrategyPipeline, validate_weights

FACTORIES = {'kraken_spot_multi_momentum_v1':'kraken_quant.strategies:multi_factory',
             'multi':'kraken_quant.strategies:multi_factory', 'btc':'kraken_quant.strategies:btc_factory',
             'multi_momentum':'kraken_quant.strategies:multi_factory', 'btc_momentum':'kraken_quant.strategies:btc_factory'}


class FrozenLiveStrategy:
    """No submission capability. Source and parameters are frozen, never overridden."""
    def __init__(self, candidate_file, *, repository=None):
        record=read_candidate(candidate_file)
        self.candidate_sha256=record['sha256']
        self.specification=record['specification']
        root=Path(repository or Path(__file__).resolve().parents[1]).resolve()
        hashes=self.specification.get('source_code_hashes',{})
        if not hashes:
            raise PermissionError('Frozen strategy source hashes are required')
        for relative, expected in hashes.items():
            source=(root/relative).resolve()
            if root not in source.parents or hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
                raise PermissionError('Frozen strategy source changed or is outside repository')
        factory_name=self.specification.get('factory') or FACTORIES.get(self.specification['strategy'])
        if not factory_name and ':' in self.specification['strategy']:
            factory_name=self.specification['strategy']
        if not factory_name or ':' not in factory_name:
            raise PermissionError('Frozen strategy factory is missing')
        module_name, function_name=factory_name.split(':',1)
        module_path=module_name.replace('.','/')+'.py'
        if module_path not in hashes:
            raise PermissionError('Factory source must be explicitly hash-frozen')
        module=importlib.import_module(module_name)
        if Path(module.__file__).resolve()!=(root/module_path).resolve():
            raise PermissionError('Imported factory differs from frozen source path')
        seed=self.specification.get('seed',0)
        strategy=getattr(module,function_name)(dict(self.specification['parameters']),seed=seed)
        feature_builder=None
        feature_name=self.specification.get('feature_factory')
        if feature_name:
            feature_module_name,feature_function_name=feature_name.split(':',1)
            feature_path=feature_module_name.replace('.','/')+'.py'
            if feature_path not in hashes:
                raise PermissionError('Feature factory source must be explicitly hash-frozen')
            feature_module=importlib.import_module(feature_module_name)
            if Path(feature_module.__file__).resolve()!=(root/feature_path).resolve():
                raise PermissionError('Feature factory differs from frozen source')
            feature_builder=getattr(feature_module,feature_function_name)(dict(self.specification['parameters']),seed)
        if isinstance(strategy,StrategyPipeline) and feature_builder is not None:
            raise PermissionError('Freeze one unambiguous feature pipeline')
        self.pipeline=strategy if isinstance(strategy,StrategyPipeline) else StrategyPipeline(strategy,feature_builder)
        self.universe=tuple(self.specification['universe'])
        if not set(self.universe)<=set(CONFIG.universe):
            raise PermissionError('Frozen universe exceeds locked Kraken spot configuration')

    def plan(self, *, historical_data, account, quotes, rules, timestamp, fee_rates):
        timestamp=utc_instant(timestamp)
        if timestamp<utc_instant(self.specification['prospective_start']):
            raise PermissionError('Frozen prospective start has not arrived')
        if {k.symbol for k in historical_data}!=set(self.universe):
            raise PermissionError('Live history must match the frozen universe')
        for key in historical_data:
            spot_only(key)
            quote=quotes[key]
            if not pd.Timedelta(0)<=timestamp-quote.timestamp<=pd.Timedelta(seconds=5):
                raise PermissionError('Stale strategy-decision quote')
        equity=account.cash+sum((q*quotes[k].bid for k,q in account.positions.items()),money(0))
        target=self.pipeline.evaluate(timestamp=timestamp,historical_data=historical_data,
            positions=account.positions,cash=account.cash,equity=equity)
        if target is None:
            return []
        target=validate_weights(target,historical_data,long_only=True,max_gross=self.specification['risk_limits'].get('max_gross',1.0))
        intents=[]
        for key in sorted(target):
            current=account.positions.get(key,money(0))
            desired=money(target[key])*equity/quotes[key].ask
            delta=desired-current
            side='buy' if delta>0 else 'sell'
            price=quotes[key].ask if side=='buy' else quotes[key].bid
            quantity=min(abs(delta),RiskGateway.MAX_ORDER/price)
            if quantity<rules[key].minimum_quantity or quantity*price<rules[key].minimum_notional:
                continue
            decision=f'{self.candidate_sha256}:{timestamp.normalize().isoformat()}:{key.symbol}'
            order_id=str(uuid.uuid5(uuid.NAMESPACE_URL,decision))
            intents.append(OrderIntent(order_id,decision,key,side,quantity,price,fee_rates[key]))
        # Sell before buy; actual execution must reconcile/re-plan between orders.
        return sorted(intents,key=lambda intent:(intent.side=='buy',intent.instrument))
