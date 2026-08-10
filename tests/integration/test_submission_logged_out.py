"""DELIV-1..4. The online half, with no credentials of any kind.

`trust_env=False` on every client is the load-bearing detail: it stops httpx reading
`~/.netrc`, which on the operator's machine holds `api.wandb.ai` credentials. Without it,
this suite authenticates as the author and every private page looks public -- which is
exactly how four broken W&B URLs survived in the documentation until 2026-08-10.

One assertion from the plan's draft is implemented differently, and the reason is in
`test_the_registry_is_public_and_promoted_over_the_anonymous_api`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import httpx
import pytest
import yaml

pytestmark = pytest.mark.integration

MANIFEST = Path("docs/submission-manifest.yml")
GRAPHQL = "https://api.wandb.ai/graphql"


@pytest.fixture(scope="module")
def anonymous():
    with httpx.Client(follow_redirects=True, timeout=30, trust_env=False) as client:
        yield client


@pytest.fixture(scope="module")
def deliverables() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["deliverables"]


def _resolve(parameter: str) -> str | None:
    """The live address, from the environment or from SSM. Never from this repository.

    Reading an address out of Parameter Store is not authenticating to the thing being
    tested: the request that follows carries no credential at all. It is only how the test
    learns where to point, because committing the Elastic IP to a public repository is the
    thing `test_the_live_url_does_not_publish_a_public_address` forbids.
    """
    env = {
        "/toxic/endpoints/frontend": "LIVE_URL",
        "/toxic/endpoints/backend": "LIVE_BACKEND_URL",
    }.get(parameter)
    if env and os.environ.get(env):
        return os.environ[env]
    if shutil.which("aws") is None:
        return None
    result = subprocess.run(
        ["aws", "ssm", "get-parameter", "--name", parameter,
         "--query", "Parameter.Value", "--output", "text"],
        capture_output=True, text=True, check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value.startswith("http") else None


def _anonymous_graphql(query: str, variables: dict) -> dict:
    """POST with every credential stripped, including the netrc httpx would have read.

    HOME is redirected to an empty directory for the duration, which is what makes urllib
    skip netrc lookup entirely rather than trusting a flag.
    """
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        GRAPHQL, data=body, headers={"Content-Type": "application/json"}
    )
    with tempfile.TemporaryDirectory() as empty:
        previous = os.environ.get("HOME")
        os.environ["HOME"] = empty
        for name in ("WANDB_API_KEY", "WANDB_ENTITY", "WANDB_BASE_URL"):
            os.environ.pop(name, None)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        finally:
            if previous is not None:
                os.environ["HOME"] = previous


# ------------------------------------------------------------------ the repository


def test_the_repository_opens_without_a_login(anonymous, deliverables):
    response = anonymous.get(deliverables["repository"]["url"])
    assert response.status_code == 200
    assert "sign in" not in response.text[:2000].lower()


def test_the_security_policy_and_model_card_are_reachable(anonymous, deliverables):
    base = deliverables["repository"]["url"].rstrip("/").replace(
        "github.com", "raw.githubusercontent.com"
    )
    for document in ("SECURITY.md", "MODEL_CARD.md", "README.md"):
        assert anonymous.get(f"{base}/main/{document}").status_code == 200, document


def test_the_ci_evidence_is_public_not_merely_the_code(anonymous, deliverables):
    """H10. A public repository can still have a private Actions log, and the CI gate is a
    graded deliverable. The evidence documents are the durable record of it."""
    base = deliverables["repository"]["url"].rstrip("/").replace(
        "github.com", "raw.githubusercontent.com"
    )
    for document in ("docs/evidence/ci-gate.md", "docs/evidence/blocked-merge-cli.txt"):
        assert anonymous.get(f"{base}/main/{document}").status_code == 200, document


# ------------------------------------------------------------------ weights & biases


def test_the_wandb_report_opens_without_a_login(anonymous, deliverables):
    assert anonymous.get(deliverables["wandb"]["url"]).status_code == 200


def test_the_wandb_project_is_readable_without_a_login(deliverables):
    """A 200 from the SPA shell proves only that the CDN is up. This asks the API the page
    itself calls, with no credential, which is the question a grader's browser asks."""
    query = """query($e:String!,$p:String!){project(name:$p,entityName:$e){access
      runs(first:10){edges{node{name}}}}}"""
    out = _anonymous_graphql(query, {"e": "rockcyber", "p": "mlops-toxic-moderation"})
    project = (out.get("data") or {}).get("project")
    assert project, f"the project does not resolve anonymously: {out.get('errors')}"
    assert project["access"] == deliverables["wandb"]["project_access"]
    visible = len(project["runs"]["edges"])
    assert visible > 0, "no runs are readable without a login"


def test_the_registry_is_public_and_promoted_over_the_anonymous_api(deliverables):
    """IMPLEMENTED DIFFERENTLY FROM THE PLAN, deliberately.

    The plan asserted the promoted stage appears in the registry page's HTML. It cannot:
    wandb.ai is a single-page app and ships the same JavaScript shell for every path, so the
    string 'production' is never in the response body regardless of whether the alias
    exists. Asserting on it would have produced a test that fails while the deliverable is
    correct, which teaches everyone to ignore it.

    The alias is asserted where it actually lives -- the GraphQL API the page calls -- with
    every credential stripped. That the page *renders* for a human is verified in a
    logged-out browser and recorded in the manifest with a date; it is not automatable here.
    """
    query = """query($e:String!,$p:String!,$c:String!){
      project(name:$p, entityName:$e){ access
        artifactType(name:"model"){ artifactCollection(name:$c){
          artifacts(first:20){ edges{ node{ versionIndex aliases{ alias } } } } } } } }"""
    out = _anonymous_graphql(
        query, {"e": "rockcyber-org", "p": "wandb-registry-model", "c": "toxic-clf"}
    )
    project = (out.get("data") or {}).get("project")
    assert project, f"the registry does not resolve anonymously: {out.get('errors')}"
    edges = project["artifactType"]["artifactCollection"]["artifacts"]["edges"]
    aliases = {a["alias"] for edge in edges for a in edge["node"]["aliases"]}
    assert deliverables["wandb"]["promoted_stage"] in aliases, aliases


# ------------------------------------------------------------------ the live stack


def test_the_live_url_answers_without_a_login(anonymous, deliverables):
    url = _resolve(deliverables["live_url"]["url_parameter"])
    if url is None:
        pytest.skip("no LIVE_URL and no SSM access; run with LIVE_URL set")
    response = anonymous.get(url)
    assert response.status_code == 200


def test_the_live_backend_health_answers_without_a_key(anonymous, deliverables):
    """/health is deliberately the one endpoint with no API key: a health check a grader
    cannot run is not a health check."""
    base = _resolve(deliverables["live_url"]["health_url_parameter"])
    if base is None:
        pytest.skip("no LIVE_BACKEND_URL and no SSM access")
    response = anonymous.get(f"{base.rstrip('/')}/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_the_reviewer_console_is_not_reachable_from_the_internet(deliverables):
    """H12. The whole acceptance of cleartext HTTP rests on 8503 never being exposed, and
    the demo window opening 8000, 8501 and 8502 makes that worth re-checking from outside."""
    base = _resolve(deliverables["live_url"]["url_parameter"])
    if base is None:
        pytest.skip("no LIVE_URL and no SSM access")
    host = httpx.URL(base).host
    with httpx.Client(timeout=8, trust_env=False) as client:
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
            client.get(f"http://{host}:8503/_stcore/health")
