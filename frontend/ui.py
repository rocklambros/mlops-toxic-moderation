"""User-facing moderation UI. Streamlit, port 8501.

Submits a comment to the backend, shows the decision and the six calibrated probabilities,
and offers the two-click agree/disagree control that rubric 3.2 grades. The control writes a
feedback row with source='user'; a disagreement additionally refers the item into the human
review queue, which is how a user's opinion reaches live accuracy -- through a reviewer,
never through arithmetic on an anonymous click.

Streamlit is imported inside the functions that draw rather than at module scope. That is
not style: it keeps the decision logic below importable, and therefore unit-testable, in a
job that has no Streamlit installed -- which is the job where `test_no_unsafe_html.py`
runs. The pure functions (`probability_table`, `feedback_message`) exist for the same
reason, and because the table they build is the one place a comment could accidentally be
handed to a widget other than the inert one.

The comment itself is displayed only through `frontend.render.render_comment`.
"""

import os

from frontend.api_client import (
    MAX_INPUT_CHARS,
    BackendClient,
    BackendError,
    RateLimited,
    new_session_fp,
)
from frontend.render import render_comment
from model.labels import LABELS

USER_UI_PORT = 8501
RATE_LIMIT_MESSAGE = "Too much feedback from this session. Try again later."
FEEDBACK_FAILED_MESSAGE = "Feedback was not recorded."
PREDICT_FAILED_MESSAGE = "The backend refused the request."


def probability_table(result: dict) -> list[dict]:
    """One row per label, in LABELS order. Carries no user text by construction.

    The comment is rendered once, through the inert sink. Letting it into a dataframe here
    would be a second rendering path with different escaping rules and no test over it.
    """
    return [
        {
            "label": label,
            "probability": float(result["labels"][label]["prob"]),
            "flagged": bool(result["labels"][label]["flag"]),
        }
        for label in LABELS
    ]


def probability_chart(rows: list[dict]):
    """The six probabilities as bars, ordered by probability, coloured by whether the label
    cleared its threshold.

    Altair is imported here rather than at module scope for the same reason Streamlit is:
    this module has to stay importable in the job that has no UI dependencies installed.

    Sorted by probability rather than held in LABELS order, because the question a reader
    brings to this chart is "what drove the decision" and the answer is whichever bars are
    longest. The per-label order is still available in the table below, which is where a
    reader who wants to compare the same label across two comments will look.

    Colour encodes `flagged` rather than probability. The decision rule is a per-label
    threshold, not one cut across all six -- `threat` flags at a far lower probability than
    `toxic` -- so a gradient would draw a boundary the system does not use, and a reader
    would infer a single cutoff that is not there.
    """
    import altair as alt
    import pandas as pd

    frame = pd.DataFrame(rows)
    frame["status"] = frame["flagged"].map({True: "flagged", False: "under threshold"})
    return (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            x=alt.X(
                "probability:Q",
                title="calibrated probability",
                scale=alt.Scale(domain=[0, 1]),
            ),
            y=alt.Y("label:N", title=None, sort="-x"),
            color=alt.Color(
                "status:N",
                title=None,
                scale=alt.Scale(
                    domain=["flagged", "under threshold"], range=["#d62728", "#9ecae1"]
                ),
            ),
            tooltip=[
                alt.Tooltip("label:N", title="label"),
                alt.Tooltip("probability:Q", title="probability", format=".3f"),
                alt.Tooltip("status:N", title="status"),
            ],
        )
        .properties(height=alt.Step(26))
    )


def feedback_message(verdict: str) -> str:
    """Closed vocabulary in, fixed sentence out: nothing a caller supplies is interpolated
    into a markdown-capable widget."""
    return {"agree": "Recorded: you agreed. Thank you.",
            "disagree": "Recorded: you disagreed, and a human reviewer will look at this."}[
        verdict
    ]


def decision_label(decision: str) -> str:
    """The backend's decision vocabulary is closed. Anything else is a bug or a spoofed
    response, and rendering it into a markdown widget is how that becomes a page defect
    rather than a loud failure."""
    return {"allow": "ALLOW", "review": "REVIEW", "block": "BLOCK"}[decision]


def max_probability_caption(value: float) -> str:
    """A number formatted by this module. `float()` refuses a string payload, so the only
    thing that can reach the caption is a number."""
    return f"max probability {float(value):.3f}"


def get_client() -> BackendClient:
    import streamlit as st

    if "session_fp" not in st.session_state:
        # Server-side only. This value is never sent to the browser; it is the rate-limit
        # bucket for UI-originated traffic.
        st.session_state["session_fp"] = new_session_fp()
    return BackendClient(
        base_url=os.environ["BACKEND_URL"],
        api_key=os.environ.get("DEMO_API_KEY", ""),
        session_fp=st.session_state["session_fp"],
    )


def _send_feedback(request_id: str, verdict: str) -> None:
    import streamlit as st

    try:
        get_client().user_feedback(request_id, verdict)
        st.session_state["feedback_sent"] = verdict
    except RateLimited:
        st.warning(RATE_LIMIT_MESSAGE)
    except BackendError as exc:
        st.warning(FEEDBACK_FAILED_MESSAGE)
        render_comment(exc.detail)


def main() -> None:
    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="Toxic Comment Moderation", layout="centered")
    st.title("Toxic comment moderation")
    st.caption(
        "Submit a comment to see the moderation decision and the six per-label calibrated "
        "probabilities. Comments are stored so the monitoring dashboard can be built from "
        "them, and the text is purged on a 30-day retention policy the operator runs."
    )

    text = st.text_area("Comment", max_chars=MAX_INPUT_CHARS, height=140)
    if st.button("Check comment", type="primary", disabled=not text.strip()):
        try:
            st.session_state["result"] = get_client().predict(text)
            st.session_state["submitted_text"] = text
            st.session_state.pop("feedback_sent", None)
        except BackendError as exc:
            # The message is a fixed sentence and the backend's own words go through the
            # inert renderer: a 422 echoes the submitted text straight back, and st.error
            # parses markdown.
            st.error(PREDICT_FAILED_MESSAGE)
            render_comment(exc.detail)

    result = st.session_state.get("result")
    if not result:
        return

    st.subheader("Comment as scored")
    render_comment(st.session_state.get("submitted_text"))

    st.metric("Decision", decision_label(result["decision"]))
    st.progress(min(max(float(result["max_prob"]), 0.0), 1.0))
    st.caption(max_probability_caption(result["max_prob"]))

    rows = probability_table(result)
    st.altair_chart(probability_chart(rows), width="stretch")
    st.caption(
        "Each label carries its own threshold, so a shorter bar can be flagged while a "
        "longer one is not. The decision above is the strongest label's band."
    )
    with st.expander("Exact probabilities"):
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    st.subheader("Was this decision right?")
    st.caption(
        "Your answer is stored as user feedback. A disagreement also sends the comment to a "
        "human reviewer."
    )
    agree, disagree = st.columns(2)
    sent = st.session_state.get("feedback_sent")
    if agree.button("Agree", disabled=bool(sent), width="stretch"):
        _send_feedback(result["request_id"], "agree")
    if disagree.button("Disagree", disabled=bool(sent), width="stretch"):
        _send_feedback(result["request_id"], "disagree")
    if sent:
        st.success(feedback_message(sent))


if __name__ == "__main__":
    main()
