"""Run the actual BTC strategy pipeline; demo is synthetic and not evidence."""
from kraken_quant.platform_cli import main
if __name__ == '__main__':
    import sys
    main(['backtest','--strategy','btc_momentum', *sys.argv[1:]])
