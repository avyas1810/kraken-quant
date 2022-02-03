from .broker import Broker
from twsq.api import get_api, get_conn_name, get_credential, KrakenAPI
from twsq.data import KrakenPrices
from threading import Lock, Event
import logging
import pandas as pd 

class Kraken(Broker):
	
	def __init__(self):

		Broker.__init__(self)

		self._default_sec_type = 'crypto'
		self._ack_lock = Lock()
		self._order_socket_ready = Event()
		self.api = get_api(self.name)
		self._start_orders_socket()
		self.pricing = KrakenPrices()

	def wait_till_ready(self):
		self._order_socket_ready.wait()

	def round_asset_quantity(self,asset,quantity,pair = False,mode='display'):

		if not pair:
			if mode=='display':
				precision = int(self.api.currencies[asset]['info']['display_decimals'])
			elif mode=='precision':
				precision = self.get_asset_precision(asset)
		else:
			if mode == 'precision':
				precision = self.api.markets[asset]['precision']['amount']

		return round(quantity,precision)

	def get_asset_precision(self,asset):
		return self.api.currencies[asset]['precision']

	def get_broker_pos(self):
		return self.api.fetchBalance()['total']

	def _set_order_currency(self,order):
		base = self.api.markets[order.symbol]['base']
		quote = self.api.markets[order.symbol]['quote']
		order.set_currency(base, quote)

	def get_min_order_qty(self,symbol):
		return self.api.markets[symbol]['limits']['amount']['min']

	def _route(self,order):
		self.wait_till_ready()
		with self._ack_lock:
			resp = self.api.create_order(
				order.symbol, 
				order.type,
				order.side, 
				order.qty, 
				order.limit_price
				)

			order.set_id(resp['id'])

	def _cancel_order(self,order):
		resp = self.api.cancel_order(order.id)
		if len(resp['error']):
			logging.error(str(resp['error']))

	def _on_order_msg(self,ws,msg):
		

		msg = KrakenAPI.parse_msg(msg)
		if type(msg)==list and msg[1]=='openOrders':
			for updates in msg[0]:
				for id_, update in updates.items():
					order = self.get_order(id_)
					if order is None:
						with self._ack_lock:
							logging.debug('Ack lock engaged')
							order = self.get_order(id_)
							
					if order is None:
						logging.warning(f'Received unrecognized order: {id_}')
						continue

					else:

						status = update.get('status', order.status)
						qty_filled = float(update.get('vol_exec', order.qty_filled))
						ntn_filled = float(update.get('cost', order.ntn_filled))
						avg_px = float(update.get('avg_price', order.avg_px))
						fee = float(update.get('fee', order.fee))
						self._process_fill(order,status,qty_filled,ntn_filled,avg_px,fee)


		elif 'event' in msg \
			and msg['event']=='subscriptionStatus':
			# overriding subscription status to set lock 
			if msg['status'] == 'subscribed':
				KrakenAPI.process_general_msg(msg,prefix='Order Socket')
				self._order_socket_ready.set()
				# from time import sleep 
				# sleep(5)
				# ws.close()	
		else:
			KrakenAPI.process_general_msg(msg,prefix='Order Socket')

		return 

	def _on_order_socket_reset(self):
		logging.info('Clearing order socket ready')
		## here should clean up any missed order messages
		self._order_socket_ready.clear()
			
	def _start_orders_socket(self):
		KrakenAPI().start_orders_socket(
				self._on_order_msg, 
				self._on_order_socket_reset)













	


	