"""Fee acquisition and raw input preparation. Mathematical engines are unavailable."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import time

from .cost_algorithms import CostAlgorithms
from .cost_contracts import CostRequest, PreparationConfig
from .cost_inputs import InputService
from .cost_storage import AuditStore, payload
from .db_environment import load_config
from .fee_data import refresh_fees, fee_report
from .ohlcv_collector import collection_universe, read_time

EXAMPLE_SIZES=(Decimal("100"),Decimal("1000"),Decimal("5000"))


def algorithm_status():
    return {name:"NOT_IMPLEMENTED" for name in ("spread_cost","book_execution","slippage","market_impact","total_cost")}


def readonly_adapter(config):
    from dotenv import dotenv_values
    from .credentials import readonly_from_env
    from .asset_catalogue import _atomic_json
    from .operations import ProcessLock
    values=dotenv_values(config.env_file,interpolate=False)
    if not values.get("KRAKEN_READONLY_API_KEY") or not values.get("KRAKEN_READONLY_API_SECRET"):
        return None
    root=AuditStore(config.data_dir).root
    root.mkdir(parents=True,exist_ok=True)
    def nonce():
        lock=ProcessLock(root/"nonce")
        try:
            path=root/"nonce.json"
            previous=json.loads(path.read_text())["nonce"] if path.exists() else 0
            value=max(time.time_ns()//1_000_000,previous+1)
            _atomic_json(path,{"nonce":value})
            return value
        finally:
            lock.close()
    return readonly_from_env(config.env_file,clock=lambda:datetime.now(timezone.utc),nonce=nonce)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component",choices=("fees","inputs","algorithms"))
    parser.add_argument("action",choices=("refresh","report","prepare","load","status"))
    parser.add_argument("--pair")
    parser.add_argument("--all-aud",action="store_true")
    parser.add_argument("--side",choices=("buy","sell"),default="buy")
    parser.add_argument("--notional-aud",type=Decimal,default=Decimal("1000"))
    parser.add_argument("--sizes-aud",type=Decimal,nargs="+",default=EXAMPLE_SIZES)
    parser.add_argument("--execution-type",choices=("maker","taker"),default="taker")
    parser.add_argument("--observation-time",type=read_time)
    parser.add_argument("--model-version",default="reserved-after-m10-v1")
    parser.add_argument("--allow-present-day-fee-assumption",action="store_true")
    parser.add_argument("--max-market-age-seconds",type=int,default=300)
    parser.add_argument("--max-candle-age-seconds",type=int,default=7200)
    parser.add_argument("--max-fee-age-seconds",type=int,default=86400)
    parser.add_argument("--manifest")
    parser.add_argument("--public-only",action="store_true")
    parser.add_argument("--root",default=".")
    parser.add_argument("--data-dir")
    args=parser.parse_args(argv)
    try:
        if (args.component,args.action)==("algorithms","status"):
            result=algorithm_status();code=0
        else:
            config=load_config(args.root,data_dir=args.data_dir)
            if (args.component,args.action)==("fees","refresh"):
                adapter=None;auth_failure=False
                if not args.public_only:
                    try:
                        adapter=readonly_adapter(config)
                    except Exception:
                        auth_failure=True
                version=refresh_fees(config.data_dir,readonly=adapter,account_unavailability="unavailable_readonly_adapter_auth_or_permissions" if auth_failure else "not_requested_public_only" if args.public_only else None)
                result=dict(version=version,account_adapter_unavailable=auth_failure,schedules=[v for v in fee_report(config.data_dir) if v["version"]==version]);code=0
            elif (args.component,args.action)==("fees","report"):
                result=fee_report(config.data_dir);code=0
            elif args.component=="inputs" and args.action in ("prepare","report","load"):
                service=InputService(config.data_dir)
                if args.action=="load":
                    if not args.manifest:
                        raise ValueError("Manifest identifier required")
                    result=payload(service.load(args.manifest));code=0
                else:
                    policy=PreparationConfig(args.max_market_age_seconds,args.max_candle_age_seconds,args.max_fee_age_seconds,args.allow_present_day_fee_assumption)
                    if args.action=="prepare":
                        if not args.pair or args.all_aud:
                            raise ValueError("Single pair required")
                        request=CostRequest(args.pair,args.side,args.notional_aud,args.execution_type,args.observation_time or datetime.now(timezone.utc),args.model_version)
                        prepared=service.prepare(request,policy)
                        version=service.persist(prepared)
                        result=dict(manifest=version,**payload(prepared));code=int(prepared.status!="inputs_ready")
                    else:
                        if args.all_aud==bool(args.pair):
                            raise ValueError("Select pair or all AUD")
                        pairs=[r["pair_id"] for r in collection_universe(config.data_dir)[0]] if args.all_aud else [args.pair]
                        result=[]
                        stamp=args.observation_time or datetime.now(timezone.utc)
                        for pair in pairs:
                            for size in args.sizes_aud:
                                prepared=service.prepare(CostRequest(pair,args.side,size,args.execution_type,stamp,args.model_version),policy)
                                result.append(dict(pair_id=pair,notional_aud=str(size),status=prepared.status,missing_inputs=prepared.missing_inputs,warnings=prepared.warnings,
                                    quote_available=prepared.inputs.quote is not None,book_available=prepared.inputs.order_book is not None,
                                    fee_available=prepared.inputs.fee_schedule is not None))
                        code=int(any(r["status"]!="inputs_ready" for r in result))
            else:
                raise ValueError("Unsupported command combination")
        print(json.dumps(payload(result),indent=2))
        return code
    except Exception as error:
        safe={"missing_or_changed_metadata_dependency","changed_source_dependency","missing_or_changed_parquet_dependency","inconsistent_source_dependency","missing_source_provenance","missing_or_inconsistent_source_provenance"}
        code="missing_referenced_dependency" if isinstance(error,FileNotFoundError) else str(error) if isinstance(error,ValueError) and str(error) in safe else "cost_infrastructure_operation_failed; private details suppressed"
        print(json.dumps({"error":code}))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
