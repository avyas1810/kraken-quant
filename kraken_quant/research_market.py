"""General research inputs. Operational calendar: UTC 24/7 crypto spot only.

All intervals are [start, end). OHLCV indices are opens; bar_end is exclusive.
The original daily/universe reader is unchanged. Project holdout locks apply here.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import json
import time
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from .config import CONFIG
from .data import canonical_json, utc_instant
from .strategy import Instrument

OHLCV = ['open', 'high', 'low', 'close', 'volume']
TIMEFRAMES = ('4h', '8h', '16h', '24h', 'weekly', 'monthly')


def research_interval(start, end):
    start, end = utc_instant(start), utc_instant(end)
    if start < CONFIG.data_start or end <= start:
        raise ValueError('Use an ordered interval inside the configured data range')
    if end > CONFIG.split('validation').end + pd.Timedelta(days=1):
        raise PermissionError('Final holdout remains locked; use train/validation dates only')
    return start, end


@dataclass(frozen=True)
class Asset:
    instrument: Instrument
    provider_id: str
    calendar: str = '24/7'
    timezone: str = 'UTC'
    multiplier: float = 1.0
    shortable: bool = False
    eligible: bool = True
    asset_class: str = 'crypto'

    def __post_init__(self):
        if (self.asset_class != 'crypto' or self.instrument.market_type != 'spot' or self.calendar != '24/7'
                or self.timezone != 'UTC' or self.multiplier != 1):
            raise ValueError('Implemented: UTC 24/7 crypto spot, multiplier 1; equities/FX/futures require a calendar and settlement adapter')
        if not self.provider_id or not isinstance(self.shortable, bool) or not isinstance(self.eligible, bool):
            raise ValueError('Asset identifier and boolean eligibility/shortability are required')


def asset(symbol, provider='coinbase', **kwargs):
    base, currency = symbol.split('/')
    return Asset(Instrument(provider, symbol, base, currency), symbol.replace('/', '-'), **kwargs)


def clean_hourly(frame, start, end, *, as_of):
    """Remove identical overlaps, reject conflicting duplicates/gaps; never fill prices."""
    start, end = research_interval(start, end)
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError('Provider must return timezone-aware hourly OHLCV')
    if set(OHLCV) - set(frame) or frame.columns.duplicated().any():
        raise ValueError('Missing/duplicate OHLCV columns')
    frame = frame[OHLCV].copy()
    frame.index = frame.index.tz_convert('UTC')
    duplicates = int(frame.index.duplicated().sum())
    if duplicates:
        for _, group in frame.groupby(level=0):
            if len(group) > 1 and not (group == group.iloc[0]).all().all():
                raise ValueError('Conflicting duplicate candles; inspect provider data')
        frame = frame[~frame.index.duplicated()].sort_index()
    frame = frame.sort_index().astype(float)
    expected = pd.date_range(start, end, freq='h', inclusive='left')
    if start.floor('h') != start or end.floor('h') != end:
        raise ValueError('Hourly requests require UTC hour boundaries')
    missing = expected.difference(frame.index)
    if len(missing) or not frame.index.equals(expected):
        raise ValueError(f'Hourly coverage mismatch: {len(missing)} missing bars; first={list(missing[:3])}. No prices filled.')
    v = frame.to_numpy()
    if not np.isfinite(v).all() or (v[:, :4] <= 0).any() or (v[:, 4] < 0).any():
        raise ValueError('Nonfinite/nonpositive prices or negative volume')
    if ((frame.low > frame[['open', 'close']].min(axis=1))
            | (frame.high < frame[['open', 'close']].max(axis=1))
            | (frame.low > frame.high)).any():
        raise ValueError('Inconsistent OHLC relationships')
    if end > utc_instant(as_of):
        raise ValueError('Requested hourly candles are incomplete')
    return frame, dict(rows=len(frame), identical_duplicates_removed=duplicates,
                       missing_bars=0, calendar='24/7', timezone='UTC', fabricated_prices=False)


def boundaries(start, end, timeframe):
    if timeframe not in TIMEFRAMES:
        raise ValueError(f'Unsupported timeframe {timeframe}; choose {TIMEFRAMES}')
    start, end = utc_instant(start), utc_instant(end)
    if timeframe.endswith('h'):
        step = pd.Timedelta(hours=int(timeframe[:-1]))
        # Epoch anchoring makes 16h bars continuous across UTC days.
        epoch = pd.Timestamp('1970-01-01', tz='UTC')
        first = epoch + ((start - epoch) // step) * step
        return pd.date_range(first, end + step, freq=step)
    if timeframe == 'weekly':
        first = start.normalize() - pd.Timedelta(days=start.weekday())
        return pd.date_range(first, end + pd.Timedelta(days=7), freq='7D')
    first = start.normalize().replace(day=1)
    return pd.date_range(first, end + pd.offsets.MonthBegin(1), freq='MS')


def next_boundary(timestamp, timeframe):
    timestamp = utc_instant(timestamp)
    b = boundaries(timestamp, timestamp + pd.Timedelta(days=40), timeframe)
    return b[b > timestamp][0]


def resample_hourly(frame, timeframe, *, as_of):
    """Only full, completed bins; partial edge bins are diagnosed, never scored."""
    if frame.empty:
        raise ValueError('No hourly observations')
    end = frame.index[-1] + pd.Timedelta(hours=1)
    checked, diagnostics = clean_hourly(frame, frame.index[0], end, as_of=as_of)
    edges = boundaries(checked.index[0], end, timeframe)
    rows, opens = [], []
    for left, right in zip(edges[:-1], edges[1:]):
        if left < checked.index[0] or right > min(end, utc_instant(as_of)):
            continue
        group = checked.loc[(checked.index >= left) & (checked.index < right)]
        if len(group) != int((right-left) / pd.Timedelta(hours=1)):
            raise ValueError('Incomplete aggregation bin')
        rows.append([group.open.iloc[0], group.high.max(), group.low.min(),
                     group.close.iloc[-1], group.volume.sum(), right])
        opens.append(left)
    if not rows:
        raise ValueError('No complete bars for selected timeframe; extend the data interval')
    result = pd.DataFrame(rows, index=pd.DatetimeIndex(opens), columns=OHLCV + ['bar_end'])
    diagnostics.update(timeframe=timeframe, complete_bars=len(result),
                       omitted_edge_hours=len(checked)-sum(int((r[-1]-o)/pd.Timedelta(hours=1)) for r,o in zip(rows, opens)))
    return result, diagnostics


def fetch_json(url):
    request = Request(url, headers={'User-Agent': 'QuantKrakenResearch/1.0'})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception as exc:
            if attempt == 2:
                raise RuntimeError(f'Public data request failed: {url.split("?")[0]}: {type(exc).__name__}') from exc
            time.sleep(0.5 * (attempt + 1))


class CoinbaseHistory:
    """Public Coinbase Exchange spot hourly candles. No API keys, no orders."""
    def __init__(self, cache, transport=fetch_json):
        self.cache, self.transport = Path(cache), transport
        self.cache.mkdir(parents=True, exist_ok=True)

    def retrieve(self, selected, start, end, *, as_of=None):
        start, end = research_interval(start, end)
        as_of = pd.Timestamp.now(tz='UTC') if as_of is None else utc_instant(as_of)
        if end > as_of:
            raise ValueError('Request includes unfinished candles')
        if selected.instrument.venue != 'coinbase':
            raise ValueError('Provider mismatch: use coinbase instruments with CoinbaseHistory')
        products = self.transport('https://api.exchange.coinbase.com/products/' + quote(selected.provider_id, safe=''))
        if not isinstance(products, dict) or products.get('id') != selected.provider_id:
            raise ValueError(f'Unsupported Coinbase product {selected.provider_id}; inspect /products')
        if products.get('quote_currency') != selected.instrument.quote or products.get('base_currency') != selected.instrument.base:
            raise ValueError('Provider metadata currency/base mismatch')
        identity = dict(provider='coinbase-exchange', product=selected.provider_id,
                        start=start.isoformat(), end=end.isoformat(), native_timeframe='1h', schema=1)
        key = hashlib.sha256(canonical_json(identity)).hexdigest()
        path = self.cache / (key + '.json')
        if path.exists():
            payload = json.loads(path.read_text())
            if payload['identity'] != identity or payload['sha256'] != hashlib.sha256(canonical_json(payload['rows'])).hexdigest():
                raise ValueError('Cache integrity check failed')
            rows = payload['rows']
        else:
            rows, cursor = [], start
            while cursor < end:
                stop = min(end, cursor + pd.Timedelta(hours=299))
                query = urlencode(dict(start=cursor.isoformat(), end=stop.isoformat(), granularity=3600))
                batch = self.transport(f'https://api.exchange.coinbase.com/products/{quote(selected.provider_id, safe="")}/candles?{query}')
                if not isinstance(batch, list) or any(len(row) != 6 for row in batch):
                    raise ValueError('Provider cannot supply requested candles; check product and interval')
                rows.extend(row for row in batch if start.timestamp() <= float(row[0]) < end.timestamp())
                cursor = stop
                if cursor < end:
                    time.sleep(0.15)
            payload = dict(identity=identity, retrieved_at=as_of.isoformat(), rows=rows,
                           sha256=hashlib.sha256(canonical_json(rows)).hexdigest())
            # Validate before caching so gaps cannot become apparently valid datasets.
            self._frame(rows, start, end, as_of)
            path.write_bytes(canonical_json(payload))
        frame, diagnostics = self._frame(rows, start, end, as_of)
        return frame, dict(**identity, dataset_sha256=payload['sha256'], retrieved_at=payload['retrieved_at'],
                           diagnostics=diagnostics, genuine_market_data=True,
                           corporate_actions='not applicable to crypto spot; token migrations unsupported')

    @staticmethod
    def _frame(rows, start, end, as_of):
        frame = pd.DataFrame([[r[3], r[2], r[1], r[4], r[5]] for r in rows], columns=OHLCV,
                             index=pd.to_datetime([r[0] for r in rows], unit='s', utc=True))
        return clean_hourly(frame, start, end, as_of=as_of)


def historical_fx(quote_currency, base_currency, timestamps, cache, transport=fetch_json):
    """Conservative ECB reference observations: available at next UTC midnight.

    Carry last published reference <=7 days, explicitly (not a tradable FX quote).
    Used for AUD valuation; FX conversion execution has its own cost assumption.
    """
    index = pd.DatetimeIndex(timestamps)
    if quote_currency == base_currency:
        return pd.Series(1., index=index), dict(source='identity', quote=quote_currency, base=base_currency)
    start = index.min().normalize() - pd.Timedelta(days=10)
    end = index.max().normalize() - pd.Timedelta(days=1)
    research_interval(max(start, CONFIG.data_start), end + pd.Timedelta(days=1))
    url = f'https://api.frankfurter.dev/v1/{start.date()}..{end.date()}?' + urlencode(dict(base=quote_currency, symbols=base_currency))
    path = Path(cache) / ('fx-' + hashlib.sha256(url.encode()).hexdigest() + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(path.read_text()) if path.exists() else transport(url)
    if payload.get('base') != quote_currency or not payload.get('rates'):
        raise ValueError(f'No historical {quote_currency}/{base_currency} reference FX; supply a tested FX adapter')
    observations = pd.Series({pd.Timestamp(day, tz='UTC') + pd.Timedelta(days=1): rate[base_currency]
                              for day, rate in payload['rates'].items()}, dtype=float).sort_index()
    if not np.isfinite(observations).all() or (observations <= 0).any():
        raise ValueError('Invalid FX reference rates')
    positions = observations.index.get_indexer(index, method='ffill')
    if (positions < 0).any() or ((index - observations.index[positions]) > pd.Timedelta(days=7)).any():
        raise ValueError('Missing/stale FX reference: no future rates or silent extrapolation')
    path.write_bytes(canonical_json(payload))
    return pd.Series(observations.iloc[positions].values, index=index), dict(
        source='frankfurter-v1-reference', quote=quote_currency, base=base_currency,
        sha256=hashlib.sha256(canonical_json(payload)).hexdigest(),
        availability='reference date + 1 day UTC; last published reference carried at most 7 days',
        limitation='reference valuation, not an executable intraday FX quote')


def load_market(selected, start, end, timeframe, cache, *, base_currency='AUD', provider=None):
    if not selected or len({a.instrument for a in selected}) != len(selected):
        raise ValueError('Select one or more unique instruments; no fixed universe size')
    research_interval(start, end)
    provider = CoinbaseHistory(cache) if provider is None else provider
    bars, manifests = {}, {}
    for item in selected:
        hourly, manifest = provider.retrieve(item, start, end)
        frame, diagnostics = resample_hourly(hourly, timeframe, as_of=end)
        times = frame.index.union(pd.DatetimeIndex(frame.bar_end))
        fx, fx_manifest = historical_fx(item.instrument.quote, base_currency, times, cache)
        frame['fx_open'] = fx.reindex(frame.index).values
        frame['fx_close'] = fx.reindex(pd.DatetimeIndex(frame.bar_end)).values
        bars[item.instrument] = frame
        manifests[str(item.instrument)] = dict(market=manifest, aggregation=diagnostics, fx=fx_manifest)
    return bars, manifests
