"""All mathematics reserved for user implementation after Milestone 10."""
from .cost_contracts import CostModelInputs, CostResult


class CostAlgorithms:
    def calculate_spread_cost(self, inputs: CostModelInputs) -> CostResult:
        raise NotImplementedError("Algorithm reserved for user implementation after Milestone 10")

    def calculate_book_execution(self, inputs: CostModelInputs) -> CostResult:
        raise NotImplementedError("Algorithm reserved for user implementation after Milestone 10")

    def calculate_slippage(self, inputs: CostModelInputs) -> CostResult:
        raise NotImplementedError("Algorithm reserved for user implementation after Milestone 10")

    def calculate_market_impact(self, inputs: CostModelInputs) -> CostResult:
        raise NotImplementedError("Algorithm reserved for user implementation after Milestone 10")

    def calculate_total_cost(self, inputs: CostModelInputs) -> CostResult:
        raise NotImplementedError("Algorithm reserved for user implementation after Milestone 10")
