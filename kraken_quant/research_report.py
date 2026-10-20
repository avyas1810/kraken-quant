"""Metrics and dashboard, all performance derived from reconciled net equity."""
import math
import numpy as np
import pandas as pd

from .research_ledger import YEAR_SECONDS


def net_metrics(result):
    result.reconcile()
    equity=result.equity
    returns=result.net_returns
    years=(equity.index[-1]-equity.index[0]).total_seconds()/YEAR_SECONDS
    periods=len(returns)/years
    volatility=returns.std(ddof=1)
    downside=np.sqrt(np.mean(np.minimum(returns,0)**2))
    drawdown=float((1-equity/equity.cummax()).max())
    annual_return=float((equity.iloc[-1]/equity.iloc[0])**(1/years)-1)
    # Realized PnL is exit events minus allocated entry costs and carrying charges.
    # Win rate/profit factor use full-portfolio net bar PnL so fees on entries and
    # open-position costs cannot be omitted. Explicitly named, never round-trip stats.
    pnl=equity.diff().iloc[1:]
    wins=pnl[pnl>0].sum(); losses=-pnl[pnl<0].sum()
    closed=result.closed_trades
    trade_profit=closed.net_pnl_aud if len(closed) else pd.Series(dtype=float)
    trade_losses=-trade_profit[trade_profit<0].sum()
    return dict(net_pnl_aud=float(equity.iloc[-1]-equity.iloc[0]),
        net_return=float(equity.iloc[-1]/equity.iloc[0]-1),annualized_net_return=annual_return,
        net_sharpe=float(returns.mean()/volatility*np.sqrt(periods)) if volatility>0 else np.nan,
        net_sortino=float(returns.mean()/downside*np.sqrt(periods)) if downside>0 else np.nan,
        net_max_drawdown=drawdown,net_calmar=annual_return/drawdown if drawdown>0 else np.nan,
        annualized_net_volatility=float(volatility*np.sqrt(periods)),
        net_bar_win_rate=float((pnl>0).mean()),net_bar_profit_factor=float(wins/losses) if losses>0 else np.nan,
        net_trade_win_rate=float((trade_profit>0).mean()) if len(trade_profit) else np.nan,
        net_trade_profit_factor=float(trade_profit[trade_profit>0].sum()/trade_losses) if trade_losses>0 else np.nan,
        net_closed_trade_pnl_aud=float(trade_profit.sum()),closed_trade_count=len(trade_profit),
        turnover=float(result.trades.notional_aud.sum()/equity.iloc[1:].mean()) if len(result.trades) else 0.,
        fill_count=len(result.trades),total_costs_aud=float(result.costs.to_numpy().sum()),
        gross_mark_pnl_aud=float(result.gross_attribution.to_numpy().sum()),
        periods_per_year=periods,elapsed_years=years)


def rolling_sharpe(result, window=30):
    r=result.net_returns
    periods=len(r)/((result.equity.index[-1]-result.equity.index[0]).total_seconds()/YEAR_SECONDS)
    std=r.rolling(window,min_periods=window).std(ddof=1)
    return r.rolling(window,min_periods=window).mean()/std.where(std>0)*math.sqrt(periods)


def dashboard(run):
    """Return eight figures with separate IS/OOS series and labelled AUD accounting."""
    import matplotlib.pyplot as plt
    results={'in-sample':run.train,'out-of-sample':run.validation}
    figures=[]
    def readable_assets(frame,labels):
        frame=frame.rename(columns=labels)
        if frame.shape[1]<=10: return frame
        ranked=frame.abs().max().sort_values(ascending=False).index
        visible=frame.loc[:,ranked[:8]].copy()
        visible['Other assets (net)']=frame.loc[:,ranked[8:]].sum(axis=1)
        return visible
    for kind in ('Net equity (AUD)','Net drawdown','Rolling net Sharpe','Allocations',
                 'Net asset PnL (AUD)','Trading/holding costs (AUD)','Gross/net exposure','Kelly constrained targets'):
        fig,axes=plt.subplots(1,2,figsize=(13,3.5),squeeze=False)
        for ax,(name,result) in zip(axes[0],results.items()):
            labels={k:k.symbol for k in result.weights}
            if kind.startswith('Net equity'):
                result.equity.plot(ax=ax,label='strategy net')
                run.benchmarks[name].equity.plot(ax=ax,label='buy-and-hold net'); ax.legend()
            elif kind=='Net drawdown': (result.equity/result.equity.cummax()-1).plot(ax=ax)
            elif kind=='Rolling net Sharpe':
                values=rolling_sharpe(result,min(30,max(3,len(result.net_returns)//3)))
                if values.notna().any(): values.plot(ax=ax)
                else: ax.text(.5,.5,'Undefined: insufficient observations or zero volatility',
                              ha='center',va='center',transform=ax.transAxes,wrap=True)
            elif kind=='Allocations':
                visible=readable_assets(result.weights,labels)
                visible['Cash']=result.ledger.cash/result.equity
                visible.plot(ax=ax)
            elif kind=='Net asset PnL (AUD)': readable_assets(result.attribution.cumsum(),labels).plot(ax=ax)
            elif kind.startswith('Trading/'):
                result.costs.sum().plot.bar(ax=ax)
                ax.set_ylim(bottom=0,top=max(1.,result.costs.sum().max()*1.1))
            elif kind=='Gross/net exposure': result.ledger[['gross_exposure','net_exposure']].plot(ax=ax)
            else: readable_assets(result.kelly_weights,labels).plot(ax=ax)
            ax.set_title(f'{name}: {kind}'); ax.grid(alpha=.2)
        fig.tight_layout(); figures.append(fig)
    return figures
