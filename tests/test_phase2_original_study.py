import hashlib

import pandas as pd
import pytest

from research.phase2.original_study import function_definitions, verified_pickle


def test_bad_hash_rejected_before_pickle_deserialization(tmp_path, monkeypatch):
    path = tmp_path / "untrusted.pkl"
    path.write_bytes(b"not a pickle")
    monkeypatch.setattr(pd, "read_pickle", lambda *args: pytest.fail("unverified pickle was opened"))
    with pytest.raises(ValueError, match="hash mismatch"):
        verified_pickle(path, "0"*64)


def test_verified_pickle_uses_exact_input_hash(tmp_path):
    frame = pd.DataFrame({"value": [1., 2.]})
    path = tmp_path / "trusted.pkl"
    frame.to_pickle(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pd.testing.assert_frame_equal(verified_pickle(path, digest), frame)


def test_function_extraction_preserves_engine_and_skips_experiments():
    source = "def untouched(x):\n    return x * 2\n\nraise RuntimeError('must not execute')\n"
    namespace = {}
    exec(function_definitions(source), namespace)
    assert namespace["untouched"](3) == 6
    with pytest.raises(ValueError):
        function_definitions("print('no engine')")
