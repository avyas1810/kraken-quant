from datetime import datetime
import pandas as pd 

class ts_utils:
	

	@staticmethod
	def unix2dt(unix,unit='pico'):

		if unit == 'pico':
			div = 1000

		elif unit == 'nano':
			div = 1
			
		else:
			div = 1

		unix = int(unix / div)
		return datetime.utcfromtimestamp(unix)

	@staticmethod
	def dt2unix(dt,unit='pico'):
		if unit == 'pico':
			mult = 1000
		else:
			mult = 1

		unix = dt.timestamp()
		unix = int(unix*mult)
		return unix 

	@staticmethod
	def get_offset(freq):
		qty, unit = ts_utils._parse_freq(freq)

		if unit == 'm':
			offset = pd.tseries.offsets.Minute(qty)

		elif unit == 'h':
			offset = pd.tseries.offsets.Hour(qty)

		elif unit == 'd':
			assert qty==1, 'quantity must be 1 for daily freq'
			offset = pd.tseries.offsets.Day()

		elif unit == 'w':
			raise ValueError('Weekly frequency not yet supported')
			offset = pd.tseries.offsets.Week(weekday=0)

		elif unit == 'M':
			assert qty==1, 'quantity must be 1 for monthly freq'
			offset = pd.tseries.offsets.MonthEnd()

		else:
			raise NotImplementedError
		
		return offset

	@staticmethod
	def _parse_freq(freq):
		return int(freq[:-1]), freq[-1]

	@staticmethod
	def last_bar(ts,freq):
		ts = pd.to_datetime(ts)

		offset = ts_utils.get_offset(freq)

		try:
			return ts.floor(offset)

		except ValueError:
			return offset.rollback(ts.floor('D'))
			# base = offset.base
			# res = base.rollback(ts.floor('D'))
			# return res - (offset.n - 1)*base

	@staticmethod
	def next_bar(ts,freq):
		ts = pd.to_datetime(ts)
		offset = ts_utils.get_offset(freq)

		try:
			floored = ts.floor(offset)

			if floored==ts:
				return ts
			
			return floored + offset

		except ValueError:
			return offset.rollforward(ts.floor('D'))

	@staticmethod
	def cur_ts(freq=None):
		ts = datetime.utcnow()
		
		if freq is None:
			return ts

		else:
			return ts_utils.last_bar(ts,freq)

	@staticmethod
	def pandas_freq(freq):
		freq_map = {'m':'min','M':'m','d':'d','w':'w','h':'h'}
		qty, unit = ts_utils._parse_freq(freq)
		return '%s%s'%(qty,freq_map[unit])
	
	@staticmethod
	def date_range(start_ts,end_ts,freq):
		return pd.date_range(start_ts,end_ts,freq=ts_utils.pandas_freq(freq))





