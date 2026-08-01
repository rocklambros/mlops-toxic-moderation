import ast
from pathlib import Path

import numpy as np
import pytest

from model.contract import probs_to_dict
from model.labels import LABELS


def test_probs_to_dict_maps_positionally_in_label_order():
    row = np.array([0.9, 0.1, 0.4, 0.03, 0.7, 0.05])
    out = probs_to_dict(row)
    assert list(out.keys()) == list(LABELS)
    assert out["toxic"] == pytest.approx(0.9)
    assert out["identity_hate"] == pytest.approx(0.05)


def test_probs_to_dict_returns_plain_floats():
    out = probs_to_dict(np.array([0.1] * 6, dtype=np.float32))
    assert all(type(value) is float for value in out.values())


def test_probs_to_dict_rejects_a_wrong_length_row():
    with pytest.raises(ValueError, match="expected 6 probabilities"):
        probs_to_dict(np.array([0.1, 0.2, 0.3]))


def test_backend_never_re_derives_the_label_zip():
    """H23: three call sites zipping LABELS independently mislabel probabilities silently
    if column order ever drifts, and the order-blind contract validator cannot see it."""
    assert list(Path("backend").rglob("*.py")), "the scan found no files to scan"
    offenders = []
    for path in sorted(Path("backend").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "zip":
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                if "LABELS" in names:
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        f"use model.contract.probs_to_dict instead of zip(LABELS, ...): {offenders}"
    )
