"""The master plan's Interface Contracts block is declared authoritative, so it is code.

This test parses that block and compares it to the live signatures. It is the reason
the block cannot drift again: a hardening commit that changes `prepare.py` without
changing the doc turns this test red.
"""

import ast
import inspect
import re
from pathlib import Path

import pandas as pd

from model.contract import PredictionResponse, probs_to_dict
from model.data.prepare import DatasetBundle, prepare_dataset
from model.data.split import make_splits

DOC = Path("docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md")
FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def _contract_ast() -> ast.Module:
    text = DOC.read_text()
    start = text.index("## Interface Contracts")
    end = text.index("## Phase Dependency Graph")
    blocks = re.findall(r"```python\n(.*?)```", text[start:end], re.S)
    return ast.parse("\n\n".join(blocks))


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is absent from the Interface Contracts block")


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is absent from the Interface Contracts block")


def _fields(node: ast.ClassDef) -> list[str]:
    return [s.target.id for s in node.body if isinstance(s, ast.AnnAssign)]


def test_dataset_bundle_fields_match_the_documented_block():
    documented = _fields(_class(_contract_ast(), "DatasetBundle"))
    live = [f.name for f in DatasetBundle.__dataclass_fields__.values()]
    assert documented == live


def test_documented_bundle_no_longer_carries_the_old_data_version_field():
    text = DOC.read_text()
    section = text[text.index("## Interface Contracts") : text.index("## Phase Dependency Graph")]
    assert "sha256 over sorted deduped ids" not in section
    for field in ("raw_sha256", "split_version", "env_version"):
        assert field in section


def test_prepare_dataset_signature_matches_the_documented_block():
    node = _func(_contract_ast(), "prepare_dataset")
    documented = [a.arg for a in node.args.args]
    live = list(inspect.signature(prepare_dataset).parameters)
    assert documented == live
    assert len(node.args.defaults) == 1, "config must be documented with its default"


def test_make_splits_is_documented_and_matches_its_signature():
    node = _func(_contract_ast(), "make_splits")
    documented = [a.arg for a in node.args.args]
    live = list(inspect.signature(make_splits).parameters)
    assert documented == live


def test_probs_to_dict_is_documented_and_matches_its_signature():
    node = _func(_contract_ast(), "probs_to_dict")
    assert [a.arg for a in node.args.args] == list(inspect.signature(probs_to_dict).parameters)


def test_prediction_response_decision_is_documented_as_a_literal():
    node = _class(_contract_ast(), "PredictionResponse")
    decision = next(
        s for s in node.body if isinstance(s, ast.AnnAssign) and s.target.id == "decision"
    )
    assert ast.unparse(decision.annotation).startswith("Literal[")
    assert PredictionResponse.model_fields["decision"].annotation.__name__ != "str"


def test_documented_fixture_size_matches_the_committed_fixture():
    text = DOC.read_text()
    match = re.search(r"synthetic `mini_jigsaw\.csv` \((\d+) rows", text)
    assert match, "the master plan must state the fixture row count"
    assert int(match.group(1)) == len(pd.read_csv(FIXTURE))
