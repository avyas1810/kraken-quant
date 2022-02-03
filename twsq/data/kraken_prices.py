from .base_prices import BasePrices
from twsq.api import KrakenAPI,get_api
import json 
from twsq.utils import ts_utils
import pandas as pd 
import logging

class KrakenPrices(BasePrices):

	def __init__(self, max_live_bars = 720):
		BasePrices.__init__(self)

		self.live_bars = {}
		self.max_live_bars = max_live_bars
		self.KrakenAPI = KrakenAPI()
		self.ticks = {}
		self.ccxt = get_api('Kraken')

	def _on_bar_message(self,ws,msg):
		
		msg = KrakenAPI.parse_msg(msg)

		if type(msg)==list and \
			msg[2].split('-')[0]=='ohlc':
			
			data = msg[1]

			ets = ts_utils.unix2dt(float(data[1]),unit='s')
			freq = int(msg[2].split('-')[1])
			ts = ets - pd.tseries.offsets.Minute(freq)

			parsed = pd.Series()
			parsed['open'] = float(data[2])
			parsed['high'] = float(data[3])
			parsed['low'] = float(data[4])
			parsed['close'] = float(data[5])
			# parsed['vwap'] = float(data[6])
			parsed['volume'] = float(data[7])
			# parsed['count'] = float(data[8])
			symbol = msg[-1]
			symbol = self.KrakenAPI.get_ccxt_symbol(symbol)
			self._update_live_bars(symbol,freq,ts,parsed)

		elif 'event' in msg and msg['event']=='systemStatus':
			status = msg['status']
			if status == 'online':
				self._bar_socket_ready.set()
			logging.debug(f'System Status: {status}')

		else:
			KrakenAPI.process_general_msg(msg,prefix='Bar Socket')

	def _on_tick_message(self, ws, msg):
		msg = KrakenAPI.parse_msg(msg)

		if type(msg)==list and \
			msg[2] == 'trade':

			last_trade = msg[1][-1]
			price = float(last_trade[0])
			symbol = msg[-1]
			symbol = self.KrakenAPI.get_ccxt_symbol(symbol)
			self.ticks[symbol]=price

		elif 'event' in msg and msg['event']=='systemStatus':
			status = msg['status']
			if status == 'online':
				self._tick_socket_ready.set()
			logging.debug(f'System Status: {status}')

		else:
			KrakenAPI.process_general_msg(msg,prefix='Tick Socket')

	def _on_tick_reset(self):
		self.ticks = {}
		self._tick_socket_ready.clear()

	def _on_bar_reset(self):
		self.live_bars = {}
		self._bar_socket_ready.clear()
		
	def stream_bars(self, pairs, freq):

		if not self.KrakenAPI.socket_status('bars'):
			self.KrakenAPI.start_bars_socket(
				self._on_bar_message,
				self._on_bar_reset)

		self._bar_socket_ready.wait()
		live_bars = self.get_live_bars(freq)
		freq = self.ccxt.timeframes[freq]

		for symbol in pairs:
			if symbol in live_bars:
				# don't request twice
				pairs = [x for x in pairs if x!=symbol]
				logging.debug('Skipping bar request for {symbol}: already requested')
			else:
				live_bars[symbol] = pd.DataFrame(
					columns = ['open','high','low','close','volume'])

		self.live_bars[freq] = live_bars
		
		if len(pairs):
			self.KrakenAPI.send_bar_subscription(freq, pairs)


	def stream_ticks(self,pairs):

		if not self.KrakenAPI.socket_status('ticks'):
			self.KrakenAPI.start_ticks_socket(
				self._on_tick_message,
				self._on_tick_reset)

		self._tick_socket_ready.wait()

		for symbol in pairs:
			self.ticks[symbol] = -1
			
		self.KrakenAPI.send_ticks_subscription(pairs)

	# def stream_ticks(self,pairs):
	# 	if len(self.ticks):
	# 		self._tick_socket_ready.wait()
		
	# 	self.KrakenAPI.start_tick_socket(
	# 		self._on_tick_message,
	# 		pairs,
	# 		self._on_tick_reset
	# 		)




