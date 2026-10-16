"""Run the actual multi-asset regime/volatility strategy pipeline."""
from kraken_quant.platform_cli import main
if __name__ == '__main__':
    import sys
    main(['backtest','--strategy','multi_momentum', *sys.argv[1:]])
