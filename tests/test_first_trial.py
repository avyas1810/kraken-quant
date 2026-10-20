"""First trial integration: traded Kelly portfolios and actual production boundary."""
from dataclasses import replace
import hashlib
from pathlib import Path
import json
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from examples.research_config import RESEARCH_UNIVERSE
from kraken_quant.boundary import ProductionAdapter
from kraken_quant.research_acceptance import run_acceptance_checks, nonzero_production_preview, DirectionStrategy
from kraken_quant.research_handoff import submit_recommended_weights
from kraken_quant.research_market import asset, validate_selection
from kraken_quant.research_report import net_metrics
from kraken_quant.risk import AccountSnapshot, Quote, InstrumentRules, RiskGateway
from kraken_quant.strategy import Instrument, StrategyPipeline


def test_forced_long_short_costed_kelly_and_nonzero_production_handoff():
    report=run_acceptance_checks()
    assert report.checks.passed.all()
    for result in (report.signed_run.train,report.signed_run.validation):
        assert result.positions.min().min()<0<result.positions.max().max()
        assert len(result.trades)>0
        for category in ('commission','spread','slippage','impact','fx','borrow'):
            assert result.costs[category].sum()>0
        metrics=net_metrics(result)
        assert metrics['net_pnl_aud']==pytest.approx(metrics['gross_mark_pnl_aud']-metrics['total_costs_aud'])
        assert metrics['net_sharpe']==pytest.approx(result.net_returns.mean()/result.net_returns.std(ddof=1)*np.sqrt(metrics['periods_per_year']))
        assert metrics['net_max_drawdown']==pytest.approx((1-result.equity/result.equity.cummax()).max())
    for result in (report.leveraged_run.train,report.leveraged_run.validation):
        assert result.ledger.cash.min()<0
        assert result.costs.margin.sum()>0
    rec=report.recommendations
    assert rec.target_weight.gt(0).any() and rec.target_weight.lt(0).any()
    assert rec.gross_exposure.max()<=.8+1e-7
    assert (rec.next_execution_timestamp>=rec.decision_timestamp).all()
    assert len(report.handoff['production_intents'])>0
    assert not report.handoff['live_supported'] and report.handoff['gateway_unchanged']


@pytest.mark.parametrize('count',[1,3,10,50])
def test_selection_supported_bounds(count):
    validate_selection([asset(f'A{i}/USD') for i in range(count)])


@pytest.mark.parametrize('count',[0,51])
def test_selection_out_of_bounds_rejected(count):
    with pytest.raises(ValueError,match='between 1 and 50'):
        validate_selection([asset(f'A{i}/USD') for i in range(count)])


def test_existing_production_dry_run_respects_kill_limits_mapping_and_fx():
    source=asset(RESEARCH_UNIVERSE[0]).instrument
    native=Instrument('kraken',source.symbol,source.base,source.quote)
    now=pd.Timestamp('2024-02-01T00:00:00Z')
    rec=pd.DataFrame([dict(instrument=source,target_weight=.003,current_weight=0.,
        decision_timestamp=now,next_execution_timestamp=now,risk_status='ok',eligible=True)])
    gateway=RiskGateway(':memory:')
    adapter=ProductionAdapter(StrategyPipeline(DirectionStrategy({source.symbol:1})),gateway,enabled=False)
    args=dict(production=adapter,now=now,account=AccountSnapshot(now,now.normalize(),'1000',{},'1000'),
        quotes={native:Quote(native,now,'99.99','100')},
        rules={native:InstrumentRules('.001','.01','.001','1',now,native)},
        instrument_map={source:native},aud_per_usd=1.5,fx_timestamp=now,
        fee_rates={native:.001},unresolved_orders=False,risk_approved=True)
    try:
        first=submit_recommended_weights(rec,**args)
        second=submit_recommended_weights(rec,**args)
        assert first['production_intents']==second['production_intents']
        assert first['production_intents'][0].quantity>0
        assert gateway.connection.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]==0
        with pytest.raises(PermissionError,match='dry-run'):
            submit_recommended_weights(rec,mode='live',**args)
        with pytest.raises(ValueError,match='Fresh positive'):
            submit_recommended_weights(rec,**dict(args,fx_timestamp=now-pd.Timedelta(hours=1)))
        with pytest.raises(ValueError,match='one-to-one'):
            submit_recommended_weights(rec,**dict(args,instrument_map={}))
        with pytest.raises(ValueError,match='underlying asset'):
            wrong=Instrument('kraken',RESEARCH_UNIVERSE[1],RESEARCH_UNIVERSE[1].split('/')[0],'USD')
            submit_recommended_weights(rec,**dict(args,instrument_map={source:wrong}))
        larger=rec.copy(); larger['target_weight']=.1
        with pytest.raises(ValueError,match='order|exposure|Order|position'):
            submit_recommended_weights(larger,**args)
        ineligible=rec.copy(); ineligible['eligible']=False
        with pytest.raises(ValueError,match='Ineligible'):
            submit_recommended_weights(ineligible,**args)
        gateway.kill()
        with pytest.raises(PermissionError,match='kill|Kill'):
            submit_recommended_weights(rec,**args)
        assert gateway.connection.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]==0
        assert gateway.connection.execute("SELECT value FROM controls WHERE name='kill'").fetchone()[0]=='1'
        assert not adapter.enabled
    finally:
        gateway.close()


def test_execution_runner_rejects_notebooks_outside_repository():
    completed=subprocess.run([sys.executable,'examples/execute_research_notebook.py',
        '--notebook','../outside.ipynb'],capture_output=True,text=True)
    assert completed.returncode!=0
    assert 'inside this repository' in completed.stderr
