"""One array-to-dict adapter, and nobody re-derives it.

Premortem H23. Three call sites zipping LABELS independently mislabel every probability the
day column order drifts, and the contract validator that would catch it is order-blind by
construction -- it checks that six keys are present, which a transposed row satisfies
perfectly. `tests/unit/test_probs_to_dict.py` holds the same line for `backend/`; this file
extends it to every module Phase 3 adds.
"""

import ast
from pathlib import Path

import numpy as np

from model.contract import probs_to_dict
from model.labels import LABELS

SCANNED_DIRS = ("rescorer", "frontend", "monitoring", "scripts")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        root = Path(directory)
        assert root.is_dir(), f"{directory} is not a directory, so the scan certifies nothing"
        files.extend(sorted(root.rglob("*.py")))
    return files


def _zips_over_labels(source: str, where: str) -> list[str]:
    """An AST walk, not a substring search.

    A substring search for `zip(LABELS` cannot tell a call from the docstring that documents
    the rule, which is how the rule ends up deleted; and it misses `zip(values, LABELS)` and
    `zip(*(LABELS, row))`, both of which produce the same mislabelling.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source, filename=where)):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", "") != "zip":
            continue
        names = {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}
        if "LABELS" in names:
            offenders.append(f"{where}:{node.lineno}")
    return offenders


def test_the_shared_adapter_exists_and_orders_by_labels():
    """Premortem H23 / Tier-1 item 1.8. `model.contract.probs_to_dict` is the one converter;
    it is owned by Phase 0 and imported here rather than re-derived."""
    result = probs_to_dict(np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6]))
    assert list(result) == list(LABELS)
    assert result["toxic"] == 0.1
    assert result["identity_hate"] == 0.6


def test_the_scanner_flags_a_planted_re_derivation():
    """Non-vacuity: a scan that cannot report is indistinguishable from a clean tree."""
    assert _zips_over_labels("d = dict(zip(LABELS, row))", "planted.py")
    assert _zips_over_labels("d = dict(zip(row, LABELS, strict=True))", "planted.py")
    assert _zips_over_labels("for label, value in zip(LABELS, values): pass", "planted.py")
    assert _zips_over_labels("d = {a: b for a, b in zip(other, values)}", "clean.py") == []


def test_no_module_re_derives_the_label_mapping():
    assert len(_python_files()) >= 8, "the scan found too few files to be measuring anything"
    offenders: list[str] = []
    for path in _python_files():
        offenders.extend(_zips_over_labels(path.read_text(encoding="utf-8"), str(path)))
    assert not offenders, (
        f"{offenders} re-derive the array-to-dict mapping; use "
        "model.contract.probs_to_dict (premortem H23)"
    )


def test_the_rescorer_writes_probabilities_through_the_shared_adapter():
    """The scan above proves nobody re-derives the mapping. This proves the re-scorer uses
    the one that exists -- a worker that wrote `dict(...)` from a list comprehension would
    pass the scan and still be a fourth independent mapping."""
    source = Path("rescorer/worker.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "model.contract"
        for alias in node.names
    }
    assert "probs_to_dict" in imported, "rescorer/worker.py does not import the shared adapter"
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "probs_to_dict" in called, "the adapter is imported but never called"
