"""The rate-limit key, and the properties that make it a token rather than an identity.

Two separate things are asserted here. That the value is unlinkable (across days, across
deploys, and back to the address) is the privacy posture. That a client cannot choose its
own bucket is the security control -- without it the per-source quota is decorative,
because the flooder simply rotates the header.
"""

import datetime as dt

import pytest

from backend.fingerprint import caller_identity, submitter_fp

KEY = bytes(range(32))
DAY = dt.date(2026, 8, 6)


def test_fingerprint_is_sixteen_lowercase_hex_chars():
    value = submitter_fp("203.0.113.7", DAY, KEY)
    assert len(value) == 16
    assert all(c in "0123456789abcdef" for c in value)


def test_fingerprint_fits_the_column_it_is_stored_in():
    """`predictions.submitter_fp` is CHAR(16). A longer digest is a DataError on the write
    path of a public endpoint."""
    assert len(submitter_fp("session:" + "f" * 64, DAY, KEY)) == 16


def test_fingerprint_is_stable_within_a_day():
    assert submitter_fp("203.0.113.7", DAY, KEY) == submitter_fp("203.0.113.7", DAY, KEY)


def test_fingerprint_is_not_linkable_across_days():
    tomorrow = DAY + dt.timedelta(days=1)
    assert submitter_fp("203.0.113.7", DAY, KEY) != submitter_fp("203.0.113.7", tomorrow, KEY)


def test_fingerprint_is_not_linkable_across_deploys():
    other_key = bytes(range(1, 33))
    assert submitter_fp("203.0.113.7", DAY, KEY) != submitter_fp("203.0.113.7", DAY, other_key)


def test_fingerprint_does_not_contain_the_address():
    value = submitter_fp("203.0.113.7", DAY, KEY)
    assert "203" not in value
    assert "113" not in value


def test_distinct_addresses_get_distinct_fingerprints():
    assert submitter_fp("203.0.113.7", DAY, KEY) != submitter_fp("203.0.113.8", DAY, KEY)


def test_a_session_bucket_can_never_collide_with_a_peer_bucket():
    """The namespace prefix is the control. Without it a frontend session token spelled like
    an address would share one quota bucket with that address, and either could spend the
    other's allowance."""
    peer = caller_identity("deadbeefdeadbeef", None, api_key_ok=False)
    session = caller_identity("10.42.1.20", "deadbeefdeadbeef", api_key_ok=True)
    assert peer != session
    assert submitter_fp(peer, DAY, KEY) != submitter_fp(session, DAY, KEY)


def test_empty_key_is_refused():
    with pytest.raises(ValueError, match="key"):
        submitter_fp("203.0.113.7", DAY, b"")


def test_identity_is_the_peer_for_a_direct_caller():
    assert caller_identity("203.0.113.7", None, api_key_ok=False) == "peer:203.0.113.7"


def test_identity_ignores_a_session_header_without_the_frontend_api_key():
    """An unauthenticated client must not be able to choose its own rate-limit bucket."""
    identity = caller_identity("203.0.113.7", "deadbeefdeadbeef", api_key_ok=False)
    assert identity == "peer:203.0.113.7"


def test_identity_uses_the_session_header_only_from_the_authenticated_frontend():
    assert (
        caller_identity("10.42.1.20", "deadbeefdeadbeef", api_key_ok=True)
        == "session:deadbeefdeadbeef"
    )


def test_identity_rejects_a_malformed_session_header():
    identity = caller_identity("10.42.1.20", "not hex; drop table", api_key_ok=True)
    assert identity == "peer:10.42.1.20"


@pytest.mark.parametrize(
    "header",
    [
        "deadbeefdeadbee",  # 15 chars
        "deadbeefdeadbeef0",  # 17 chars
        "DEADBEEFDEADBEEF",  # uppercase: a second spelling of one bucket
        "deadbeefdeadbeef\n",  # trailing newline: a second spelling of one bucket
        "deadbeefdeadbeef\ndeadbeefdeadbeef",  # newline-anchored regex would accept this
        "",
        "  deadbeefdeadbeef",
        "zzzzzzzzzzzzzzzz",
    ],
)
def test_only_an_exactly_shaped_session_header_is_honoured(header):
    """Anything the pattern lets through is a bucket the frontend never minted. `$` in a
    Python regex matches before a trailing newline, so the anchors have to be \\A and \\Z."""
    assert caller_identity("10.42.1.20", header, api_key_ok=True) == "peer:10.42.1.20"


def test_identity_never_reads_x_forwarded_for():
    """caller_identity has no parameter for it. A spoofed hop cannot reach this function."""
    import inspect

    assert "forwarded" not in inspect.signature(caller_identity).parameters


def _executable_source(module) -> str:
    """The module's code with docstrings and comments removed.

    Scanning the raw text would trip on the module docstring, which says in prose that
    X-Forwarded-For is never consulted -- a scan that its own documentation fails is a scan
    that gets deleted rather than fixed. `ast.unparse` drops comments outright; docstrings
    are stripped explicitly. String literals that appear in real code survive, which is the
    only place a header name could actually be read.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                body.pop(0)
    return ast.unparse(tree).lower()


def test_the_module_reads_no_header_but_the_two_it_is_given():
    """A later refactor that reaches for request.headers inside this module would reopen the
    spoofable path these functions were written to close."""
    import backend.fingerprint as module

    source = _executable_source(module)
    assert source.strip(), "the scan must not pass over an empty string"
    assert "def caller_identity" in source, "the scan is not looking at the real module"
    for banned in ("x-forwarded-for", "x-real-ip", "forwarded", "request.headers", "environ"):
        assert banned not in source, banned
