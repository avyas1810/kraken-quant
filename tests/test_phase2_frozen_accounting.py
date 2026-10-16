import numpy as np
import pytest

from research.phase2.frozen_accounting import batch_frozen_accounting


def scalar_reference(weights, returns, initial, rate):
    """Independent scalar translation of original frozen notebook accounting."""
    positions = initial.copy()
    equity = 1.0
    result = []
    for t, (target, change) in enumerate(zip(weights, returns)):
        previous = equity
        exposure = np.abs(positions / previous).sum()
        pnl = float(np.dot(positions, change))
        before = previous + pnl
        positions = positions * (1 + change)
        pretrade = positions / before
        after = before
        if t != len(returns) - 1:
            if rate:
                low, high = 0., before
                for _ in range(55):
                    mid = (low + high) / 2
                    traded = np.abs(target * mid - positions).sum()
                    if mid + rate * traded > before:
                        high = mid
                    else:
                        low = mid
                after = (low + high) / 2
            positions = target * after
        notional = np.abs(positions - pretrade * before).sum()
        result.append([after / previous - 1, pnl / previous,
                       notional / previous, exposure, (before - after) / previous])
        equity = after
    return np.array(result)


@pytest.mark.parametrize("cost_bps", [0., 20.])
@pytest.mark.parametrize("carried", [False, True])
def test_batch_matches_scalar_frozen_equations(cost_bps, carried):
    rng = np.random.default_rng(82)
    momentum = rng.normal(size=(30, 4))
    momentum /= np.abs(momentum).sum(axis=1)[:, None]
    returns = rng.normal(0, .01, size=momentum.shape)
    masks = rng.integers(0, 2, size=(7, 30))
    initial = np.array([.2, -.3, .1, -.1]) if carried else np.zeros(4)
    outputs = batch_frozen_accounting(momentum, returns, masks,
                                      initial_held=initial, cost_bps=cost_bps)
    for trial in range(len(masks)):
        expected = scalar_reference(momentum * masks[trial, :, None], returns,
                                    initial, cost_bps / 10000)
        for column, key in enumerate(outputs):
            np.testing.assert_allclose(outputs[key][trial], expected[:, column], atol=1e-12, rtol=0)


def test_hand_calculated_self_financing_and_final_no_rebalance():
    output = batch_frozen_accounting([[1.], [0.]], [[0.], [.1]], [[1, 0]])
    assert output["net_return"][0, 0] == pytest.approx(1 / 1.002 - 1)
    assert output["net_return"][0, 1] == pytest.approx(.1)
    assert output["turnover"][0, 0] == pytest.approx(1 / 1.002)
    assert output["turnover"][0, 1] == pytest.approx(0, abs=1e-15)
    assert output["gross_exposure"][0, 1] == pytest.approx(1)


def test_invalid_input_rejected():
    with pytest.raises(ValueError):
        batch_frozen_accounting([[2.]], [[0.]], [[1]])
    with pytest.raises(ValueError):
        batch_frozen_accounting([[1.]], [[-1.]], [[1]])


def test_batch_against_verbatim_frozen_notebook_functions():
    import ast
    import json
    from pathlib import Path
    import pandas as pd
    source_path = Path(__file__).resolve().parents[1] / "research/phase1_frozen/source_cells.json"
    cells = json.loads(source_path.read_text(encoding="utf-8"))["cells"]
    source = next(cell["source"] for cell in cells
                  if cell["prefix"].startswith("# SECTION 8: Exact self-financing"))
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {"np": np, "pd": pd}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source_path), "exec"), namespace)
    rng = np.random.default_rng(71)
    targets = rng.normal(size=(24, 3))
    targets /= np.abs(targets).sum(axis=1)[:, None]
    changes = rng.normal(0, .02, size=targets.shape)
    masks = rng.integers(0, 2, size=(4, len(targets)))
    index = pd.date_range("2020", periods=len(targets), freq="4h")
    output = batch_frozen_accounting(targets, changes, masks)
    for trial, mask in enumerate(masks):
        expected = namespace["compute_transaction_costs"](
            pd.DataFrame(targets * mask[:, None], index=index),
            returns=pd.DataFrame(changes, index=index), cost_bps=20.)
        for actual_key, expected_key in [("net_return", "net_returns"),
                                         ("gross_return", "gross_returns"),
                                         ("turnover", "turnover"),
                                         ("trading_costs", "trading_costs")]:
            np.testing.assert_allclose(output[actual_key][trial], expected[expected_key],
                                       atol=1e-12, rtol=0)
        np.testing.assert_allclose(output["gross_exposure"][trial],
                                   expected["lagged_weights"].abs().sum(axis=1), atol=1e-12, rtol=0)
