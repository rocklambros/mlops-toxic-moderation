"""The master plan's Interface Contracts block is executable (premortem H24).

It declares itself authoritative across phases, and it drifted because hardening commits
changed the code without changing the contract. A phase implementer sees only their own phase
file plus that block, so a stale contract is a wrong instruction delivered with authority.
Phase 4 is the only phase that can see all the seams at once, which is why the reconciliation
becomes a test here instead of a promise.

Contract definitions must be written on ONE line each, under a `# path/to/module.py` header,
so the substring parser below can read them. That constraint is cheap and it is what makes the
block checkable at all -- and it is load-bearing rather than stylistic: `load_challenger` was
wrapped across two lines and silently dropped out of every check, and `drain_once` sat under a
`# rescorer/` header naming no file, so it was attributed to the `backend/review_api.py`
header above it.

**Two parsers, one file, on purpose.** Phase 0 v2 Task 18 wrote
`tests/unit/test_interface_contract_doc.py`, which reads the same block with an AST parser;
this file was written independently with a substring parser. Two suites asserting the same
section of the same document in mutually exclusive ways is how the contract acquires two
meanings and both pass locally, so they are merged here and
`test_there_is_exactly_one_interface_contract_conformance_suite` is what keeps them merged.
Nothing was dropped in the merge: the AST cases are below, unchanged in substance, and the
overlap between the two parsers is deliberate redundancy rather than duplication.
"""

import ast
import dataclasses
import importlib
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
MASTER_PLAN = REPO / "docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md"
FIXTURE = REPO / "tests/fixtures/mini_jigsaw.csv"
README = REPO / "README.md"

FENCE = "`" * 3  # built, not written literally, so this file can live inside a fenced block
MODULE_RE = re.compile(r"^#\s*(?P<path>[\w/]+\.py)\b")
CLASS_RE = re.compile(r"^class\s+(?P<name>\w+)")
SUPERSEDED = (
    "sha256 over sorted deduped ids",
    'config: SplitConfig) -> DatasetBundle',
    "about 60 rows",
    "tf plan gate on PR",
    # Phase 0 v2 Task 14 split the composite into three fields. A contract row that still
    # declares the single opaque field is a wrong instruction delivered with authority.
    "data_version: str                 # sha256 over",
    # A header naming a directory rather than a module file: every `def` under it is
    # attributed to whichever module header came before, which is how `drain_once` was
    # documented as living in backend/review_api.py.
    "# rescorer/ --",
)
REQUIRED = (
    'Literal["allow", "review", "block"]',
    "probs_to_dict",
    "write_pending",
    "init_db",
    # Phase 0 v2 Task 18 wrote this block. Phase 4 verifies it; it does not rewrite it.
    "config: SplitConfig = DEFAULT_SPLIT",
    "raw_sha256",
    "split_version",
    "env_version",
    "normalize_for_serving",
    "def make_splits(",
)
# The seams other phases build against. Named here so a definition that wraps onto a second
# line, or loses its module header, fails loudly instead of dropping out of the scan.
DECLARED_SEAMS = frozenset(
    {
        ("model.normalize", "normalize"),
        ("model.normalize", "normalize_for_serving"),
        ("model.data.prepare", "prepare_dataset"),
        ("model.contract", "probs_to_dict"),
        ("model.contract", "PredictionResponse"),
        ("backend.db", "init_db"),
        ("backend.db", "write_pending"),
        ("backend.db", "insert_prediction"),
        ("backend.db", "enqueue_review"),
        ("backend.db", "fetch_pending_reviews"),
        ("backend.policy", "decide"),
        ("backend.model_loader", "LoadedModel"),
        ("backend.queue_guard", "admit_review"),
        ("backend.queue_guard", "admit_user_feedback"),
        ("backend.feedback", "derive_feedback"),
        ("backend.feedback", "user_feedback"),
        ("backend.feedback", "insert_feedback"),
        ("backend.reviewer_auth", "issue_session_token"),
        ("backend.reviewer_auth", "current_reviewer"),
        ("rescorer.challenger", "load_challenger"),
        ("rescorer.worker", "drain_once"),
    }
)


# --------------------------------------------------------------------------------------
# the substring parser
# --------------------------------------------------------------------------------------


def contracts_section() -> str:
    text = MASTER_PLAN.read_text(encoding="utf-8")
    start = text.index("## Interface Contracts")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def python_blocks(section: str) -> list[str]:
    return re.findall(rf"{FENCE}python\n(.*?){FENCE}", section, re.S)


def split_params(raw: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in raw:
        if char in "[({":
            depth += 1
        elif char in "])}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def parse_def(line: str) -> tuple[str, list[str]] | None:
    match = re.match(r"^def\s+(\w+)\(", line)
    if match is None:
        return None
    depth, start = 0, line.index("(")
    for index in range(start, len(line)):
        if line[index] in "([{":
            depth += 1
        elif line[index] in ")]}":
            depth -= 1
            if depth == 0:
                names = [
                    param.split(":")[0].split("=")[0].strip().lstrip("*")
                    for param in split_params(line[start + 1 : index])
                ]
                return match.group(1), [name for name in names if name]
    return None


def declared_symbols() -> list[tuple[str, str, list[str] | None]]:
    found: list[tuple[str, str, list[str] | None]] = []
    for block in python_blocks(contracts_section()):
        module: str | None = None
        for line in block.splitlines():
            header = MODULE_RE.match(line.strip())
            if header:
                module = header.group("path")[:-3].replace("/", ".")
                continue
            if module is None or line[:1].isspace():
                continue  # methods and fields belong to the class above them
            parsed = parse_def(line)
            if parsed:
                found.append((module, parsed[0], parsed[1]))
                continue
            klass = CLASS_RE.match(line)
            if klass:
                found.append((module, klass.group("name"), None))
    return found


# --------------------------------------------------------------------------------------
# the AST parser (merged from tests/unit/test_interface_contract_doc.py)
# --------------------------------------------------------------------------------------


def _section() -> str:
    return contracts_section()


def _phase_2_section() -> str:
    text = MASTER_PLAN.read_text(encoding="utf-8")
    start = text.index("## Phase 2: FastAPI backend")
    return text[start : text.index("## Phase 3", start)]


def _contract_ast() -> ast.Module:
    return ast.parse("\n\n".join(python_blocks(_section())))


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


# --------------------------------------------------------------------------------------
# the block is parseable, and everything in it exists
# --------------------------------------------------------------------------------------


def test_the_contracts_block_declares_something_parseable():
    symbols = declared_symbols()
    assert len(symbols) >= 10, f"the contracts parser found only {len(symbols)} symbols"
    assert ("model.contract", "probs_to_dict", ["row"]) in symbols


def test_every_seam_the_phases_build_against_is_declared_on_one_line():
    """The parser above skips a `def` whose parentheses do not close on the same line, and
    attributes every definition to the last `# module.py` header it saw. Both failure modes
    are SILENT -- the symbol simply is not checked -- so the set of symbols found has to be
    asserted, not just its size."""
    found = {(module, name) for module, name, _ in declared_symbols()}
    missing = sorted(DECLARED_SEAMS - found)
    assert not missing, (
        "these seams are documented but did not reach the conformance check. A definition "
        "wrapped onto a second line, or one sitting under a header that names a directory "
        f"rather than a module file, drops out silently: {missing}"
    )


def test_every_contract_symbol_exists_with_the_declared_parameters():
    problems = []
    for module_path, name, params in declared_symbols():
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            problems.append(f"{module_path}: not importable ({exc})")
            continue
        target = getattr(module, name, None)
        if target is None:
            problems.append(f"{module_path}.{name}: declared in the contract, absent in code")
            continue
        if params is None:
            continue
        actual = [p for p in inspect.signature(target).parameters if p not in {"self", "cls"}]
        if actual != params:
            problems.append(f"{module_path}.{name}: contract {params} != code {actual}")
    assert not problems, (
        "the authoritative contract block does not match the code:\n  " + "\n  ".join(problems)
    )


def test_the_contracts_block_carries_no_superseded_text():
    text = MASTER_PLAN.read_text(encoding="utf-8")
    stale = [phrase for phrase in SUPERSEDED if phrase in text]
    assert not stale, f"pre-hardening text still in the authoritative plan (H24): {stale}"


def test_the_contracts_block_carries_the_corrected_text():
    text = MASTER_PLAN.read_text(encoding="utf-8")
    missing = [phrase for phrase in REQUIRED if phrase not in text]
    assert not missing, f"the corrections were not applied (H24): {missing}"


def test_there_is_exactly_one_interface_contract_conformance_suite():
    """H24, one layer down. Phase 0 v2 Task 18 wrote
    tests/unit/test_interface_contract_doc.py and this file was written independently;
    both asserted the contents of the SAME section of the SAME document, in mutually
    exclusive ways. Two conformance suites for one contract is how the contract acquires
    two meanings and both pass locally."""
    suites = sorted(p.name for p in (REPO / "tests/unit").glob("test_interface_contract*.py"))
    assert suites == ["test_interface_contracts.py"], (
        f"{suites}: keep one suite. Phase 0's cases were merged into this file; delete "
        "tests/unit/test_interface_contract_doc.py rather than maintaining two."
    )


def test_probs_to_dict_is_defined_exactly_once():
    """H23 recurring inside the remediation for H23. Phase 0 Task 12, Phase 1 Task 1 and
    Phase 2 Task 1 each say 'Append to model/contract.py' and each ship a DIFFERENT body
    with a DIFFERENT error message. Python keeps the last def, so whichever phase lands
    last silently redefines the adapter for the two that landed earlier, and the earlier
    phases' `pytest.raises(match=...)` cases go red without anyone touching them."""
    source = (REPO / "model/contract.py").read_text(encoding="utf-8")
    assert source.count("def probs_to_dict(") == 1, "the adapter was redefined (H23)"


def test_the_canonical_adapter_raises_both_documented_messages():
    """Pins the ONE body all three phases' tests must be written against."""
    import numpy as np
    import pytest as _pytest

    with _pytest.raises(ValueError, match="1-D"):
        probs_to_dict(np.zeros((2, 6)))
    with _pytest.raises(ValueError, match="expected 6 probabilities"):
        probs_to_dict(np.zeros(5))


# --------------------------------------------------------------------------------------
# the AST cases, merged from Phase 0 v2 Task 18's suite
# --------------------------------------------------------------------------------------


def test_dataset_bundle_fields_match_the_documented_block():
    documented = _fields(_class(_contract_ast(), "DatasetBundle"))
    live = [f.name for f in DatasetBundle.__dataclass_fields__.values()]
    assert documented == live


def test_documented_bundle_no_longer_carries_the_old_data_version_field():
    section = _section()
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
    text = MASTER_PLAN.read_text(encoding="utf-8")
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
