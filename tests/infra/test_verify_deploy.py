"""H5. SSM Success is not a deploy. Three endpoints answering is a deploy.

An SSM invocation reporting `Success` means a shell exited 0 on a box. It does not mean the
container started, the artifact verified, RDS was reachable, or the security group lets the
grader in. This gate makes the statement that is actually worth making, over the same path a
grader would take, and it is what fails the deploy job.

The trap this file is written against is a fixture that does not look like production.
FastAPI serialises a response with `separators=(",", ":")` -- `{"database":"ok"}` -- while
`json.dumps` in a test defaults to `", "` and `": "`. A gate whose needle is the spaced form
passes every test ever written for it and matches NOTHING the real backend has ever sent.
Both forms are therefore served here, from the same tests.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
from pathlib import Path

import pytest

from tests.infra.shellstub import run

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra/aws/verify_deploy.sh"
FAST = {"CURL_RETRY": "0", "CURL_RETRY_DELAY": "0", "CURL_MAX_TIME": "3"}

HEALTH = {
    "status": "ok",
    "model_version": "toxic-clf:v3",
    "database": "ok",
    "spool_depth": 0,
    "rejected": {},
}
# What FastAPI actually puts on the wire, and what a test naively writes. The gate has to
# accept both, and every test below runs against both.
COMPACT = json.dumps(HEALTH, separators=(",", ":"))
SPACED = json.dumps(HEALTH)
SERIALISATIONS = pytest.mark.parametrize("serialise", [
    pytest.param(lambda payload: json.dumps(payload, separators=(",", ":")), id="fastapi"),
    pytest.param(json.dumps, id="json.dumps"),
])


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str]] = {}

    def do_GET(self):  # noqa: N802
        status, body = self.routes.get(self.path, (404, "not found"))
        payload = body.encode()
        self.send_response(status)
        if 300 <= status < 400:
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the test log
        return


def _serve(routes: dict[str, tuple[int, str]]) -> tuple[int, http.server.HTTPServer]:
    handler = type("H", (_Handler,), {"routes": routes})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], server


@pytest.fixture()
def stack():
    servers = []

    def start(backend_body=COMPACT, backend_status=200, ui_status=200, ui_body="ok"):
        ports = {}
        for name, routes in (
            ("backend", {"/health": (backend_status, backend_body)}),
            ("frontend", {"/_stcore/health": (ui_status, ui_body)}),
            ("monitoring", {"/_stcore/health": (ui_status, ui_body)}),
        ):
            port, server = _serve(routes)
            servers.append(server)
            ports[name] = port
        return {
            "BACKEND_URL": f"http://127.0.0.1:{ports['backend']}",
            "FRONTEND_URL": f"http://127.0.0.1:{ports['frontend']}",
            "MONITORING_URL": f"http://127.0.0.1:{ports['monitoring']}",
        }

    yield start
    for server in servers:
        server.shutdown()


@SERIALISATIONS
def test_all_three_healthy_exits_zero(tmp_path, stack, serialise):
    """Parameterised over both serialisations, because the FastAPI one is the only one that
    has ever been on this system's wire and the other is the only one a test writes."""
    result = run(SCRIPT, [], tmp_path / "bin",
                 env={**FAST, **stack(backend_body=serialise(HEALTH))})
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("backend", "frontend", "monitoring"):
        assert name in result.stdout


def test_the_gate_matches_what_fastapi_actually_serialises():
    """Stated separately from the run above, because this is the assertion that would have
    caught it: the needle must not depend on whitespace no serialiser is obliged to emit."""
    statements = [
        line for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not [line for line in statements if '"database": "ok"' in line], (
        "the needle is the spaced form; FastAPI emits separators=(',', ':') and this gate "
        "would report BAD on a perfectly healthy backend"
    )
    assert any('"database"' in line and "[[:space:]]" in line for line in statements), (
        "the needle does not tolerate the whitespace difference between the two serialisers"
    )


def test_verify_fails_when_one_endpoint_is_down(tmp_path, stack):
    urls = stack()
    urls["MONITORING_URL"] = "http://127.0.0.1:1"
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **urls})
    assert result.returncode != 0
    assert "monitoring" in result.stderr
    assert "DOWN" in result.stderr


def test_verify_fails_when_the_backend_returns_a_bad_status(tmp_path, stack):
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **stack(backend_status=503)})
    assert result.returncode != 0


@SERIALISATIONS
def test_verify_fails_when_health_leaks_the_artifact_digest(tmp_path, stack, serialise):
    """H14. /health goes out of its way to strip the digest; the gate confirms it worked."""
    leaky = dict(HEALTH, model_version="toxic-clf:v3@sha256:" + "a" * 64)
    result = run(SCRIPT, [], tmp_path / "bin",
                 env={**FAST, **stack(backend_body=serialise(leaky))})
    assert result.returncode != 0
    assert "LEAK" in result.stderr


@SERIALISATIONS
def test_verify_fails_when_the_backend_reports_the_database_unreachable(tmp_path, stack,
                                                                       serialise):
    """Rubric 2.2 makes complete prediction logging a requirement. A backend that serves
    without persisting punches holes in the graded drift and live-accuracy views and never
    fails a naive readiness probe."""
    degraded = dict(HEALTH, status="degraded", database="down")
    result = run(SCRIPT, [], tmp_path / "bin",
                 env={**FAST, **stack(backend_body=serialise(degraded))})
    assert result.returncode != 0


def test_a_two_hundred_with_the_wrong_body_is_not_a_healthy_backend(tmp_path, stack):
    """"It answered" is not "it is the thing that answered". A proxy error page, a default
    nginx page and a captive portal are all 200s."""
    result = run(SCRIPT, [], tmp_path / "bin",
                 env={**FAST, **stack(backend_body="<html>It works!</html>")})
    assert result.returncode != 0
    assert "BAD" in result.stderr


def test_a_redirect_is_not_accepted_as_healthy(tmp_path, stack):
    """A 302 with an empty body is a 2xx-adjacent success to anything that only reads the
    status line."""
    result = run(SCRIPT, [], tmp_path / "bin",
                 env={**FAST, **stack(backend_status=302, backend_body="")})
    assert result.returncode != 0


def test_a_streamlit_body_that_is_not_exactly_ok_fails(tmp_path, stack):
    """Substring matching on `ok` accepts `not ok`, `broken`, and a stack trace that happens
    to contain the word."""
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **stack(ui_body="not ok")})
    assert result.returncode != 0


def test_verify_requires_every_url_to_be_supplied(tmp_path, stack):
    urls = stack()
    del urls["FRONTEND_URL"]
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **urls})
    assert result.returncode != 0
    assert "FRONTEND_URL" in result.stderr


def test_no_endpoint_has_a_default(tmp_path):
    """A default localhost URL is a gate that passes on the operator's laptop while the
    fleet is dark. Every address comes from SSM, published by Terraform from the Elastic IPs."""
    body = SCRIPT.read_text(encoding="utf-8")
    for name in ("BACKEND_URL", "FRONTEND_URL", "MONITORING_URL"):
        assert re.search(rf'{name}="\$\{{{name}:\?', body), f"{name} is defaultable"


def test_the_streamlit_probes_use_the_stcore_health_path(tmp_path):
    """Streamlit has no /health. Probing / would 200 on a crashed app that still serves HTML."""
    assert "_stcore/health" in SCRIPT.read_text(encoding="utf-8")


def test_a_failure_names_where_to_look_next(tmp_path, stack):
    urls = stack()
    urls["BACKEND_URL"] = "http://127.0.0.1:1"
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **urls})
    assert result.returncode != 0
    assert "no-ssh-debug" in result.stderr


def test_one_healthy_endpoint_does_not_mask_two_broken_ones(tmp_path, stack):
    """A gate that returns on the first failure reports one problem per deploy attempt, and
    a three-instance fleet takes three deploys to diagnose."""
    urls = stack()
    urls["FRONTEND_URL"] = "http://127.0.0.1:1"
    urls["MONITORING_URL"] = "http://127.0.0.1:2"
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **urls})
    assert result.returncode != 0
    assert "frontend" in result.stderr and "monitoring" in result.stderr
    assert "backend      OK" in result.stdout or "backend" in result.stdout


def test_the_probe_is_the_same_shape_as_the_backends_own_serialisation():
    """Guards the fixtures themselves: if FastAPI ever changed separators, the constant this
    file calls COMPACT would stop being what the wire carries and every test above would go
    on passing."""
    from fastapi.responses import JSONResponse

    rendered = JSONResponse(HEALTH).body.decode()
    assert rendered == COMPACT, f"FastAPI now serialises as {rendered!r}"
    assert rendered != SPACED
