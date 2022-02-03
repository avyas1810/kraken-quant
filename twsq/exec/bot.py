from twsq.exec import BacktestBinance, BacktestRunner, Runner
from twsq.utils import set_logging, ts_utils

class Bot:
	
	def __init__(self):
		self.pos_pnl = {}
		self.orders = {}

	def load_alpha(self,alpha,**kwargs):
		return

	def run_live(self, Alpha=None, **kwargs):
		return

	def run_backtest(self, Alpha, start_ts = None, end_ts = None,
			freq='1h',name=None, taker_fee=None,maker_fee=None,slip=None,
			**kwargs):

		"""
		Run a backtest on Alpha

		Parameters
		----------
		Alpha : Alpha object
			strategy to run your backtest on 
		start_ts : str, datetime or pandas timestamp, optional
			start time of backtest. default is 365 days prior to end_ts
		end_ts : str, datetime or pandas timestamp, optional
			end time of backtest. default is current time.
		freq : {'1m', '5m','15m', '30m', '1h', '4h', '1d'}, optional
			Rebalance or iteration frequency. 
			IE '1m' will call self.rebalance every minute.
			1m = 1 minute, 5m = 5 minutes, 15m = 15 minutes,
			30m = 30 minutes, 1h = 1 hour, 4h = 4 hours,
			1d = 1 day
		name :  str, optional
			Name of strategy. Used in logging and for naming the folder to save down 
			results in MyTWSQ. Will default to the name of the Alpha class passed in.
		taker_fee : float, optional
			Commissions for market orders. Will default to Kraken's fee of 26 bps
			per dollar traded.
		maker_fee : float, optional
			Commissions for limit orders. Will default to Kraken's fee of 16 bps
			per dollar traded.
		slip : float, optional
			Slippage for market orders. Will default to 10 bps per dollar traded.

		Optional custom strategy parameters can be passed through **kwargs
		"""
		broker = BacktestBinance(taker_fee, maker_fee, slip)
		alpha = Alpha(broker,name = name,freq = freq,**kwargs)

		set_logging(debug=False)
		runner = BacktestRunner(alpha,start_ts,end_ts)
		runner.run()						


