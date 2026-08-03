"""H26. The full path, against the deployed stack, over the network. Marked integration.

Delivery spec 3.3: no phase is complete until every route and integration it introduces is
proven working against a real dependency. Phase 3 proved this traversal on local compose.
Here the same path has to succeed across three EC2 instances, over the public internet for
the client hops and across two security groups for the instance-to-instance hop, through an
RDS that is not publicly accessible.

Skips loudly rather than passing when the stack is not configured. A traversal gate that
quietly reports success against nothing is the exact failure the fake-green guard in
conftest.py exists to catch, and `-m integration` in CI is asserted not to pass on skips.
"""

import os
import re
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

EVIDENCE = Path("docs/evidence/p5-deploy-traversal.md")


@pytest.fixture(scope="module")
def endpoints() -> dict[str, str]:
    missing = [
        k for k in ("BACKEND_URL", "FRONTEND_URL", "MONITORING_URL") if not os.environ.get(k)
    ]
    if missing:
        pytest.skip(f"deployed stack not configured: {missing}")
    return {k.lower(): os.environ[k] for k in ("BACKEND_URL", "FRONTEND_URL", "MONITORING_URL")}


@pytest.fixture(scope="module")
def api_key() -> str:
    key = os.environ.get("DEMO_API_KEY")
    if not key:
        pytest.skip("DEMO_API_KEY not set")
    return key


def test_backend_health_reports_the_database_reachable(endpoints):
    body = httpx.get(f"{endpoints['backend_url']}/health", timeout=15).json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_health_never_exposes_the_artifact_digest(endpoints):
    text = httpx.get(f"{endpoints['backend_url']}/health", timeout=15).text
    assert not re.search(r"[0-9a-f]{64}", text)


def test_predict_over_the_network_returns_a_contract_valid_response(endpoints, api_key):
    marker = f"integration probe {uuid.uuid4()}"
    response = httpx.post(
        f"{endpoints['backend_url']}/predict",
        headers={"X-API-Key": api_key},
        json={"text": f"you are an absolute clueless idiot. {marker}"},
        timeout=30,
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body["labels"]) == {
        "toxic", "severe_toxic", "obscene", "threat", "insult", "identity_hate"
    }
    assert body["decision"] in {"allow", "review", "block"}
    assert 0.0 <= body["max_prob"] <= 1.0
    assert body["latency_ms"] >= 0
    assert not re.search(r"[0-9a-f]{64}", response.text), "the response leaks the digest"


def test_a_clean_comment_is_allowed_over_the_network(endpoints, api_key):
    """The decision that was unreachable code until 224da4149c4a.

    `allow` is the branch the review band's floor made unreachable: REVIEW_MARGIN is 0.10 and
    three labels are tuned to 0.05, so the floor was -0.05 and every input matched the review
    branch. Asserting a specific decision rather than merely a valid one is what turns this
    from a contract check into a behaviour check -- the contract test above passed throughout
    the whole period the defect was live, because "review" is a perfectly valid decision.
    """
    response = httpx.post(
        f"{endpoints['backend_url']}/predict",
        headers={"X-API-Key": api_key},
        json={"text": "Thank you for the helpful edit, I appreciate it."},
        timeout=30,
    )
    assert response.status_code == 200
    body = response.json()
    assert not any(label["flag"] for label in body["labels"].values())
    assert body["decision"] == "allow", (
        f"a clean comment with no flag set decided {body['decision']}; if this is 'review', "
        f"the review band floor has gone non-positive again"
    )


def test_predict_is_rejected_without_the_demo_key(endpoints):
    response = httpx.post(
        f"{endpoints['backend_url']}/predict", json={"text": "hello"}, timeout=15
    )
    assert response.status_code == 401


def test_the_frontend_instance_can_reach_the_backend_instance(endpoints):
    """Instance-to-instance HTTP through two security groups."""
    page = httpx.get(endpoints["frontend_url"], timeout=30, follow_redirects=True)
    assert page.status_code == 200
    health = httpx.get(f"{endpoints['frontend_url']}/_stcore/health", timeout=15)
    assert health.text.strip() == "ok"


def test_the_monitoring_instance_is_a_different_host_from_the_frontend(endpoints):
    """Rubric 3.2 requires the dashboard on a different EC2 server."""
    frontend_host = httpx.URL(endpoints["frontend_url"]).host
    monitoring_host = httpx.URL(endpoints["monitoring_url"]).host
    backend_host = httpx.URL(endpoints["backend_url"]).host
    assert len({frontend_host, monitoring_host, backend_host}) == 3


def test_the_reviewer_ui_is_not_reachable_from_the_internet(endpoints):
    """H12. Opening 8503 would hand the graded feedback metric to any visitor."""
    host = httpx.URL(endpoints["frontend_url"]).host
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        httpx.get(f"http://{host}:8503/_stcore/health", timeout=8)


def test_the_prediction_reached_the_database_and_the_dashboard(endpoints, api_key):
    """Rubric 2.2 plus 3.2: the row must exist and the dashboard must be able to see it.

    Counted through the monitoring read-only role, so this asserts both halves. Counting as
    the master would prove the row landed and assume the dashboard can see it.
    """
    from scripts.traversal_check import count_predictions

    before = count_predictions()
    httpx.post(
        f"{endpoints['backend_url']}/predict",
        headers={"X-API-Key": api_key},
        json={"text": f"traversal row {uuid.uuid4()}"},
        timeout=30,
    ).raise_for_status()
    assert count_predictions() == before + 1


def test_the_dashboards_credentials_cannot_write(endpoints):
    """H16, observed rather than inferred from the grant statements."""
    from scripts.traversal_check import readonly_role_cannot_write

    error = readonly_role_cannot_write()
    assert error, "the monitoring role created a table; it is not read-only"
    assert "permission denied" in error.lower(), f"refused, but not by permissions: {error}"


def test_the_traversal_evidence_accounts_for_every_first_time_integration():
    """H26. Each integration named, with where it was first exercised.

    The plan for this task asserted the evidence cites `a2-smoke-deploy`, a day-9 rehearsal
    intended to exercise all of this five days before it mattered. That smoke deploy never
    happened -- the schedule compressed and the first real deploy was 2026-08-01, which is
    recorded in docs/cut-log.md as the day-11 checkpoint still PENDING. Asserting a citation
    to a document that does not exist would make this test enforce a fiction, so it asserts
    the property the citation was a proxy for instead: every first-time-ever integration is
    named, and the evidence says when each was first exercised.
    """
    body = EVIDENCE.read_text(encoding="utf-8")
    for integration in ("ECR auth", "arm64", "digest", "instance-to-instance", "RDS",
                        "Elastic IP", "SSM"):
        assert integration.lower() in body.lower(), f"unaccounted integration: {integration}"
    assert "first exercised" in body.lower(), (
        "the evidence names the integrations but not when each first ran"
    )
