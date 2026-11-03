"""Ten factual/reserved filters. Financial screening is reserved until after M10."""
from dataclasses import dataclass,field
from datetime import datetime,timedelta
from decimal import Decimal
from typing import Literal

from .storage_schema import utc_timestamp

FilterStatus=Literal["PASS","FAIL","UNKNOWN","NOT_IMPLEMENTED"]
FILTER_NAMES={"F01":"Exchange trading status","F02":"Market type","F03":"Quote currency",
    "F04":"24-hour AUD volume","F05":"Average spread","F06":"Execution-side depth",
    "F07":"Historical availability","F08":"Minute completeness","F09":"Order minimums","F10":"One-way execution cost"}


@dataclass(frozen=True)
class ScreeningPolicy:
    required_status:str="online"
    required_market_type:str="spot"
    required_quote:str="AUD"
    min_volume_aud:Decimal=Decimal("1000000")
    max_spread_pct:Decimal=Decimal("0.20")
    depth_multiple:Decimal=Decimal("5")
    proposed_notional_aud:Decimal=Decimal("1000")
    depth_band_pct:Decimal=Decimal("0.5")
    history_days:int=90
    min_completeness_pct:Decimal=Decimal("99")
    max_cost_pct:Decimal=Decimal("0.50")
    order_side:Literal["buy","sell"]="buy"
    execution_type:Literal["maker","taker"]="taker"
    metadata_max_age_seconds:int=86400
    market_max_age_seconds:int=300
    fee_max_age_seconds:int=86400

    def __post_init__(self):
        if (self.required_status,self.required_market_type,self.required_quote)!=("online","spot","AUD"):
            raise ValueError("This milestone preserves online/AUD/spot identity gates")
        for name in ("min_volume_aud","max_spread_pct","depth_multiple","proposed_notional_aud","depth_band_pct","min_completeness_pct","max_cost_pct"):
            value=getattr(self,name)
            if not isinstance(value,Decimal) or not value.is_finite() or value<=0:
                raise ValueError("Positive exact Decimal thresholds required")
        if any(getattr(self,n)>100 for n in ("max_spread_pct","depth_band_pct","min_completeness_pct","max_cost_pct")):
            raise ValueError("Percentage thresholds cannot exceed 100")
        if type(self.history_days) is not int or not 1<=self.history_days<=3660:
            raise ValueError("Bounded positive history days required")
        if any(type(getattr(self,n)) is not int or getattr(self,n)<=0 for n in ("metadata_max_age_seconds","market_max_age_seconds","fee_max_age_seconds")):
            raise ValueError("Positive freshness limits required")
        if self.order_side not in ("buy","sell") or self.execution_type not in ("maker","taker"):
            raise ValueError("Invalid proposed order parameters")

    @classmethod
    def from_dict(cls,values):
        decimal_names={"min_volume_aud","max_spread_pct","depth_multiple","proposed_notional_aud","depth_band_pct","min_completeness_pct","max_cost_pct"}
        if any(isinstance(values.get(n),float) for n in decimal_names):
            raise ValueError("Use exact decimal strings or JSON Decimal parsing")
        return cls(**{k:Decimal(v) if k in decimal_names else v for k,v in values.items()})


@dataclass(frozen=True)
class FilterResult:
    filter_id:str
    status:FilterStatus
    observed_value:object|None
    threshold:object|None
    evidence:tuple[str,...]
    reason:str|None

    def __post_init__(self):
        if self.filter_id not in FILTER_NAMES or self.status not in ("PASS","FAIL","UNKNOWN","NOT_IMPLEMENTED"):
            raise ValueError("Invalid filter result")
        if self.status=="PASS" and (self.observed_value is None or not self.evidence):
            raise ValueError("PASS requires observed evidence")


@dataclass(frozen=True)
class FilterContext:
    as_of:datetime
    policy:ScreeningPolicy
    metadata:dict|None
    history:dict|None
    inputs:dict=field(default_factory=dict)

    def __post_init__(self):
        utc_timestamp(self.as_of)


class ScreeningAlgorithms:
    def volume_24h_aud(self,context:FilterContext)->Decimal:
        raise NotImplementedError("Financial algorithm reserved for user implementation after Milestone 10")

    def average_spread(self,context:FilterContext)->Decimal:
        raise NotImplementedError("Financial algorithm reserved for user implementation after Milestone 10")

    def execution_side_depth(self,context:FilterContext)->Decimal:
        raise NotImplementedError("Financial algorithm reserved for user implementation after Milestone 10")

    def order_feasibility(self,context:FilterContext)->bool:
        raise NotImplementedError("Financial algorithm reserved for user implementation after Milestone 10")

    def execution_cost(self,context:FilterContext)->Decimal:
        raise NotImplementedError("Financial algorithm reserved for user implementation after Milestone 10")


def factual(filter_id,context):
    p=context.policy;m=context.metadata
    threshold={"F01":p.required_status,"F02":p.required_market_type,"F03":p.required_quote}[filter_id]
    if m is None:
        return FilterResult(filter_id,"UNKNOWN",None,threshold,(),"metadata_unavailable_as_of")
    observed=utc_timestamp(datetime.fromisoformat(m["observed_at"].replace("Z","+00:00")))
    refs=(m["receipt_id"],m["receipt_hash"],m["observed_at"])
    if observed>context.as_of:
        return FilterResult(filter_id,"UNKNOWN",None,threshold,refs,"future_metadata_rejected")
    key={"F01":"status","F02":"market_type","F03":"quote_asset"}[filter_id]
    value=m.get(key)
    if filter_id=="F01" and observed!=context.as_of:
        reason="stale_metadata_status" if (context.as_of-observed).total_seconds()>p.metadata_max_age_seconds else "status_between_observations_unavailable"
        return FilterResult(filter_id,"UNKNOWN",dict(last_observed=value,observed_at=m["observed_at"]),threshold,refs,reason)
    return FilterResult(filter_id,"UNKNOWN" if value is None else "PASS" if value==threshold else "FAIL",value,threshold,refs,
                        "missing_metadata_fact" if value is None else None if value==threshold else "exchange_requirement_failed")


def history_filter(filter_id,context):
    h=context.history;p=context.policy
    threshold=p.history_days if filter_id=="F07" else p.min_completeness_pct
    if not h or h.get("error") or not h.get("known_minutes"):
        return FilterResult(filter_id,"UNKNOWN",None,threshold,tuple(h.get("evidence",())) if h else (),(h.get("error") or "no_verified_history_known_as_of") if h else "no_verified_history_known_as_of")
    if filter_id=="F07":
        observed={k:h[k] for k in ("first","last","span_days","observed_minute_days","known_minutes")}
        start=datetime.fromisoformat(h["first"].replace("Z","+00:00"));end=datetime.fromisoformat(h["last"].replace("Z","+00:00"))+timedelta(minutes=1)
        passed=end-start>=timedelta(days=p.history_days)
    else:
        observed=h["coverage"]
        value=observed["coverage_pct"]
        if value is None:
            return FilterResult(filter_id,"UNKNOWN",None,threshold,tuple(h["evidence"]),"completeness_unavailable")
        passed=Decimal(str(value))>=threshold
    return FilterResult(filter_id,"PASS" if passed else "FAIL",observed,threshold,tuple(h["evidence"]),None if passed else "insufficient_history" if filter_id=="F07" else "incomplete_minute_coverage")


def reserved(filter_id,context):
    p=context.policy;m=context.metadata
    thresholds={"F04":p.min_volume_aud,"F05":p.max_spread_pct,
        "F06":dict(multiple=p.depth_multiple,proposed_notional_aud=p.proposed_notional_aud,within_mid_pct=p.depth_band_pct,side=p.order_side),
        "F09":dict(proposed_notional_aud=p.proposed_notional_aud,side=p.order_side,execution_type=p.execution_type),"F10":p.max_cost_pct}
    observed=None
    if filter_id=="F09":
        if not m:
            return FilterResult(filter_id,"UNKNOWN",None,thresholds[filter_id],(),"minimum_order_metadata_unavailable")
        if datetime.fromisoformat(m['observed_at'].replace('Z','+00:00'))>context.as_of:
            return FilterResult(filter_id,"UNKNOWN",None,thresholds[filter_id],(),"future_metadata_rejected")
        observed={k:m.get(k) for k in ("min_order_qty","min_order_value","qty_precision","price_precision","observed_at")}
        if observed["min_order_qty"] is None or observed["min_order_value"] is None:
            return FilterResult(filter_id,"UNKNOWN",observed,thresholds[filter_id],(m["receipt_id"],),"minimum_order_constraints_unknown")
    methods={"F04":"volume_24h_aud","F05":"average_spread","F06":"execution_side_depth","F09":"order_feasibility","F10":"execution_cost"}
    try:
        getattr(ScreeningAlgorithms(),methods[filter_id])(context)
    except NotImplementedError:
        refs=tuple(context.inputs.get("evidence",()))+((m["receipt_id"],) if filter_id=="F09" else ())
        return FilterResult(filter_id,"NOT_IMPLEMENTED",observed,thresholds[filter_id],refs,"algorithm_reserved_after_milestone_10")
    raise RuntimeError("Financial screening implementations require explicit post-M10 approval")


FILTERS={"F01":factual,"F02":factual,"F03":factual,"F04":reserved,"F05":reserved,"F06":reserved,"F07":history_filter,"F08":history_filter,"F09":reserved,"F10":reserved}


def dimension(results,ids):
    values=[r.status for r in results if r.filter_id in ids]
    return "FAIL" if "FAIL" in values else "UNKNOWN" if "UNKNOWN" in values else "NOT_IMPLEMENTED" if "NOT_IMPLEMENTED" in values else "PASS"


def aggregate(results):
    if len(results)!=10 or {r.filter_id for r in results}!=set(FILTERS):
        raise ValueError("Exactly ten distinct required filters must be evaluated")
    dimensions=dict(exchange=dimension(results,("F01","F02","F03")),data=dimension(results,("F07","F08")),
        liquidity=dimension(results,("F04","F05","F06")),execution=dimension(results,("F09","F10")))
    if any(r.status=="FAIL" for r in results):overall="REJECTED"
    elif all(r.status=="PASS" for r in results):overall="ELIGIBLE"
    elif dimensions["exchange"]=="UNKNOWN":overall="UNKNOWN"
    elif dimensions["data"]=="UNKNOWN":overall="INSUFFICIENT_DATA"
    elif any(r.status=="UNKNOWN" for r in results):overall="UNKNOWN"
    else:overall="PENDING_EVALUATION"
    return dimensions,overall


def filter_status():
    return {key:dict(name=FILTER_NAMES[key],implementation="FACTUAL" if key in ("F01","F02","F03","F07","F08") else "STRUCTURAL_ONLY_CALCULATION_RESERVED" if key=="F09" else "NOT_IMPLEMENTED") for key in FILTERS}
