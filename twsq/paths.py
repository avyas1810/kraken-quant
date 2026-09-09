import logging 
import os 

def safe_path(*args):
	
	PATH = os.path.join(*args)

	if not os.path.exists(PATH):
	    try:
	        os.makedirs(PATH)

	    except Exception as e:
	        logging.critical(
	            f"""Could not create required path because of the following error: {e}"""
	        )

	return PATH

def get_data_path():

	PATH = os.getenv('TWSQROOT')

	if not PATH:
		PATH = os.path.expanduser('~')
		PATH = os.path.join(PATH,'MyTWSQ')

	return PATH

ROOT = safe_path(get_data_path())
ALPHA_PATH = safe_path(ROOT, 'alphas')
DATA_PATH = safe_path(ROOT, 'data')

