"""Read public split ZIP members with standard, verified HTTP byte ranges.

Only selected AUD trade members are downloaded. Each member's ZIP CRC and local
SHA256 are checked; no whole-archive checksum is claimed for partial downloads.
"""
import io,json,urllib.request,zipfile,bisect,zlib
from pathlib import Path
from .historical_population import immutable,encoded,sha,ALIASES
BASE='https://assets.kraken.com/marketing/institutions/Kraken_Trades_Full.zip.part'
class PublicSplitArchive(io.RawIOBase):
 def __init__(self,root):
  self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.position=0;self.starts=[0];self.sizes=[]
  for n in range(14):
   request=urllib.request.Request(BASE+f'{n:02d}',headers={'Range':'bytes=0-0','User-Agent':'Mozilla/5.0'})
   with urllib.request.urlopen(request,timeout=45) as response:
    if response.status!=206:raise ValueError('public_range_not_supported')
    total=int(response.headers['Content-Range'].rsplit('/',1)[1]);response.read(1)
   self.sizes.append(total);self.starts.append(self.starts[-1]+total)
 def seekable(self):return True
 def readable(self):return True
 def tell(self):return self.position
 def seek(self,offset,whence=0):
  value=offset if whence==0 else self.position+offset if whence==1 else self.starts[-1]+offset
  if value<0:raise ValueError('negative_seek')
  self.position=value;return value
 def read(self,n=-1):
  if n<0:n=self.starts[-1]-self.position
  result=[]
  while n and self.position<self.starts[-1]:
   part=bisect.bisect_right(self.starts,self.position)-1;local=self.position-self.starts[part]
   count=min(n,self.sizes[part]-local);name=f'{part:02d}-{local}-{count}.bin';path=self.root/name
   if path.exists():content=path.read_bytes()
   else:
    request=urllib.request.Request(BASE+f'{part:02d}',headers={'Range':f'bytes={local}-{local+count-1}','User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(request,timeout=60) as response:
     if response.status!=206 or response.headers.get('Content-Range')!=f'bytes {local}-{local+count-1}/{self.sizes[part]}':raise ValueError('public_range_response_mismatch')
     content=response.read(count+1)
    if len(content)!=count:raise ValueError('truncated_public_range')
    immutable(path,content)
   result.append(content);self.position+=count;n-=count
  return b''.join(result)

def download_members(data_dir,assets):
 root=Path(data_dir)/'historical_imports/official_trades';root.mkdir(parents=True,exist_ok=True)
 source=PublicSplitArchive(root/'ranges');records=[]
 with zipfile.ZipFile(source) as archive:
  manifest=json.loads(archive.read('MANIFEST.json'))
  if manifest.get('product')!='trades' or manifest.get('schema_version')!=2 or manifest.get('has_header') is not True or manifest.get('columns')!=['timestamp','price','volume','type','order_type','misc','trade_id']:raise ValueError('unsupported_official_trade_archive')
  immutable(root/'MANIFEST.json',encoded(manifest));print('TRADE ARCHIVE',manifest,flush=True)
  for asset in assets:
   name=ALIASES.get(asset['base_asset'],asset['base_asset'])+'AUD.csv';target=root/name
   info=archive.getinfo(name)
   if target.exists():
    raw=target.read_bytes()
    if len(raw)!=info.file_size or zlib.crc32(raw)&0xffffffff!=info.CRC:raise ValueError('existing_trade_member_crc_mismatch')
   else:
    raw=archive.read(name);immutable(target,raw)
   doc=dict(pair_id=asset['pair_id'],kraken_pair_id=asset['kraken_pair_id'],member=name,bytes=len(raw),sha256=sha(raw),zip_crc32=f'{info.CRC:08x}',compressed_bytes=info.compress_size,source_url='https://assets.kraken.com/marketing/institutions/Kraken_Trades_Full.zip',range_parts=source.sizes,integrity='HTTPS public source; ZIP member CRC and selected-member SHA256; whole-archive SHA256 NOT verified')
   immutable(root/(name+'.receipt.json'),encoded(doc));records.append(doc);print('TRADE MEMBER',asset['pair_id'],len(raw),flush=True)
 return records

def recover_precision(data_dir,asset,ohlc_file,*,prepare_only=False):
 """Recover only unrepresentable archive candles from exact official trade values.

No rounding: OHLC are actual first/extreme/last trades; volume is their exact sum.
Original malformed OHLCVT records remain immutable source-quality evidence.
"""
 import csv
 from decimal import Decimal,localcontext
 from datetime import datetime,timezone
 from .historical_population import import_csv
 root=Path(data_dir);pair=asset['pair_id'];name=ALIASES.get(asset['base_asset'],asset['base_asset'])+'AUD.csv'
 member=root/'historical_imports/official_trades'/name;receipt=member.with_name(name+'.receipt.json')
 doc=json.loads(receipt.read_text());raw=member.read_bytes()
 if sha(raw)!=doc['sha256']:raise ValueError('official_trade_member_changed')
 wanted=set()
 with Path(ohlc_file).open() as source:
  for values in csv.reader(source):
   for v in values[1:6]:
    number=Decimal(v);digits=list(number.as_tuple().digits);exponent=number.as_tuple().exponent
    while digits and digits[-1]==0:digits.pop();exponent+=1
    if exponent< -18:wanted.add(int(values[0]));break
 if not wanted:return dict(pair_id=pair,recovered=0,required=0)
 groups={};seen={};last=None
 with localcontext() as context:
  context.prec=80
  with member.open() as source:
   for v in csv.DictReader(source):
    stamp=Decimal(v['timestamp'])
    if not stamp.is_finite() or stamp<0 or last is not None and stamp<last:raise ValueError('invalid_official_trade_timestamp_order')
    last=stamp;minute=int(stamp)//60*60
    if minute not in wanted:continue
    ident=int(v['trade_id']);value=(stamp,Decimal(v['price']),Decimal(v['volume']))
    if ident in seen:
     if seen[ident]!=value:raise ValueError('conflicting_official_trade_id')
     continue
    seen[ident]=value;price,qty=value[1:]
    if not price.is_finite() or price<=0 or not qty.is_finite() or qty<0:raise ValueError('invalid_official_trade_value')
    if minute not in groups:groups[minute]=[price,price,price,price,qty,1]
    else:
     row=groups[minute];row[1]=max(row[1],price);row[2]=min(row[2],price);row[3]=price;row[4]+=qty;row[5]+=1
  content=''.join(','.join(map(str,[minute,*values]))+'\n' for minute,values in sorted(groups.items())).encode()
 target=root/'historical_imports/recovered'/sha(content)/(name[:-4]+'_1.csv');immutable(target,content)
 deps=[dict(path=member.relative_to(root).as_posix(),sha256=doc['sha256']),dict(path=receipt.relative_to(root).as_posix(),sha256=sha(receipt.read_bytes()))]
 if prepare_only:return dict(pair_id=pair,required=len(wanted),recovered=len(groups),unavailable_minutes=sorted(wanted-set(groups)),member=str(target),dependencies=deps,source_sha256=doc['sha256'])
 records=import_csv(root,pair,target,source_url=doc['source_url'],archive_sha256=doc['sha256'],coverage_end=datetime(2026,10,1,tzinfo=timezone.utc),source_kind='kraken_official_trade_archive_ohlcv',dependencies=deps)
 return dict(pair_id=pair,required=len(wanted),recovered=len(groups),unavailable_minutes=sorted(wanted-set(groups)),source_member=doc)
