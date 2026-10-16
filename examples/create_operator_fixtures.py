"""Create explicitly synthetic post-lock paper fixtures; never research evidence."""
import argparse,json
from pathlib import Path
import pandas as pd


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args()
    root=Path(args.output);root.mkdir(parents=True,exist_ok=False)
    dates=pd.date_range('2026-10-01',periods=15,freq='D',tz='UTC')
    close=[100,101,102,103,102,101,100,99,100,101,102,103,102,101,100]
    frame=pd.DataFrame({'timestamp':dates,'open':close,'high':[v+1 for v in close],'low':[v-1 for v in close],'close':close,'volume':10000})
    frame.to_csv(root/'BTC_USD.csv',index=False)
    (root/'fixture_provenance.json').write_text(json.dumps({'source':'SYNTHETIC SOFTWARE FIXTURE','prospective_evidence':False,'purpose':'Accelerated paper accounting/recovery demonstration only'},indent=2),encoding='utf-8')
    print(str(root/'BTC_USD.csv'))

if __name__=='__main__':main()
