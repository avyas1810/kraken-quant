from twsq.alpha import Alpha


class ETHDCA(Alpha):

    def prepare(self):
        self.first_rebalance = True

    def rebalance(self):
        if self.first_rebalance:
            self.create_order(
                symbol="ETH/USD",
                qty=0.01,
                side="buy",
                route=True
            )

            self.first_rebalance = False


result = ETHDCA.run_backtest(
    start_ts="20240101",
    end_ts="20240201",
    freq="1d",
    name="FirstBacktest"
)

print(result.pos_pnl.tail())
print(result.orders)