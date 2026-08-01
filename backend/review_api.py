"""Review and feedback endpoints.

Mounted onto the Phase 2 FastAPI app. This router exists so that neither Streamlit
container needs a database credential: the premortem (H12, H16) found that opening the demo
port also exposed a console with direct RDS write access to the graded metric, and the
cheapest way to stop that being true is for the UI to have no database access at all.

Four properties here are controls rather than conveniences.

* `SubmitRequest` has no `reviewer_id` field and forbids extras, so a client cannot assert
  an identity even if it holds the reviewer token. The identity written to the row is the
  one `backend.reviewer_auth.current_reviewer` returns, which is server configuration.
* The 409 on a second review comes from the guarded UPDATE's row count, not from a
  preceding SELECT. A pre-check would be a time-of-check/time-of-use gap that no sequential
  test can see, and the feedback row -- the numerator of the graded accuracy metric -- would
  be written twice under concurrency. The row is locked FOR UPDATE and the feedback insert
  happens only when the UPDATE claimed it.
* `/review/login` is rate limited per caller. The shared secret is the only thing between
  the internet and the graded metric, and an unlimited endpoint makes it brute-forceable
  online. Every attempt costs a token, including a successful one, so the counter does not
  leak whether a guess was right.
* `/feedback/user` needs no credential -- rubric 3.2 grades anonymous user feedback -- but
  every verdict must name a prediction that exists, is inside the feedback window, has no
  verdict yet, and belongs to a source under its quota. The ceiling on the whole path is
  therefore the number of predictions the caller could make, and /predict is itself keyed
  and rate limited.

Flags are recomputed from the pinned `thresholds.json` rather than a 0.5 default, so
reviewer agreement is measured against the decision rule that actually produced the
decision the reviewer is judging.
"""

import datetime as dt
import json
import os
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from backend.feedback import derive_feedback, insert_feedback, user_feedback
from backend.queue_guard import AdmissionConfig, admit_review, admit_user_feedback
from backend.ratelimit import RateLimiter
from backend.reviewer_auth import current_reviewer, issue_session_token
from model.labels import LABELS

router = APIRouter()

_ADMISSION_REFUSAL_STATUS = {
    "unknown_request": 404,
    "expired": 410,
    "duplicate": 409,
    "source_quota": 429,
    "queue_full": 429,
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _config() -> AdmissionConfig:
    return AdmissionConfig(
        max_pending=_int_env("REVIEW_QUEUE_MAX_PENDING", 500),
        max_pending_per_source=_int_env("REVIEW_QUEUE_MAX_PENDING_PER_SOURCE", 20),
        max_user_feedback_per_source_per_window=_int_env(
            "MAX_USER_FEEDBACK_PER_SOURCE_PER_WINDOW", 20
        ),
        random_audit_rate=float(os.environ.get("RANDOM_AUDIT_RATE", "0.05")),
    )


def _connection(request: Request):
    return request.app.state.engine.connect()


def _thresholds(request: Request) -> dict[str, float]:
    """Prefer the thresholds the app already loaded at startup.

    One process must not run two decision rules: the flags in the response and the flags a
    reviewer is judged against have to come from the same numbers.
    """
    loaded = getattr(request.app.state, "thresholds", None)
    if loaded:
        return loaded
    from monitoring.baseline import load_thresholds

    return load_thresholds(Path(os.environ.get("THRESHOLDS_PATH", "artifacts/thresholds.json")))


def _login_limiter(request: Request) -> RateLimiter:
    limiter = getattr(request.app.state, "review_login_limiter", None)
    if limiter is None:
        per_minute = max(1, _int_env("REVIEW_LOGIN_ATTEMPTS_PER_MINUTE", 5))
        limiter = RateLimiter(per_minute, per_minute)
        request.app.state.review_login_limiter = limiter
    return limiter


def _reviewer(authorization: str | None) -> str:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    who = current_reviewer(
        token,
        _now(),
        os.environ.get("REVIEWER_SHARED_SECRET", ""),
        os.environ.get("REVIEWER_ID", ""),
    )
    if who is None:
        raise HTTPException(status_code=401, detail="reviewer session required")
    return who


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: str = Field(max_length=512)


class SubmitRequest(BaseModel):
    # No reviewer_id field, and extra="forbid": the identity is unassertable by a client.
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(max_length=64)
    labels: dict[str, int]


class UserFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(max_length=64)
    verdict: str = Field(pattern="^(agree|disagree)$")


@router.post("/review/login")
def login(request: Request, payload: LoginRequest) -> dict:
    import hmac

    peer = request.client.host if request.client else "unknown"
    if not _login_limiter(request).allow(peer):
        raise HTTPException(
            status_code=429,
            detail="too many reviewer sign-in attempts",
            headers={"Retry-After": "60"},
        )
    secret = os.environ.get("REVIEWER_SHARED_SECRET", "")
    reviewer_id = os.environ.get("REVIEWER_ID", "")
    if not secret or not reviewer_id:
        raise HTTPException(status_code=401, detail="invalid reviewer secret")
    if not hmac.compare_digest(payload.secret.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid reviewer secret")
    return {"token": issue_session_token(_now(), secret, reviewer_id)}


@router.get("/review/pending")
def pending(request: Request, limit: int = 20, authorization: str | None = Header(None)) -> dict:
    _reviewer(authorization)
    prob_columns = ", ".join(f"p.prob_{label}" for label in LABELS)
    with _connection(request) as conn:
        rows = (
            conn.execute(
                text(
                    f"SELECT q.request_id, q.enqueued_ts, q.source, q.status, "
                    f"q.input_text_snapshot, q.distilbert_probs, {prob_columns} "
                    "FROM review_queue q JOIN predictions p ON p.request_id = q.request_id "
                    "WHERE q.status IN ('pending', 'rescored') "
                    "ORDER BY q.enqueued_ts LIMIT :limit"
                ),
                {"limit": min(max(limit, 1), 100)},
            )
            .mappings()
            .all()
        )
    items = [
        {
            "request_id": row["request_id"],
            "enqueued_ts": row["enqueued_ts"].isoformat(),
            "source": row["source"],
            "status": row["status"],
            "input_text_snapshot": row["input_text_snapshot"],
            "model_probs": {label: float(row[f"prob_{label}"]) for label in LABELS},
            "distilbert_probs": row["distilbert_probs"],
        }
        for row in rows
    ]
    return {"items": items}


@router.post("/review/submit")
def submit(
    request: Request, payload: SubmitRequest, authorization: str | None = Header(None)
) -> dict:
    reviewer_id = _reviewer(authorization)
    unknown = sorted(set(payload.labels) - set(LABELS))
    if unknown:
        raise HTTPException(status_code=422, detail=f"labels this model does not score: {unknown}")
    missing = [label for label in LABELS if label not in payload.labels]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing labels: {missing}")
    if any(payload.labels[label] not in (0, 1) for label in LABELS):
        raise HTTPException(status_code=422, detail="labels must be 0 or 1")

    thresholds = _thresholds(request)
    with _connection(request) as conn:
        row = (
            conn.execute(
                text(
                    "SELECT q.status, " + ", ".join(f"p.prob_{label}" for label in LABELS) + " "
                    "FROM review_queue q JOIN predictions p ON p.request_id = q.request_id "
                    "WHERE q.request_id = :rid FOR UPDATE OF q"
                ),
                {"rid": payload.request_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="request_id is not in the review queue")

        model_flags = {
            label: float(row[f"prob_{label}"]) >= thresholds[label] for label in LABELS
        }
        record = derive_feedback(payload.request_id, payload.labels, model_flags, reviewer_id)
        now = _now()
        # The guard IS the idempotency control: only the writer whose UPDATE claimed the row
        # goes on to insert the feedback row.
        claimed = conn.execute(
            text(
                "UPDATE review_queue SET status = 'reviewed', reviewer_labels = "
                "CAST(:labels AS jsonb), reviewer_id = :who, reviewed_ts = :ts "
                "WHERE request_id = :rid AND status <> 'reviewed'"
            ),
            {
                "labels": json.dumps({label: int(payload.labels[label]) for label in LABELS}),
                "who": reviewer_id,
                "ts": now,
                "rid": payload.request_id,
            },
        ).rowcount
        if claimed == 0:
            conn.rollback()
            raise HTTPException(status_code=409, detail="already reviewed")
        insert_feedback(conn, record, ts=now)
        conn.commit()
    return {"request_id": payload.request_id, "exact_match": record.exact_match}


@router.post("/feedback/user")
def submit_user_feedback(request: Request, payload: UserFeedbackRequest) -> dict:
    now = _now()
    config = _config()
    with _connection(request) as conn:
        # The quota is keyed on the fingerprint of whoever submitted the prediction, which
        # is what `admit_user_feedback` counts against. Reading it from the row rather than
        # from the caller keeps the argument and the SQL talking about the same thing.
        fp = conn.execute(
            text("SELECT submitter_fp FROM predictions WHERE request_id = :rid"),
            {"rid": payload.request_id},
        ).scalar()
        decision = admit_user_feedback(
            conn, request_id=payload.request_id, submitter_fp=fp, now=now, config=config
        )
        if not decision.admitted:
            raise HTTPException(
                status_code=_ADMISSION_REFUSAL_STATUS[decision.reason], detail=decision.reason
            )

        insert_feedback(conn, user_feedback(payload.request_id, payload.verdict), ts=now)
        conn.commit()

        if payload.verdict == "disagree":
            # A referral, not an arithmetic contribution. `admit_review` writes
            # sample_rate=NULL for 'user-report', so the design-weighted estimator ignores
            # the row until a human labels it; and it returns "duplicate" without touching
            # an item already drawn as 'flagged' or 'random-audit', so an anonymous click
            # cannot rewrite a known inclusion probability.
            admit_review(
                conn,
                request_id=payload.request_id,
                source="user-report",
                submitter_fp=fp,
                now=now,
                config=config,
            )
    return {"request_id": payload.request_id, "verdict": payload.verdict}
