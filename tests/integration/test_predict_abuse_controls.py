import pytest

from backend.config import MAX_INPUT_CHARS
from backend.ratelimit import RateLimiter
from tests.integration.conftest import AUTH, DEMO_KEY

pytestmark = pytest.mark.integration


def test_predict_requires_a_valid_api_key(client):
    """REG-6.3c."""
    assert client.post("/predict", json={"text": "hello"}).status_code == 401
    assert (
        client.post("/predict", json={"text": "hello"}, headers={"X-API-Key": "wrong"}).status_code
        == 401
    )
    assert client.post("/predict", json={"text": "hello"}, headers=AUTH).status_code == 200


def test_the_key_never_appears_in_a_response(client):
    response = client.post("/predict", json={"text": "hello"}, headers={"X-API-Key": "wrong"})
    assert DEMO_KEY not in response.text


def test_health_is_reachable_without_a_key(client):
    assert client.get("/health").status_code == 200


def test_authentication_precedes_body_validation(client):
    """An unauthenticated caller must not be able to make the server parse and validate a
    16 KB body; the gate runs before pydantic."""
    response = client.post("/predict", json={"text": "a" * (MAX_INPUT_CHARS + 1)})
    assert response.status_code == 401


def test_oversize_text_is_rejected_with_422(client):
    """REG-6.3a."""
    response = client.post("/predict", json={"text": "a" * (MAX_INPUT_CHARS + 1)}, headers=AUTH)
    assert response.status_code == 422


def test_oversize_body_is_rejected_before_parsing(client):
    response = client.post(
        "/predict",
        content=b'{"text": "' + b"a" * 20000 + b'"}',
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_a_body_without_content_length_is_refused(client):
    def chunks():
        yield b'{"text": "hello"}'

    response = client.post(
        "/predict", content=chunks(), headers={**AUTH, "Content-Type": "application/json"}
    )
    assert response.status_code == 411


def test_rate_limited_after_the_burst_is_exhausted(client):
    """REG-6.3b. Without this the endpoint is free denial-of-service capacity, and the
    durable spool it protects fills SPOOL_MAX_ROWS times faster."""
    client.app.state.limiter = RateLimiter(per_minute=60, burst=3)
    codes = [
        client.post("/predict", json={"text": f"comment {index}"}, headers=AUTH).status_code
        for index in range(5)
    ]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


def test_rate_limited_responses_carry_retry_after_and_are_counted(client):
    client.app.state.limiter = RateLimiter(per_minute=60, burst=1)
    client.post("/predict", json={"text": "one"}, headers=AUTH)
    limited = client.post("/predict", json={"text": "two"}, headers=AUTH)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert client.get("/health").json()["rejected"]["rate_limited"] == 1


def test_rejected_requests_do_not_write_rows(client, session):
    from sqlalchemy import select

    from backend.db import Prediction

    client.post("/predict", json={"text": "hello"})  # 401
    client.post("/predict", json={"text": ""}, headers=AUTH)  # 422
    assert session.scalars(select(Prediction)).all() == []
