from .base_prices import BasePrices
import logging 
from twsq.api import BinanceAPI 

class BinancePrices(BasePrices):

	def __init__(self):

		BasePrices.__init__(self)
		logging.debug('Using Binance prices: will substitute USDT for USD. Binance historical pricing does not have USD')

	def get_current_price(self, symbol):
		if hasattr(self,'ts'):
			# backtesting mode
			symbol = BinanceAPI.usd2usdt(symbol)
			
		return BasePrices.get_current_price(self,symbol)

	def get_bars(self, symbol, freq = '1d', start_ts = None, end_ts = None):
		symbol = BinanceAPI.usd2usdt(symbol)
		return BasePrices.get_bars(self,symbol,freq,start_ts,end_ts)

	def get_lastn_bars(self,symbol,num_bars,freq,lag=0):
		if hasattr(self,'ts'):
			symbol = BinanceAPI.usd2usdt(symbol)

		return BasePrices.get_lastn_bars(self,symbol,num_bars,freq,lag)