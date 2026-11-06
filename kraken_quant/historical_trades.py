"""Bounded public Kraken trade replay into exact traded-minute OHLCV.

This is source-record aggregation, not an execution or financial cost algorithm.
No zero filling, inferred trades, prices, VWAP or historical books are produced.
"""
from datetime import datetime,timezone
from decimal import Decimal,localcontext
from pathlib import Path
import json,time
from . import market_data
from .historical_population import immutable,encoded,sha,dependency,import_csv,ALIASES
from .ohlcv_collector import collection_universe
UTC=timezone.utc

def aggregate(pages,pair,start,end):
 """Preserve trade-ID identity/order; group actual trades by original UTC minute."""
 trades={}
 for page in pages:
  result=page['body']['result'];keys=set(result)-{'last'}
  if keys!={pair}:raise ValueError('trade_pair_identity_mismatch')
  for v in result[pair]:
   if len(v)!=7 or type(v[6]) is not int:raise ValueError('trade_identity_required')
   value=(Decimal(v[2]),Decimal(v[0]),Decimal(v[1]))
   if value[0]<0 or not value[0].is_finite() or not value[1].is_finite() or value[1]<=0 or not value[2].is_finite() or value[2]<0:raise ValueError('invalid_trade')
   if v[6] in trades and trades[v[6]]!=value:raise ValueError('conflicting_trade_id')
   trades[v[6]]=value
 groups={}
 for ident,(stamp,price,qty) in sorted(trades.items(),key=lambda item:(item[1][0],item[0])):
  minute=int(stamp)//60*60
  if not start<=minute<end:continue
  groups.setdefault(minute,[]).append((price,qty))
 lines=[]
 with localcontext() as context:
  context.prec=80
  for stamp,values in sorted(groups.items()):
   prices=[v[0] for v in values]
   lines.append(','.join(map(str,(stamp,prices[0],max(prices),min(prices),prices[-1],sum((v[1] for v in values),Decimal(0)),len(values))))+'\n')
 return ''.join(lines).encode()

def fetch_gap(data_dir,pair,*,start,end,max_requests=1000,pace_seconds=1.2,transport=None,sleeper=time.sleep,prepare_only=False):
 """Replay cursor-bound pages; retry restarts reuse immutable original responses.

 End is a fixed, completed UTC minute. Returns incomplete when budget exhausted.
 Trading endpoints and credentials are never used. Missing history is not success.
 """
 if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None or start>=end or end>datetime.now(UTC).replace(second=0,microsecond=0):raise ValueError('invalid_trade_window')
 start=start.astimezone(UTC);end=end.astimezone(UTC)
 if start.second or start.microsecond or end.second or end.microsecond:raise ValueError('minute_aligned_trade_window_required')
 if max_requests<1 or pace_seconds<1:raise ValueError('bounded_paced_requests_required')
 assets,_,_=collection_universe(data_dir);asset=next((v for v in assets if v['pair_id']==pair),None)
 if asset is None:raise ValueError('unverified_aud_pair')
 root=Path(data_dir);native=asset['kraken_pair_id'];symbol=asset['base_asset']+'/AUD'
 cursor=str(int(start.timestamp())*1000000000);pages=[];deps=[];complete=False;requests=0
 folder=root/'historical_imports/trades'/pair;folder.mkdir(parents=True,exist_ok=True)
 fetch=transport or (lambda endpoint,params:market_data.public_get(endpoint,params,exact_numbers=True))
 for _ in range(max_requests):
  path=folder/(cursor+'.json')
  if path.exists():
   cached=json.loads(path.read_text());cached_result=cached['body']['result']
   if len(cached_result.get(symbol,[]))<1000 and datetime.fromisoformat(cached['received_at'].replace('Z','+00:00'))<end:
    path=folder/(cursor+'-asof-'+str(int(end.timestamp()))+'.json')
  if path.exists():page=json.loads(path.read_text())
  else:
   sleeper(pace_seconds);body=None
   for attempt in range(3):
    try:
     body=fetch('Trades',dict(pair=native,assetVersion=1,count=1000,since=cursor));requests+=1
     if not isinstance(body,dict) or body.get('error')!=[] or not isinstance(body.get('result'),dict):raise ValueError('trade_api_error')
     break
    except (OSError,ValueError):
     if attempt==2:raise
     sleeper(2**(attempt+1))
   page=dict(request_since=cursor,kraken_pair_id=native,received_at=datetime.now(UTC),body=body)
   immutable(path,encoded(page));page=json.loads(path.read_text())
  if page['request_since']!=cursor or page['kraken_pair_id']!=native:raise ValueError('trade_page_request_mismatch')
  result=page['body']['result'];keys=set(result)-{'last'}
  if keys!={symbol} or not isinstance(result[symbol],list):raise ValueError('trade_response_identity_mismatch')
  values=result[symbol];next_cursor=str(result['last'])
  if not next_cursor.isdigit() or int(next_cursor)<int(cursor):raise ValueError('invalid_trade_cursor')
  pages.append(page);deps.append(dict(path=path.relative_to(root).as_posix(),sha256=sha(path.read_bytes())))
  print(pair,'trade page',len(pages),'rows',len(values),'cursor',next_cursor,flush=True)
  if len(values)<1000 and datetime.fromisoformat(page['received_at'].replace('Z','+00:00'))>=end or values and max(Decimal(v[2]) for v in values)>=Decimal(str(end.timestamp())):
   complete=True;break
  if next_cursor==cursor:break
  cursor=next_cursor
 if not complete:return dict(pair_id=pair,status='incomplete_request_budget_or_source_exhausted',pages=len(pages),requests=requests)
 raw=aggregate(pages,symbol,int(start.timestamp()),int(end.timestamp()))
 member=root/'historical_imports/constructed'/sha(raw)/(ALIASES.get(asset['base_asset'],asset['base_asset'])+'AUD_1.csv')
 immutable(member,raw)
 if prepare_only:return dict(pair_id=pair,status='source_window_downloaded',member=str(member),pages=len(pages),requests=requests,dependencies=deps,coverage_end=end.isoformat())
 docs=import_csv(root,pair,member,source_url='https://api.kraken.com/0/public/Trades',archive_sha256=sha(encoded(deps)),coverage_end=end,source_kind='kraken_public_trades_ohlcv',dependencies=deps)
 return dict(pair_id=pair,status='source_window_replayed',start=start.isoformat(),end_exclusive=end.isoformat(),pages=len(pages),requests=requests,source_rows=sum(d['source_rows'] for d in docs),aggregation='exact trade ID ordered OHLCV, traded minutes only')
