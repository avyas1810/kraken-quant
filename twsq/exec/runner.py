from time import sleep, time 
import logging 
import traceback
from twsq.utils import ts_utils, Emojis, print_pct_done
import pandas as pd 
from datetime import datetime 

class Runner:

	def __init__(self, alpha, run_event):

		self.alpha = alpha 
		self.run_event = run_event

	@property
	def freq(self):
		return self.alpha.freq

	def sleep(self,first=False):
		if type(self.freq) == str:
			
			cur_ts = ts_utils.cur_ts()
			next_bar = ts_utils.next_bar(cur_ts,self.freq)
			# sleep_time = (next_bar - cur_ts).seconds			
			# sleep(sleep_time)
			
			while (ts_utils.cur_ts() < next_bar) \
				and (self.run_event.isSet()):
				sleep(1)
		else:
			if not first:

				end = time() + self.freq

				while (time() < end) \
					and (self.run_event.isSet()):
					sleep(1)


	def _run(self):
		self.alpha._track_pnl()
		self.sleep(first=True)

		while self.run_event.isSet():
			self.alpha.manage_open_orders()
			self.alpha._n_orders = 0
			self.alpha.rebalance()
			self.alpha.logging('%s Rebalance complete: Ceated %s new order(s).' 
				% (Emojis.rebal,self.alpha._n_orders))
			self.sleep()

		return

	def _prepare(self):
		self.alpha._prepare()

	def _finish(self):
		self.alpha._finish()
		return

	def _on_crash(self,e):
		return

	def run(self):
		self.alpha.wait_till_ready()
		try:
			self._prepare()
			self._run()

		except KeyboardInterrupt as e:
			pass

		except Exception as e:
			logging.error(e)
			logging.error(traceback.format_exc())
			self._on_crash(e)

		finally:
			self._finish()

class BacktestRunner(Runner):

	def __init__(self, alpha, start_ts=None, end_ts=None):

		Runner.__init__(self, alpha, None)

		if end_ts is None:
			end_ts = ts_utils.cur_ts(self.freq)

		if start_ts is None:
			start_ts = end_ts - pd.tseries.offsets.Day()*365

		if type(end_ts)==str:
			end_ts = pd.to_datetime(end_ts)

		if type(start_ts)==str:
			start_ts = pd.to_datetime(start_ts)

		assert start_ts <= end_ts, "start_ts must be <= end_ts"

		self.start_ts  = ts_utils.next_bar(start_ts, self.freq)
		self.end_ts = min(ts_utils.cur_ts(self.freq), ts_utils.last_bar(end_ts, self.freq))

		# must initialize time for use in load_pos
		self.alpha.broker.pricing.ts = start_ts - ts_utils.get_offset(self.freq)

	def _run(self):
		

		# logging.info('Launching %s backtest: %s > %s @ freq %s'% (
		# 		self.alpha.name,
		# 		self.start_ts.strftime('%d-%b-%y %H:%M:%S'),
		# 		self.end_ts.strftime('%d-%b-%y %H:%M:%S'),
		# 		self.freq
		# 	 ))

		ts = self.start_ts
		ts_delta = ts_utils.get_offset(self.freq)
		
		last_log_ts = time()

		ts0 =time()
		while (ts <= self.end_ts):

			if (time() - last_log_ts > 1) or (ts == self.end_ts):

				# pct_done = (ts.timestamp() - self.start_ts.timestamp()) \
				# 	/ (self.end_ts.timestamp() - self.start_ts.timestamp())
					
				# if pct_done < 0.99:
				#pct_done = ("{:.%df}" % 0).format(pct_done*100)
				#date = ts.strftime('%d-%b-%y %H:%M:%S')
				pnl = '{:,}'.format(int(self.alpha.get_port_val().iloc[-1]))
				#logging.info(f'Backtest {pct_done}% Done, Date: {date}, Total PnL: ${pnl}')
				duration = round(time() - ts0)
				print_pct_done(ts.timestamp(), self.start_ts.timestamp(), 
					self.end_ts.timestamp(),
					prefix=f'Running {self.alpha.name} backtest:',
					suffix=f'done | Total PnL ({self.alpha.broker.crncy}): {pnl} | Duration (s): {duration}  ')

				last_log_ts = time()

			# pass down backtest params
			self.alpha.broker.pricing.ts = ts
			self.alpha.broker.pricing.freq = self.freq

			# fill orders
			self.alpha.broker.fill_orders(self.freq)
			self.alpha.manage_open_orders()
			self.alpha.rebalance()
			self.alpha.broker.fill_orders(self.freq,'market')

			# update pos 
			self.alpha.snap_port()

			# update ts
			ts += ts_delta


		







