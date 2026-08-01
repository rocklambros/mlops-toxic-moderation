"""The Phase 3 traversal, end to end, against a real Postgres.

Delivery spec section 3.3: no phase is complete until every route and integration it
introduces is proven working against a real dependency, not a mock. For Phase 3 that
traversal is submit -> predict -> log -> enqueue -> review -> feedback -> dashboard.

The prediction row is written here rather than through `/predict`, because `/predict` is
Phase 2's route and needs a loaded model artifact; `tests/integration/test_predict_api.py`
covers it. What this file proves is that Phase 3's four routes, its two derivation modules
and the dashboard's three aggregations agree with each other and with the schema -- against
a database that enforces every CHECK constraint, which is where the disagreements would
surface.

The INSERT below carries `input_chars`, `status` and `persist_status` because Phase 2
declares all three NOT NULL. Omitting one aborts the transaction, and every assertion after
it would be a statement about an empty table rather than about the traversal.
"""

import datetime as dt
import pathlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.review_api import router
from model.labels import LABELS
from monitoring.queries import live_accuracy, review_counts, user_feedback_panel

pytestmark = pytest.mark.integration

SECRET = "reviewer-shared-secret"
COMMENT = "**you** are an idiot"


@pytest.fixture()
def client(conn, engine, monkeypatch):
    monkeypatch.setenv("REVIEWER_SHARED_SECRET", SECRET)
    monkeypatch.setenv("REVIEWER_ID", "rock")
    monkeypatch.setenv("THRESHOLDS_PATH", "tests/fixtures/thresholds.json")
    app = FastAPI()
    app.include_router(router)
    app.state.engine = engine
    return TestClient(app)


def _predicted_and_enqueued(conn, request_id: str, now: dt.datetime) -> dict[str, float]:
    probs = dict.fromkeys(LABELS, 0.02)
    probs["toxic"] = 0.91
    probs["insult"] = 0.88
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    binds = ", ".join(f":p_{label}" for label in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, input_chars, model_version, "
            f"{cols}, decision, max_prob, latency_ms, status, persist_status, submitter_fp) "
            f"VALUES (:rid, :ts, :body, :chars, 'toxic-clf:v3', {binds}, 'review', 0.91, 23, "
            "'ok', 'direct', 'aaaabbbbccccdddd')"
        ),
        {
            "rid": request_id,
            "ts": now,
            "body": COMMENT,
            "chars": len(COMMENT),
            **{f"p_{label}": probs[label] for label in LABELS},
        },
    )
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot) VALUES (:rid, :ts, 'pending', 'flagged', 1.0, :body)"
        ),
        {"rid": request_id, "ts": now, "body": COMMENT},
    )
    conn.commit()
    return probs


def test_full_traversal_submit_to_dashboard(client, conn):
    """One comment, all the way through, against a real Postgres."""
    request_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.UTC)
    _predicted_and_enqueued(conn, request_id, now)

    # The reviewer sees the comment byte-identical to what was scored.
    token = client.post("/review/login", json={"secret": SECRET}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    item = client.get("/review/pending", headers=headers).json()["items"][0]
    assert item["input_text_snapshot"] == COMMENT

    from frontend.render import render_comment

    captured: list[str] = []
    render_comment(item["input_text_snapshot"], renderer=captured.append)
    assert captured == [COMMENT], "the markdown in the comment was transformed on its way out"

    # Review, which derives feedback. The reviewer agrees with the model on every label, so
    # the derived record is an exact match and live accuracy is 1.0 over one item.
    labels = dict.fromkeys(LABELS, 0)
    labels["toxic"] = 1
    labels["insult"] = 1
    submitted = client.post(
        "/review/submit", headers=headers, json={"request_id": request_id, "labels": labels}
    )
    assert submitted.status_code == 200
    assert submitted.json()["exact_match"] is True

    # User feedback, on the same request, from an anonymous visitor with no token.
    assert (
        client.post(
            "/feedback/user", json={"request_id": request_id, "verdict": "agree"}
        ).status_code
        == 200
    )

    # The dashboard sees it -- and sees the two kinds of feedback separately.
    since = now - dt.timedelta(days=1)
    assert review_counts(conn, since)["reviewed"] == 1
    report = live_accuracy(conn, since)
    assert report.n == 1
    assert report.point == pytest.approx(1.0)
    assert report.strata[0].stratum == "flagged"
    assert report.strata[0].sample_rate == pytest.approx(1.0)
    panel = user_feedback_panel(conn, since)
    assert panel.n == 1 and panel.agree == 1


def test_live_accuracy_moves_when_a_reviewer_disagrees(client, conn):
    """The premortem's sharpest finding about this metric was a traversal in which the
    reviewer and the model never disagreed, so live accuracy was exactly 1.0 and the whole
    pipeline could have been `return 1.0`. Two items, one agreeing and one not, and the
    estimate has to land between them -- through the API, the derivation, the CHECK
    constraints and the Horvitz-Thompson weighting, not around any of them."""
    now = dt.datetime.now(dt.UTC)
    agreeing, disagreeing = str(uuid.uuid4()), str(uuid.uuid4())
    for request_id in (agreeing, disagreeing):
        _predicted_and_enqueued(conn, request_id, now)

    token = client.post("/review/login", json={"secret": SECRET}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    matching = dict.fromkeys(LABELS, 0)
    matching["toxic"] = 1
    matching["insult"] = 1
    assert client.post(
        "/review/submit", headers=headers, json={"request_id": agreeing, "labels": matching}
    ).json()["exact_match"] is True

    # The model flagged `insult` at 0.88 against a 0.47 threshold; this reviewer says no.
    differing = dict(matching, insult=0)
    assert client.post(
        "/review/submit", headers=headers, json={"request_id": disagreeing, "labels": differing}
    ).json()["exact_match"] is False

    report = live_accuracy(conn, now - dt.timedelta(days=1))
    assert report.n == 2
    assert report.point == pytest.approx(0.5)
    assert report.lo < 0.5 < report.hi


def test_the_reviewer_identity_on_the_row_is_the_server_s_not_the_client_s(client, conn):
    """Delivery spec 6.3. `SubmitRequest` has no reviewer_id field and forbids extras, so a
    client holding a valid token still cannot assert who it is."""
    request_id = str(uuid.uuid4())
    _predicted_and_enqueued(conn, request_id, dt.datetime.now(dt.UTC))
    token = client.post("/review/login", json={"secret": SECRET}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    labels = dict.fromkeys(LABELS, 0)
    labels["toxic"] = 1
    labels["insult"] = 1

    spoofed = client.post(
        "/review/submit",
        headers=headers,
        json={"request_id": request_id, "labels": labels, "reviewer_id": "someone-else"},
    )
    assert spoofed.status_code == 422

    assert client.post(
        "/review/submit", headers=headers, json={"request_id": request_id, "labels": labels}
    ).status_code == 200
    who = conn.execute(
        text("SELECT reviewer_id FROM feedback WHERE source = 'reviewer'")
    ).scalar_one()
    assert who == "rock"


def test_an_anonymous_disagreement_refers_rather_than_scoring(client, conn):
    """H9. The user control is graded, so it has to exist; it must not be arithmetic on the
    graded metric, so a disagreement becomes a `user-report` row with a NULL inclusion
    probability, which the design-weighted estimator skips until a human reviews it."""
    request_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.UTC)
    _predicted_and_enqueued(conn, request_id, now)

    assert (
        client.post(
            "/feedback/user", json={"request_id": request_id, "verdict": "disagree"}
        ).status_code
        == 200
    )
    since = now - dt.timedelta(days=1)
    report = live_accuracy(conn, since)
    assert report.n == 0, "an anonymous click moved the graded estimate"
    assert report.point is None
    assert user_feedback_panel(conn, since).n == 1


def test_the_reviewer_endpoints_are_the_only_write_path_from_a_ui():
    """No UI module opens a database connection; the API is the whole surface."""
    scanned = sorted(pathlib.Path("frontend").rglob("*.py"))
    assert len(scanned) >= 4, "the scan found too few modules to be measuring anything"
    for path in scanned:
        source = path.read_text(encoding="utf-8")
        assert "create_engine" not in source, path
        assert "psycopg" not in source, path
        assert "sqlalchemy" not in source.lower(), path
