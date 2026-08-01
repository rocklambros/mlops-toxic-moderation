"""FastAPI moderation backend: POST /predict and GET /health.

Ordering in this module is load-bearing. The `_gate` middleware runs the three abuse controls
(delivery spec section 6.3) before FastAPI parses the body, so unauthenticated or
rate-limited traffic never reaches validation, the model, or the database. Inside the
handler, `latency_ms` is stamped through persistence rather than before it (premortem H28),
and a failure writes a row rather than vanishing.
"""

import json
import logging
import random
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
from backend.model_loader import load_from_settings
from backend.persistence import persist_prediction
from backend.policy import decide, load_thresholds
from backend.preprocess import prepare_input
from backend.ratelimit import RateLimiter
from backend.schemas import PredictRequest
from backend.spool import Spool, SpoolFull
from model.contract import LabelScore, PredictionResponse, enforce_hierarchy, probs_to_dict
from model.labels import LABELS

log = logging.getLogger("backend.request")


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
        # Startup fails closed: a digest or allowlist violation must stop the container from
        # ever accepting traffic, not surface as a 500 on the first request.
        app.state.model = load_from_settings(settings)
        app.state.thresholds = load_thresholds(settings.thresholds_path)
        app.state.spool = Spool(settings.spool_path, settings.spool_max_rows)
        app.state.limiter = RateLimiter(settings.rate_limit_per_minute, settings.rate_limit_burst)
        # SystemRandom, not a seeded PRNG: the repository is public, and a predictable audit
        # sample lets an attacker time submissions to miss it.
        app.state.rng = random.SystemRandom()
        app.state.rejected = {"unauthenticated": 0, "rate_limited": 0, "oversize": 0}
        yield
        app.state.engine.dispose()

    app = FastAPI(title="Toxic Comment Moderation API", version="2.0", lifespan=lifespan)
    app.state.settings = settings

    def _reject(kind: str, status_code: int, detail: str, headers=None) -> JSONResponse:
        app.state.rejected[kind] += 1
        return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        if request.url.path != "/predict":
            return await call_next(request)
        raw_length = request.headers.get("content-length")
        if raw_length is None:
            return _reject("oversize", 411, "Content-Length header is required")
        if not raw_length.isdigit() or int(raw_length) > settings.max_body_bytes:
            return _reject("oversize", 413, "request body too large")
        presented = request.headers.get(API_KEY_HEADER)
        if not check_api_key(presented, settings.demo_api_key):
            return _reject("unauthenticated", 401, f"a valid {API_KEY_HEADER} header is required")
        fingerprint = client_fingerprint(presented)
        if not app.state.limiter.allow(fingerprint):
            return _reject("rate_limited", 429, "rate limit exceeded", {"Retry-After": "60"})
        request.state.client_fp = fingerprint
        return await call_next(request)

    @app.post("/predict", response_model=PredictionResponse)
    def predict(payload: PredictRequest, request: Request) -> PredictionResponse:
        started = time.perf_counter()
        state = request.app.state
        request_id = str(uuid.uuid4())
        client_fp = getattr(request.state, "client_fp", None)
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
