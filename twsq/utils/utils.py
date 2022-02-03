import logging 
import sys 
import os 
from datetime import datetime 
from telegram_handler import TelegramHandler
from pathlib import Path 
import yaml

class Emojis:
	#https://unicode.org/emoji/charts/full-emoji-list.html
	pnl = "\U0001F4B0"
	order_filled = "\U0001F7E2"
	order_canceled = "\U0001F534"
	start = "\U0001F331"
	rebal = "\U0001F680"
	exit = "\U0001F44B"
	
def get_twsqroot():
	from twsq.paths import ROOT
	print (ROOT)
	return ROOT

def get_settings(*args):
	from twsq.paths import ROOT
	settings_path = Path(os.path.join(ROOT,'settings.yml'))

	try:
		with settings_path.open("r") as f:
			settings = yaml.safe_load(f.read())
	except FileNotFoundError:
		settings = dict()

	res = settings
	for x in args:
		res = res.get(x)
		if res is None:
			return
	return res


def set_logging(debug=False,telegram_log=False,logfile = None):
	# configure logging

	logformat = "%(asctime)s: %(levelname)s: %(message)s"
	datefmt = '%d-%b-%y %H:%M:%S'

	if debug:
		level = logging.DEBUG
	else:
		level = logging.INFO

	handlers = []
	handlers.append(logging.StreamHandler(stream=sys.stdout))

	if logfile:
		# logfile = os.path.join(log_path,
		# 		'%s.log' % datetime.now().strftime('%m%d%y_%H%M%S'))
		handlers.append(logging.FileHandler(logfile, mode="w"))

	if telegram_log:
		tele_settings = get_settings('telegram')
		if tele_settings:
			token = tele_settings['token']
			chat_id = tele_settings['chat_id']

			if token and chat_id:
				handler = TelegramHandler(token,chat_id)
				formatter = logging.Formatter(logformat,
				datefmt = datefmt)
				handler.setFormatter(formatter)
				handler.setLevel(logging.INFO)
				handlers.append(handler)



	logging.basicConfig(
		handlers = handlers,
		format = logformat, 
		datefmt = datefmt, 
		level = level,
		force = True)


	return

def print_pct_done(
	value,
	start_value,
	end_value,
	file=sys.stdout,
	prefix="Pct Complete",
	suffix="",
	decimals=2,
):
	total_length = end_value - start_value
	current_length = value - start_value
	percent = min((current_length / total_length) * 100, 100)
	percent_str = (" {:.%df}" % decimals).format(percent)
	percent_str = percent_str[-decimals - 4 :]
	
	terminal_length, _ = os.get_terminal_size()
	pad = terminal_length - len(prefix) - 1 -len(percent_str) - 2 - len(suffix)
	line = f"\r{prefix} {percent_str}% {suffix}" + ' '*pad
	# if value >= end_value:
	# 	line = f"{line}\n"

	file.write(line)
	file.flush()
	return line

def ask_user(msg, answers=('yes', 'no')):
	print ('%s (%s)' % (msg, '/'.join(answers)))
	while True:
		ans = input('>> ')
		if ans == answers[0]:
			return True
		elif ans == answers[1]:
			return False
		print ('invalid answer \'%s\', try again...' % ans)