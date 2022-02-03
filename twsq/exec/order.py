import pandas as pd 

class Order:

	def __init__(
		self,
		strategy,
		symbol,
		qty,
		side,
		sec_type,
		limit_price = None,
		custom_id = None,
		start_ts = None,
		arrival_px = None,
		):

		self.strategy = strategy
		self.symbol = symbol
		self.qty = qty 
		self.side = side
		self.sec_type = sec_type
		self.limit_price = limit_price
		
		if self.limit_price is None:
			self.type = 'market'
		else:
			self.type = 'limit'

		self.custom_id = custom_id
		self.id = None

		self.status = 'new'
		self.qty_filled = 0
		self.ntn_filled = 0
		self.avg_px = 0
		self.fee = 0
		self.start_ts = start_ts
		self.arrival_px = arrival_px

	def __repr__(self):
		repr_ = f'Order to {self.side} {self.qty} {self.symbol}'
		return repr_
		
	# def set_arr_px(self,px):
	# 	self.arr_px = px

	def set_id(self, id_):
		self.id = id_

	def set_currency(self,base,quote):
		self.base = base 
		self.quote = quote 

	def update_order(self, status, qty_filled, ntn_filled, avg_px, fee):

		if qty_filled != self.qty_filled or \
			ntn_filled != self.ntn_filled or \
			fee != self.fee:

			fill = {
					'qty': qty_filled - self.qty_filled,
					'ntn': ntn_filled - self.ntn_filled,
					'fee': fee - self.fee,
					'status': status
					}

		else:
			fill = None 

		self.status = status 
		self.qty_filled = qty_filled
		self.ntn_filled = ntn_filled
		self.avg_px = avg_px
		self.fee = fee

		return fill




