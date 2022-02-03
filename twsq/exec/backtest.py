from .broker import Broker 
from twsq.data import BinancePrices
from twsq.api import get_api, BinanceAPI 

class BacktestBroker(Broker):

	def __init__(self):
		Broker.__init__(self)
		self.is_backtesting = True


	def _route(self,order):
		if not hasattr(self,'_order_id'):
			self._order_id = 0

		order.set_id(self._order_id)
		self._order_id+=1

	def _cancel_order(self, order):
		status = 'canceled'
		self._process_fill(order,status,order.qty_filled,
			order.ntn_filled,order.avg_px,order.fee)

	def fill_orders(self, freq, order_type='all'):
		ct = 0 
		for order in self._orders:	
			
			ct+=1
			avg_px = 0 
			if (order.type != order_type) and (order_type!='all'):
				continue

			elif (order.type=='market'):

				bar = self.pricing.get_lastn_bars(order.symbol,1,freq)
				if order.start_ts < self.pricing.ts:
					# missing bar led to not being filled
					field = 'open'

				else:
					field = 'close'

				if bar.empty:
					avg_px = 0

				else:
					avg_px = bar.iloc[-1][field]

					if order.side=='buy':
						avg_px = avg_px + avg_px*self.slip
					else:
						avg_px = avg_px - avg_px*self.slip


			elif (order.type=='limit'):

				bar = self.pricing.get_lastn_bars(order.symbol,1,freq)
				if bar.empty:
					avg_px = 0

				else:
					bar = bar.iloc[-1]
					limit_price = order.limit_price

					if order.side == 'sell':

						if (order.status=='new') \
							and (limit_price <= bar['open']):
							# marketable limit order
							avg_px = bar['open']

						elif limit_price <= bar['high']:
							avg_px = limit_price

						else:
							pass

					elif order.side == 'buy':

						if (order.status=='new') \
							and (limit_price >= bar['open']):
							# marketable limit order
							avg_px = bar['open']

						elif limit_price >= bar['low']:
							avg_px = limit_price

						else:
							pass

			if avg_px > 0:

				status = 'closed'
				qty_filled = order.qty 
				ntn_filled = avg_px*qty_filled

				if order.type=='limit':
					fee = ntn_filled*self.maker_fee

				elif order.type=='market':
					fee = ntn_filled*self.taker_fee

				self._process_fill(
					order, status, qty_filled,
					ntn_filled, avg_px, fee
					)

			elif order.status=='new':
				order.status = 'open'


class BacktestBinance(BacktestBroker):

	def __init__(self,taker_fee = None, maker_fee = None, slip = None):
		BacktestBroker.__init__(self)

		self.pricing = BinancePrices()
		self._default_sec_type = 'crypto'
		self.api = get_api('Binance')

		if maker_fee is None:
			maker_fee = 16e-4

		if taker_fee is None:
			taker_fee = 26e-4

		if slip is None:
			slip = 10e-4

		self.maker_fee = maker_fee
		self.taker_fee = taker_fee
		self.slip = slip

	def _set_order_currency(self,order):
		base = self.api.markets[order.symbol]['base']
		quote = self.api.markets[order.symbol]['quote']
		order.set_currency(base, quote)

	def create_order(
			self,
			strategy,
			symbol,
			qty,
			side,
			**kwargs,
		):
	
		# create new order with option to route 
		symbol = BinanceAPI.usd2usdt(symbol)
		return BacktestBroker.create_order(self,strategy,symbol,qty,side,**kwargs)










