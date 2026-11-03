"""Inspect real stored Kraken inputs, offline, without signals or calculations."""
import argparse
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

if __package__ in (None,''):sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from kraken_quant.research_data import ResearchData
from kraken_quant.cost_contracts import CostRequest
from kraken_quant.cost_inputs import InputService
from kraken_quant.cost_algorithms import CostAlgorithms
from kraken_quant.db_environment import load_config


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data-dir');p.add_argument('--days',type=int,default=7)
    args=p.parse_args(argv)
    if not 1<=args.days<=3660:raise ValueError('bounded_days_required')
    directory=load_config(data_dir=args.data_dir).data_dir
    reader=ResearchData(directory);end=reader.as_of;start=end-timedelta(days=args.days)
    for label,method in (('Completed BTC/AUD candles',reader.load_prices),('Original BTC/AUD quotes',reader.load_quotes)):
        data=method('BTC-AUD',start,end)
        print(label,':',len(data),'rows;',data.attrs['status'])
        print(data.head(2).to_string(index=False))
        print('Source references:',len(data.attrs['sources']))
    levels=reader.load_orderbooks('BTC-AUD',start,end)
    print('Original book-level rows:',len(levels),'; snapshots:',levels['timestamp'].nunique())
    if len(levels):print(levels[levels['timestamp']==levels['timestamp'].max()].to_string(index=False))
    fees=reader.load_fee_schedules('BTC-AUD')
    print('Available fee-source tier rows:',len(fees),'; no applicable tier selected')
    print(fees[['retrieved_at','category','tier_label','maker_pct','taker_pct','fee_applicability']].head(8).to_string(index=False))
    quality=reader.load_quality_report('BTC-AUD',start,end)
    print('Candle coverage and missing intervals:',json.dumps(quality.datasets['prices_1m']['coverage'],indent=2))
    request=CostRequest('BTC-AUD','buy',Decimal('1000'),'taker',end,'user-algorithm-reserved-after-m10-v1')
    prepared=InputService(directory).prepare(request)
    print('Cost raw-input preparation:',prepared.status,'missing:',prepared.missing_inputs,'warnings:',prepared.warnings)
    try:
        CostAlgorithms().calculate_total_cost(prepared.inputs)
    except NotImplementedError as error:
        print('Expected algorithm boundary:',str(error))
    print('No financial metric, signal, cost estimate, strategy or trade was calculated.')
    return 0


if __name__=='__main__':raise SystemExit(main())
