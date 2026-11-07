"""Explicit calendar/data requirements, not financial or model algorithms."""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
import json
from .market_continuity import ContinuityRequirements, DataPolicy

PHASES = ('training', 'validation', 'test')
ALGORITHM_VERSION = 'common-calendar-v1'


@dataclass(frozen=True)
class RollingConfig:
    durations: tuple[timedelta, timedelta, timedelta]
    step: timedelta
    folds: int = 1


@dataclass(frozen=True)
class PartitionRequest:
    assets: tuple[str, ...]
    identifier: str
    cutoff: object
    start: object | None = None
    end: object | None = None
    mode: str = 'research'
    datasets: tuple[str, ...] = ('prices_1m',)
    frequency: timedelta = timedelta(minutes=1)
    proportions: tuple[Decimal, Decimal, Decimal] = (Decimal('.6'), Decimal('.2'), Decimal('.2'))
    minimum_observations: tuple[int, int, int] = (1, 1, 1)
    minimum_durations: tuple[timedelta, timedelta, timedelta] = (timedelta(minutes=1),)*3
    continuity: ContinuityRequirements = field(default_factory=ContinuityRequirements)
    data_policy: DataPolicy = DataPolicy.EVENT_TIME
    selection_policy: str = 'COMMON_CALENDAR'
    independent_experiments: bool = False
    feature_lookback: timedelta = timedelta(0)
    minimum_context_observations: int = 1
    reference_lookback: timedelta = timedelta(0)
    label_horizon: timedelta = timedelta(0)
    purge: timedelta = timedelta(0)
    embargo: timedelta = timedelta(0)
    rolling: RollingConfig | None = None
    maximum_grid_cells: int = 2_000_000
    maximum_records: int = 1_000_000
    maximum_candidates: int = 128


@dataclass(frozen=True)
class PartitionPlan:
    status: str
    manifest_id: str | None
    content: bytes

    @property
    def ready(self):
        return self.status == 'READY' and self.manifest_id is not None

    @property
    def report(self):
        # Stored contract bytes are immutable; callers receive independent views.
        return json.loads(self.content)
