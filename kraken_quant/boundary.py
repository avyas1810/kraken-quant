"""Shared strategy evaluation in shadow and disabled Kraken production modes."""

from .risk import spot_only
from .strategy import StrategyPipeline, validate_weights


class ShadowAdapter:
    def __init__(self, pipeline, *, max_gross=1.0, spot_rehearsal=False):
        if not isinstance(pipeline, StrategyPipeline):
            raise TypeError("Expected shared StrategyPipeline")
        self.pipeline, self.max_gross, self.spot_rehearsal = pipeline, max_gross, spot_rehearsal

    def evaluate(self, **context):
        keys = tuple(context["historical_data"])
        if self.spot_rehearsal:
            for key in keys:
                spot_only(key)
        target = self.pipeline.evaluate(**context)
        return None if target is None else validate_weights(target, keys,
            long_only=self.spot_rehearsal, max_gross=1.0 if self.spot_rehearsal else self.max_gross)

    def submit(self, *args, **kwargs):
        raise PermissionError("Shadow has no submission capability")


class ProductionAdapter:
    def __init__(self, pipeline, gateway, *, enabled=False):
        if not isinstance(enabled, bool):
            raise ValueError("Production enablement must be explicit boolean")
        self.pipeline, self.gateway, self.enabled = pipeline, gateway, enabled

    def evaluate(self, **context):
        if not self.enabled:
            raise PermissionError("Production adapter is disabled")
        for key in context["historical_data"]:
            spot_only(key)
        target = self.pipeline.evaluate(**context)
        return None if target is None else validate_weights(target, context["historical_data"], long_only=True)

    def reserve(self, intent, **snapshots):
        if not self.enabled:
            raise PermissionError("Production adapter is disabled")
        return self.gateway.reserve(intent, **snapshots)

    def submit(self, *args, **kwargs):
        if not self.enabled:
            raise PermissionError("Production adapter is disabled")
        raise NotImplementedError("No live transport is implemented; explicit enablement permits dry validation only")
