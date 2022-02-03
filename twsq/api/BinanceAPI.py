import re 

class BinanceAPI:

	@staticmethod
	def usd2usdt(symbol):

		if re.match('^.+/USD$',symbol):
			return symbol.replace('/USD','/USDT')

		elif re.match('^USD/.+$',symbol):
			return symbol.replace('USD/','USDT/')

		else:
			return symbol