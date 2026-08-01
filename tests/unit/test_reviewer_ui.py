"""The reviewer console: its port, its defaults, and what it is not allowed to hold.

The console writes the graded metric, so three separate properties matter and none of them
implies the others: it runs on a port the demo toggle never carries, it holds no database
credential, and it does not pre-fill the human's answer with the machine's.
"""

import ast
from pathlib import Path

import pytest

from frontend.reviewer import (
    NO_CHALLENGER,
    REVIEWER_PORT,
    build_label_payload,
    challenger_column,
    initial_label_state,
    queue_caption,
)
from infra.exposure import DEMO_EXPOSED_PORTS, PORTS
from model.labels import LABELS
from tests.unit.sink_scan import interpolated_sink_calls

SOURCE = Path("frontend/reviewer.py").read_text(encoding="utf-8")
VETTED_FORMATTERS = frozenset({"queue_caption"})
XSS = "<img src=x onerror=alert(1)>"


def _item(**overrides) -> dict:
    item = {
        "request_id": "3f6b1c9a-0000-4000-8000-000000000001",
        "source": "flagged",
        "status": "pending",
        "input_text_snapshot": XSS,
        "model_probs": dict.fromkeys(LABELS, 0.1),
        "distilbert_probs": None,
    }
    item.update(overrides)
    return item


def test_reviewer_runs_on_its_own_port_not_the_user_ui_port():
    assert REVIEWER_PORT == PORTS["reviewer_ui"].number
    assert REVIEWER_PORT != PORTS["user_ui"].number
    assert REVIEWER_PORT != PORTS["monitoring"].number


def test_the_demo_toggle_never_carries_the_reviewer_port():
    """H12 restated where the console itself can see it: opening 8501 for a grader must not
    open the console that writes the graded metric."""
    assert REVIEWER_PORT not in DEMO_EXPOSED_PORTS


def test_label_payload_is_complete_and_binary():
    payload = build_label_payload({"toxic": True, "insult": True})
    assert set(payload) == set(LABELS)
    assert payload["toxic"] == 1
    assert payload["threat"] == 0
    assert all(value in (0, 1) for value in payload.values())


def test_label_payload_ignores_a_label_this_model_does_not_score():
    payload = build_label_payload({"toxic": True, "spicy": True})
    assert set(payload) == set(LABELS)


def test_the_checkboxes_do_not_start_pre_filled_with_the_models_answer():
    """Agreement between reviewer and model is the numerator of the graded accuracy
    metric. A default derived from the model's own scores manufactures that agreement, and
    the reviewer would have to actively disagree to record a disagreement."""
    confident = dict.fromkeys(LABELS, 0.99)
    quiet = dict.fromkeys(LABELS, 0.01)
    assert initial_label_state(confident) == initial_label_state(quiet)
    assert set(initial_label_state(confident).values()) == {False}
    assert set(initial_label_state(confident)) == set(LABELS)


def test_challenger_column_degrades_when_the_rescorer_is_cut():
    """C8: the re-scorer sits behind the cut-line. The reviewer must still work."""
    assert challenger_column(None) == dict.fromkeys(LABELS, None)
    assert challenger_column(dict.fromkeys(LABELS, 0.5))["toxic"] == 0.5


def test_challenger_column_tolerates_a_partial_payload():
    assert challenger_column({"toxic": 0.9})["threat"] is None


def test_the_missing_challenger_notice_is_a_fixed_sentence():
    assert "{" not in NO_CHALLENGER


def test_the_queue_caption_never_carries_the_comment():
    caption = queue_caption(3, _item())
    assert XSS not in caption
    assert "3 item(s)" in caption


def test_the_queue_caption_refuses_a_source_or_status_outside_the_closed_vocabulary():
    with pytest.raises(ValueError):
        queue_caption(1, _item(source="<script>alert(1)</script>"))
    with pytest.raises(ValueError):
        queue_caption(1, _item(status="reviewed"))
    with pytest.raises(ValueError):
        queue_caption(1, _item(request_id="<img src=x onerror=alert(1)>"))


def test_no_markdown_sink_in_the_reviewer_ui_renders_a_value_it_did_not_choose():
    assert interpolated_sink_calls(SOURCE, VETTED_FORMATTERS) == []


def _code_names(source: str, needle: str) -> list[str]:
    """Every place the identifier appears in CODE -- never in a docstring or a comment.

    A raw substring scan would flag the paragraph that documents the rule, which is how a
    rule of this shape ends up deleted rather than obeyed.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and node.value == needle:
            found.append(f"line {node.lineno}: literal")
        elif isinstance(node, ast.Name) and node.id == needle:
            found.append(f"line {node.lineno}: name")
        elif isinstance(node, ast.Attribute) and node.attr == needle:
            found.append(f"line {node.lineno}: attribute")
        elif isinstance(node, ast.keyword) and node.arg == needle:
            found.append(f"line {node.lineno}: keyword")
    return found


def test_the_code_name_scan_reports_before_it_is_trusted():
    assert _code_names('x = {"reviewer_id": 1}', "reviewer_id")
    assert _code_names("body.reviewer_id", "reviewer_id")
    assert _code_names('"""a docstring naming reviewer_id"""', "reviewer_id") == []


def test_the_comment_reaches_exactly_one_sink():
    """`input_text_snapshot` is read once, and the read feeds render_comment."""
    assert len(_code_names(SOURCE, "input_text_snapshot")) == 1
    assert SOURCE.count('render_comment(item["input_text_snapshot"])') == 1


def test_the_backend_error_body_is_shown_through_the_inert_renderer():
    assert 'st.error(f"' not in SOURCE and "st.error(f'" not in SOURCE
    assert SOURCE.count("render_comment(exc.detail)") >= 1


def test_the_sign_in_failure_does_not_say_why():
    """A message that distinguishes 'wrong secret' from 'too many attempts' tells a guesser
    when to back off, which is the only thing the rate limit was buying."""
    assert "render_comment(exc.detail)" not in SOURCE.split("def _login_form")[1].split(
        "def main"
    )[0]


def test_reviewer_module_holds_no_database_import():
    for forbidden in ("sqlalchemy", "psycopg", "create_engine", "DATABASE_URL", "asyncpg"):
        assert forbidden not in SOURCE, (
            "The reviewer UI must reach Postgres only through the backend API (H12/H16)."
        )


def test_no_module_in_the_frontend_package_reaches_a_database():
    """The single-file grep above is the weak form of the rule; this is the whole package,
    including anything a later task adds beside these two."""
    offenders = []
    for path in sorted(Path("frontend").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("create_engine", "DATABASE_URL", "sessionmaker", "psycopg."):
            if forbidden in source:
                offenders.append(f"{path}: {forbidden}")
    assert offenders == [], offenders


def test_the_reviewer_never_names_a_reviewer_id_field():
    """Delivery spec 6.3: the identity is derived server-side, so the console has no way to
    assert one even by accident."""
    assert _code_names(SOURCE, "reviewer_id") == []
