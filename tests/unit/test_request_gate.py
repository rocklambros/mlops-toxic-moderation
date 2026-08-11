"""The abuse gate covers the whole app, and its rate limit keys on the caller, not the key.

Two defects this file exists to keep dead.

**The gate used to serve one route.** `_gate` opened with
`if request.url.path != "/predict": return await call_next(request)`, so every route the
reviewer router mounts -- `/review/login`, `/review/pending`, `/review/submit`,
`/feedback/user` -- reached FastAPI with no demo key, no rate limit and no body cap. Port
8000 answers `0.0.0.0/0` during the demo window, so that was the review queue and the graded
feedback metric on the open internet, defended only by whatever each handler checked for
itself. `/feedback/user` checked nothing. `SECURITY.md` and `docs/tls-decision.md` both
asserted the opposite, and both were graded deliverables.

**The rate limit used to key on the API key.** `client_fingerprint(presented)` is a digest of
the one demo key, and `frontend/ui.py` proxies every anonymous visitor through that key, so
the whole internet shared a single 30-request-per-minute bucket: one visitor leaning on the
button returned 429 to the next visitor, and to a grader. `backend/fingerprint.py` had
already built the per-submitter key for the review-queue quotas; it was simply never wired
into the limiter, and the gate derived it one line too late to be usable there.

The fix is a per-caller bucket plus a peer ceiling, and the second half is not decoration.
`caller_identity` honours any 16-hex `X-Session-Fp` from a caller holding the key, so a key
holder can mint a fresh bucket per request. The TCP peer is the part of the identity a caller
cannot choose, which is why a looser bucket is keyed on it as well.

The app is built here with the lifespan's I/O stubbed -- SQLite in place of RDS, no artifact
load -- because every assertion below is about what the middleware does before a request
reaches a handler. Requests that must pass the gate carry `{"text": ""}`, which
`PredictRequest` refuses with a 422: a status only reachable once the gate has let the
request through, and one that needs no model.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import backend.app as app_module
from backend.auth import API_KEY_HEADER, client_fingerprint
from backend.config import load_settings
from backend.fingerprint import SESSION_FP_HEADER, caller_identity, submitter_fp
from backend.ratelimit import RateLimiter
from backend.review_api import router as review_router
from model.labels import LABELS

DEMO_KEY = "unit-test-demo-key"
AUTH = {API_KEY_HEADER: DEMO_KEY}
GATE_REFUSAL = f"a valid {API_KEY_HEADER} header is required"

# 422 is what a request that got past the gate looks like in this file: the gate runs before
# FastAPI parses the body, so a schema refusal proves the middleware allowed the request.
PASSED = 422

# Every route the reviewer router mounts, read from the router rather than restated, so a
# fifth route added later is covered by these tests on the day it is added.
REVIEWER_ROUTES = sorted(
    (sorted(method for method in route.methods if method != "HEAD")[0], route.path)
    for route in review_router.routes
)
KEYED_REVIEWER_ROUTES = [
    (method, path) for method, path in REVIEWER_ROUTES if path not in app_module.UNKEYED_PATHS
]

BODIES = {
    "/review/login": {"secret": "wrong"},
    "/review/submit": {"request_id": "r", "labels": dict.fromkeys(LABELS, 0)},
    "/feedback/user": {"request_id": "r", "verdict": "agree"},
}


class _Model:
    """Only /health reads the model, and only for its public version string."""

    public_version = "toxic-clf:v1"
    model_version = "toxic-clf:v1:0000"


class _RecordingLimiter:
    """Records what the gate hands it. The bug was the *value* of the key, so the assertions
    that matter are about the key itself, not about an allow/deny outcome."""

    def __init__(self, verdict: bool = True) -> None:
        self.keys: list[str] = []
        self._verdict = verdict

    def allow(self, key: str) -> bool:
        self.keys.append(key)
        return self._verdict


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Deterministic regardless of the operator's shell: an inherited REVIEWER_SHARED_SECRET
    # would change which 401 `/review/login` answers with.
    monkeypatch.delenv("REVIEWER_SHARED_SECRET", raising=False)
    monkeypatch.delenv("REVIEWER_ID", raising=False)
    # StaticPool with check_same_thread off: TestClient answers on a worker thread and tears
    # down on the main one, and the default SQLite pool raises on the close. Nothing here is a
    # statement about SQLite -- /health is the only handler these tests reach, and all it
    # needs is a connection that answers `select 1`.
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
    with TestClient(app_module.create_app(settings)) as live:
        yield live


def _predict(client, session_fp: str | None = None) -> int:
    headers = dict(AUTH)
    if session_fp is not None:
        headers[SESSION_FP_HEADER] = session_fp
    return client.post("/predict", json={"text": ""}, headers=headers).status_code


# --- Finding 1: the reviewer capability was on the open listener, ungated -------------------


@pytest.mark.parametrize(("method", "path"), KEYED_REVIEWER_ROUTES)
def test_every_reviewer_route_refuses_a_caller_holding_no_demo_key(client, method, path):
    """Verified live before the fix: these answered 401 from their own auth, not
    connection-refused, which is what proved they were being served to the internet.
    `/feedback/user` answered 200. The refusal has to come from the gate, so the assertion is
    on the gate's own detail string -- a handler-level 401 would pass a bare status check
    while the request had already reached the router."""
    response = client.request(method, path, json=BODIES.get(path))
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == GATE_REFUSAL


def test_login_is_the_only_reviewer_route_the_gate_serves_without_a_key(client):
    """`frontend.api_client.BackendClient.login` posts the secret with no headers at all, so
    a key requirement there locks the reviewer console out of its own console. Every other
    reviewer route is called through `_headers()`, which carries the key. Adding a second
    path to the exemption set is a decision, not an oversight, so it fails here."""
    reviewer_paths = {path for _, path in REVIEWER_ROUTES}
    assert app_module.UNKEYED_PATHS & reviewer_paths == {"/review/login"}
    refused = client.post("/review/login", json={"secret": "wrong"})
    assert refused.status_code == 401
    assert refused.json()["detail"] == "invalid reviewer secret", "the gate swallowed it"


def test_the_headers_the_streamlit_client_sends_still_reach_the_router(client):
    """The reviewer console signs in, then calls `/review/pending` with
    `{X-API-Key, X-Session-Fp, Authorization}`. A gate that refuses that combination has
    closed the hole by breaking the deliverable. `reviewer session required` is the router's
    own answer to a missing bearer token, so seeing it proves the gate passed the request
    on."""
    response = client.get("/review/pending", headers={**AUTH, SESSION_FP_HEADER: "0" * 16})
    assert response.status_code == 401
    assert response.json()["detail"] == "reviewer session required"


def test_the_user_ui_can_still_post_feedback_through_the_gate(client):
    """`frontend/ui.py` posts an agree/disagree verdict through `_headers()`. Rubric 3.2
    grades that path, so the control must sit in front of it, not across it."""
    response = client.post(
        "/feedback/user",
        headers={**AUTH, SESSION_FP_HEADER: "0" * 16},
        json={"request_id": "r", "verdict": "maybe"},
    )
    assert response.status_code == PASSED, "the gate refused the legitimate user UI"


def test_a_reviewer_body_is_capped_before_the_router_parses_it(client):
    """`LoginRequest.secret` caps a field at 512 characters. It does not cap the body, and a
    field cap is enforced by pydantic, which runs after the whole body has been read."""
    response = client.post(
        "/feedback/user",
        content=b'{"request_id": "' + b"a" * 20000 + b'", "verdict": "agree"}',
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_a_reviewer_post_that_declares_no_length_is_refused(client):
    """A chunked body has no Content-Length, so the size cap above cannot be applied to it.
    Refusing is the only way the cap means anything."""

    def chunks():
        yield b'{"secret": "wrong"}'

    response = client.post(
        "/review/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 411


def test_a_get_is_not_asked_to_declare_a_body_length(client):
    """The length rule applies to methods that carry a body. Applying it to every method
    refuses `GET /health` and `GET /review/pending`, which is the gate breaking the deploy
    gate and the reviewer console to enforce a rule about bodies neither one sends."""
    assert client.get("/health").status_code == 200
    assert client.get("/review/pending", headers=AUTH).status_code == 401


def test_health_stays_reachable_with_no_key_at_all(client):
    """The grader, `infra/aws/verify_deploy.sh` and the container HEALTHCHECK all call it
    unauthenticated, and it returns counters rather than comment text."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["model_version"] == _Model.public_version


def test_an_undeclared_path_is_refused_rather_than_probed(client):
    """Deny by default. The old gate named the one path it covered, so any route added later
    was outside the controls by default; this one names the exemptions instead, which is what
    makes the reviewer-route tests above a statement about routes that do not exist yet."""
    assert client.get("/admin").status_code == 401
    assert client.post("/predict/v2", json={"text": ""}).status_code == 401


def test_the_schema_link_the_readme_publishes_still_answers_a_grader(client):
    """README.md lists `:8000/docs` in the deliverables table. Deny-by-default would refuse
    it, and the confidentiality that buys is nothing: Swagger renders the route list out of
    `backend/review_api.py`, which is readable in a public repository beside a README that
    publishes the address. Refusing a graded link to hide a public fact is a bad trade."""
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_a_refused_reviewer_request_is_counted_like_a_refused_prediction(client):
    """`/health` publishes these counters and the dashboard reads them. A refusal the gate
    does not count is an attack that leaves no trace anywhere."""
    client.get("/review/pending")
    assert client.app.state.rejected["unauthenticated"] == 1


# --- Finding 2: the rate limit keyed on the shared API key ----------------------------------


def test_the_rate_limit_is_not_keyed_on_the_shared_demo_key(client):
    """`client_fingerprint` is a digest of the one key `frontend/ui.py` holds, so keying the
    limiter on it gives every anonymous visitor in the world one shared bucket."""
    recorder = _RecordingLimiter()
    client.app.state.limiter = recorder
    _predict(client, session_fp="0" * 16)
    _predict(client, session_fp="f" * 16)
    assert client_fingerprint(DEMO_KEY) not in recorder.keys
    assert len(set(recorder.keys)) == 2, "two visitors behind one key shared one bucket"


def test_one_visitor_leaning_on_the_button_cannot_rate_limit_the_next_one(client):
    """The reported symptom, as a test. Before the fix all three requests below drew from one
    bucket and the third caller -- a grader -- got a 429 they did nothing to earn."""
    client.app.state.limiter = RateLimiter(per_minute=60, burst=1)
    assert _predict(client, session_fp="a" * 16) == PASSED
    assert _predict(client, session_fp="a" * 16) == 429, "a visitor must exhaust their own"
    assert _predict(client, session_fp="b" * 16) == PASSED


def test_rotating_the_session_header_cannot_escape_the_peer_ceiling(client):
    """The reason a per-caller bucket alone is not enough. `caller_identity` accepts any
    16-hex `X-Session-Fp` from a caller that presented the key, so a key holder can mint a
    fresh bucket on every request and the per-caller limit never binds. The peer is the one
    part of the identity a caller cannot choose."""
    client.app.state.peer_limiter = RateLimiter(per_minute=60, burst=2)
    codes = [_predict(client, session_fp=f"{index:016x}") for index in range(4)]
    assert codes == [PASSED, PASSED, 429, 429]


def test_the_peer_ceiling_is_derived_from_the_configured_limit(client):
    """Not a second environment variable. `make seed-demo` raises RATE_LIMIT_PER_MINUTE for
    the replay window, and a fixed ceiling would refuse the seeder at whatever number was
    hard-coded here; a deploy also cannot widen the ceiling without widening the per-visitor
    limit it is a multiple of."""
    ceiling = client.app.state.peer_limiter
    assert ceiling is not client.app.state.limiter
    assert ceiling.burst == 10 * app_module.PEER_CEILING_MULTIPLIER
    assert ceiling.rate == pytest.approx(30 * app_module.PEER_CEILING_MULTIPLIER / 60)


def test_a_rate_limited_caller_gets_retry_after_and_is_counted(client):
    client.app.state.limiter = RateLimiter(per_minute=60, burst=1)
    assert _predict(client, session_fp="c" * 16) == PASSED
    limited = client.post(
        "/predict", json={"text": ""}, headers={**AUTH, SESSION_FP_HEADER: "c" * 16}
    )
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert client.app.state.rejected["rate_limited"] == 1


def test_the_unkeyed_login_route_is_metered_by_the_gate_as_well(client):
    """It cannot require the key, so metering is the whole of what the gate can do for it.
    `backend/review_api.py` limits sign-in attempts per peer on top of this; the two are
    independent, and this asserts the gate's own."""
    client.app.state.limiter = RateLimiter(per_minute=60, burst=1)
    assert client.post("/review/login", json={"secret": "wrong"}).status_code == 401
    limited = client.post("/review/login", json={"secret": "wrong"})
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"


def test_health_is_not_spent_out_of_the_callers_bucket(client):
    """`verify_deploy.sh` retries /health up to 18 times while an instance boots, and the
    HEALTHCHECK probes it every 30 seconds forever. A liveness path that answers 429 is a
    deploy gate that fails for a reason that has nothing to do with the deploy."""
    recorder = _RecordingLimiter()
    client.app.state.limiter = recorder
    assert client.get("/health").status_code == 200
    assert recorder.keys == []


def test_health_is_still_held_under_the_peer_ceiling(client):
    """Exempt from the per-caller bucket is not the same as free. /health opens a database
    session on every call, so an unmetered one is a way to spend the connection pool."""
    client.app.state.peer_limiter = RateLimiter(per_minute=60, burst=1)
    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 429


def test_the_limiter_key_is_the_value_the_prediction_row_stores(client):
    """One derivation, used twice. `predictions.submitter_fp` is what
    `backend/queue_guard.py` counts a source's review enqueues against, and the limiter now
    meters the same value -- so the quota and the rate limit describe the same caller. The
    old code derived it after the limit check, which is how the two came to disagree."""
    recorder = _RecordingLimiter()
    client.app.state.limiter = recorder
    assert _predict(client, session_fp="d" * 16) == PASSED
    expected = submitter_fp(
        caller_identity("testclient", "d" * 16, api_key_ok=True),
        dt.datetime.now(dt.UTC).date(),
        client.app.state.submitter_fp_key,
    )
    assert recorder.keys == [expected]


# --- Integration follow-ups: what the adversarial verifiers found ---------------------------


def test_the_reviewer_console_is_not_metered_out_of_the_inference_budget(client, monkeypatch):
    """A verifier found the gate signed the reviewer out of their own console.

    Streamlit re-runs the whole script on every widget interaction, and
    `frontend/reviewer.py` fetches the queue at the top of that script, so six label
    checkboxes on one item is seven `GET /review/pending` calls. Against a burst of 10 that
    is two items before 429, and `reviewer.py` treated any error as an auth failure and
    dropped the token, so the reviewer had to re-enter the shared secret.

    The per-caller bucket meters INFERENCE. This route runs no model, and without a valid
    reviewer bearer token it answers 401 from `review_api` regardless, so leaving it out of
    that bucket widens nothing. It stays under the peer ceiling.
    """
    recorder = _RecordingLimiter()
    monkeypatch.setattr(client.app.state, "limiter", recorder)
    for _ in range(40):
        client.get("/review/pending", headers=AUTH)
    assert recorder.keys == [], (
        "GET /review/pending is spending the inference budget; a reviewer clicking through "
        "the queue will be rate limited out of their own console"
    )


def test_the_reviewer_write_paths_are_still_metered(client, monkeypatch):
    """The exemption is a read. /review/submit and /feedback/user change the graded live
    accuracy metric, so they stay in the bucket."""
    recorder = _RecordingLimiter()
    monkeypatch.setattr(client.app.state, "limiter", recorder)
    # These two get past the gate and reach their handlers, which query tables this app's
    # stubbed SQLite does not have. That is irrelevant here and deliberately not asserted on:
    # the question is whether the gate consulted the bucket on the way in, which it did
    # before the handler ever ran.
    for path in ("/review/submit", "/feedback/user"):
        try:
            client.post(path, json=BODIES[path], headers=AUTH)
        except Exception:  # noqa: BLE001 - the handler's storage error is not under test
            pass
    assert len(recorder.keys) == 2, "a reviewer write escaped the per-caller bucket"


def test_the_exempt_read_is_still_held_by_the_peer_ceiling(client, monkeypatch):
    """Exempt from the per-caller bucket is not exempt from metering. Otherwise the
    exemption is an unmetered authenticated route, which is most of the finding again."""
    recorder = _RecordingLimiter()
    monkeypatch.setattr(client.app.state, "peer_limiter", recorder)
    client.get("/review/pending", headers=AUTH)
    assert recorder.keys, "GET /review/pending is not held by the peer ceiling either"


def test_a_request_declaring_two_body_framings_is_refused(client):
    """A verifier defeated the body cap with a request-smuggling desync: send
    `Content-Length: 4` and `Transfer-Encoding: chunked` together, and the gate reads the
    small Content-Length and admits it while h11 frames on chunked and streams a body of any
    size past the cap.

    RFC 9112 section 6.1 says a recipient that sees both MUST NOT process the message as
    anything other than an error. Refusing it is cheaper and safer than picking a winner and
    hoping the server picked the same one.
    """
    response = client.post(
        "/predict",
        content=b"4\r\ntest\r\n0\r\n\r\n",
        headers={**AUTH, "Content-Length": "4", "Transfer-Encoding": "chunked"},
    )
    assert response.status_code == 400, response.text
    assert "conflicting" in response.json()["detail"].lower()
