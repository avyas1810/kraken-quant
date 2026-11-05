"""Point-in-time facts from retained receipts, never reconstructed status history."""
from datetime import timedelta
import json
from pathlib import Path

from . import asset_catalogue as catalogue
from . import ohlcv_collector as ohlc
from . import book_collector as books
from .cost_inputs import InputService,ordered
from .cost_contracts import CostRequest,PreparationConfig
from .cost_storage import digest
from .history_coverage import minute_coverage,intervals
from .metadata_store import connect_metadata
from .parquet_store import MonthlyParquetStore


class PointInTimeEvidence:
    def __init__(self,data_dir,as_of,policy,*,history_pins=None):
        self.data_dir=Path(data_dir);self.as_of=ohlc.utc_timestamp(as_of);self.policy=policy
        self.store=MonthlyParquetStore(data_dir);self.receipts=[];self.errors=[];self.history_pins=history_pins or {}
        with connect_metadata(data_dir,readonly=True) as db:
            self.assets={r[0]:dict(zip(catalogue.COLUMNS,r)) for r in db.execute('SELECT '+','.join(catalogue.COLUMNS)+' FROM assets')}
        for path in sorted((self.data_dir/'catalogue_discovery').glob('*.json')):
            if path.name=='latest.json':continue
            try:
                receipt=json.loads(path.read_text())
                if ohlc.read_time(receipt['observed_at'])<=self.as_of:
                    self.receipts.append((receipt,path))
            except Exception:
                self.errors.append('unreadable_discovery_evidence')
        self.receipts.sort(key=lambda item:(item[0]['observed_at'],item[0]['run_id']))

    def candidates(self):
        # Historical union, including formerly observed/offline pairs. Never select
        # a newly discovered future asset merely because today's SQL contains it.
        return sorted({r['pair_id'] for receipt,_ in self.receipts for r in receipt['records'] if r.get('quote_asset')=='AUD'})

    def metadata(self,pair):
        if self.errors:return None,'discovery_provenance_unreadable'
        candidates=[(receipt,path,row) for receipt,path in self.receipts for row in receipt['records'] if row['pair_id']==pair]
        if not candidates:return None,'metadata_unavailable_as_of'
        receipt,path,row=candidates[-1]
        try:
            if receipt.get('schema_version')!=1:raise ValueError()
            sql=self.assets.get(pair)
            if sql and sql['kraken_pair_id']!=row['kraken_pair_id']:raise ValueError()
            source=next(s for s in receipt['raw_sources'] if s['asset_class']=='currency' and row['kraken_pair_id'] in s['pairs'])
            # Reuse approved parsing only for this accepted record. The broader
            # unresolved class/venue records are not reclassified or modified.
            parsed=catalogue.parse_sources([dict(asset_class='currency',pairs={row['kraken_pair_id']:source['pairs'][row['kraken_pair_id']]})],receipt['native_assets'],receipt['display_assets'],ohlc.read_time(receipt['observed_at']))
            if len(parsed['records'])!=1 or parsed['records'][0]!=row:raise ValueError()
            result={k:row.get(k) for k in catalogue.COLUMNS}
            result.update(observed_at=receipt['observed_at'],market_type='spot',receipt_id=str(path.relative_to(self.data_dir)).replace('\\','/'),receipt_hash=digest(receipt),
                status_validity='observed_instant_only; transitions_not_recorded')
            return result,None
        except Exception:
            return None,'metadata_identity_or_provenance_invalid'

    def _namespace(self,folder):
        path=self.data_dir/folder/'source.json'
        try:
            return json.loads(path.read_text())['source'] if path.exists() else None
        except (ValueError,KeyError,TypeError,OSError):
            return None

    def operational_evidence(self,pair):
        with connect_metadata(self.data_dir,readonly=True) as db:
            issues=[dict(zip(('issue_id','dataset','issue_type','detected_at','observation_at','period_start','period_end'),row)) for row in db.execute(
                'SELECT issue_id,dataset,issue_type,detected_at,observation_at,period_start,period_end FROM data_quality_issues WHERE pair_id=? AND detected_at<=? ORDER BY issue_id',(pair,ohlc.sql_time(self.as_of)))]
        return dict(known_quality_issues=issues,checkpoint_history='UNKNOWN_unversioned_checkpoint_not_used',
            limitation='resolution/status transition history is unavailable; current resolution and future checkpoint updates are not applied retrospectively')

    def history(self,pair,metadata):
        if not metadata:return dict(error='metadata_unavailable_for_history_identity',evidence=[])
        end=ohlc.minute_start(self.as_of);start=end-timedelta(days=self.policy.history_days)
        namespace=self._namespace('ohlcv_collection')
        if namespace is None:return dict(error='price_source_provenance_missing',evidence=[])
        try:
            months=self.store.months(pair,'prices_1m')
            # Frozen evaluations depend only on their explicitly pinned windows.
            # Later archive additions outside those windows are not past evidence.
            history_pin=self.history_pins.get(pair)
            if history_pin and not history_pin.get('error'):
                windows=history_pin.get('selection_intervals',history_pin['coverage']['complete_intervals'])
                month_windows=[(ohlc.read_time(v['start']).strftime('%Y-%m'),(ohlc.read_time(v['end_exclusive'])-ohlc.MINUTE).strftime('%Y-%m')) for v in windows]
                months=tuple(m for m in months if any(a<=m<=b for a,b in month_windows))
            physical={r['timestamp']:r for month in months for r in self.store.read(pair,'prices_1m',month).to_pylist() if r['is_complete'] and r['timestamp']+ohlc.MINUTE<=self.as_of}
        except Exception:
            return dict(error='invalid_or_conflicting_price_partition',evidence=[])
        known={};sources={};conflicts=False
        pin=self.history_pins.get(pair)
        selected=None
        if pin and not pin.get('error'):
            selected=pin.get('selection_intervals',pin['coverage']['complete_intervals'])
            if sum(int((ohlc.read_time(r['end_exclusive'])-ohlc.read_time(r['start']))/ohlc.MINUTE) for r in selected)!=pin['known_minutes']:
                return dict(error='legacy_history_selection_unavailable',evidence=pin.get('evidence',[]))
            selected=[(ohlc.read_time(r['start']),ohlc.read_time(r['end_exclusive'])) for r in selected]
        for path in sorted((self.data_dir/'ohlcv_collection/responses'/pair).glob('*.json')):
            if selected is not None and str(path.relative_to(self.data_dir)).replace('\\','/') not in pin['sources']:continue
            try:
                receipt=json.loads(path.read_text())
            except (ValueError,OSError):
                return dict(error='unreadable_history_provenance',evidence=[str(path.relative_to(self.data_dir))])
            try:
                received=ohlc.read_time(receipt['fetched_at'])
                if received>self.as_of:continue
                if receipt.get('source')!=namespace or receipt['params']['pair']!=metadata['kraken_pair_id']:continue
                parsed=ohlc.parse_ohlc(receipt['body'],metadata,received)
                if parsed['invalid_observations']:continue
                matches=[]
                for row in parsed['records']:
                    stamp=row['timestamp']
                    if stamp+ohlc.MINUTE>self.as_of:continue
                    if selected is not None and not any(a<=stamp<b for a,b in selected):continue
                    if stamp not in physical:
                        continue  # Raw responses may include rows outside a prior collection request.
                    if physical[stamp]!=row:
                        conflicts=True
                    else:
                        if stamp in known and known[stamp]!=row:conflicts=True
                        known[stamp]=row;matches.append(stamp)
                if matches:
                    sources[str(path.relative_to(self.data_dir)).replace('\\','/')]=dict(hash=digest(receipt),received_at=ohlc.sql_time(received),timestamp_semantics='UTC_candle_open; knowledge_time_is_fetched_at')
            except (ValueError,KeyError,TypeError):
                continue
        if conflicts:return dict(error='known_history_dependency_changed_or_missing',evidence=sorted(sources))
        if not known:return dict(error='no_verified_history_known_as_of',evidence=sorted(sources))
        times=sorted(known)
        coverage=minute_coverage(times,start,end,self.as_of)
        span=times[-1]-times[0]+ohlc.MINUTE
        return dict(error=None,first=ohlc.sql_time(times[0]),last=ohlc.sql_time(times[-1]),span_days=span.total_seconds()/86400,
            whole_span_days=span.days,observed_minute_days=len(times)/1440,known_minutes=len(times),coverage=coverage,
            selection_intervals=[dict(start=ohlc.sql_time(a),end_exclusive=ohlc.sql_time(b),minutes=int((b-a)/ohlc.MINUTE)) for a,b in intervals(times)],
            records_hash=digest(ordered('prices_1m',list(known.values()))),sources=sources,evidence=sorted(sources))

    def raw_inputs(self,pair,metadata):
        """Expose source references for future algorithms; never derive metrics."""
        result=dict(quote=None,orderbook=None,fee=None,issues=[],evidence=[])
        if not metadata:
            result['issues'].append('metadata_unavailable_as_of');return result
        namespace=self._namespace('book_collection')
        if namespace:
            for path in sorted((self.data_dir/'book_collection/responses'/pair).glob('*.json')):
                try:
                    receipt=json.loads(path.read_text())
                except (ValueError,OSError):
                    result['issues'].append('unreadable_book_provenance');continue
                try:
                    received=ohlc.read_time(receipt['finished_at']);stamp=ohlc.read_time(receipt['started_at'])
                    if received>self.as_of or stamp>self.as_of:continue
                    if receipt.get('source')!=namespace or receipt['kraken_pair_id']!=metadata['kraken_pair_id']:continue
                    parsed=books.parse_depth(receipt['body'],metadata,stamp,received,receipt['provenance']['requested_depth'])
                    if parsed['provenance']!=receipt['provenance']:continue
                    base=dict(pair_id=pair,response_id=str(path.relative_to(self.data_dir)).replace('\\','/'),response_hash=digest(receipt),observed_at=ohlc.sql_time(stamp),received_at=ohlc.sql_time(received),
                        timestamp_semantics=parsed['provenance']['timestamp_semantics'],latency_seconds=parsed['provenance']['latency_seconds'])
                    for dataset,key in (('quotes_1m','quote'),('orderbook','orderbook')):
                        rows=[r for r in self.store.read(pair,dataset,stamp.strftime('%Y-%m')).to_pylist() if r['timestamp']==stamp]
                        if rows and ordered(dataset,rows)==ordered(dataset,parsed[dataset]) and (result[key] is None or result[key]['observed_at']<base['observed_at']):
                            result[key]={**base,'dataset':dataset,'records_hash':digest(ordered(dataset,rows))}
                except (ValueError,KeyError,TypeError):
                    continue
        for name in ('quote','orderbook'):
            if result[name] is None:result['issues'].append('missing_'+name+'_as_of')
            else:
                result['evidence'].append(result[name]['response_id'])
                if (self.as_of-ohlc.read_time(result[name]['observed_at'])).total_seconds()>self.policy.market_max_age_seconds:
                    result['issues'].append('stale_'+name)
        try:
            request=CostRequest(pair,self.policy.order_side,self.policy.proposed_notional_aud,self.policy.execution_type,self.as_of,'screening-inputs-only-v1')
            config=PreparationConfig(max_market_age_seconds=self.policy.market_max_age_seconds,max_fee_age_seconds=self.policy.fee_max_age_seconds)
            fee,error,warnings=InputService(self.data_dir)._fee(request,self._namespace('ohlcv_collection'),config)
            result['issues'].extend(warnings)
            if error:result['issues'].append(error)
            if fee:
                result['fee']=dict(version=fee.version,source=fee.source_url,retrieved_at=ohlc.sql_time(fee.retrieved_at),effective_at=ohlc.sql_time(fee.effective_at) if fee.effective_at else None,
                    verification_status=fee.verification_status,account_specific=fee.account_specific,applicable_tier=fee.applicable_tier)
                result['evidence'].append('cost_infrastructure/fees/'+fee.version+'.json')
        except Exception:
            result['issues'].append('fee_provenance_unavailable')
        result['issues']=sorted(set(result['issues']))
        result['evidence']=sorted(set(result['evidence']))
        result['aligned_quote_book']=bool(result['quote'] and result['orderbook'] and result['quote']['response_id']==result['orderbook']['response_id'])
        if result['quote'] and result['orderbook'] and not result['aligned_quote_book']:
            result['issues'].append('quote_book_snapshot_mismatch')
        return result


def resolve_input_reference(data_dir,reference,as_of):
    """Read exact referenced rows for future approved algorithms, with no metrics."""
    if reference['dataset'] not in ('quotes_1m','orderbook'):
        raise ValueError('Unsupported raw market reference')
    stamp=ohlc.read_time(reference['observed_at'])
    if stamp>as_of or ohlc.read_time(reference['received_at'])>as_of:
        raise ValueError('Future input reference prohibited')
    root=Path(data_dir);source=root/reference['response_id']
    if not source.resolve().is_relative_to(root.resolve()) or digest(json.loads(source.read_text()))!=reference['response_hash']:
        raise ValueError('Source dependency changed or invalid')
    rows=[r for r in MonthlyParquetStore(data_dir).read(reference['pair_id'],reference['dataset'],stamp.strftime('%Y-%m')).to_pylist() if r['timestamp']==stamp]
    if digest(ordered(reference['dataset'],rows))!=reference['records_hash']:
        raise ValueError('Referenced market rows changed or missing')
    return tuple(rows)
