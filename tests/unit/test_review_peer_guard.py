"""The reviewer routes are mounted on the same app as /predict, which demo_cidrs opens to
0.0.0.0/0. A security group rule is per-port; an application is per-path, so the port cannot
tell the moderation queue from the prediction endpoint.

Every legitimate caller is inside the VPC -- roll.sh points the console at the backend's
private address -- so a public peer on /review/* is by definition not the console.

404 rather than 403: the response should not confirm the route exists.

This does NOT remove the shared secret and does not pretend to. The public UI container
shares the frontend instance's security group, so it reaches these routes from a private
address; the secret is still the only control on that path.

The four "public" addresses below are not this project's own Elastic IPs -- `scripts/redact.py`
treats any globally routable address that is not one of the three published endpoints as
something to mask, and `docs/superpowers/plans/2026-08-11-review-exposure-and-graded-panels.md`
carries the masked form `<elastic-ip>` in this parametrize list as a result. What the test
needs is only that `ipaddress.ip_address(host).is_global` is True, so well-known public
resolvers stand in: none of them is infrastructure this project owns.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import backend.app as app_module
from backend.app import REVIEWER_PATH_PREFIX, peer_is_public
from backend.auth import API_KEY_HEADER
from backend.config import load_settings
from model.labels import LABELS


@pytest.mark.parametrize(
    "host",
    ["8.8.8.8", "1.1.1.1", "9.9.9.9", "2001:4860:4860::8888"],
)
def test_a_routable_address_is_public(host):
    assert peer_is_public(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "10.42.0.173",       # the backend's private address, from /toxic/endpoints/backend-internal
        "10.0.1.55",
        "172.17.0.4",        # docker bridge
        "192.168.1.10",
        "127.0.0.1",
        "::1",
    ],
)
def test_a_private_or_loopback_address_is_not_public(host):
    assert peer_is_public(host) is False


def test_a_peer_that_is_not_an_address_is_not_public():
    """Starlette's TestClient reports 'testclient'. A non-address peer means there is no TCP
    peer at all, which in this deployment is only ever an in-process caller. Treating it as
    public would fail every existing reviewer test for a reason unrelated to the control."""
    assert peer_is_public("testclient") is False
    assert peer_is_public(None) is False
    assert peer_is_public("") is False


def test_the_prefix_covers_login_as_well_as_the_read_and_write_routes():
    for path in ("/review/login", "/review/pending", "/review/submit"):
        assert path.startswith(REVIEWER_PATH_PREFIX)


def test_the_prefix_does_not_cover_the_graded_anonymous_feedback_route():
    """rubric 3.2 grades /feedback/user, and the user UI calls it over the internet."""
    assert not "/feedback/user".startswith(REVIEWER_PATH_PREFIX)
    assert not "/predict".startswith(REVIEWER_PATH_PREFIX)


# --- Middleware-level coverage ----------------------------------------------------------------
#
# Everything above calls `peer_is_public` and reads `REVIEWER_PATH_PREFIX` as bare values.
# None of it sends a request through `_gate`, so a wrong operator, a swapped branch order, or a
# bad rebase on the clause in `backend/app.py` that calls them would not fail here. The only
# tests that reach that line are the two live assertions in
# `tests/integration/test_deployed_traversal.py`, and those SKIP without BACKEND_URL -- which is
# every unit-only run, local or in CI. The tests below close that gap by driving real requests
# through `_gate` with a TCP peer Starlette's `TestClient` does not use by default.

DEMO_KEY = "unit-test-demo-key"
AUTH = {API_KEY_HEADER: DEMO_KEY}


class _Model:
    """Only /health reads the model, and only for its public version string."""

    public_version = "toxic-clf:v1"
    model_version = "toxic-clf:v1:0000"


@pytest.fixture()
def gated_app(tmp_path, monkeypatch):
    """The app built exactly as `tests/unit/test_request_gate.py`'s `client` fixture builds it
    -- lifespan I/O stubbed to SQLite, no artifact load -- but handed back unwrapped rather than
    bound to a `TestClient`. These tests need a different TCP peer per assertion, and a
    `TestClient`'s peer is fixed at construction, so the wrapping happens per test instead of
    once here.
    """
    monkeypatch.delenv("REVIEWER_SHARED_SECRET", raising=False)
    monkeypatch.delenv("REVIEWER_ID", raising=False)
    monkeypatch.setattr(
        app_module,
        "make_engine",
        lambda settings: create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        ),
    )
    monkeypatch.setattr(app_module, "init_db", lambda engine: None)
    monkeypatch.setattr(app_module, "apply_phase3_schema", lambda engine: None)
    monkeypatch.setattr(app_module, "load_from_settings", lambda settings: _Model())
    monkeypatch.setattr(app_module, "load_thresholds", lambda path: dict.fromkeys(LABELS, 0.5))
    settings = load_settings(
        {
            "DATABASE_URL": "sqlite://",
            "DEMO_API_KEY": DEMO_KEY,
            "MODEL_ARTIFACT_PATH": str(tmp_path / "toxic-clf.skops"),
            "MODEL_CARD_PATH": str(tmp_path / "MODEL_CARD.md"),
            "MODEL_DIGEST": "0" * 64,
            "MODEL_REGISTRY_VERSION": "1",
            "THRESHOLDS_PATH": str(tmp_path / "thresholds.json"),
            "SUBMITTER_FP_KEY": "unit-test-submitter-fp-key",
            "SPOOL_PATH": str(tmp_path / "spool.jsonl"),
            "RATE_LIMIT_PER_MINUTE": "30",
            "RATE_LIMIT_BURST": "10",
        }
    )
    return app_module.create_app(settings)


def _client_from(host: str, app) -> TestClient:
    """A `TestClient` whose reported TCP peer is `host`. `client=(host, port)` is the one
    constructor argument that changes `request.client.host` inside `_gate`; the default,
    `("testclient", 50000)`, is what every other reviewer test in this repository relies on
    staying untouched.
    """
    return TestClient(app, client=(host, 12345))


def test_a_public_peer_on_review_pending_is_refused_before_the_key_check(gated_app):
    """The one line this file exists to protect: `_gate` refusing a public peer on a
    `/review/*` path with a 404, ahead of the API-key check. No `X-API-Key` header is sent, so
    a 404 here can only come from the peer guard -- the key check would answer 401 if it ran
    first."""
    with _client_from("8.8.8.8", gated_app) as client:
        response = client.get("/review/pending")
    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"


def test_a_private_peer_on_review_pending_reaches_the_reviewer_session_check(gated_app):
    """The mirror of the test above: a private peer must not be caught by the guard at all.
    Presenting the demo key and no bearer token drives the request past both the peer guard
    and the gate's key check, to `review_api`'s own 401 for a missing reviewer session --
    proof the request reached the router rather than being refused a second way by the
    gate."""
    with _client_from("10.0.1.55", gated_app) as client:
        response = client.get("/review/pending", headers=AUTH)
    assert response.status_code == 401
    assert response.json()["detail"] == "reviewer session required"


def test_a_public_peer_on_feedback_user_is_refused_by_the_key_check_not_the_peer_guard(
    gated_app,
):
    """`/feedback/user` does not start with `REVIEWER_PATH_PREFIX`, so a public peer there must
    still get the ordinary 401 the demo key enforces, not the 404 that would hide it. The guard
    discriminates by path rather than hiding every route -- a blanket-by-peer refusal here
    would break rubric 3.2's anonymous, internet-facing feedback route."""
    with _client_from("8.8.8.8", gated_app) as client:
        response = client.post("/feedback/user", json={"request_id": "r", "verdict": "agree"})
    assert response.status_code == 401
    assert response.json()["detail"] == f"a valid {API_KEY_HEADER} header is required"


def test_a_public_peer_on_predict_is_unaffected_by_the_reviewer_guard(gated_app):
    """`/predict` is the route the whole gate exists to protect, and its path is nowhere near
    `REVIEWER_PATH_PREFIX`. A public peer there must reach exactly as far as it always did --
    through the key check to pydantic's own validation, the same 422
    `tests/unit/test_request_gate.py` calls `PASSED`."""
    with _client_from("8.8.8.8", gated_app) as client:
        response = client.post("/predict", headers=AUTH, json={"text": ""})
    assert response.status_code == 422
