"""The master plan's Interface Contracts block is declared authoritative, so it is code.

This test parses that block and compares it to the live signatures. It is the reason
the block cannot drift again: a hardening commit that changes `prepare.py` without
changing the doc turns this test red.

Premortem H24 recorded that the block had drifted anyway, and that a supersession note is
not a fix, because a phase implementer reads one narrow slice and never opens the document
carrying the correction. So the Phase 2 seams are asserted here against the shipped code
rather than described in prose somewhere else.
"""

import ast
import dataclasses
import inspect
import re
from pathlib import Path

import pandas as pd

from backend.audit import FLAGGED_SAMPLE_RATE
from backend.db import (
    REVIEW_SOURCES,
    enqueue_review,
    fetch_pending_reviews,
    init_db,
    insert_prediction,
    write_pending,
)
from backend.model_loader import LoadedModel, load_model
from model.contract import PredictionResponse, probs_to_dict
from model.data.prepare import DatasetBundle, prepare_dataset
from model.data.split import make_splits

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md"
FIXTURE = REPO / "tests/fixtures/mini_jigsaw.csv"
README = REPO / "README.md"


def _section() -> str:
    text = DOC.read_text()
    return text[text.index("## Interface Contracts") : text.index("## Phase Dependency Graph")]


def _phase_2_section() -> str:
    text = DOC.read_text()
    start = text.index("## Phase 2: FastAPI backend")
    return text[start : text.index("## Phase 3", start)]


def _contract_ast() -> ast.Module:
    blocks = re.findall(r"```python\n(.*?)```", _section(), re.S)
    return ast.parse("\n\n".join(blocks))


def _assigned(tree: ast.Module, name: str) -> object:
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if isinstance(node, ast.Assign | ast.AnnAssign) and any(
            isinstance(t, ast.Name) and t.id == name for t in targets if t is not None
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is absent from the Interface Contracts block")


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


def test_master_plan_interface_block_matches_phase_2():
    """H24. The block said `LoadedModel.model_version` was the only version field and gave
    it as the value the response returns. Phase 2 ships two fields precisely so the digest
    that `/health` strips is never handed to a client, and a Phase 3 implementer building
    against the stale block would reintroduce the leak."""
    tree = _contract_ast()
    documented = _fields(_class(tree, "LoadedModel"))
    live = {field.name for field in dataclasses.fields(LoadedModel)}
    assert documented == ["model_version", "public_version"]
    assert set(documented) <= live

    node = _func(tree, "load_model")
    assert [a.arg for a in node.args.args] == list(inspect.signature(load_model).parameters)


def test_the_documented_response_version_is_the_opaque_one():
    """H14. The block's PredictionResponse example carried a digest."""
    section = _section()
    assert re.search(
        r"PredictionResponse\.model_version`? carries[^\n]*public_version", section
    ), "the block must state that the response carries the opaque public_version"
    assert "wandb-digest" not in section


def test_the_documented_db_seam_is_the_pending_write_path():
    """H28 and H30. The block documented `insert_prediction(session, response, input_text)`,
    which cannot exist: a failed request has no PredictionResponse and must still write a
    row, and the spool has to replay a prediction and its review row through one call."""
    tree = _contract_ast()
    for name, live in (
        ("init_db", init_db),
        ("write_pending", write_pending),
        ("insert_prediction", insert_prediction),
        ("enqueue_review", enqueue_review),
        ("fetch_pending_reviews", fetch_pending_reviews),
    ):
        node = _func(tree, name)
        assert [a.arg for a in node.args.args] == list(inspect.signature(live).parameters), name


def test_phase_3_writers_are_not_documented_as_phase_2_functions():
    """Both need reviewer session identity and re-scorer status semantics that Phase 2 has
    no way to supply. Leaving them in the Phase 2 seam invites a Phase 3 implementer to
    assume they already exist."""
    documented = {n.name for n in ast.walk(_contract_ast()) if isinstance(n, ast.FunctionDef)}
    assert "submit_review" not in documented
    assert "write_distilbert_probs" not in documented


def _documented_parameters(node: ast.FunctionDef) -> list[str]:
    """Positional and keyword-only, in declaration order.

    `node.args.args` alone drops everything after `*`, and both admission functions are
    keyword-only past the connection -- deliberately, so a caller cannot transpose
    `source` and `submitter_fp`. Comparing only the positional slice would call two
    signatures equal while they disagreed about every argument that matters.
    """
    return [argument.arg for argument in node.args.posonlyargs + node.args.args] + [
        argument.arg for argument in node.args.kwonlyargs
    ]


def test_the_documented_phase_3_seams_match_the_shipped_signatures():
    """H24 again, for the interfaces Phase 4's CI gate and Phase 5's deploy build against.

    Documenting a seam and never comparing it to the code is how the block drifted the first
    time. Every function named in the Phase 3 sub-block is checked against the live one here,
    and the check is what makes writing it down worth anything.
    """
    from backend.feedback import derive_feedback, insert_feedback, user_feedback
    from backend.queue_guard import admit_review, admit_user_feedback
    from backend.reviewer_auth import current_reviewer, issue_session_token
    from rescorer.challenger import load_challenger
    from rescorer.worker import drain_once

    tree = _contract_ast()
    seams = {
        "admit_review": admit_review,
        "admit_user_feedback": admit_user_feedback,
        "derive_feedback": derive_feedback,
        "user_feedback": user_feedback,
        "insert_feedback": insert_feedback,
        "issue_session_token": issue_session_token,
        "current_reviewer": current_reviewer,
        "load_challenger": load_challenger,
        "drain_once": drain_once,
    }
    for name, live in seams.items():
        documented = _documented_parameters(_func(tree, name))
        assert documented == list(inspect.signature(live).parameters), name


def test_the_documented_review_api_surface_is_the_router_that_ships():
    """The four routes are documented as comments, so nothing parses them into an AST. This
    reads the router instead: a fifth write path added to `backend/review_api.py` without a
    line in the block is an undocumented anonymous write surface."""
    from backend.review_api import router

    section = _section()
    live = {
        (method, route.path)
        for route in router.routes
        for method in route.methods
        if method != "HEAD"
    }
    assert live == {
        ("POST", "/review/login"),
        ("GET", "/review/pending"),
        ("POST", "/review/submit"),
        ("POST", "/feedback/user"),
    }, live
    for _, path in live:
        assert path in section, f"{path} is a live route with no line in the contract block"
    assert 'extra="forbid"' in section, "the block must state that identity is unassertable"


def test_the_documented_review_vocabulary_matches_the_check_constraint():
    """IFACE-DB-SCHEMA. `ck_review_source` rejects any value the doc and the table disagree
    on, and `user-report` is the H9 remedy Phase 3 depends on."""
    documented = _assigned(_contract_ast(), "REVIEW_SOURCES")
    assert tuple(documented) == tuple(REVIEW_SOURCES)
    assert "user-report" in documented


def test_the_documented_sampling_column_is_the_shipped_one():
    """The needle is assembled at runtime for the same reason the repo-wide scan does it:
    this module is grepped like any other, and a literal here would be the only hit."""
    retired = "inclusion" + "_probability"
    section = _section()
    assert retired not in section
    assert retired.upper() not in section
    assert "sample_rate" in section
    assert _assigned(_contract_ast(), "FLAGGED_SAMPLE_RATE") == FLAGGED_SAMPLE_RATE


def test_the_documented_schema_entry_point_is_init_db():
    """Phase 3's conftest does `from backend.db import init_db`. The block named the alias."""
    section = _section()
    assert "init_db" in section
    assert "init_schema" in section, "the alias must be documented as an alias, not the name"


def test_the_phase_2_task_list_names_what_phase_2_actually_shipped():
    """The scope in the master plan is what a reviewer reads to decide whether the phase is
    done. It listed neither the abuse controls, nor the spool, nor the retention purge."""
    section = _phase_2_section()
    for token in (
        "X-API-Key",
        "rate limit",
        "backend/spool.py",
        "backend/retention.py",
        "review_queue",
        "latency",
    ):
        assert token in section, f"the Phase 2 task list does not mention {token}"


def test_readme_documents_a_runnable_example_request():
    """Rubric 5.3 grades example user requests, and the premortem found the clause had no
    owning task."""
    text = README.read_text(encoding="utf-8")
    assert "/predict" in text
    assert "X-API-Key" in text
    assert "curl" in text


def test_the_readme_does_not_publish_the_demo_key():
    """D4. The control is worthless if the value is in a public repository: the key travels
    with the assignment submission and is rotated after grading."""
    text = README.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "X-API-Key" in line:
            assert "$DEMO_API_KEY" in line, f"the demo key must stay a variable: {line!r}"
