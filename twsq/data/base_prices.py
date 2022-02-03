from twsq.paths import DATA_PATH, safe_path
from twsq.utils import ts_utils, print_pct_done
from twsq.api import get_api
from datetime import datetime
import logging
import pandas as pd 
import pickle 
import os 
from threading import Event
from functools import lru_cache

class BasePrices:

	def __init__(self):

		self.name = self.__class__.__name__.replace('Prices','')
		self.path = safe_path(DATA_PATH, self.name)
		self.api = get_api(self.name)
		self.data = {}

		# need to wait for tick / bar connection to be online
		# before requesting additional ticks
		self._tick_socket_ready = Event()
		self._bar_socket_ready = Event()

	def _pickle_path(self, symbol, freq):
		symbol = symbol.replace('/','_')
		name = f'PX_{symbol}_{freq}.pk'
		return os.path.join(self.path, name)
		

	def load_pickle(self, symbol, freq):
		name = '%s_%s' % (symbol, freq)

		if name in self.data:
			return self.data[name]

		path = self._pickle_path(symbol, freq)
		if os.path.exists(path):
			with open(path, 'rb') as handle:
				data = pickle.load(handle)
				self.data[name] = data 
				return data
		return pd.DataFrame()

	def dump_pickle(self, data, symbol, freq):
		if not data.empty:
			path = self._pickle_path(symbol, freq)
			with open(path, 'wb') as handle:
			    pickle.dump(data, handle)
			    name = '%s_%s' % (symbol, freq)
			    self.data[name] = data 

	def load_bars(self, symbol, freq, reset=False, 
		live=False,start_ts=1):

		all_data = []

		if reset:
			cur_data = pd.DataFrame()

		else:

			cur_data = self.load_pickle(symbol, freq)
			if not cur_data.empty:
				start_ts = max(start_ts,
					ts_utils.dt2unix(cur_data.index.max())+1)

		logging.debug(f'Loading {symbol} {freq} bars from {self.name}...')
		first = None 

		#ct = 0
		while True:

			data = self.api.fetch_ohlcv(symbol, freq, since = start_ts)
			if len(data) > 0:

				data_start = ts_utils.unix2dt(data[0][0])\
					.strftime('%m/%d/%y %H:%M:%S')

				data_end = ts_utils.unix2dt(data[-1][0])\
					.strftime('%m/%d/%y %H:%M:%S')
				
				logging.debug(f'Loaded {symbol} {freq} bars from  ' 
							f'{self.name}: {data_start} > {data_end}')

				start_ts = data[-1][0]+1				

				if cur_data.empty:
					first = data[0][0]

					if not live:
						output = print_pct_done(start_ts,first,
							ts_utils.dt2unix(ts_utils.cur_ts(freq)),
							prefix = f'Downloading {symbol} {freq} bars:',
							suffix='done')

				all_data += data
				#ct+=len(data)
			else:
				break

		# if 'output' in locals():
		# 	print (output)

		# skip the most recent bar 
		if not live:
			all_data = all_data[:-1] 

		if len(all_data):
			all_data = pd.DataFrame(all_data, 
				columns = ['ts','open','high','low','close','volume'])
			all_data['ts'] = all_data['ts'].map(ts_utils.unix2dt)
			all_data.set_index('ts',inplace=True)
			all_data.sort_index(inplace = True)

			all_data = pd.concat([cur_data,all_data])
			if not live:
				self.dump_pickle(all_data, symbol, freq)

			return all_data

		return cur_data

	@lru_cache(16)
	def get_price_start(self,symbol,freq=None):
		# if not hasattr(self,'_price_start'):
		# 	self._price_start = {}

		if freq is None:
			freq = self.freq

		# name = '%s_%s'%(symbol,freq)
		# if name in self._price_start:
		# 	return self._price_start[name]
		data = self.load_pickle(symbol,freq)
		res = None
		if not data.empty:
			res = data.index[0]
		else:
			bar = self.api.fetch_ohlcv(symbol, freq, since = 0,limit = 1)
			if len(bar):
				res = ts_utils.unix2dt(bar[0][0])

		#self._price_start[name] = res 
		return res

	def get_bars(self, symbol, freq = '1d', start_ts = None, end_ts = None):
		"""
        Pull historical symbol bars of size freq between start_ts and end_ts

        On the first call, the function will download all bars of size `freq` for `symbol` 
        and cache it as a pickle file. On each subsequent call, if `end_ts` is contained 
        within the data in the pickle file, the function simpley reads from the pickle. 
        If `end_ts` is beyond what has been downloaded, the incremental data will be 
        downloaded, cached and then returned.

        Pickle files are stored in MyTWSQ/data/

        Parameters
        ----------
        symbol : str
            Symbol to pull bars for
        freq : str {'1m', '5m','15m', '30m', '1h', '4h', '1d'}
            Frequency (size) of bars. 1m = 1 minute, 1h = 1 hour,
            1d = 1 day, etc. 
        start_ts : str, datetime or pandas timestamp, optional
            start time of bar history. defaults to earliest available.
        end_ts : str, datetime or pandas timestamp, optional
            end time of bar history. default is most recent bar

        Returns
        -------
        DataFrame 
        columns = ['open','high','low','close','volume']
        index = Bar open times
		"""

		if type(end_ts)==str:
			end_ts = pd.to_datetime(end_ts)

		max_ts = ts_utils.cur_ts(freq) - ts_utils.get_offset(freq)

		if end_ts is None:
			end_ts = max_ts
		else:
			end_ts = min(max_ts, end_ts)

		return self._get_bars(symbol, start_ts, end_ts, freq)

	def _get_bars(self, symbol, start_ts, end_ts, freq = None):

		if freq is None:
			freq = self.freq
		
		data = self.load_pickle(symbol, freq)
		end_ts = min(end_ts, ts_utils.cur_ts(freq))

		if not data.empty:
			if data.index[-1] < end_ts:
				data = self.load_bars(symbol, freq)
		else:
			data = self.load_bars(symbol, freq)

		return data.loc[start_ts:end_ts]

	def get_lastn_bars(self,symbol, num_bars, freq, lag=0):

		if hasattr(self, 'ts'):
			
			# this means we're in backtest mode 
			offset = ts_utils.get_offset(freq)
			start_ts = self.ts - offset*(num_bars+lag)
			end_ts = self.ts - offset*(lag+1)
			bars = self._get_bars(symbol, start_ts, end_ts,freq)
			return bars
			
		else:
			num_bars = min(num_bars, self.max_live_bars)

			live_bars = self.get_live_bars(freq)

			ts = ts_utils.cur_ts(freq)
			offset = ts_utils.get_offset(freq)
			start_ts = ts - offset*(num_bars+lag )#- 1 + 1)
			end_ts = ts - offset*(lag + 1)
		

			if symbol not in live_bars:
				self.stream_bars([symbol],freq)
				bars = self.load_bars(symbol,freq,
					start_ts = ts_utils.dt2unix(start_ts),live=True)
				bars = bars.loc[start_ts:end_ts]

				return bars 

			else:
				bars = live_bars[symbol]

				if bars.empty or (start_ts < bars.index[0]):

					hist = self.load_bars(symbol,freq,
						start_ts = ts_utils.dt2unix(start_ts)
						,live=True)

					if not bars.empty:
						for dt in bars.index:
							hist.loc[dt]=bars.loc[dt]

					bars = hist.iloc[-self.max_live_bars:]
					live_bars[symbol] = bars
					
					self.set_live_bars(live_bars,freq)
					return bars.loc[start_ts:end_ts]

				else:
					return bars.loc[start_ts:end_ts]

	def set_live_bars(self,bars,freq):
		freq = self.ccxt.timeframes[freq]
		self.live_bars[freq] = bars

	def get_live_bars(self,freq):
		live_bars = self.live_bars.get(
			self.ccxt.timeframes[freq],{})
		return live_bars

	def get_current_price(self, symbol):
		if hasattr(self, 'ts'):
			# backtesting mode 
			offset = ts_utils.get_offset(self.freq)
			end_ts = self.ts - offset
			bars = self._get_bars(symbol,None,end_ts,self.freq)
			return bars['close'].iloc[-1]

		else:
			if (symbol in self.ticks) and (self.ticks[symbol]>0):
				return self.ticks[symbol]

			else:
				if symbol not in self.ticks:
					self.stream_ticks([symbol])

				return self.api.fetchTicker(symbol)['last']

	def _update_live_bars(self,symbol,freq,ts,values):

		live_bars = self.live_bars.get(freq)
		live_bars[symbol].loc[ts] = values

		if len(live_bars[symbol]) > self.max_live_bars:

			first_ts = live_bars[symbol].index[0]
			live_bars[symbol] = live_bars[symbol].drop(first_ts)

		self.live_bars[freq] = live_bars













