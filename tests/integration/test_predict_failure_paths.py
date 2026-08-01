from contextlib import contextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.db import Prediction
from backend.persistence import drain_spool
from tests.integration.conftest import AUTH

pytestmark = pytest.mark.integration


def break_the_database(client):
    @contextmanager
    def broken():
        raise OperationalError("connect", {}, Exception("connection refused"))
        yield  # pragma: no cover

    client.app.state.session_factory = broken


def test_predict_stays_available_when_the_database_is_down(client, engine, session):
    """H30, the finding this phase exists to close. Under the original design this returns
    503, which hands an attacker an off switch: exhaust a db.t4g.micro's connections and
    moderation is down, not degraded, for as long as the pressure lasts."""
    break_the_database(client)

    response = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["decision"] in {"allow", "review", "block"}
    assert client.app.state.spool.depth() == 1

    drained = drain_spool(
        sessionmaker(bind=engine, expire_on_commit=False), client.app.state.spool
    )
    assert drained == 1
    stored = session.scalars(select(Prediction)).all()
    assert len(stored) == 1
    assert stored[0].persist_status == "spooled"
    assert stored[0].input_text == "you are an idiot"


def test_health_reports_the_degradation_rather_than_hiding_it(client):
    break_the_database(client)
    client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    body = client.get("/health").json()
    assert body["database"] == "degraded"
    assert body["spool_depth"] == 1
    assert body["status"] == "degraded"


def test_a_saturated_spool_fails_closed_with_retry_after(client):
    """The one remaining 503. Reaching it costs SPOOL_MAX_ROWS successful requests through
    the rate limiter rather than a handful of concurrent connections."""
    break_the_database(client)
    client.app.state.spool.max_rows = 1
    assert client.post("/predict", json={"text": "first"}, headers=AUTH).status_code == 200
    saturated = client.post("/predict", json={"text": "second"}, headers=AUTH)
    assert saturated.status_code == 503
    assert saturated.headers["Retry-After"] == "30"


def test_failed_prediction_still_writes_a_row(client, session, monkeypatch):
    """H28's second half. If the failure path writes no row, the slowest requests are
    structurally absent from the graded latency series."""

    def explode(texts):
        raise RuntimeError("estimator blew up")

    monkeypatch.setattr(client.app.state.model, "predict_proba", explode)
    response = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    assert response.status_code == 500

    stored = session.scalars(select(Prediction)).all()
    assert len(stored) == 1
    assert stored[0].status == "error"
    assert stored[0].error_kind == "RuntimeError"
    assert stored[0].decision is None
    assert stored[0].prob_toxic is None
    assert stored[0].latency_ms >= 0
    assert stored[0].input_text == "you are an idiot"


def test_the_error_response_leaks_no_internals(client, monkeypatch):
    def explode(texts):
        raise RuntimeError("/srv/artifacts/toxic-clf.skops is corrupt")

    monkeypatch.setattr(client.app.state.model, "predict_proba", explode)
    response = client.post("/predict", json={"text": "x"}, headers=AUTH)
    assert response.json() == {"detail": "prediction failed"}
    assert "skops" not in response.text
