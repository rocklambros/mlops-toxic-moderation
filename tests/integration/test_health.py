import re

import pytest

pytestmark = pytest.mark.integration

HEX64 = re.compile(r"[0-9a-f]{64}")


def test_health_reports_model_version_and_database_readiness(client):
    """Rubric 2.1 requires a health check; delivery spec section 3.3 makes it the deploy
    gate, so it has to distinguish 'loaded and connected' from 'process is up'."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["model_version"] == "toxic-clf:v3"
    assert body["database"] == "ok"
    assert body["spool_depth"] == 0


def test_health_never_fingerprints_the_model(client, artifact_bundle):
    """H14. Delivery spec section 6.3 strips the digest here specifically so an attacker
    cannot confirm which artifact is deployed while crafting evasions."""
    response = client.get("/health")
    assert "sha256" not in response.text
    assert artifact_bundle["digest"] not in response.text
    assert not HEX64.search(response.text)


def test_health_exposes_the_rejection_counters(client):
    client.post("/predict", json={"text": "hello"})  # 401
    counters = client.get("/health").json()["rejected"]
    assert counters["unauthenticated"] == 1
    assert set(counters) == {"unauthenticated", "rate_limited", "oversize"}


def test_health_answers_200_even_while_degraded(client):
    """A 5xx here would take the instance out of the deploy gate and out of any future load
    balancer at the exact moment the operator needs to see why."""
    from tests.integration.test_predict_failure_paths import break_the_database

    break_the_database(client)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "degraded"
