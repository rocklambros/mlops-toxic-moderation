import datetime as dt
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.review_api import router
from model.labels import LABELS

pytestmark = pytest.mark.integration

SECRET = "reviewer-shared-secret"
REVIEWER = "rock"
FP = "aaaabbbbccccdddd"
HEX64 = re.compile(r"[0-9a-f]{64}")


@pytest.fixture()
def review_client(conn, monkeypatch, engine):
    monkeypatch.setenv("REVIEWER_SHARED_SECRET", SECRET)
    monkeypatch.setenv("REVIEWER_ID", REVIEWER)
    monkeypatch.setenv("THRESHOLDS_PATH", "tests/fixtures/thresholds.json")
    app = FastAPI()
    app.include_router(router)
    app.state.engine = engine
    return TestClient(app)


def _seed(
    conn,
    request_id: str,
    probs: dict[str, float] | None = None,
    *,
    submitter_fp: str | None = FP,
    ts: dt.datetime | None = None,
    enqueue: bool = True,
    decision: str = "review",
) -> None:
    probs = probs or dict.fromkeys(LABELS, 0.1)
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    binds = ", ".join(f":p_{label}" for label in LABELS)
    params = {f"p_{label}": probs[label] for label in LABELS}
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, input_chars, model_version, "
            f"{cols}, decision, max_prob, latency_ms, status, persist_status, submitter_fp) "
            f"VALUES (:rid, COALESCE(CAST(:ts AS timestamptz), now()), 'you are an idiot', 16, "
            f"'toxic-clf:v3', {binds}, :decision, :mx, 12, 'ok', 'direct', :fp)"
        ),
        {
            "rid": request_id,
            "ts": ts,
            "mx": max(probs.values()),
            "fp": submitter_fp,
            "decision": decision,
            **params,
        },
    )
    if enqueue:
        conn.execute(
            text(
                "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
                "input_text_snapshot) VALUES (:rid, now(), 'pending', 'flagged', 1.0, "
                "'you are an idiot')"
            ),
            {"rid": request_id},
        )
    conn.commit()


def _login(client) -> str:
    response = client.post("/review/login", json={"secret": SECRET})
    assert response.status_code == 200
    return response.json()["token"]


def _auth(client) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client)}"}


# --------------------------------------------------------------------------- login


def test_login_rejects_a_wrong_secret(review_client):
    assert review_client.post("/review/login", json={"secret": "nope"}).status_code == 401


def test_login_on_an_unconfigured_server_rejects_every_secret(review_client, monkeypatch):
    """A deploy that forgot the secret must not accept the empty string as one."""
    monkeypatch.delenv("REVIEWER_SHARED_SECRET")
    assert review_client.post("/review/login", json={"secret": ""}).status_code == 401
    assert review_client.post("/review/login", json={"secret": SECRET}).status_code == 401


def test_login_is_rate_limited_per_caller(review_client, monkeypatch):
    """The shared secret is the only thing between the internet and the graded metric, so
    the guess rate is capped. Without this the secret is brute-forceable online."""
    monkeypatch.setenv("REVIEW_LOGIN_ATTEMPTS_PER_MINUTE", "3")
    codes = [
        review_client.post("/review/login", json={"secret": "guess"}).status_code
        for _ in range(6)
    ]
    assert codes[:3] == [401, 401, 401]
    assert 429 in codes[3:], codes


def test_the_rate_limit_does_not_leak_whether_the_guess_was_right(review_client, monkeypatch):
    """A cap that only counts failures would answer 'was that the secret?' by whether the
    counter moved. Every attempt costs a token."""
    monkeypatch.setenv("REVIEW_LOGIN_ATTEMPTS_PER_MINUTE", "2")
    assert review_client.post("/review/login", json={"secret": SECRET}).status_code == 200
    assert review_client.post("/review/login", json={"secret": SECRET}).status_code == 200
    assert review_client.post("/review/login", json={"secret": SECRET}).status_code == 429


# --------------------------------------------------------------------------- pending


def test_pending_requires_a_token(review_client):
    assert review_client.get("/review/pending").status_code == 401


def test_pending_rejects_a_forged_token(review_client):
    forged = {"Authorization": "Bearer rock.99999999999.deadbeef"}
    assert review_client.get("/review/pending", headers=forged).status_code == 401


def test_pending_returns_the_snapshot_verbatim(review_client, conn):
    _seed(conn, "a1")
    response = review_client.get("/review/pending", headers=_auth(review_client))
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["request_id"] == "a1"
    assert items[0]["input_text_snapshot"] == "you are an idiot"
    assert set(items[0]["model_probs"]) == set(LABELS)


def test_pending_carries_no_artifact_digest(review_client, conn):
    """No response may carry the 64-hex artifact digest; the reviewer console is a
    response like any other."""
    _seed(conn, "a1b")
    body = review_client.get("/review/pending", headers=_auth(review_client)).text
    assert not HEX64.search(body), "a 64-hex digest reached a client"


def test_pending_skips_rows_that_were_already_reviewed(review_client, conn):
    _seed(conn, "a1c")
    conn.execute(text("UPDATE review_queue SET status = 'reviewed' WHERE request_id = 'a1c'"))
    conn.commit()
    items = review_client.get("/review/pending", headers=_auth(review_client)).json()["items"]
    assert items == []


def test_pending_still_serves_rescored_rows_when_the_rescorer_is_cut(review_client, conn):
    """C8: the re-scorer sits behind the cut-line, so 'rescored' must be drainable and a
    missing challenger column must not hide the row."""
    _seed(conn, "a1d")
    conn.execute(text("UPDATE review_queue SET status = 'rescored' WHERE request_id = 'a1d'"))
    conn.commit()
    items = review_client.get("/review/pending", headers=_auth(review_client)).json()["items"]
    assert [item["request_id"] for item in items] == ["a1d"]
    assert items[0]["distilbert_probs"] is None


# --------------------------------------------------------------------------- submit


def test_submit_body_rejects_a_client_supplied_reviewer_id(review_client, conn):
    """H12/section 6.3: the field does not exist, so the identity cannot be asserted."""
    _seed(conn, "a2")
    response = review_client.post(
        "/review/submit",
        headers=_auth(review_client),
        json={
            "request_id": "a2",
            "labels": dict.fromkeys(LABELS, 0),
            "reviewer_id": "admin",
        },
    )
    assert response.status_code == 422
    assert conn.execute(
        text("SELECT reviewer_id FROM review_queue WHERE request_id = 'a2'")
    ).scalar_one() is None


def test_submit_requires_a_token(review_client, conn):
    _seed(conn, "a2b")
    response = review_client.post(
        "/review/submit", json={"request_id": "a2b", "labels": dict.fromkeys(LABELS, 0)}
    )
    assert response.status_code == 401


def test_submit_writes_labels_status_and_a_derived_feedback_row(review_client, conn):
    _seed(conn, "a3", {**dict.fromkeys(LABELS, 0.1), "toxic": 0.9, "insult": 0.8})
    labels = dict.fromkeys(LABELS, 0)
    labels["toxic"] = 1
    labels["insult"] = 1
    response = review_client.post(
        "/review/submit",
        headers=_auth(review_client),
        json={"request_id": "a3", "labels": labels},
    )
    assert response.status_code == 200
    row = conn.execute(
        text(
            "SELECT status, reviewer_id, reviewer_labels, reviewed_ts FROM review_queue "
            "WHERE request_id = 'a3'"
        )
    ).one()
    assert row.status == "reviewed"
    assert row.reviewer_id == REVIEWER
    assert row.reviewer_labels["toxic"] == 1
    assert row.reviewed_ts is not None
    feedback = conn.execute(
        text("SELECT source, reviewer_id, exact_match, agreement FROM feedback "
             "WHERE request_id = 'a3'")
    ).one()
    assert feedback.source == "reviewer"
    assert feedback.reviewer_id == REVIEWER
    assert feedback.exact_match is True
    assert set(feedback.agreement) == set(LABELS)


def test_agreement_is_computed_against_the_pinned_thresholds_not_a_hard_coded_half(
    review_client, conn
):
    """tests/fixtures/thresholds.json puts `threat` at 0.18. A 0.30 probability is a model
    flag under that rule and is not under a 0.5 default, so a reviewer who says `threat=1`
    agrees. Hard-coding 0.5 makes it a disagreement and silently rewrites the metric."""
    _seed(conn, "a3b", {**dict.fromkeys(LABELS, 0.05), "threat": 0.30})
    labels = dict.fromkeys(LABELS, 0)
    labels["threat"] = 1
    response = review_client.post(
        "/review/submit",
        headers=_auth(review_client),
        json={"request_id": "a3b", "labels": labels},
    )
    assert response.status_code == 200
    assert response.json()["exact_match"] is True
    agreement = conn.execute(
        text("SELECT agreement FROM feedback WHERE request_id = 'a3b'")
    ).scalar_one()
    assert agreement["threat"] is True


def test_submit_records_a_disagreement_as_a_disagreement(review_client, conn):
    _seed(conn, "a3c", {**dict.fromkeys(LABELS, 0.05), "toxic": 0.95})
    response = review_client.post(
        "/review/submit",
        headers=_auth(review_client),
        json={"request_id": "a3c", "labels": dict.fromkeys(LABELS, 0)},
    )
    assert response.status_code == 200
    assert response.json()["exact_match"] is False
    row = conn.execute(
        text("SELECT exact_match, agreement FROM feedback WHERE request_id = 'a3c'")
    ).one()
    assert row.exact_match is False
    assert row.agreement["toxic"] is False


def test_submitting_twice_does_not_double_count(review_client, conn):
    _seed(conn, "a4")
    labels = dict.fromkeys(LABELS, 0)
    headers = _auth(review_client)
    assert review_client.post(
        "/review/submit", headers=headers, json={"request_id": "a4", "labels": labels}
    ).status_code == 200
    second = review_client.post(
        "/review/submit", headers=headers, json={"request_id": "a4", "labels": labels}
    )
    assert second.status_code == 409
    count = conn.execute(
        text("SELECT count(*) FROM feedback WHERE request_id = 'a4'")
    ).scalar_one()
    assert count == 1


def test_submit_on_an_unqueued_request_is_404(review_client, conn):
    _seed(conn, "a4b", enqueue=False)
    response = review_client.post(
        "/review/submit",
        headers=_auth(review_client),
        json={"request_id": "a4b", "labels": dict.fromkeys(LABELS, 0)},
    )
    assert response.status_code == 404


def test_submit_refuses_an_incomplete_label_vector(review_client, conn):
    """A missing label defaulted to 0 manufactures agreement, and agreement is the
    numerator of the graded metric."""
    _seed(conn, "a4c")
    partial = dict.fromkeys(LABELS, 0)
    partial.pop("threat")
    response = review_client.post(
        "/review/submit",
        headers=_auth(review_client),
        json={"request_id": "a4c", "labels": partial},
    )
    assert response.status_code == 422
    assert conn.execute(
        text("SELECT count(*) FROM feedback WHERE request_id = 'a4c'")
    ).scalar_one() == 0


def test_submit_refuses_a_non_binary_label(review_client, conn):
    _seed(conn, "a4d")
    labels = dict.fromkeys(LABELS, 0)
    labels["toxic"] = 7
    response = review_client.post(
        "/review/submit",
        headers=_auth(review_client),
        json={"request_id": "a4d", "labels": labels},
    )
    assert response.status_code == 422


def test_submit_refuses_a_label_this_model_does_not_score(review_client, conn):
    _seed(conn, "a4e")
    labels = dict.fromkeys(LABELS, 0)
    labels["spicy"] = 1
    response = review_client.post(
        "/review/submit",
        headers=_auth(review_client),
        json={"request_id": "a4e", "labels": labels},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- user feedback


def test_user_feedback_writes_a_user_sourced_row(review_client, conn):
    _seed(conn, "a5")
    response = review_client.post(
        "/feedback/user", json={"request_id": "a5", "verdict": "agree"}
    )
    assert response.status_code == 200
    row = conn.execute(
        text("SELECT source, reviewer_id, exact_match, agreement FROM feedback "
             "WHERE request_id = 'a5'")
    ).one()
    assert row.source == "user"
    assert row.reviewer_id is None
    assert row.exact_match is True
    assert row.agreement == {}


def test_user_feedback_needs_no_reviewer_token_but_is_rate_limited(
    review_client, conn, monkeypatch
):
    """H9: an anonymous write path into a graded metric is capped per source. The cap keys
    on `predictions.submitter_fp`, which is why /predict has to write one.

    The cap is 2 rather than 1 so that a bug which refuses the second verdict outright --
    without counting anything -- cannot pass."""
    monkeypatch.setenv("MAX_USER_FEEDBACK_PER_SOURCE_PER_WINDOW", "2")
    _seed(conn, "a6")
    _seed(conn, "a7")
    _seed(conn, "a7b")
    for request_id, expected in (("a6", 200), ("a7", 200), ("a7b", 429)):
        response = review_client.post(
            "/feedback/user", json={"request_id": request_id, "verdict": "agree"}
        )
        assert response.status_code == expected, request_id


def test_the_quota_is_per_source_not_global(review_client, conn, monkeypatch):
    """Two fingerprints must not share one bucket, or one flooder silences everybody."""
    monkeypatch.setenv("MAX_USER_FEEDBACK_PER_SOURCE_PER_WINDOW", "2")
    _seed(conn, "a6c")
    _seed(conn, "a7c")
    _seed(conn, "a7d", submitter_fp="1111111111111111")
    assert review_client.post(
        "/feedback/user", json={"request_id": "a6c", "verdict": "agree"}
    ).status_code == 200
    assert review_client.post(
        "/feedback/user", json={"request_id": "a7c", "verdict": "agree"}
    ).status_code == 200
    assert review_client.post(
        "/feedback/user", json={"request_id": "a7d", "verdict": "agree"}
    ).status_code == 200


def test_user_feedback_rejects_an_unknown_verdict(review_client, conn):
    _seed(conn, "a8")
    response = review_client.post(
        "/feedback/user", json={"request_id": "a8", "verdict": "spam"}
    )
    assert response.status_code == 422


def test_user_feedback_rejects_free_text(review_client, conn):
    _seed(conn, "a9")
    response = review_client.post(
        "/feedback/user",
        json={"request_id": "a9", "verdict": "agree", "comment": "x" * 100000},
    )
    assert response.status_code == 422
    assert conn.execute(
        text("SELECT count(*) FROM feedback WHERE request_id = 'a9'")
    ).scalar_one() == 0


def test_user_feedback_on_an_unknown_request_is_404(review_client, conn):
    assert review_client.post(
        "/feedback/user", json={"request_id": "nope", "verdict": "agree"}
    ).status_code == 404


def test_user_feedback_on_a_purged_prediction_is_410(review_client, conn):
    """`input_text` is purged at 30 days, so a verdict on an ancient row is a verdict on
    text nobody can check."""
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    _seed(conn, "a9b", ts=old)
    assert review_client.post(
        "/feedback/user", json={"request_id": "a9b", "verdict": "agree"}
    ).status_code == 410


def test_a_second_verdict_on_the_same_request_is_refused(review_client, conn):
    _seed(conn, "a9c")
    assert review_client.post(
        "/feedback/user", json={"request_id": "a9c", "verdict": "agree"}
    ).status_code == 200
    assert review_client.post(
        "/feedback/user", json={"request_id": "a9c", "verdict": "disagree"}
    ).status_code == 409
    assert conn.execute(
        text("SELECT count(*) FROM feedback WHERE request_id = 'a9c'")
    ).scalar_one() == 1


def test_user_disagreement_enqueues_a_user_report_whose_sample_rate_stays_null(
    review_client, conn
):
    """The referral is how user feedback reaches live accuracy: through a human, not
    through arithmetic. sample_rate stays NULL so the estimator ignores it."""
    _seed(conn, "a10", dict.fromkeys(LABELS, 0.05), enqueue=False, decision="allow")
    assert review_client.post(
        "/feedback/user", json={"request_id": "a10", "verdict": "disagree"}
    ).status_code == 200
    row = conn.execute(
        text("SELECT source, sample_rate, status FROM review_queue WHERE request_id = 'a10'")
    ).one()
    assert row.source == "user-report"
    assert row.sample_rate is None
    assert row.status == "pending"


def test_user_agreement_never_enqueues_a_review(review_client, conn):
    _seed(conn, "a11", enqueue=False)
    assert review_client.post(
        "/feedback/user", json={"request_id": "a11", "verdict": "agree"}
    ).status_code == 200
    assert conn.execute(
        text("SELECT count(*) FROM review_queue WHERE request_id = 'a11'")
    ).scalar_one() == 0


def test_a_disagreement_on_an_already_queued_item_leaves_its_sample_rate_alone(
    review_client, conn
):
    """Rewriting a flagged row's pi to NULL would delete it from the estimator, which is a
    write path from an anonymous click into the graded metric by the back door."""
    _seed(conn, "a12")
    assert review_client.post(
        "/feedback/user", json={"request_id": "a12", "verdict": "disagree"}
    ).status_code == 200
    row = conn.execute(
        text("SELECT source, sample_rate FROM review_queue WHERE request_id = 'a12'")
    ).one()
    assert row.source == "flagged"
    assert row.sample_rate == 1.0


def test_the_user_feedback_row_is_never_attributed_to_a_reviewer(review_client, conn):
    """A user row with a reviewer_id would be counted by the design-weighted estimator."""
    _seed(conn, "a13")
    review_client.post("/feedback/user", json={"request_id": "a13", "verdict": "disagree"})
    assert conn.execute(
        text("SELECT count(*) FROM feedback WHERE source = 'user' AND reviewer_id IS NOT NULL")
    ).scalar_one() == 0
