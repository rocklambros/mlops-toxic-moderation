"""The per-source quotas key on `predictions.submitter_fp`.

`backend/queue_guard.py` counts a source's review enqueues and user verdicts by joining
`predictions.submitter_fp`. If the serving path never writes that column, every one of
those counts is zero for every caller and all three quotas are dead code that no unit test
of `admit_review` can notice, because those tests write the column by hand.

So this module tests the wiring, not the arithmetic: does a real POST /predict leave a
fingerprint behind, is it the keyed digest rather than the API-key digest, and does it
separate two UI sessions that share one API key and one TCP peer.
"""

import re

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration

AUTH = {"X-API-Key": "test-demo-key"}
HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")


def _fingerprint_of(conn, request_id: str) -> str | None:
    return conn.execute(
        text("SELECT submitter_fp FROM predictions WHERE request_id = :rid"), {"rid": request_id}
    ).scalar()


def _predict(client, text_value: str, session_fp: str | None = None) -> str:
    headers = dict(AUTH)
    if session_fp is not None:
        headers["X-Session-Fp"] = session_fp
    response = client.post("/predict", json={"text": text_value}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["request_id"]


def test_predict_records_a_submitter_fingerprint(client, conn):
    request_id = _predict(client, "hello there")
    stored = _fingerprint_of(conn, request_id)
    assert stored is not None, "the quota key is NULL, so every per-source quota counts zero"
    assert HEX16.match(stored), stored


def test_the_submitter_fingerprint_is_not_the_api_key_digest(client, conn):
    """`client_fp` is a digest of the shared demo key, so every request the frontend
    proxies carries the same one. Reusing it as the quota key gives the whole internet a
    single bucket."""
    request_id = _predict(client, "hello there", session_fp="0123456789abcdef")
    row = conn.execute(
        text("SELECT client_fp, submitter_fp FROM predictions WHERE request_id = :rid"),
        {"rid": request_id},
    ).one()
    assert row.submitter_fp != row.client_fp


def test_two_ui_sessions_behind_one_api_key_get_different_buckets(client, conn):
    first = _fingerprint_of(conn, _predict(client, "hello", session_fp="0" * 16))
    second = _fingerprint_of(conn, _predict(client, "hello", session_fp="f" * 16))
    assert first != second


def test_the_same_session_keeps_one_bucket(client, conn):
    first = _fingerprint_of(conn, _predict(client, "hello", session_fp="abcdef0123456789"))
    second = _fingerprint_of(conn, _predict(client, "goodbye", session_fp="abcdef0123456789"))
    assert first == second


def test_a_malformed_session_header_falls_back_to_the_peer_bucket(client, conn):
    """An attacker who can spell anything into X-Session-Fp would otherwise mint a fresh
    quota bucket per request. Only a well-formed 16-hex value is accepted as a session."""
    forged = _fingerprint_of(conn, _predict(client, "hello", session_fp="../../etc/passwd"))
    plain = _fingerprint_of(conn, _predict(client, "hello"))
    assert forged == plain


def test_a_deploy_that_forgot_the_key_still_fingerprints(app_settings, conn):
    """The absent-key path must not be the fail-open path: no key means a random
    per-process key, never a NULL column and three silently dead quotas."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from backend.app import create_app

    with TestClient(create_app(replace(app_settings, submitter_fp_key=""))) as unkeyed:
        request_id = _predict(unkeyed, "hello there")
    stored = _fingerprint_of(conn, request_id)
    assert stored is not None and HEX16.match(stored), stored


def test_the_fingerprint_is_never_echoed_to_the_client(client, conn):
    request_id = _predict(client, "hello there", session_fp="0123456789abcdef")
    stored = _fingerprint_of(conn, request_id)
    body = client.post(
        "/predict", json={"text": "hello there"}, headers={**AUTH, "X-Session-Fp": "0" * 16}
    ).text
    assert stored not in body
