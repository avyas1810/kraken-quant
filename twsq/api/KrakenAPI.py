from .connections import get_conn_name, get_credential
import time 
import urllib.parse
import hashlib
import hmac
import base64
import requests 
import websocket 
from threading import Thread
import logging 
import traceback 
import json 
from functools import partial
from .python_apis import get_api
import gc 

class CryptoAPI:
	def __init__(self,exch_name):
		self.ws = {}
		self.ccxt = get_api(exch_name)


NAME = 'Kraken'

class KrakenAPI(CryptoAPI):

	def __init__(self):
		CryptoAPI.__init__(self,'Kraken')

		self.bars_socket_name = f'{NAME}_bars_socket'
		self.ticks_socket_name = f'{NAME}_tick_socket'
	def get_exch_symbol(self,symbol):
		wsname = self.ccxt.markets[symbol]['info']['wsname']
		if not hasattr(self,'_exch2ccxt'):
			self._exch2ccxt = {}
		self._exch2ccxt[wsname]=symbol 
		return wsname

	def get_ccxt_symbol(self,symbol):
		return self._exch2ccxt[symbol]

	# authentication methods
	@staticmethod
	def _get_rest_headers(uri_path,data={}):
		
		key = get_credential(NAME,'key')
		secret = get_credential(NAME,'secret')	

		headers = {}             
		headers['API-Key'] = key
		headers['API-Sign'] = KrakenAPI._get_rest_signature(uri_path,data,secret)
		return headers

	@staticmethod
	def _get_rest_signature(urlpath, data, secret):
		postdata = urllib.parse.urlencode(data)
		encoded = (str(data['nonce']) + postdata).encode()
		message = urlpath.encode() + hashlib.sha256(encoded).digest()

		mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
		sigdigest = base64.b64encode(mac.digest())
		return sigdigest.decode()

	@staticmethod
	def _get_nonce():
		return int(time.time() * 1000)

	@staticmethod
	def _post_auth_request(api_url, uri_path, headers, data):
		req = requests.post((api_url + uri_path), headers=headers, data=data)
		return req.json()

	@staticmethod
	def get_ws_auth():

		api_url = get_conn_name(NAME,'REST','private_url')
		uri_path = get_conn_name(NAME,'REST','web_auth')
		
		data = {'nonce': KrakenAPI._get_nonce()}
		headers = KrakenAPI._get_rest_headers(uri_path,data)
		req = KrakenAPI._post_auth_request(api_url, uri_path, headers, data)
		return req['result']['token']

	# Socket methods 
	@staticmethod
	def _on_open_orders(ws):

		token = KrakenAPI.get_ws_auth()
		sub = get_conn_name(NAME,'WS','orders')

		to_send = {}
		to_send["event"] = "subscribe"
		to_send["subscription"] = {'name':sub, 'token': token} 
		to_send = json.dumps(to_send)
		ws.send(to_send)

	@staticmethod
	def _send_bar_subscription(ws, interval, pairs):

		sub = get_conn_name(NAME,'WS','bars')

		to_send = {}
		to_send["event"] = "subscribe"
		to_send['subscription'] = {
			'name': sub,
			'interval':interval,
		}

		to_send['pair'] = pairs

		to_send = json.dumps(to_send)
		ws.send(to_send)

	@staticmethod
	def _send_tick_subscription(ws, pairs):

		sub = get_conn_name(NAME,'WS','ticks')

		to_send = {}
		to_send["event"] = "subscribe"
		to_send['subscription'] = {
			'name': sub
		}

		to_send['pair'] = pairs

		to_send = json.dumps(to_send)
		ws.send(to_send)

	@staticmethod
	def _on_error(ws, error):
		logging.error(traceback.format_exc())

	@staticmethod
	def _on_close(ws, close_status_code, close_msg):
		logging.info('websocket closed')
		logging.info(close_msg)

	@staticmethod
	def _on_reset():
		pass

	def start_orders_socket(self,on_message, on_reset):

		self._start_socket(
			on_open = KrakenAPI._on_open_orders,
			on_message = on_message,
			on_error = KrakenAPI._on_error,
			on_close = KrakenAPI._on_close,
			on_reset = on_reset,
			name = f'{NAME}_orders_socket',
			private = True
			)
		return


	# def start_bars_socket(self,on_message, freq, pairs,on_reset):
	# 	name =  f'{NAME}_bars_socket'
	# 	pairs = [self.get_exch_symbol(x) for x in pairs]

	# 	if name in self.ws:
	# 		ws = self.ws[name]
	# 		KrakenAPI._on_open_bars(ws, freq, pairs)

	# 	else:
	# 		on_open = partial(
	# 			KrakenAPI._on_open_bars,
	# 			interval = freq,
	# 			pairs = pairs
	# 			)

	# 		self._start_socket(
	# 			on_open = on_open,
	# 			on_message = on_message,
	# 			on_error = KrakenAPI._on_error,
	# 			on_close = KrakenAPI._on_close,
	# 			on_reset = on_reset,
	# 			name = name,
	# 			)

	# 	return

	def start_bars_socket(self,on_message,on_reset):
		self._start_socket(
			on_open = None,
			on_message = on_message,
			on_error = KrakenAPI._on_error,
			on_close = KrakenAPI._on_close,
			on_reset = on_reset,
			name = self.bars_socket_name,
			)

	def send_bar_subscription(self, freq, pairs):
		# assumes you have already started the bar socket 
		pairs = [self.get_exch_symbol(x) for x in pairs]
		ws = self.ws[self.bars_socket_name]
		KrakenAPI._send_bar_subscription(ws, freq, pairs)

	def socket_status(self,name):
		if name == 'bars':
			return self.bars_socket_name in self.ws
		elif name == 'ticks':
			return self.ticks_socket_name in self.ws
		else:
			raise NotImplementedError 


	def start_ticks_socket(self,on_message, on_reset):
		self._start_socket(
			on_open = None,
			on_message = on_message,
			on_error = KrakenAPI._on_error,
			on_close = KrakenAPI._on_close,
			on_reset = on_reset,
			name = self.ticks_socket_name
			)

	def send_ticks_subscription(self,pairs):
		pairs = [self.get_exch_symbol(x) for x in pairs]
		ws = self.ws[self.ticks_socket_name]
		KrakenAPI._send_tick_subscription(ws,pairs)


	# def start_tick_socket(self,on_message, pairs, on_reset):
	# 	name = f'{NAME}_tick_socket'
	# 	pairs = [self.get_exch_symbol(x) for x in pairs]

	# 	#try:
	# 	if name in self.ws:
	# 		ws = self.ws[name]
	# 		KrakenAPI._on_open_ticks(ws,pairs)

	# 	else:
	# 		on_open = partial(
	# 			KrakenAPI._on_open_ticks,
	# 			pairs = pairs
	# 			)

	# 		self._start_socket(
	# 			on_open = on_open,
	# 			on_message = on_message,
	# 			on_error = KrakenAPI._on_error,
	# 			on_close = KrakenAPI._on_close,
	# 			on_reset = on_reset,
	# 			name = name
	# 			)
		# except Exception as e:
		# 	import pdb
		# 	pdb.set_trace()
			
		return

	def _start_socket(self,on_open, on_message, on_error, on_close, 
			on_reset, name, private=False):

		if private:
			api_url = get_conn_name(NAME,'WS','private_url')
		else:
			api_url = get_conn_name(NAME,'WS','public_url')

		#websocket.enableTrace(True)

		def _run_forever(self,api_url,on_open,on_message,on_error,on_close,on_reset,name):
			while True:
				try:
					ws = websocket.WebSocketApp(
						api_url, 
						on_open = on_open, 
						on_message = on_message,
						on_error = on_error,
						on_close = on_close)

					self.ws[name] = ws

					ws.run_forever(
						skip_utf8_validation=True,
						ping_interval=10,
						ping_timeout=8)

				except Exception as e:
					gc.collect()
					logging.error(f"{name} connection Error : {0}".format(e))   

				finally:
					on_reset()
					logging.info(f"Reconnecting {name} after 5 sec")
					ws.close()
					time.sleep(5)

		orders_socket = Thread(
			target=_run_forever, daemon=True, name=name,
			args = [self,api_url,on_open,on_message,on_error,on_close,on_reset,name])
		orders_socket.start()
		
		# import rel 
		# rel.safe_read()
		# ws.run_forever(dispatcher=rel)
		# orders_socket = Thread(
		# 	target=rel.dispatch, daemon=True, name=name
		# )
		# orders_socket.start()
		return

	@staticmethod
	def process_general_msg(msg,prefix=''):

		if 'event' in msg:

			event = msg['event']

			if event == 'systemStatus':
				status = msg['status']
				logging.debug(f'{prefix}: System Status: {status}')

			elif event == 'subscriptionStatus':
				name = msg['subscription']['name']
				status = msg['status']
				logging.debug(f'{prefix}: Subscription Status to {name}: {status}')

			elif event == 'heartbeat':
				logging.debug(f'{prefix}:<3')

			else:
				logging.warning(f'{prefix}: Unrecognized msg: {msg}')

		else:
			logging.warning(f'{prefix}: Unrecognized msg: {msg}')

	@staticmethod
	def parse_msg(msg):
		return json.loads(msg)

		


