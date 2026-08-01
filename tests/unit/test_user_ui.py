"""The user UI's decision logic, and the one comment-shaped hole in it.

The UI is mostly Streamlit calls, which are not worth asserting. What is worth asserting is
that the comment reaches exactly one widget -- the inert one -- and that everything else the
page shows is derived from the closed vocabularies the backend returns.
"""

from pathlib import Path

import pytest

from frontend.ui import (
    RATE_LIMIT_MESSAGE,
    USER_UI_PORT,
    decision_label,
    feedback_message,
    max_probability_caption,
    probability_table,
)
from model.labels import LABELS
from tests.unit.sink_scan import interpolated_sink_calls

# Markdown sinks may be handed a literal, or the output of one of these vetted formatters.
# Each is tested below to prove it cannot carry text a caller chose.
VETTED_FORMATTERS = frozenset({"feedback_message", "decision_label", "max_probability_caption"})

SOURCE = Path("frontend/ui.py").read_text(encoding="utf-8")
XSS = "<img src=x onerror=alert(1)>"


def _result(text: str = XSS) -> dict:
    return {
        "request_id": "r1",
        "decision": "review",
        "max_prob": 0.91,
        "labels": {label: {"prob": 0.1, "flag": False} for label in LABELS},
        "submitted": text,
    }


def test_the_probability_table_is_complete_and_in_label_order():
    rows = probability_table(_result())
    assert [row["label"] for row in rows] == list(LABELS)
    assert all(isinstance(row["probability"], float) for row in rows)
    assert all(isinstance(row["flagged"], bool) for row in rows)


def test_the_probability_table_carries_no_user_text():
    """st.dataframe is not the inert sink and has its own escaping rules. The comment goes
    through frontend.render and nowhere else."""
    rendered = repr(probability_table(_result()))
    assert XSS not in rendered
    assert "submitted" not in rendered


def test_the_feedback_confirmation_is_a_fixed_sentence_per_verdict():
    assert feedback_message("agree") != feedback_message("disagree")
    with pytest.raises(KeyError):
        feedback_message("<script>alert(1)</script>")


def test_the_rate_limit_message_names_no_caller_supplied_value():
    assert "{" not in RATE_LIMIT_MESSAGE and "%" not in RATE_LIMIT_MESSAGE


def test_the_decision_label_refuses_a_decision_outside_the_closed_vocabulary():
    assert decision_label("block") == "BLOCK"
    with pytest.raises(KeyError):
        decision_label(XSS)


def test_the_probability_caption_refuses_anything_that_is_not_a_number():
    assert max_probability_caption(0.9128) == "max probability 0.913"
    with pytest.raises(ValueError):
        max_probability_caption(XSS)


def test_the_user_ui_runs_on_the_graded_demo_port():
    from infra.exposure import PORTS

    assert USER_UI_PORT == PORTS["user_ui"].number
    assert PORTS["user_ui"].demo_exposed is True


def test_the_scanner_flags_an_interpolated_markdown_sink():
    """Non-vacuity: prove the walk can report before trusting that it reported nothing."""
    assert interpolated_sink_calls('st.error(f"failed: {exc}")')
    assert interpolated_sink_calls("st.caption(item)")
    assert interpolated_sink_calls('st.metric("Decision", result["decision"])')
    assert interpolated_sink_calls('st.progress(0.5, text=f"{comment}")') == []
    assert interpolated_sink_calls('st.caption(f"{comment}", help=comment)')
    assert interpolated_sink_calls('st.error("fixed sentence")') == []
    assert interpolated_sink_calls(
        "st.caption(max_probability_caption(x))", VETTED_FORMATTERS
    ) == []
    assert interpolated_sink_calls('MESSAGE = "fixed"\nst.warning(MESSAGE)') == []
    assert interpolated_sink_calls("MESSAGE = comment\nst.warning(MESSAGE)")


def test_no_markdown_sink_in_the_user_ui_renders_a_value_this_module_did_not_choose():
    assert interpolated_sink_calls(SOURCE, VETTED_FORMATTERS) == []


def test_the_backend_error_body_is_shown_through_the_inert_renderer():
    """A backend 422 echoes the submitted comment back. It must not be pasted into
    st.error, which parses markdown; it goes to render_comment like any other user text."""
    assert "render_comment(exc.detail)" in SOURCE
    assert 'st.error(f"' not in SOURCE
    assert "st.error(f'" not in SOURCE


def test_the_ui_module_holds_no_database_import():
    for forbidden in ("sqlalchemy", "psycopg", "create_engine", "DATABASE_URL"):
        assert forbidden not in SOURCE, (
            "the user UI must reach Postgres only through the backend API (H12/H16)."
        )
