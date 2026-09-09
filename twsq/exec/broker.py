from .order import Order
import logging 
from datetime import datetime 
import pandas as pd 
from twsq.utils import ask_user, ts_utils, Emojis
import traceback 

class Broker:

	def __init__(self):
		
		self.name = self.__class__.__name__ 
		self._orders = []
		self._closed_orders = []
		self.is_backtesting = False
		self.pos = {}
		self.port_val = {}
		self.hist_pos = {}
		self.alphas = {}

	def wait_till_ready(self):
		return

	@property
	def crncy(self):
		if self.pricing.name=='Binance':
			crncy='USDT'
		else:
			crncy='USD'
		return crncy

	@property
	def all_orders(self):
		return self._orders + self._closed_orders

	@property
	def ts(self):
		if self.is_backtesting:
			return self.pricing.ts
		else:
			return datetime.utcnow()
			
	@property	
	def freq(self):
		return self.pricing.freq

	def create_order(
		self,
		strategy,
		symbol,
		qty,
		side,
		sec_type = None,
		limit_price = None,
		custom_id = None,
		route = False
	):
		# create new order with option to route 

		if sec_type is None:
			sec_type = self._default_sec_type

		if self.is_backtesting:
			start_ts = self.ts

		else:
			start_ts = datetime.utcnow()

		arrival_px = self.pricing.get_current_price(symbol)

		order = Order(strategy, symbol, qty, side, 
			sec_type, limit_price, custom_id, start_ts = start_ts,
			arrival_px = arrival_px)

		self.preprocess_order(order)

		if route:
			self.route(order)
		return order

	def preprocess_order(self,order):
		self._set_order_currency(order)

	def _set_order_currency(self,order):
		raise NotImplementedError

	def route(self, order):
		# route order and append to orders
		if self.is_backtesting:

			px_start = self.pricing.get_price_start(order.symbol)

			if px_start is None:
				cur_ts = self.ts.strftime('%d-%b-%y %H:%M:%S')
				msg = f'Pricing for {order.symbol} does not exit, '\
					  f'but attempted to route {order} on {cur_ts}. Please check pricing data.'
				raise Exception(msg)

			else:
				# offset b/c we begin on close				
				px_start = px_start + ts_utils.get_offset(self.freq)
				if self.ts < px_start:
					px_start = px_start.strftime('%d-%b-%y %H:%M:%S')
					cur_ts = self.ts.strftime('%d-%b-%y %H:%M:%S')
					msg = f'Pricing for {order.symbol} starts {px_start}, '\
						  f'but attempted to route {order} on {cur_ts}. Please adjust backtest start date.'
					raise Exception(msg)
		
		self._orders.append(order)
		try:
			self._route(order)
		except Exception as e:
			logging.error(f'{order} rejected')
			raise e


	def get_order(self, id_):
		for order in self._orders:
			if order.id == id_:
				return order

	def cancel_order(self,custom_id, strategy):
		for order in self._orders:
			if (order.custom_id == custom_id) \
				and order.strategy == strategy:
				
				self._cancel_order(order)

	def cancel_all_orders(self,strategy):
		for order in self._orders:
			if order.strategy == strategy:
				self._cancel_order(order)

	def get_pos(self,strategy):
		pos = self.pos.get(strategy)

		if pos is None:
			pos = dict()
			self.pos[strategy]=pos

		return pos

	def update_pos(self, strategy, base, quote, side, qty, ntn, fee):
		pos = self.get_pos(strategy)

		if side == 'sell':
			mult = -1
		else:
			mult = 1

		if base in pos:
			pos[base] += qty*mult
		else:
			pos[base] = qty*mult

		if quote in pos:
			pos[quote] += ntn*mult*-1
		else:
			pos[quote] = ntn*mult*-1
		
		pos[quote] -= fee

		if pos[base] == 0:
			pos.pop(base)

		if pos[quote] == 0:
			pos.pop(quote)

	def get_broker_pos(self):
		raise NotImplementedError

	def _load_other_pos(self):
		# load non-strategy positions 
		logging.info('Loading non-alpha positions')
		self._other_pos = self.get_broker_pos()
		for _ , strat in self.pos.items():
			for symbol, qty in strat.items():
				self._other_pos[symbol] = \
				self._other_pos.get(symbol,0) - qty 

	def align_pos(self):

		if not hasattr(self,'_other_pos'):
			self._load_other_pos()

		broker_pos = self.get_broker_pos()

		pos = {}
		for _ , strat in self.pos.items():
			for symbol, qty in strat.items():
				pos[symbol] = pos.get(symbol, 0) + qty 

		univ = set().union(pos.keys(), 
			broker_pos.keys(), self._other_pos.keys())

		matches = True

		for symbol in univ:
			broker = broker_pos.get(symbol,0)
			broker = round(broker, 
				self.api.currencies[symbol]['precision']-2)

			internal = pos.get(symbol,0) + \
				self._other_pos.get(symbol,0)
			internal = round(internal, 
				self.api.currencies[symbol]['precision']-2)
		
			if broker != internal :
				matches = False
				logging.error(
					f'Position mismatch {symbol}: {broker} broker, {internal} internal')
			
				import pdb
				pdb.set_trace()

		if matches:
			logging.info('Positions Match Broker')

		return

	def _process_fill(self, order, status, qty_filled, ntn_filled, avg_px, fee):

		fill = order.update_order(status, qty_filled, ntn_filled, avg_px, fee)

		if fill:		
			self.update_pos(order.strategy, order.base, order.quote, 
							order.side, fill['qty'], fill['ntn'], fill['fee'])

			if not self.is_backtesting:
				# for backtesting do this on the runner 1x to not slow things down

				# if fill['qty'] < self.round_asset_quantity(order.symbol,
				# 	order.qty,pair=True,mode='precision'):
					
				# 	logging.info('%s: %s Order to %s %s %s %s: %s filled @ %s' % (
				# 		order.strategy,
				# 		Emojis.order_filled, 
				# 		order.side, 
				# 		self.round_asset_quantity(order.base,order.qty), 
				# 		order.symbol, 
				# 		'fill', 
				# 		self.round_asset_quantity(order.base,fill['qty']), 
				# 		self.round_asset_quantity(order.quote,fill['ntn'] / fill['qty'])
				# 		))
				# else:

				logging.debug('%s: %s %s fill: %s' % (order.strategy,Emojis.order_filled,order,fill['qty']))
				self.snap_port(order.strategy)

		if status:
			if not self.is_backtesting:
				logging.debug('%s status update: %s' % (order, status))

			if status in ['closed','canceled']:
				if not self.is_backtesting:
					if status=='closed':
						logging.info('%s: %s Order to %s %s %s %s: %s filled @ %s' % (
							order.strategy,
							Emojis.order_filled, 
							order.side, 
							self.round_asset_quantity(order.base,order.qty), 
							order.symbol, 
							status,
							self.round_asset_quantity(order.base,order.qty_filled), 
							self.round_asset_quantity(order.quote,order.avg_px)
							))
					else:
						logging.info('%s: %s Order to %s %s %s %s' % (
							order.strategy,
							Emojis.order_canceled, 
							order.side,
							self.round_asset_quantity(order.base,order.qty),
							order.symbol, 
							status
							))

					#self.align_pos()
				
				if self.is_backtesting:
					order.end_ts = self.ts
				else:
					order.end_ts = datetime.utcnow()
					
				self._orders = [x for x in self._orders if x.id != order.id]
				# we reset _orders here. so anything looping through it 
				# will have the same contents even as we modify here and in 
				# on_finished_orders
				self._closed_orders.append(order)

				if status=='closed':
					self.alphas[order.strategy].on_finished_order(order)
					
	def snap_port(self,strategy):
		ts = self.ts

		hist_pos = self.hist_pos.get(strategy,{})
		hist_pos[ts] = self.get_pos(strategy).copy()
		self.hist_pos[strategy]=hist_pos
		
		port_val = self.port_val.get(strategy,pd.Series())
		port_val[ts] = self.get_port_val(strategy)
		self.port_val[strategy]=port_val

	def get_port_val(self,strategy):
		pos = self.get_pos(strategy).copy()
		val = 0
		for symbol, qty in pos.items():
			if symbol == self.crncy:
				val+=qty
			else:
				symbol = '%s/%s'%(symbol,self.crncy)
				
				price = self.pricing.get_current_price(symbol)
				val+=qty*price
		return val 

	# def get_pnl(self,strategy):

	# 	# val0 = self.port_val.get(strategy).iloc[0]
	# 	# val1 = self.port_val.get(strategy).iloc[-1]#self.get_port_val(strategy)
	# 	# pnl = val1 - val0

	# 	# logging.info(f'{strategy} | Total Session PnL: %.2f'%pnl)
	# 	return self.get_port_val(strategy) - val1
















