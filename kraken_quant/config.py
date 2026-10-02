"""Immutable research settings sourced only from the existing configuration."""

from dataclasses import dataclass

import pandas as pd

from examples import research_config as locked


def utc_day(value):
    """Interpret configuration date labels as UTC midnight, never local time."""
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("Calendar date must not be missing")
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    if stamp != stamp.normalize():
        raise ValueError("Expected a UTC calendar date")
    return stamp


@dataclass(frozen=True)
class Split:
    name: str
    start: pd.Timestamp
    end: pd.Timestamp

    def __post_init__(self):
        object.__setattr__(self, "start", utc_day(self.start))
        object.__setattr__(self, "end", utc_day(self.end))
        if self.start > self.end:
            raise ValueError("Split start exceeds end")


@dataclass(frozen=True)
class ResearchConfig:
    universe: tuple
    data_start: pd.Timestamp
    data_end: pd.Timestamp
    splits: tuple

    def __post_init__(self):
        object.__setattr__(self, "universe", tuple(self.universe))
        object.__setattr__(self, "splits", tuple(self.splits))
        object.__setattr__(self, "data_start", utc_day(self.data_start))
        object.__setattr__(self, "data_end", utc_day(self.data_end))
        if not self.universe or len(set(self.universe)) != len(self.universe):
            raise ValueError("Universe must be nonempty and unique")
        if any(not isinstance(s, str) or not s.endswith("/USD") for s in self.universe):
            raise ValueError("Research instruments must be USD pairs")
        if tuple(s.name for s in self.splits) != ("train", "validation", "test"):
            raise ValueError("Expected chronological train/validation/test splits")
        if self.splits[0].start != self.data_start or self.splits[-1].end != self.data_end:
            raise ValueError("Splits must cover the full configured range")
        for previous, following in zip(self.splits, self.splits[1:]):
            if previous.end + pd.Timedelta(days=1) != following.start:
                raise ValueError("Splits must be consecutive and non-overlapping")

    def split(self, name):
        for split in self.splits:
            if split.name == name:
                return split
        raise ValueError(f"Unknown split: {name}")


CONFIG = ResearchConfig(
    universe=tuple(locked.RESEARCH_UNIVERSE),
    data_start=locked.DATA_START,
    data_end=locked.DATA_END,
    splits=(
        Split("train", locked.TRAIN_START, locked.TRAIN_END),
        Split("validation", locked.VALIDATION_START, locked.VALIDATION_END),
        Split("test", locked.TEST_START, locked.TEST_END),
    ),
)
