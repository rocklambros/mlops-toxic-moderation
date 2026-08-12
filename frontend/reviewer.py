"""Reviewer console. Streamlit, port 8503 -- deliberately not 8501.

The premortem (H12) found that opening ingress for the demo also exposed this console, on
the same host and port as the user UI, behind one shared secret, with direct database write
access to the graded metric. Three changes answer that: its own port, which Phase A2's
security groups give no ingress at all (the operator reaches it over an SSM port forward);
no database credential, because every write goes through the backend, which derives
`reviewer_id` from the session it issued; and no client-supplied identity anywhere in the
request body.

The comment is rendered verbatim through `frontend.render`, so the string being labelled is
byte-identical to the string the classifier scored.

`initial_label_state` is the non-obvious control here. Pre-ticking the reviewer's checkboxes
from the model's own scores anchors the human on the model's answer, and the quantity being
measured is *agreement between the two* -- so a model-derived default manufactures the
numerator of the graded accuracy metric. The default is therefore a constant, independent of
what the model said.

Streamlit is imported inside the drawing functions so the decision logic below stays
importable, and unit-testable, without a Streamlit runtime.
"""

import os

from frontend.api_client import BackendClient, BackendError, new_session_fp
from frontend.render import render_comment
from infra.exposure import PORTS
from model.labels import LABELS

REVIEWER_PORT = PORTS["reviewer_ui"].number

QUEUE_SOURCES = ("flagged", "random-audit", "user-report")
QUEUE_STATUSES = ("pending", "rescored")

SIGN_IN_FAILED = "Invalid reviewer secret."
QUEUE_UNAVAILABLE = "Could not load the queue."

# A 429 is the backend asking for a pause, not the backend rejecting who you are. Signing the
# reviewer out on one turns a one-minute wait into re-entering the shared secret, and the
# secret is the thing worth typing least often.
QUEUE_BUSY = (
    "The backend is rate limiting this session. Wait a moment and reload. You are still "
    "signed in."
)

# Statuses that mean the token is no longer good. Anything else is transient and the session
# survives it.
AUTH_FAILURE_STATUSES = frozenset({401, 403})
QUEUE_EMPTY = "The queue is empty."
NO_CHALLENGER = "Challenger scores are not available for this item."
REVIEW_FAILED = "The review was not recorded."
REVIEW_RECORDED = "Review recorded."


def build_label_payload(checked: dict[str, bool]) -> dict[str, int]:
    return {label: int(bool(checked.get(label, False))) for label in LABELS}


def initial_label_state(model_probs: dict[str, float]) -> dict[str, bool]:
    """A constant, on purpose: see the module docstring. `model_probs` is accepted so the
    call site reads as a deliberate refusal rather than an omission."""
    return dict.fromkeys(LABELS, False)


def challenger_column(distilbert_probs: dict[str, float] | None) -> dict[str, float | None]:
    probs = distilbert_probs or {}
    return {label: probs.get(label) for label in LABELS}


def queue_caption(waiting: int, item: dict) -> str:
    """Everything interpolated here is validated against a closed vocabulary first.

    `input_text_snapshot` must never reach this string: st.caption parses markdown, and the
    comment has exactly one sink.
    """
    source = item["source"]
    status = item["status"]
    if source not in QUEUE_SOURCES:
        raise ValueError(f"unknown review_queue source {source!r}")
    if status not in QUEUE_STATUSES:
        raise ValueError(f"unexpected review_queue status {status!r}")
    request_id = str(item["request_id"])
    if not request_id.replace("-", "").isalnum():
        raise ValueError("request_id is not the identifier the backend issues")
    return f"{int(waiting)} item(s) waiting. Showing {request_id} ({source}, {status})"


def get_client() -> BackendClient:
    import streamlit as st

    if "session_fp" not in st.session_state:
        st.session_state["session_fp"] = new_session_fp()
    return BackendClient(
        base_url=os.environ["BACKEND_URL"],
        api_key=os.environ.get("DEMO_API_KEY", ""),
        session_fp=st.session_state["session_fp"],
    )


def _login_form() -> None:
    import streamlit as st

    st.title("Moderation review queue")
    secret = st.text_input("Reviewer secret", type="password")
    if st.button("Sign in", type="primary", disabled=not secret):
        try:
            st.session_state["token"] = get_client().login(secret)
            st.rerun()
        except BackendError:
            # No detail: the backend distinguishes a wrong secret from a rate-limited one,
            # and echoing which is which helps a guesser more than it helps the operator.
            st.error(SIGN_IN_FAILED)


def comparison_chart(rows: list[dict]):
    """Production model against the DistilBERT challenger, one grouped pair per label.

    Altair is imported here rather than at module scope for the same reason Streamlit is:
    the decision logic in this module has to stay importable without a UI stack installed.

    Held in LABELS order rather than sorted by probability, which is the opposite of the
    choice `frontend/ui.py` makes for its chart, and deliberately. A reviewer works down a
    queue and compares the same label across many items; a re-sorting axis would move
    `threat` to a different row on every comment and make that comparison impossible. The
    user-facing chart has no such continuity to preserve, so there it sorts by magnitude.

    A label the challenger did not score is dropped from the challenger series rather than
    plotted as zero. Zero is a claim -- "the challenger saw this and said no" -- and absence
    is not that claim.
    """
    import altair as alt
    import pandas as pd

    records = []
    for row in rows:
        records.append(
            {"label": row["label"], "series": "production", "probability": row["production model"]}
        )
        if row["challenger (DistilBERT)"] is not None:
            records.append(
                {
                    "label": row["label"],
                    "series": "challenger",
                    "probability": row["challenger (DistilBERT)"],
                }
            )
    return (
        alt.Chart(pd.DataFrame(records))
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            y=alt.Y("label:N", title=None, sort=list(LABELS)),
            x=alt.X(
                "probability:Q",
                title="calibrated probability",
                scale=alt.Scale(domain=[0, 1]),
            ),
            yOffset="series:N",
            color=alt.Color("series:N", title=None),
            tooltip=[
                alt.Tooltip("label:N"),
                alt.Tooltip("series:N"),
                alt.Tooltip("probability:Q", format=".3f"),
            ],
        )
        .properties(height=alt.Step(14))
    )


def main() -> None:
    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="Moderation review queue", layout="wide")
    if "token" not in st.session_state:
        _login_form()
        return

    client = get_client()
    token = st.session_state["token"]
    st.title("Moderation review queue")

    try:
        items = client.pending(token, limit=20)
    except BackendError as exc:
        if exc.status_code in AUTH_FAILURE_STATUSES:
            st.error(QUEUE_UNAVAILABLE)
            render_comment(exc.detail)
            st.session_state.pop("token", None)
        elif exc.status_code == 429:
            st.warning(QUEUE_BUSY)
        else:
            st.error(QUEUE_UNAVAILABLE)
            render_comment(exc.detail)
        return

    # Rendered from session state on the run AFTER the submit, not inline with it. Until
    # 2026-08-12 the submit branch called `st.success(...)` and then `st.rerun()`, and
    # `st.rerun()` abandons the current script run -- so the message was discarded before the
    # browser ever painted it. A reviewer saw the page silently advance to the next item and
    # had no way to tell a recorded review from a dropped one. The error branch has no rerun,
    # so failures showed and successes did not, which is the worst way round.
    #
    # `frontend/ui.py` already does it this way for user feedback; this is that pattern, and
    # it pops rather than persisting because the confirmation belongs to one submit.
    if st.session_state.pop("review_recorded", None):
        st.success(REVIEW_RECORDED)

    if not items:
        st.info(QUEUE_EMPTY)
        return

    item = items[0]
    st.caption(queue_caption(len(items), item))

    st.subheader("Comment, exactly as scored")
    render_comment(item["input_text_snapshot"])

    challenger = challenger_column(item.get("distilbert_probs"))
    rows = [
        {
            "label": label,
            "production model": item["model_probs"][label],
            "challenger (DistilBERT)": challenger[label],
        }
        for label in LABELS
    ]
    st.altair_chart(comparison_chart(rows), width="stretch")
    st.caption(
        "Where the two models disagree is where your judgement is worth the most. Held in "
        "label order, not sorted, so the same label sits in the same place on every item."
    )
    with st.expander("Exact probabilities"):
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if all(value is None for value in challenger.values()):
        st.caption(NO_CHALLENGER)

    st.subheader("Your labels")
    defaults = initial_label_state(item["model_probs"])
    checked = {
        label: st.checkbox(label, value=defaults[label], key=f"cb_{label}") for label in LABELS
    }

    if st.button("Submit review", type="primary"):
        try:
            client.submit(token, item["request_id"], build_label_payload(checked))
            for label in LABELS:
                st.session_state.pop(f"cb_{label}", None)
            # The flag, not the message. `st.rerun()` below discards everything this run
            # drew; only session state survives it.
            st.session_state["review_recorded"] = item["request_id"]
            st.rerun()
        except BackendError as exc:
            st.error(REVIEW_FAILED)
            render_comment(exc.detail)


if __name__ == "__main__":
    main()
