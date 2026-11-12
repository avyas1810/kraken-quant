"""Read-only resource/replay benchmark; never runs a financial strategy.

Run from the repository: python -m examples.research_stream_benchmark --scope all
Output contains counts/hashes/timing/RSS only, never another market dataset.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from kraken_quant.research_data import ResearchData
from kraken_quant.research_stream import resident_bytes
from kraken_quant.historical_population import encoded


def benchmark(reader, pairs, start, end, *, batch_size=10000, manifest=None):
    began=time.monotonic(); cpu=time.process_time(); h=hashlib.sha256(); counts={p:0 for p in pairs}; previous=None
    stream=(reader.stream_partition_phase(manifest,'training',columns=['close','volume_base'],batch_size=batch_size)
        if manifest else reader.load_market_data(pairs,start,end,columns=['close','volume_base'],batch_size=batch_size))
    with stream:
        for batch in stream:
            # Consumer example: bounded event inspection, no price filling or trades.
            for row in batch.to_pylist():
                key=(row['timestamp'],row['pair_id'])
                if previous is not None and key <= previous:raise ValueError('replay_not_strictly_chronological')
                previous=key;counts[row['pair_id']]+=1
                h.update(encoded(row));h.update(b'\n')
    current,peak=resident_bytes()
    return dict(pairs=len(pairs),rows=sum(counts.values()),counts=counts,sha256=h.hexdigest(),
        seconds=round(time.monotonic()-began,3),cpu_seconds=round(time.process_time()-cpu,3),current_rss_bytes=current,peak_rss_bytes=peak,
        batch_size=batch_size,verification=stream.verification)


def main():
    p=argparse.ArgumentParser();p.add_argument('--scope',choices=['day','quarter','ten','all','partition'],default='day')
    p.add_argument('--batch-size',type=int,default=10000);p.add_argument('--output');p.add_argument('--data-dir',default='data')
    args=p.parse_args();root=Path(args.data_dir)
    enrolled=json.loads((root/'automated_collection/universe.json').read_text())['pairs']
    pairs=['BTC-AUD'] if args.scope in ('day','quarter') else enrolled[:10] if args.scope=='ten' else enrolled
    manifest=None
    if args.scope=='partition':
        manifest='46aae41665516ec51e0b9a9f5a2f42156362f6341ba23e23ec121918e7b219f6'
        doc=json.loads((root/'partition_plans/v1'/f'{manifest}.json').read_text());pairs=doc['configuration']['assets']
    result=benchmark(ResearchData(root),pairs,'2026-07-01','2026-07-02' if args.scope=='day' else '2026-10-01',batch_size=args.batch_size,manifest=manifest)
    result.update(scope=args.scope,manifest=manifest)
    if args.output:Path(args.output).write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='verification'},indent=2),flush=True)


if __name__=='__main__':main()
