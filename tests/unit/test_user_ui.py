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


# --------------------------------------------------------------- the probability chart
#
# Asserted over the SOURCE, not by executing it. Neither altair, streamlit nor pandas is in
# the dev environment -- that absence is deliberate, and is exactly what lets
# `test_no_unsafe_html.py` prove this module imports without a UI stack -- so
# `probability_chart` cannot be called from here. `tests/unit/test_dashboard.py` solves the
# same problem the same way, with `ast` over the file.
#
# The weakness is worth stating rather than hiding: this proves the chart is SPECIFIED
# correctly, not that altair renders it. Rendering is covered by loading the deployed page.

UI_SOURCE = Path("frontend/ui.py").read_text(encoding="utf-8")
_CHART_SRC = UI_SOURCE[
    UI_SOURCE.index("def probability_chart") : UI_SOURCE.index("def feedback_message")
]


def test_the_bars_are_ordered_by_probability():
    """This chart answers "what drove the decision", and the answer is whichever bars are
    longest. LABELS order would bury a flagged `threat` in fourth position."""
    assert 'sort="-x"' in _CHART_SRC, "the bars are not ordered by magnitude"


def test_colour_encodes_the_flag_rather_than_the_probability():
    """The decision rule is a per-label threshold: `threat` flags at 0.05 while `toxic` can
    sit unflagged at 0.30. A gradient over probability would draw one cutoff across all six,
    and a reader would infer a boundary the system does not use."""
    colour = _CHART_SRC[_CHART_SRC.index("color=") : _CHART_SRC.index("tooltip=")]
    assert '"status:N"' in colour, "colour does not encode the flag"
    assert '"probability' not in colour, "colour is encoding the probability"


def test_the_probability_axis_is_pinned_to_zero_one():
    """An autoscaled axis makes a 0.05 threat probability look like a large value."""
    assert "domain=[0, 1]" in _CHART_SRC, "the probability axis autoscales"


def test_the_chart_is_fed_only_the_text_free_table():
    """The comment reaches exactly one sink, the inert renderer. A chart embeds its data in
    the page as JSON, so feeding it anything but `probability_table` output would be a second
    rendering path with different escaping rules and no test over it."""
    assert "rows = probability_table(result)" in UI_SOURCE
    assert "st.altair_chart(probability_chart(rows)" in UI_SOURCE
    assert "submitted_text" not in _CHART_SRC, "the chart function can see the comment text"
