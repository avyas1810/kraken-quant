"""Point-in-time eligibility without today's surviving constituent list."""
from dataclasses import dataclass
import hashlib
import numpy as np
import pandas as pd

@dataclass(frozen=True)
class AssetHistory:
    symbol: str
    listing: pd.Timestamp
    delisting: object
    known_at: pd.Timestamp
    provenance: str
    source_sha256: str
    def __post_init__(self):
        for name in ('listing', 'known_at'):
            stamp = pd.Timestamp(getattr(self,name))
            if stamp.tzinfo is None: raise ValueError('UTC-aware listing knowledge required')
            object.__setattr__(self,name,stamp.tz_convert('UTC'))
        if self.delisting is not None:
            stamp = pd.Timestamp(self.delisting)
            if stamp.tzinfo is None or stamp <= self.listing: raise ValueError('Invalid delisting')
            object.__setattr__(self,'delisting',stamp.tz_convert('UTC'))
        if not self.provenance or len(self.source_sha256) != 64 or any(c not in '0123456789abcdef' for c in self.source_sha256): raise ValueError('Historical provenance/hash required')

def eligibility(close, dollar_volume, metadata, *, minimum_dollar_volume=1e6, lookback=30, minimum_assets=20, maximum_assets=30):
    """Decision at day t uses only observations through t-1.

    Missing observations exclude an asset, not the entire historical panel.
    Delisting excludes execution from its effective timestamp; holdings require
    explicit liquidation/recovery treatment, never implicit zero-return fills.
    """
    if not isinstance(close.index,pd.DatetimeIndex) or close.index.tz is None or not close.index.is_unique or not close.index.is_monotonic_increasing: raise ValueError('Unique ordered UTC daily panel required')
    if not close.index.equals(pd.date_range(close.index[0],close.index[-1],freq='D')): raise ValueError('Panel calendar gaps')
    if not close.index.equals(dollar_volume.index) or not close.columns.equals(dollar_volume.columns): raise ValueError('Aligned liquidity required')
    if close.columns.has_duplicates or set(close.columns) != set(metadata): raise ValueError('One historical metadata record per asset required')
    if any(isinstance(x,bool) or not isinstance(x,int) for x in (lookback,minimum_assets,maximum_assets)) or not np.isfinite(minimum_dollar_volume): raise ValueError('Finite integer eligibility policy required')
    if any(info.symbol != symbol for symbol,info in metadata.items()): raise ValueError('Metadata symbol mismatch')
    if np.isinf(close.to_numpy(dtype=float)).any() or np.isinf(dollar_volume.to_numpy(dtype=float)).any(): raise ValueError('Infinite market observations')
    if lookback < 2 or not 1 <= minimum_assets <= maximum_assets or minimum_dollar_volume < 0: raise ValueError('Invalid eligibility policy')
    if ((close <= 0) & close.notna()).any().any() or ((dollar_volume < 0)&dollar_volume.notna()).any().any(): raise ValueError('Invalid price or volume')
    complete = close.notna().rolling(lookback,min_periods=lookback).sum().shift(1).eq(lookback)
    liquidity = dollar_volume.rolling(lookback,min_periods=lookback).mean().shift(1)
    mask = complete & liquidity.ge(minimum_dollar_volume)
    for symbol, info in metadata.items():
        mask[symbol] &= (close.index >= info.listing + pd.Timedelta(days=lookback)) & (close.index >= info.known_at)
        if info.delisting is not None: mask[symbol] &= close.index < info.delisting
    # Historical liquidity ranking; stable lexical tie ordering via source order.
    ranked = liquidity.where(mask).rank(axis=1,ascending=False,method='first')
    mask &= ranked.le(maximum_assets)
    valid = mask.sum(axis=1).ge(minimum_assets)
    return mask & valid.to_numpy()[:,None]
