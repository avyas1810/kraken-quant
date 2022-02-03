import os
import logging 
from twsq.paths import ROOT 
import yaml
from pathlib import Path 

# get api connection urls / endpoints
connections = {
    'Kraken': {
    	'REST':{
    		'public_url':'https://api.kraken.com',
            'private_url':'https://api.kraken.com',
            'web_auth':'/0/private/GetWebSocketsToken',
    	},

	    'WS': {
	    	'public_url': 'wss://ws.kraken.com',
	    	'private_url':'wss://ws-auth.kraken.com',
            'orders':'openOrders',
            'bars':'ohlc',
            'ticks':'trade'
        }
    },
}

def get_conn_name(source,api_type,item):
	return connections.get(source, dict()).get(api_type,dict()).get(item)

# Load database credentials from credentials yaml file
# Handle missing credentials gracefully

credentials_path = Path(os.path.join(ROOT,'settings.yml'))

try:
    with credentials_path.open("r") as f:
        credentials = yaml.safe_load(f.read())
except FileNotFoundError:
    credentials = dict()

def get_credential(group, item):
    return credentials.get('exchange_apis',dict()).get(group, dict()).get(item)
