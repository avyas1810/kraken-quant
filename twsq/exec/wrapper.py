from .kraken import Kraken

def get_broker(broker,**kwargs):
	
	if type(broker)!=str:
		return broker 
		
	elif broker == 'Kraken':
		broker = Kraken
	
	else:
		raise ValueError(f"Unsupported broker <{broker}>")
	
	return broker(**kwargs)


