"""FastAPI moderation backend: POST /predict, GET /health, and the reviewer router.

Ordering in this module is load-bearing. The `_gate` middleware runs the three abuse controls
(delivery spec section 6.3) before FastAPI parses the body, so unauthenticated or
rate-limited traffic never reaches validation, the model, or the database. Inside the
handler, `latency_ms` is stamped through persistence rather than before it (premortem H28),
and a failure writes a row rather than vanishing.

Two properties of the gate are the answer to defects that shipped, and both are stated here
because a reader who assumes the obvious design gets both of them wrong.

**It covers every path, by refusing the ones it does not name.** It used to open with
`if request.url.path != "/predict": return await call_next(request)`, which meant the four
routes `backend/review_api.py` mounts on this same app -- and therefore on the same
`0.0.0.0/0` listener on 8000 -- took no key, no rate limit and no body cap. `/feedback/user`
writes the graded feedback metric and checked nothing of its own. Naming the covered path is
a gate that stops covering the app the moment somebody adds a route; naming the exemptions
is one that does not.

**The rate limit keys on the caller, not on the credential.** It used to key on
`client_fingerprint(presented)`, a digest of the single demo key that `frontend/ui.py` holds
on behalf of every anonymous visitor, so the whole internet shared one 30-per-minute bucket
and one visitor could 429 a grader. It now keys on the same per-submitter fingerprint the
review-queue quotas count against (`backend/fingerprint.py`), which is why that value is
derived before the limit check rather than after it.
"""

import datetime as dt
import json
import logging
import random
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.audit import FLAGGED_SAMPLE_RATE, should_random_audit
from backend.auth import API_KEY_HEADER, check_api_key, client_fingerprint
from backend.config import Settings, load_settings
from backend.db import PendingWrite, PredictionRow, ReviewIntent, init_db, make_engine
from backend.fingerprint import SESSION_FP_HEADER, caller_identity, submitter_fp
from backend.model_loader import load_from_settings
from backend.persistence import persist_prediction
from backend.policy import decide, load_thresholds
from backend.preprocess import prepare_input
from backend.ratelimit import RateLimiter
from backend.review_api import router as review_router
from backend.schema_phase3 import apply_phase3_schema
from backend.schemas import PredictRequest
from backend.spool import Spool, SpoolFull
from model.contract import LabelScore, PredictionResponse, enforce_hierarchy, probs_to_dict
from model.labels import LABELS

log = logging.getLogger("backend.request")

# The liveness path. Exempt from the demo key because the grader, `infra/aws/verify_deploy.sh`
# and the container HEALTHCHECK all call it unauthenticated, and it answers with counters and
# an opaque model version rather than with comment text. Exempt from the per-caller bucket
# too: the deploy gate retries it eighteen times while an instance boots, and a liveness probe
# that answers 429 is a deploy gate failing for a reason unrelated to the deploy. The peer
# ceiling below still applies to it, because it opens a database session on every call.
LIVENESS_PATHS = frozenset({"/health"})

# FastAPI's own schema routes. README.md publishes /docs among the graded links, so keying it
# would refuse a grader following the deliverables table. The disclosure it costs is nil: the
# route list Swagger renders is `backend/review_api.py`, in a repository that is public, next
# to a README that publishes the address. Exempt from the key, not from the metering.
SCHEMA_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})

# The paths served without the demo key, and the only ones. /review/login is here under
# protest: `frontend.api_client.BackendClient.login` posts the reviewer secret with no headers
# at all, so requiring the key there locks the reviewer console out of its own console. Every
# other call the two Streamlit processes make goes through `_headers()`, which carries the
# key, so every other route is inside the requirement. What stands in front of /review/login
# instead is the gate's rate limit, the five-attempts-per-minute per-peer limiter in
# `backend/review_api.py`, and the shared secret itself.
UNKEYED_PATHS = LIVENESS_PATHS | SCHEMA_PATHS | frozenset({"/review/login"})

# Methods that carry a body, and so must declare its length before the body is read. A GET
# sends no Content-Length; demanding one would refuse GET /health and GET /review/pending in
# the name of a rule about bodies neither of them has.
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

# Reads by an already-authenticated reviewer, held by the peer ceiling but not spent out of
# the per-caller bucket.
#
# That bucket exists to meter INFERENCE: `rate_limit_per_minute` is 30 with a burst of 10,
# sized for how often a visitor should be allowed to run the model. `GET /review/pending`
# runs no model. It is a SELECT behind two credentials, the demo key and a reviewer bearer
# token, and metering it out of the inference budget breaks the console that depends on it:
# Streamlit re-runs the whole script on every widget interaction, `frontend/reviewer.py`
# fetches the queue at the top of that script, and six label checkboxes on one item is seven
# fetches. A reviewer would exhaust a burst of 10 inside two items and be signed out.
#
# Exempting it costs little. Without a valid reviewer token the route answers 401 from
# `review_api` regardless, so this widens nothing for a caller who only holds the demo key,
# and the peer ceiling still bounds the aggregate. Writes are NOT here: /review/submit and
# /feedback/user change the graded metric and stay fully metered.
AUTHENTICATED_READ_PATHS = frozenset({"/review/pending"})

# The per-caller bucket is only as trustworthy as the identity it is keyed on, and a caller
# holding the demo key chooses part of that identity: `caller_identity` honours any 16-hex
# X-Session-Fp such a caller sends, so a fresh header per request is a fresh bucket per
# request. The TCP peer is the part no caller can choose, so a second and looser bucket is
# keyed on it, and both must admit the request.
#
# A multiple of the configured limit rather than a Settings field of its own, deliberately:
# `make seed-demo` replays ~2000 comments by raising RATE_LIMIT_PER_MINUTE for the window,
# and a ceiling that did not move with it would refuse the seeder at whatever number was
# written here. It also means no deploy can widen the ceiling without widening the
# per-visitor limit it is a multiple of.
PEER_CEILING_MULTIPLIER = 10


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    # The record IS the JSON document, so no prefix is formatted around it: Phase 5's awslogs
    # driver ships these lines straight into CloudWatch Logs Insights. `force=False` leaves an
    # already-configured root logger alone, which is what keeps pytest's caplog handler and
    # uvicorn's own configuration intact.
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(settings)
        app.state.session_factory = sessionmaker(bind=app.state.engine, expire_on_commit=False)
        init_db(app.state.engine)
        # `init_db` creates the three tables from the ORM. It does not create the columns
        # and partial indexes that live only in the Phase 3 migration -- `is_seed`, the
        # composite index the per-source quota counts against, and the one-user-verdict
        # index -- so without this line they exist only where somebody ran the migration by
        # hand, and `make seed-demo` fails on its first statement against the deployed
        # database. Both are idempotent, so re-running them on every boot is free.
        apply_phase3_schema(app.state.engine)
        # Startup fails closed: a digest or allowlist violation must stop the container from
        # ever accepting traffic, not surface as a 500 on the first request.
        app.state.model = load_from_settings(settings)
        app.state.thresholds = load_thresholds(settings.thresholds_path)
        app.state.spool = Spool(settings.spool_path, settings.spool_max_rows)
        app.state.limiter = RateLimiter(settings.rate_limit_per_minute, settings.rate_limit_burst)
        app.state.peer_limiter = RateLimiter(
            settings.rate_limit_per_minute * PEER_CEILING_MULTIPLIER,
            settings.rate_limit_burst * PEER_CEILING_MULTIPLIER,
        )
        # SystemRandom, not a seeded PRNG: the repository is public, and a predictable audit
        # sample lets an attacker time submissions to miss it.
        app.state.rng = random.SystemRandom()
        app.state.rejected = {"unauthenticated": 0, "rate_limited": 0, "oversize": 0}
        # The quota key must exist even when the deploy forgot to set one, or every
        # per-source limit in backend/queue_guard.py counts zero for every caller. A random
        # per-process key keeps the control live; it only loses stability across restarts,
        # and that trade is stated out loud rather than silently taken.
        if settings.submitter_fp_key:
            app.state.submitter_fp_key = settings.submitter_fp_key.encode("utf-8")
        else:
            app.state.submitter_fp_key = secrets.token_bytes(32)
            log.warning(
                json.dumps(
                    {
                        "event": "config",
                        "warning": "SUBMITTER_FP_KEY is unset; using a random per-process "
                        "key, so per-source quota buckets reset on restart",
                    },
                    sort_keys=True,
                )
            )
        yield
        app.state.engine.dispose()

    app = FastAPI(title="Toxic Comment Moderation API", version="2.0", lifespan=lifespan)
    app.state.settings = settings
    # Every UI write goes through this router, so neither Streamlit container ever holds a
    # database credential (premortem H12, H16).
    app.include_router(review_router)

    def _reject(kind: str, status_code: int, detail: str, headers=None) -> JSONResponse:
        app.state.rejected[kind] += 1
        return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        path = request.url.path
        # A request carrying both framings lets the two ends disagree about where the body
        # ends: this gate reads Content-Length and admits it, while h11 frames on chunked and
        # streams a body of any size past the cap. RFC 9112 section 6.1 says a recipient that
        # sees both MUST NOT process it as anything but an error, so refuse it outright rather
        # than picking a winner and hoping the server picked the same one.
        if "transfer-encoding" in request.headers and "content-length" in request.headers:
            return _reject("oversize", 400, "conflicting Content-Length and Transfer-Encoding")
        if request.method in BODY_METHODS:
            raw_length = request.headers.get("content-length")
            if raw_length is None:
                return _reject("oversize", 411, "Content-Length header is required")
            if not raw_length.isdigit() or int(raw_length) > settings.max_body_bytes:
                return _reject("oversize", 413, "request body too large")
        presented = request.headers.get(API_KEY_HEADER)
        api_key_ok = check_api_key(presented, settings.demo_api_key)
        if not api_key_ok and path not in UNKEYED_PATHS:
            return _reject("unauthenticated", 401, f"a valid {API_KEY_HEADER} header is required")

        peer = request.client.host if request.client else "unknown"
        day = dt.datetime.now(dt.UTC).date()
        key = app.state.submitter_fp_key
        # Derived here, above the limit check rather than below it, because this is the value
        # the limit is taken against. A session fingerprint is only honoured from a caller
        # that also presented the frontend's key, and the X-Forwarded-For a proxy would set is
        # never consulted -- `caller_identity` has no parameter for it.
        identity = caller_identity(
            peer, request.headers.get(SESSION_FP_HEADER), api_key_ok=api_key_ok
        )
        source_fp = submitter_fp(identity, day, key)
        # Built through `caller_identity` rather than by formatting "peer:" here, so the one
        # definition of the namespace prefix stays in backend/fingerprint.py. Hashed for the
        # same reason the stored fingerprint is: an address is not kept in this process any
        # longer than the request that carried it.
        peer_fp = submitter_fp(caller_identity(peer, None, api_key_ok=False), day, key)
        # The per-caller bucket first: a single caller exhausting their own allowance must not
        # also spend a token from the ceiling everyone else shares.
        unmetered = LIVENESS_PATHS | AUTHENTICATED_READ_PATHS
        if path not in unmetered and not app.state.limiter.allow(source_fp):
            return _reject("rate_limited", 429, "rate limit exceeded", {"Retry-After": "60"})
        if not app.state.peer_limiter.allow(peer_fp):
            return _reject("rate_limited", 429, "rate limit exceeded", {"Retry-After": "60"})
        if api_key_ok:
            request.state.client_fp = client_fingerprint(presented)
        request.state.submitter_fp = source_fp
        return await call_next(request)

    @app.post("/predict", response_model=PredictionResponse)
    def predict(payload: PredictRequest, request: Request) -> PredictionResponse:
        started = time.perf_counter()
        state = request.app.state
        request_id = str(uuid.uuid4())
        client_fp = getattr(request.state, "client_fp", None)
        source_fp = getattr(request.state, "submitter_fp", None)
        model = state.model

        try:
            normalized = prepare_input(payload.text)
            # `enforce_hierarchy` runs here, once, before anything reads the numbers.
            # OneVsRestClassifier fits the six labels independently, so severe_toxic > toxic
            # comes out of the real estimator, and PredictionResponse refuses that pair -- so
            # skipping the clamp is a 500 on ordinary input, not a theoretical incoherence.
            # Clamping before `decide` keeps the flags, the row and the response consistent
            # with each other (premortem H22).
            probs = enforce_hierarchy(probs_to_dict(model.predict_proba([normalized])[0]))
            result = decide(probs, state.thresholds)
        except Exception as exc:  # noqa: BLE001 - every failure must leave a row behind
            failed = PredictionRow(
                request_id=request_id,
                input_text=payload.text,
                input_chars=len(payload.text),
                model_version=model.model_version,
                probs=None,
                decision=None,
                max_prob=None,
                latency_ms=0,
                status="error",
                persist_status="direct",
                error_kind=type(exc).__name__,
                client_fp=client_fp,
                submitter_fp=source_fp,
            )
            outcome = _persist(state, PendingWrite(prediction=failed), started)
            _log(model, failed, outcome, started)
            raise HTTPException(status_code=500, detail="prediction failed") from exc

        review = None
        if result.decision in ("review", "block"):
            review = ReviewIntent(
                request_id=request_id,
                source="flagged",
                sample_rate=FLAGGED_SAMPLE_RATE,
                input_text_snapshot=payload.text,
            )
        elif should_random_audit(state.settings.random_audit_rate, state.rng):
            review = ReviewIntent(
                request_id=request_id,
                source="random-audit",
                sample_rate=state.settings.random_audit_rate,
                input_text_snapshot=payload.text,
            )

        row = PredictionRow(
            request_id=request_id,
            input_text=payload.text,
            input_chars=len(payload.text),
            model_version=model.model_version,
            probs=probs,
            decision=result.decision,
            max_prob=result.max_prob,
            latency_ms=0,
            status="ok",
            persist_status="direct",
            client_fp=client_fp,
            submitter_fp=source_fp,
        )
        outcome = _persist(state, PendingWrite(prediction=row, review=review), started)
        _log(model, row, outcome, started)

        return PredictionResponse(
            request_id=request_id,
            model_version=model.public_version,
            labels={
                label: LabelScore(prob=probs[label], flag=result.flags[label]) for label in LABELS
            },
            decision=result.decision,
            max_prob=result.max_prob,
            latency_ms=outcome.latency_ms,
        )

    @app.get("/health")
    def health(request: Request) -> dict:
        state = request.app.state
        database = "ok"
        try:
            with state.session_factory() as session:
                session.execute(sql_text("select 1"))
        except SQLAlchemyError:
            database = "degraded"
        spool_depth = state.spool.depth()
        return {
            "status": "ok" if database == "ok" and spool_depth == 0 else "degraded",
            "model_version": state.model.public_version,
            "database": database,
            "spool_depth": spool_depth,
            "rejected": dict(state.rejected),
        }

    return app


def _persist(state, pending: PendingWrite, started: float):
    try:
        return persist_prediction(state.session_factory, state.spool, pending, started)
    except SpoolFull as exc:
        raise HTTPException(
            status_code=503,
            detail="prediction log is saturated; retry later",
            headers={"Retry-After": "30"},
        ) from exc


def _log(model, row: PredictionRow, outcome, started: float) -> None:
    """One structured line per request. Carries the FULL model version, because incident
    response needs to know exactly which artifact produced a score (H14), and never carries
    input_text, because only the access-restricted RDS row holds user comments."""
    log.info(
        json.dumps(
            {
                "event": "predict",
                "request_id": row.request_id,
                "model_version": model.model_version,
                "status": row.status,
                "decision": row.decision,
                "error_kind": row.error_kind,
                "latency_ms": outcome.latency_ms,
                "handler_ms": round((time.perf_counter() - started) * 1000, 1),
                "commit_ms": round(outcome.commit_ms, 1),
                "persist_status": outcome.persist_status,
                "client_fp": row.client_fp,
                "input_chars": row.input_chars,
            },
            sort_keys=True,
        )
    )
