"""Read-only notebook access to verified raw Kraken data; no financial metrics."""
from datetime import datetime,timedelta,timezone
from dataclasses import dataclass
from decimal import Decimal
from collections import OrderedDict
import hashlib
import json
from pathlib import Path

import pandas as pd
from . import ohlcv_collector as candles,book_collector as books
from .asset_catalogue import COLUMNS
from .cost_inputs import ordered
from .cost_storage import AuditStore,digest,payload
from .db_environment import load_config
from .eligibility_evidence import PointInTimeEvidence
from .eligibility_filters import ScreeningPolicy
from .fee_data import fee_view
from .history_coverage import inventory,minute_coverage,availability,window
from .metadata_store import connect_metadata
from .parquet_store import MonthlyParquetStore
from .storage_schema import dataset_schema,LOGICAL_KEYS,validate_pair_id,utc_timestamp

SOURCE_COLUMNS=('source_id','source_sha256','source_received_at','source_endpoint','timestamp_semantics','source_namespace')


def instant(value):
    stamp=pd.Timestamp(value)
    if stamp.tzinfo is None:raise ValueError('timezone_aware_timestamp_required')
    if stamp.nanosecond:raise ValueError('microsecond_precision_timestamp_required')
    return utc_timestamp(stamp.to_pydatetime())


def bounds(start,end):
    start,end=instant(start),instant(end)
    if start>=end or end-start>timedelta(days=3660):raise ValueError('bounded_ordered_time_range_required')
    return start,end


@dataclass(frozen=True)
class QualityReport:
    pair_id: str
    start: datetime
    end_exclusive: datetime
    as_of: datetime
    datasets: dict[str, dict[str, object]]
    quality_issues: tuple[dict, ...]
    limitation: str


class ResearchDataError(ValueError):
    def __init__(self,code,**report):
        self.code=code;self.report=dict(code=code,**report);super().__init__(code)


def key_text(dataset,row):
    return json.dumps([candles.sql_time(instant(row[k])) if k=='timestamp' else str(row[k]) for k in LOGICAL_KEYS[dataset]])


def frame(rows,columns,timestamps=()):
    result=pd.DataFrame(rows,columns=columns,dtype=object)
    for name in timestamps:
        if name in result:result[name]=pd.to_datetime(result[name],utc=True)
    for name in ('trade_count','level','qty_precision','price_precision'):
        if name in result:result[name]=pd.array(result[name],dtype='Int64')
    return result


class ResearchData:
    """Bind read-only canonical data and a knowledge-time cutoff.

    Market ranges are [start,end). Decimal columns remain Python Decimal objects.
    A historical event-time query defaults to information available now; pass an
    explicit as_of to restrict receipt knowledge time. No current-data fallback.
    M13 research release/registration writes immutable governance sidecars only;
    ordinary raw reads and integrity validation do not write market storage.
    """
    def __init__(self,data_dir=None,*,as_of=None,clock=lambda:datetime.now(timezone.utc)):
        self.data_dir=Path(data_dir) if data_dir is not None else load_config().data_dir
        self.clock=clock;self.as_of=instant(as_of or clock())
        if self.as_of>instant(clock()):raise ValueError('future_knowledge_time_prohibited')
        with connect_metadata(self.data_dir,readonly=True):pass
        self.store=MonthlyParquetStore(self.data_dir);self.audit=AuditStore(self.data_dir)
        self.evidence=PointInTimeEvidence(self.data_dir,self.as_of,ScreeningPolicy())
        self._cache_key=None;self._cache=None;self.source_selection=None;self.record_selection=None
        self._parsed_pair=None;self._parsed_sources=OrderedDict();self._historical_sources=OrderedDict()
        self._metadata_hashes={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for _,path in self.evidence.receipts}

    def pairs(self, quote_currencies=("AUD",)):
        pairs=[];unresolved=[]
        for pair in self.evidence.candidates(quote_currencies):
            meta,error=self.evidence.metadata(pair)
            if error:unresolved.append(dict(pair_id=pair,reason=error))
            elif meta['market_type']=='spot' and meta['quote_asset'] in quote_currencies:pairs.append(pair)
        if unresolved:raise ResearchDataError('unresolved_market_identity',records=unresolved)
        return pairs

    def _metadata(self,pair):
        validate_pair_id(pair)
        with connect_metadata(self.data_dir,readonly=True) as db:
            if db.execute('SELECT 1 FROM assets WHERE pair_id=?',(pair,)).fetchone() is None:raise ResearchDataError('unknown_pair',pair_id=pair)
        meta,error=self.evidence.metadata(pair)
        if meta:
            path=self.data_dir/meta['receipt_id']
            try:
                content=path.read_bytes();actual=hashlib.sha256(content).hexdigest()
                if self._metadata_hashes.get(str(path))!=actual:
                    if digest(json.loads(content))!=meta['receipt_hash']:raise ValueError()
                    self._metadata_hashes[str(path)]=actual
            except Exception:raise ResearchDataError('metadata_source_dependency_changed',pair_id=pair) from None
        return meta,error

    def load_market_metadata(self,pair,as_of=None)->pd.DataFrame:
        if as_of is not None and instant(as_of)!=self.as_of:
            return ResearchData(self.data_dir,as_of=as_of,clock=self.clock).load_market_metadata(pair)
        meta,error=self._metadata(pair)
        columns=[*COLUMNS,'market_type','source_id','source_sha256','observed_at','trading_status_as_of']
        if meta:
            first=[r['first_seen'] for receipt,_ in self.evidence.receipts for r in receipt['records'] if r['pair_id']==pair]
            meta={**meta,'first_seen':min(first) if first else meta['first_seen'],'source_id':meta['receipt_id'],'source_sha256':meta['receipt_hash'],
                'trading_status_as_of':meta['status'] if instant(meta['observed_at'])==self.as_of else None}
            for name in ('min_order_qty','min_order_value'):
                if meta[name] is not None:meta[name]=Decimal(meta[name])
        result=frame([meta] if meta else [],columns,('first_seen','last_seen','observed_at'))
        result.attrs=dict(as_of=candles.sql_time(self.as_of),status='known' if meta else 'unknown',issues=[error] if error else ['status_known_at_observed_instant_only'],
            meaning='status is last recorded status, not reconstructed historical/current status; trading_status_as_of is unknown between observations')
        return result

    def quality_issues(self,pair,start,end,dataset=None):
        with connect_metadata(self.data_dir,readonly=True) as db:
            rows=db.execute('SELECT * FROM data_quality_issues WHERE pair_id=? AND detected_at<=? ORDER BY issue_id',(pair,candles.sql_time(self.as_of)))
            names=[d[0] for d in rows.description];result=[]
            for values in rows:
                row=dict(zip(names,values))
                if dataset and row['dataset'] not in (None,dataset):continue
                begin=row['observation_at'] or row['period_start'];finish=row['observation_at'] or row['period_end']
                if begin and instant(begin)>=end or finish and instant(finish)<start:continue
                row['resolution_status_current']=row.pop('resolution_status')
                row['resolution_as_of']='unknown_unversioned_resolution_history';result.append(row)
        return result

    def _index(self,pair,dataset,start,end,meta):
        key=(pair,dataset,start,end,self.as_of,tuple(sorted(self.source_selection or ())))
        folder='ohlcv_collection' if dataset=='prices_1m' else 'book_collection'
        binding=self.data_dir/folder/'source.json'
        if not binding.exists():raise ResearchDataError('source_binding_missing',pair_id=pair,dataset=dataset)
        namespace=json.loads(binding.read_text())['source'];index={};warnings=[]
        documents=[(path,path.read_bytes()) for path in sorted((self.data_dir/folder/'responses'/pair).glob('*.json')) if self.source_selection is None or str(path.relative_to(self.data_dir)).replace('\\','/') in self.source_selection]
        fingerprint=tuple((str(path),hashlib.sha256(content).hexdigest()) for path,content in documents)
        key=(key,namespace,fingerprint)
        if key==self._cache_key:return self._cache
        if self._parsed_pair!=pair:self._parsed_sources.clear();self._parsed_pair=pair
        for path,content_bytes in documents:
            source_id=str(path.relative_to(self.data_dir)).replace('\\','/')
            if self.source_selection is not None and source_id not in self.source_selection:continue
            try:
                cache_key=(str(path),hashlib.sha256(content_bytes).hexdigest(),meta['kraken_pair_id'])
                cached=self._parsed_sources.get(cache_key)
                doc=cached[0] if cached else json.loads(content_bytes);received=instant(doc['fetched_at'] if dataset=='prices_1m' else doc['finished_at'])
                if received>self.as_of:continue
                if doc.get('source')!=namespace:raise ValueError('source_namespace_mismatch')
                if dataset=='prices_1m':
                    if doc['params']['pair']!=meta['kraken_pair_id']:raise ValueError('native_identity_mismatch')
                    parsed=cached[1] if cached else candles.parse_ohlc(doc['body'],meta,received)
                    if parsed['invalid_observations']:raise ValueError('invalid_source_batch')
                    records=parsed['records'];semantics='UTC_candle_open; received_at_is_knowledge_time'
                else:
                    stamp=instant(doc['started_at'])
                    if doc['kraken_pair_id']!=meta['kraken_pair_id']:raise ValueError('native_identity_mismatch')
                    parsed=cached[1] if cached else books.parse_depth(doc['body'],meta,stamp,received,doc['provenance']['requested_depth'])
                    if parsed['provenance']!=doc['provenance']:raise ValueError('snapshot_provenance_mismatch')
                    records=parsed[dataset];semantics=doc['provenance']['timestamp_semantics']
                doc_hash=cached[2] if cached else digest(doc)
                self._parsed_sources[cache_key]=(doc,parsed,doc_hash);self._parsed_sources.move_to_end(cache_key)
                while len(self._parsed_sources)>64:self._parsed_sources.popitem(last=False)
                reference=dict(source_id=source_id,source_sha256=doc_hash,source_received_at=candles.sql_time(received),
                    source_endpoint='https://api.kraken.com/0/public/'+('OHLC' if dataset=='prices_1m' else 'Depth'),timestamp_semantics=semantics,source_namespace=namespace)
                groups={}
                for row in records:
                    if start<=row['timestamp']<end:groups.setdefault(row['timestamp'],[]).append(row)
                for stamp,rows in groups.items():
                    content=digest(ordered(dataset,rows))
                    candidate=(received,source_id,reference)
                    if content not in index or candidate[:2]<index[content][:2]:index[content]=candidate
            except Exception:
                warnings.append(dict(source_id=source_id,code='unreadable_or_invalid_source_receipt'))
        self._cache_key=key;self._cache=(index,warnings)
        return self._cache

    def _load(self,dataset,pair,start,end):
        start,end=bounds(start,end)
        if end>self.as_of:raise ValueError('query_end_exceeds_knowledge_time')
        meta,error=self._metadata(pair)
        if error:raise ResearchDataError('metadata_unknown_as_of',pair_id=pair,reason=error)
        columns=[*dataset_schema(dataset).names,*SOURCE_COLUMNS,'query_quality_issue_ids']
        rows=[];partitions=[];outside=0
        try:
            for month in self.store.months(pair,dataset):
                if month<start.strftime('%Y-%m') or month>end.strftime('%Y-%m'):continue
                table=self.store.read(pair,dataset,month);partitions.append(month)
                for row in table.to_pylist():
                    if start<=row['timestamp']<end and (self.record_selection is None or key_text(dataset,row) in self.record_selection.get((dataset,pair),set())):rows.append(row)
                    else:outside+=1
        except Exception:raise ResearchDataError('invalid_or_conflicting_partition',pair_id=pair,dataset=dataset) from None
        if any(r['timestamp']>self.as_of or dataset=='prices_1m' and (not r['is_complete'] or r['timestamp']+candles.MINUTE>self.as_of) for r in rows):
            raise ResearchDataError('incomplete_or_future_observation',pair_id=pair,dataset=dataset)
        if dataset=='orderbook' and rows:
            try:books.validate_snapshot(rows)
            except ValueError:raise ResearchDataError('invalid_book_snapshot',pair_id=pair) from None
        issues=self.quality_issues(pair,start,end,dataset);refs={};warnings=[]
        issue_ids_json=json.dumps([i['issue_id'] for i in issues],separators=(',',':'))
        if rows:
            try:index,warnings=self._index(pair,dataset,start,end,meta)
            except ResearchDataError as error:
                if error.code!='source_binding_missing':raise
                index,warnings={},[]
            groups={}
            archive={}
            if dataset=='prices_1m':
                from .historical_population import references
                try:archive=references(self.data_dir,pair,start,end,self.as_of,self.source_selection)
                except Exception:raise ResearchDataError('invalid_historical_provenance',pair_id=pair) from None
            elif dataset in ('quotes_1m','orderbook'):
                from .historical_execution import references
                try:archive=references(self.data_dir,pair,dataset,start,end,self.as_of,self.source_selection,cache=self._historical_sources)
                except Exception:raise ResearchDataError('invalid_historical_execution_provenance',pair_id=pair) from None
            for row in rows:groups.setdefault(row['timestamp'],[]).append(row)
            for stamp,group in groups.items():
                match=None
                if archive:
                    for original,reference in archive.get(stamp,()):
                        if (len(group)==1 and group[0]==original if isinstance(original,dict) else ordered(dataset,group)==ordered(dataset,original)):
                            match=(None,None,reference);break
                if match is None:match=index.get(digest(ordered(dataset,group)))
                if match is None:raise ResearchDataError('missing_or_inconsistent_provenance',pair_id=pair,dataset=dataset,timestamp=candles.sql_time(stamp))
                ref=match[2];refs[ref['source_id']]=ref
                for row in group:row.update(ref,query_quality_issue_ids=issue_ids_json)
        rows.sort(key=lambda r:tuple(r[k] for k in LOGICAL_KEYS[dataset]))
        result=frame(rows,columns,('timestamp','source_received_at'))
        saved=books.checkpoint(self.data_dir,pair,dataset)
        if saved and instant(saved['updated_at'])>self.as_of:saved=None;checkpoint_status='unknown_unversioned_checkpoint_after_cutoff'
        else:checkpoint_status='recorded' if saved else 'unavailable'
        result.attrs=dict(pair_id=pair,dataset=dataset,start=candles.sql_time(start),end_exclusive=candles.sql_time(end),as_of=candles.sql_time(self.as_of),
            status='verified' if rows else 'unavailable',outside_requested_range=outside,invalid_records_excluded=0,partitions=partitions,
            sources=[dict(v) for v in refs.values()],source_warnings=[dict(v) for v in warnings],quality_issues=issues,
            latest_observation=candles.sql_time(rows[-1]['timestamp']) if rows else None,checkpoint=saved,checkpoint_status=checkpoint_status,
            units={f.name:(f.metadata or {}).get(b'unit',b'').decode() for f in dataset_schema(dataset)},
            missing_policy='No filling or invalid-record dropping; unverifiable selected records fail closed. Quality issue IDs apply to the query window.')
        return result

    def load_partition(self,pair,split,manifest_id)->pd.DataFrame:
        from .historical_readiness import load_partition
        return load_partition(self,pair,split,manifest_id)

    def continuity(self,request):
        """Inspect event-time/grid readiness offline; never mutate observations."""
        from .market_continuity import MarketDataContinuity
        return MarketDataContinuity(self).evaluate(request)

    def load_for_research(self,request):
        """Fail closed on explicit continuity limits before releasing raw data."""
        from .market_continuity import MarketDataContinuity
        return MarketDataContinuity(self).release(request)

    def generate_partition_plan(self,request):
        from .partition_algorithm import PartitionAlgorithm
        return PartitionAlgorithm(self).generate_partition_plan(request)

    def load_partition_phase(self,manifest_id,phase,asset,**options):
        from .partition_manifest import load_plan_phase
        return load_plan_phase(self,manifest_id,phase,asset,**options)

    def validate_partition_plan(self,manifest_id,**options):
        from .partition_manifest import validate_plan
        return validate_plan(self,manifest_id,**options)

    def freeze_partition_plan(self,manifest_id):
        from .partition_governance import freeze_plan
        return freeze_plan(self,manifest_id)

    def partition_governance_status(self,manifest_id):
        from .partition_governance import governance_status
        return governance_status(self,manifest_id)

    def record_partition_feedback(self,manifest_id,reason):
        from .partition_governance import record_use
        return record_use(self,manifest_id,feedback=reason)

    def load_prices(self,pair,start,end)->pd.DataFrame:return self._load('prices_1m',pair,start,end)
    def load_quotes(self,pair,start,end)->pd.DataFrame:return self._load('quotes_1m',pair,start,end)
    def load_orderbooks(self,pair,start,end)->pd.DataFrame:return self._load('orderbook',pair,start,end)

    def load_fee_schedules(self,pair,start=None,end=None)->pd.DataFrame:
        _,error=self._metadata(pair)
        if error:raise ResearchDataError('metadata_unknown_as_of',pair_id=pair,reason=error)
        start=instant(start) if start is not None else datetime.min.replace(tzinfo=timezone.utc)
        end=instant(end) if end is not None else self.as_of+timedelta(microseconds=1)
        if start>=end or end>self.as_of+timedelta(microseconds=1):raise ValueError('invalid_fee_retrieval_window')
        columns=['pair_id','kraken_pair_id','version','retrieved_at','effective_at','source_url','account_source_url','category','tier_currency','tier_label','conditions_json','maker_pct','taker_pct','account_specific','account_maker_pct','account_taker_pct','applicable_tier','fee_applicability','verification_status','source_id','source_sha256','source_namespace']
        rows=[]
        for version in self.audit.versions('fees'):
            doc=self.audit.get('fees',version);retrieved=instant(doc['retrieved_at'])
            if pair not in doc['pair_fees'] or not start<=retrieved<end or retrieved>self.as_of:continue
            namespaces={json.loads(p.read_text())['source'] for p in (self.data_dir/'ohlcv_collection/source.json',self.data_dir/'book_collection/source.json') if p.exists()}
            if namespaces and doc['source_namespace'] not in namespaces:raise ResearchDataError('fee_source_namespace_mismatch',version=version)
            value=fee_view(self.data_dir,version,pair)
            for table in value.public_tables:
                for tier in table.tiers:
                    rows.append(dict(pair_id=pair,kraken_pair_id=value.kraken_pair_id,version=version,retrieved_at=value.retrieved_at,effective_at=value.effective_at,source_url=value.source_url,
                        account_source_url=value.account_source_url,category=table.category,tier_currency=table.tier_currency,tier_label=tier.label,conditions_json=json.dumps(payload(tier.conditions),separators=(',',':')),
                        maker_pct=tier.maker_pct,taker_pct=tier.taker_pct,account_specific=value.account_specific,account_maker_pct=value.account_maker_pct,account_taker_pct=value.account_taker_pct,
                        applicable_tier=value.applicable_tier,fee_applicability='account_rates_available_tier_unknown' if value.account_specific else 'unknown_account_applicability',verification_status=value.verification_status,
                        source_id='cost_infrastructure/fees/'+version+'.json',source_sha256=version,source_namespace=doc['source_namespace']))
        rows.sort(key=lambda r:(r['retrieved_at'],r['version'],r['category'],r['tier_label']))
        result=frame(rows,columns,('retrieved_at','effective_at'))
        result.attrs=dict(status='source_available' if rows else 'unavailable',as_of=candles.sql_time(self.as_of),selection='retrieval time, not inferred effective history',
            missing_policy='No tier selection, zero defaults or present-day substitution; fee percentages are percentage points.')
        return result

    def load_quality_report(self,pair,start,end)->QualityReport:
        start,end=bounds(start,end)
        if end>self.as_of:raise ValueError('query_end_exceeds_knowledge_time')
        _,error=self._metadata(pair)
        if error:raise ResearchDataError('metadata_unknown_as_of',pair_id=pair,reason=error)
        datasets={}
        for dataset in ('prices_1m','quotes_1m','orderbook'):
            stats,times,bad=inventory(self.data_dir,pair,dataset,start,end,self.as_of)
            stats['inventory_scope']='Current physical storage; verified_requested_* fields apply receipt knowledge cutoff'
            try:
                verified=self._load(dataset,pair,start,end)
                stats['verified_requested_rows']=len(verified)
                stats['verified_requested_timestamps']=verified['timestamp'].nunique()
                stats['query_verification_error']=None
            except ResearchDataError as error:
                verified=None;stats['verified_requested_rows']=None;stats['verified_requested_timestamps']=None;stats['query_verification_error']=error.code
            if dataset=='prices_1m':
                price_start=candles.minute_start(start)+(candles.MINUTE if start!=candles.minute_start(start) else timedelta(0))
                price_end=candles.minute_start(end)
                source=availability(self.data_dir,pair)
                if source and instant(source['observed_at'])>self.as_of:source=None
                known_times=verified['timestamp'].to_list() if verified is not None else []
                stats['coverage']=minute_coverage(known_times,price_start,price_end,self.as_of,invalid_times=bad,source=source) if price_start<price_end else None
                if (stats['integrity_unknown'] or verified is None) and stats['coverage']:
                    stats['coverage']['coverage_pct']=None;stats['coverage']['missing_minutes']=None
            else:stats['sampling']='Physical observations; no assumed historical minute coverage'
            datasets[dataset]=stats
        return QualityReport(pair_id=pair,start=start,end_exclusive=end,as_of=self.as_of,datasets=datasets,
            quality_issues=tuple(self.quality_issues(pair,start,end)),limitation='Counts describe persisted storage; use market loaders to enforce exact source verification. Current issue resolution is not reconstructed historically.')


def load_prices(pair,start,end,*,data_dir=None,as_of=None):return ResearchData(data_dir,as_of=as_of).load_prices(pair,start,end)
def load_quotes(pair,start,end,*,data_dir=None,as_of=None):return ResearchData(data_dir,as_of=as_of).load_quotes(pair,start,end)
def load_orderbooks(pair,start,end,*,data_dir=None,as_of=None):return ResearchData(data_dir,as_of=as_of).load_orderbooks(pair,start,end)
def load_fee_schedules(pair,start=None,end=None,*,data_dir=None,as_of=None):return ResearchData(data_dir,as_of=as_of).load_fee_schedules(pair,start,end)
def load_market_metadata(pair,as_of=None,*,data_dir=None):return ResearchData(data_dir,as_of=as_of).load_market_metadata(pair)
def load_quality_report(pair,start,end,*,data_dir=None,as_of=None)->QualityReport:return ResearchData(data_dir,as_of=as_of).load_quality_report(pair,start,end)
