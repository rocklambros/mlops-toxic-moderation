import json
import logging

import pytest

from tests.integration.conftest import AUTH

pytestmark = pytest.mark.integration


def emitted(caplog) -> list[dict]:
    return [
        json.loads(record.message) for record in caplog.records if record.name == "backend.request"
    ]


def test_one_structured_line_per_request(client, caplog):
    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    lines = emitted(caplog)
    assert len(lines) == 1
    assert lines[0]["event"] == "predict"
    assert lines[0]["status"] == "ok"
    assert lines[0]["persist_status"] == "direct"
    assert lines[0]["input_chars"] == 16
    assert lines[0]["latency_ms"] >= 0
    # `latency_ms` is an integer rounded to the nearest millisecond and `handler_ms` keeps a
    # decimal, so the honest form of "the handler measured at least as much as persistence"
    # allows for half a millisecond of rounding. Asserting it strictly is a coin flip on
    # whichever side of .5 the persistence stamp landed.
    assert lines[0]["handler_ms"] >= lines[0]["latency_ms"] - 0.5


def test_the_log_carries_the_full_digest(client, caplog, artifact_bundle):
    """H14's other half. The digest is stripped from the public listener precisely so that
    it can live where incident response needs it: the log and the database row."""
    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    assert artifact_bundle["digest"] in emitted(caplog)[0]["model_version"]


def test_the_log_never_carries_raw_user_text(client, caplog):
    """Only the access-restricted RDS row holds user comments, and it holds them for 30 days.
    A log line copies them into CloudWatch, where the retention purge cannot reach."""
    secret = "my home address is 221b baker street"
    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": secret}, headers=AUTH)
    rendered = json.dumps(emitted(caplog))
    assert emitted(caplog), "the scan found no log lines to scan"
    assert secret not in rendered
    assert "221b" not in rendered


def test_the_log_never_carries_the_api_key(client, caplog):
    from tests.integration.conftest import DEMO_KEY

    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": "hello"}, headers=AUTH)
    assert DEMO_KEY not in json.dumps(emitted(caplog))
    assert emitted(caplog)[0]["client_fp"] is not None


def test_a_failed_request_is_logged_with_its_error_kind(client, caplog, monkeypatch):
    def explode(texts):
        raise RuntimeError("estimator blew up")

    monkeypatch.setattr(client.app.state.model, "predict_proba", explode)
    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": "hello"}, headers=AUTH)
    line = emitted(caplog)[0]
    assert line["status"] == "error"
    assert line["error_kind"] == "RuntimeError"
