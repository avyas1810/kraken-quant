from twsq.exec import get_broker
from twsq.paths import ALPHA_PATH, safe_path
from datetime import datetime
import pickle 
import os
import logging 
import pandas as pd 
from functools import lru_cache
from twsq.utils import ts_utils, Emojis
from threading import Thread, Event
from time import sleep 
from twsq.utils import set_logging
from twsq.exec import BacktestBinance, BacktestRunner, Runner 
import os

class Alpha:

	def __init__(self, broker,  name = None, freq='1h', **kwargs):
		"""
		Alpha parent class used for making all strategies.

		Parameters
		----------
		Broker : str or Broker object
			Broker to use. Currently users cannot set this parameter. 
			Uses Binance for backtesting and Kraken for live trading by default. 
			Will allow for more flexibility in future.
		name :  str, optional
			Name of strategy. Used in logging and saving down results in MyTWSQ. 
			Will default to the name of the class.
		freq : {'1m', '5m','15m', '30m', '1h', '4h', '1d'}
			Rebalance frequency. IE '1m' will call self.rebalance 
			every minute.
			1m = 1 minute, 5m = 5 minutes, 15m = 15 minutes,
			30m = 30 minutes, 1h = 1 hour, 4h = 4 hours,
			1d = 1 day

		"""

		self.broker = get_broker(broker)

		if name is None:
			name = self.__class__.__name__
		self.name = name
			
		self._staged_orders = []
		self.custom_params = kwargs
		self.broker.alphas[self.name]=self
		self.freq = freq

	@property
	def is_backtesting(self):
		return self.broker.is_backtesting

	@property
	def open_orders(self):
		open_orders = [x for x in self.broker._orders if x.strategy == self.name]
		return open_orders

	def get_open_orders(self):
		open_orders = [x for x in self.broker._orders if x.strategy == self.name]
		return open_orders
		
	def manage_open_orders(self):
		return
		
	def _prepare(self):
		# move this stuff to initialize on run later
		self.load_pos()
		self.prepare(**self.custom_params)
		
	def prepare(self):
		"""
		Function users can fill out to set custom parameters
		or otherwise prepare the strategy.
		"""
		return

	@property
	def ts(self):
		"""current timestamp"""
		return self.broker.ts

	def get_port_val(self):
		return self.broker.port_val[self.name]

	def _finish(self):


		if self.is_backtesting:
			path = safe_path(ALPHA_PATH, self.name, 'backtest')

		else:
			path = safe_path(ALPHA_PATH, self.name, 'live_trading', 
				self.broker.name)

			self.logging(f'{Emojis.exit} Exiting trader...')
			pnl = self.snap_pnl(verbose=False)
			self.logging(f'{Emojis.pnl} Final session PnL %.2f'% pnl)
			self.on_exit()

			if len(self.get_open_orders()):
				sleep(7) # process any final order updates

		self.orders  = self.save_orders(path)
		self.pos_pnl = self.save_pos_pnl(path)
		self.pos_pnl = self.pos_pnl.set_index('Date')

		if not self.is_backtesting:
			pos_str = self.get_pos_string()
			self.logging(f'Ending positions: {pos_str}')
		return

	# def get_bars(self,symbol,start_ts,end_ts,freq=None):
	# 	return self.broker.pricing.get_bars(symbol, start_ts, end_ts)

	def save_pos_pnl(self,path):
		
		filepath = os.path.join(path,'pos_pnl.csv')

		val = self.broker.port_val.get(self.name)
		
		if val is not None:
			pnl = val.diff().fillna(0)
			df = pd.DataFrame({'port_val':val,'pnl':pnl}).sort_index()
			hist_pos = pd.DataFrame(self.broker.hist_pos[self.name]).T.fillna(0).sort_index()
			df = pd.concat([df,hist_pos],axis=1)			
			df = df.reset_index()#.sort_index()

			df = df.rename(columns = {'index':'Date'})
			df = df.iloc[1:]

			if not df.empty:
				if not self.is_backtesting and os.path.exists(filepath):
					current = pd.read_csv(filepath)
					if not current.empty:
						current['Date'] = pd.DatetimeIndex(current['Date'])
						df = pd.concat([current,df],axis=0,ignore_index=True)

				df.to_csv(filepath,index=False)
				return df


	def save_hist_pos(self,path):
		df = pd.DataFrame(self.broker.hist_pos).T.fillna(0)
		df.index.name = 'Date'
		df = df.reset_index()
		df.to_csv(os.path.join(path,'pos.csv'),index=False)

	def save_orders(self,path):
		filepath = os.path.join(path,'orders.csv')

		orders_df = []
		for order in self.broker.all_orders:
			if order.strategy == self.name:
				orders_df.append(order.__dict__)
		orders_df = pd.DataFrame(orders_df)

		if not orders_df.empty:
			if not self.is_backtesting and os.path.exists(filepath):
				current = pd.read_csv(filepath)
				if not current.empty:

					if 'start_ts' in current:
						current['start_ts']=pd.DatetimeIndex(current['start_ts'])
					
					if 'end_ts' in current:
						current['end_ts']=pd.DatetimeIndex(current['end_ts'])

					orders_df = pd.concat([current,orders_df],axis=0,
						ignore_index=True)
			orders_df.to_csv(filepath, index=False)
			return orders_df 

	def on_exit(self):
		"""
		Function run when the trader exits or crashes
		"""
		
	def logging(self,msg,level='info'):
		func = getattr(logging,level)
		msg = f'{self.name}: {msg}'
		func(msg)

	def create_order(
		self,
		symbol,
		qty,
		side,
		limit_price = None,
		custom_id = None,
		route = False
	):
		"""
		Create market or limit orders. 

		To create a limit order, specify a limit_price. If 
		you want to immediately send the order to the broker 
		pass route=True. Otherwise, it will wait to send orders 
		until you call self.route(). Calling self.route() 
		this way allows you to send all of your orders in the 
		current rebalance to the broker at the same time. 
		
		Parameters
		----------
		symbol : str
			Symbol for the security you want to trade. (IE. 'ETH/USD')
		qty :  float
			Amount you want to trade. Must be positive.
		side : str {"buy", "sell"}
			Side of trade. Must be "buy" or "sell".
		limit_price : float, optional
			Price for a limit order.
		custom_id : str or float, optional
			Custom internal identifier you want to attach to this order
		route : bool, optional
			If True, immediately sends order to broker. 
			If False, will wait until you call self.route()

		Returns
		-------
		None

		"""

		
		
		
		if not self.broker.is_backtesting:
			min_qty = self.broker.get_min_order_qty(symbol)
			self._n_orders+=1
		else:
			min_qty = 0

		if qty > min_qty:
			order = self.broker.create_order(self.name, 
				symbol, qty, side, limit_price = limit_price,
				custom_id = custom_id, route = route)

			if not route:
				self._staged_orders.append(order)
		else:
			logging.info(f'Order to {side} {qty} {symbol} rejected: qty <= min qty {min_qty}')

	def trade_to_target(self, target, quote = 'USD',route = False):

		"""
		Creates market orders that rebalance your positions to 
		target positions specified in target. 

		For instance, if you specify target = {'ETH': 0.1}, and 
		your current ETH position is 0.2, it will create a market 
		order to sell 0.1 ETH. This is useful for liquidity-taking 
		strategies that best thought of as generating 
		target positions rather than trades.

		Parameters
		----------
		target : dict
			Dictionary with securities as keys and target positions as values.
			Ex: {'ETH': 0.1 , 'BTC': 0.01}
		quote :  str, optional
			Quote currency to use to trade to target.
		route : bool, optional
			If True, immediately sends order to broker. 
			If False, will wait until you call self.route()

		Returns
		-------
		None

		"""

		for base, tgt in target.items():

			pos = self.broker.get_pos(self.name)
			trd = tgt - pos.get(base,0)
			symbol = f'{base}/{quote}'
			side = 'buy' if trd > 0 else 'sell'
			trd = abs(trd)

			if trd>0:
				self.create_order(	
					symbol,
					trd,
					side,
					route = route
				)

		return


	def cancel_order(self, custom_id):
		"""
		Cancel order(s) specified by custom_id. custom_id is attached to an order through self.create_order. If you attached custom_id to more than 1 order, all of them will be canceled.

		Parameters
		----------
		custom_id : str or float
			Custom identifier of order(s) to be canceled

		Returns
		-------
		None

		"""
		self.broker.cancel_order(custom_id, self.name)

	def cancel_all_orders(self):
		"""
		Cancel all open orders

		Returns
		-------
		None
		"""

		self.broker.cancel_all_orders(self.name)

	def route(self):
		# route all new orders
		for order in self._staged_orders:
			self.broker.route(order)
		self._staged_orders = []

	def get_pos(self):
		"""
		Returns positions of your strategy as a dictionary

		Note that the returned positions are for your strategy only. 
		It excludes any other positions you have with your broker. 
		Moreover, the positions returned are essentially cumulative trade values. 
		So if you bought $1000 of ETH/USD, your position would have the $1000 
		equivalent of ETH, but also a -$(1000 + x) USD position. 
		Where the x represents comissions.
		
		Returns
		-------
		Dict
		keys = security name (IE. "ETH")
		values = amount held
		"""

		return self.broker.get_pos(self.name).copy()


	def save_live_pos(self, path):
		pos = self.broker.get_pos(self.name)
		filename = 'pos'
		file = os.path.join(path, filename)
		
		str_pos =' | '.join([f'{x} {y}' for x, y in pos.items()])
		self.logging(f'Saving Pos: {str_pos}',level='info')

		with open(file, 'wb') as handle:
			pickle.dump(pos, handle, protocol=pickle.HIGHEST_PROTOCOL)

	def get_pos_string(self):
		pos = self.get_pos()
		pos_str = []
		for asset, qty in pos.items():
			if qty != 0:
				qty = self.broker.round_asset_quantity(asset,qty)
				pos_str.append(f'{asset} {qty}')

		pos_str = ' | '.join(pos_str) if len(pos_str) else 'None'
		return pos_str

	def load_pos(self):
		ts = self.broker.ts
		start_pos = {}
		start_port_val = 0

		if not self.broker.is_backtesting:
			path = safe_path(ALPHA_PATH, self.name, 'live_trading', 
				self.broker.name)

			filepath = os.path.join(path,'pos_pnl.csv')
		
			if os.path.exists(filepath):
				pos_pnl = pd.read_csv(filepath)

				if not pos_pnl.empty:

					pos_pnl['Date'] = pd.DatetimeIndex(pos_pnl['Date'])
					dt = pos_pnl['Date'].iloc[-1]

					self.logging('Loading starting pos from %s' 
					% dt.strftime('%d-%b-%y %H:%M:%S'),level='debug')

					start_pos =  pos_pnl.drop(['Date','port_val','pnl'],axis=1)
					start_pos = start_pos.iloc[-1].to_dict()

					ts = pd.to_datetime(pos_pnl['Date'].values[-1])
					start_port_val = pos_pnl['port_val'].iloc[-1]
					
		self.broker.pos[self.name] = start_pos.copy()
		
		if not self.broker.is_backtesting:
			pos_str = self.get_pos_string()
			self.logging(f'Initial positions: {pos_str}',level='info')

		hist_pos = {}
		hist_pos[ts] = start_pos.copy()
		self.broker.hist_pos[self.name] = hist_pos

		port_val = pd.Series()
		port_val[ts] = start_port_val
		self.broker.port_val[self.name] = port_val

	def wait_till_ready(self):
		self.logging('Waiting till feeds ready',level='debug')
		self.broker.wait_till_ready()

	def snap_port(self):
		self.broker.snap_port(self.name)

	def get_current_price(self, symbol):
		"""
		Get the last trade price of a symbol

		Parameters
		----------
		symbol : str
			Security symbol (IE. 'ETH/USD')

		Returns
		-------
		float
			Last trade price
		"""

		return self.broker.pricing.get_current_price(symbol)

	def get_lastn_bars(self, symbol, n, freq, lag=0):
		"""
		Get the last n bars (of size freq) for a symbol.

		Parameters
		----------
		symbol : str
			Security symbol (IE. 'ETH/USD')
		n :  int
			Number of bars to pull. In live trading, you can
			only look back 720 bars.
		freq : {'1m', '5m','15m', '30m', '1h', '4h', '1d'}
			Frequency (size) of the bars.
			1m = 1 minute, 5m = 5 minutes, 15m = 15 minutes,
			30m = 30 minutes, 1h = 1 hour, 4h = 4 hours,
			1d = 1 day
		lag : int, optional
			Whether to lag the bars. Using lag=2 with n=5 
			is the same using lag=0, n=7 and dropping the 
			most recent two bars returned.

		Returns
		-------
		DataFrame 
		columns = ['open','high','low','close','volume']
		index = Bar open times
		"""
		return self.broker.pricing.get_lastn_bars(symbol, n, freq, lag)

	def on_finished_order(self,order):
		"""
		React to a finished order immediately.

		Parameters
		----------
		order : Order object
		   The finished order to react to
		"""

	def rebalance(self):
		pass

	# def get_pnl(self):
	# 	self.broker.get_pnl(self.name)

	def snap_pnl(self,verbose=True):
		self.snap_port()
		port_val = self.get_port_val()
		pnl = port_val.iloc[-1] - port_val.iloc[0]
		if verbose:
			self.logging(f'{Emojis.pnl} Total session PnL %.2f'% pnl)
		return pnl 

	def _track_pnl(self):

		def _track_pnl(self):
			while True:
				sleep(5*60)
				self.snap_pnl()

		thread = Thread(
			target=_track_pnl, daemon=True, name=f'{self.name} pnl',
			args = [self])
		thread.start()

	@classmethod
	def run_backtest(cls, start_ts = None, end_ts = None,
			freq='1h',name=None, taker_fee=None,maker_fee=None,slip=None,
			**kwargs):

		"""
		Run a backtest on Alpha

		Parameters
		----------
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

		Returns
		-------
		Alpha
		Your instantiated strategy (Alpha sub-class)
		Use Alpha.pos_pnl to see backtest positions and pnl
		Use Alpha.orders to see all backtest orders 
		"""

		broker = BacktestBinance(taker_fee, maker_fee, slip)
		alpha = cls(broker,name = name,freq = freq,**kwargs)

		set_logging(debug=False)
		runner = BacktestRunner(alpha,start_ts,end_ts)
		runner.run()
		return alpha

	@classmethod
	def run_live(cls, freq='1h',name=None, **kwargs):

		"""
		Run live trading for your Alpha

		Parameters
		----------
		freq : float or {'1m', '5m','15m', '30m', '1h', '4h', '1d'}, optional

			Rebalance or iteration frequency. 

			You can specifiy the number of seconds between each rebalance by passing 
			a float. In this case, you rebalance immediately and every freq seconds
			thereafter.

			Or you can use one of the preset rebalance frequencies:

			1m = 1 minute, 5m = 5 minutes, 15m = 15 minutes,
			30m = 30 minutes, 1h = 1 hour, 4h = 4 hours,
			1d = 1 day.

			If you use these, you only rebalance on bar end times. So if it is 3:15 PM
			and you call this function with 30m you won't rebalance until 3:30 PM.
			Next you'll rebalance at 4:00PM, 4:30 PM, etc

		name :  str, optional
			Name of strategy. Used in logging and for naming the folder to save down 
			results in MyTWSQ. Will default to the name of the Alpha class passed in.

		Optional custom strategy parameters can be passed through **kwargs

		Returns
		-------
		None
		"""

		run_event = Event()
		run_event.set()

		def _run_live():
			alpha = cls('Kraken',name = name,freq = freq,**kwargs)

			log_path = safe_path(ALPHA_PATH, alpha.name, 'live_trading',
				alpha.broker.name,'logs')
			logfile = os.path.join(log_path,'%s.log' % datetime.now().strftime('%Y%m%d_%H%M%S'))
			set_logging(debug=False, telegram_log=True,logfile = logfile)	
			runner = Runner(alpha,run_event)
			logging.info(f'{Emojis.start} Live trader initiated')
			runner.run()
		
		thread = Thread(
			target=_run_live, daemon=False)
		thread.start()

		try:
			thread.join()
		except:
			pass
		finally:
			run_event.clear()
			thread.join()
			os._exit(0)
			









