import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from backend.fingerprint import SESSION_FP_HEADER
from frontend.api_client import MAX_INPUT_CHARS, BackendClient, new_session_fp
from model.labels import LABELS

REPO = Path(__file__).resolve().parents[2]

XSS = "<script>alert(1)</script>"


def _client(handler) -> BackendClient:
    transport = httpx.MockTransport(handler)
    return BackendClient(
        base_url="http://backend:8000",
        api_key="demo-key",
        session_fp="a1b2c3d4e5f60718",
        transport=transport,
    )


def test_new_session_fp_is_sixteen_hex_chars_and_unique():
    first, second = new_session_fp(), new_session_fp()
    assert len(first) == 16 and all(c in "0123456789abcdef" for c in first)
    assert first != second


def test_the_session_fingerprint_is_the_shape_the_backend_will_accept():
    """`caller_identity` only honours a 16-lowercase-hex session value; anything else falls
    back to the peer bucket, which would silently put every UI visitor in one bucket."""
    from backend.fingerprint import caller_identity

    value = new_session_fp()
    assert caller_identity("10.0.0.1", value, api_key_ok=True) == f"session:{value}"


def test_predict_sends_the_api_key_and_the_session_fingerprint():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(
            200,
            json={
                "request_id": "r1",
                "model_version": "toxic-clf:v3",
                "labels": {label: {"prob": 0.1, "flag": False} for label in LABELS},
                "decision": "allow",
                "max_prob": 0.1,
                "latency_ms": 9,
            },
        )

    result = _client(handler).predict("hello")
    assert result["request_id"] == "r1"
    assert seen["x-api-key"] == "demo-key"
    assert seen[SESSION_FP_HEADER.lower()] == "a1b2c3d4e5f60718"


def test_predict_refuses_oversized_input_before_the_network():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    with pytest.raises(ValueError, match="MAX_INPUT_CHARS"):
        _client(handler).predict("x" * (MAX_INPUT_CHARS + 1))
    assert calls == []


def test_the_input_cap_is_the_projects_single_untunable_one():
    """Delivery spec 6.3: an abuse control a deploy-time variable can widen is not a
    control. The UI must not introduce a second, environment-tunable definition."""
    from model.normalize import MAX_INPUT_CHARS as CANONICAL

    assert MAX_INPUT_CHARS == CANONICAL
    # Every environment lookup has to spell the name as a string literal, so the absence of
    # the quoted form across the whole surface is the property, not a spot check.
    for path in sorted(Path("frontend").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert '"MAX_INPUT_CHARS"' not in source, f"{path} reads the cap from the environment"
        assert "'MAX_INPUT_CHARS'" not in source, f"{path} reads the cap from the environment"


def test_user_feedback_posts_the_closed_verdict_vocabulary():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"request_id": "r1", "verdict": "agree"})

    _client(handler).user_feedback("r1", "agree")
    assert captured["url"].endswith("/feedback/user")
    assert '"verdict": "agree"' in captured["body"] or '"verdict":"agree"' in captured["body"]


def test_user_feedback_rejects_an_invented_verdict_client_side():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("must not reach the network")

    with pytest.raises(ValueError, match="verdict"):
        _client(handler).user_feedback("r1", "maybe")


def test_the_verdict_vocabulary_has_one_definition():
    """Two spellings of a closed vocabulary is a client that can send something the server
    will refuse, or worse, accept."""
    from backend.feedback import USER_VERDICTS
    from frontend.api_client import ALLOWED_VERDICTS

    assert ALLOWED_VERDICTS is USER_VERDICTS


def test_rate_limited_feedback_surfaces_as_a_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "source_quota"})

    from frontend.api_client import RateLimited

    with pytest.raises(RateLimited):
        _client(handler).user_feedback("r1", "agree")


def test_an_error_message_never_carries_the_backend_response_body():
    """The UI shows exceptions through markdown-capable widgets (st.error), and a backend
    422 echoes the offending input back. Interpolating the body into that message is a
    second rendering path for attacker text that bypasses frontend.render entirely."""
    from frontend.api_client import BackendError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": XSS})

    with pytest.raises(BackendError) as caught:
        _client(handler).predict("hello")
    assert XSS not in str(caught.value)
    assert "400" in str(caught.value)
    assert XSS in caught.value.detail, "the body is kept for the inert path, not discarded"


def test_the_detail_is_bounded_so_a_huge_body_cannot_be_pasted_into_the_page():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="x" * 100000)

    from frontend.api_client import BackendError

    with pytest.raises(BackendError) as caught:
        _client(handler).predict("hello")
    assert len(caught.value.detail) <= 500


def test_submit_never_sends_a_reviewer_id():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"request_id": "r1", "exact_match": True})

    _client(handler).submit("token", "r1", dict.fromkeys(LABELS, 0))
    assert "reviewer_id" not in captured["body"]


def test_the_reviewer_token_travels_in_the_authorization_header_only():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"items": []})

    _client(handler).pending("s3cret-token")
    assert captured["headers"]["authorization"] == "Bearer s3cret-token"
    assert "s3cret-token" not in captured["url"]


def test_login_does_not_put_the_shared_secret_in_the_url():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"token": "t"})

    _client(handler).login("hunter2")
    assert "hunter2" not in captured["url"]
    assert "hunter2" in captured["body"]


def test_the_client_repr_does_not_leak_the_api_key():
    """A Streamlit traceback renders straight into the page."""
    assert "demo-key" not in repr(_client(lambda request: httpx.Response(200, json={})))


def test_the_ui_client_imports_no_database_driver():
    """H16 stated as an import-closure fact rather than a single-file grep. The UI reaching
    Postgres only through the backend API is only true if it cannot open a connection at
    all -- and a transitive import through backend.feedback would put the driver right
    back in the image."""
    probe = (
        "import frontend.api_client, sys;"
        "print(','.join(sorted(m for m in sys.modules "
        "if m.split('.')[0] in {'sqlalchemy','psycopg','psycopg2','asyncpg','pg8000'})))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", result.stdout
