import re

from backend.auth import API_KEY_HEADER, check_api_key, client_fingerprint


def test_header_name_is_the_documented_one():
    assert API_KEY_HEADER == "X-API-Key"


def test_the_correct_key_is_accepted():
    assert check_api_key("s3cret-demo-key", "s3cret-demo-key") is True


def test_a_wrong_key_is_rejected():
    assert check_api_key("wrong", "s3cret-demo-key") is False


def test_a_missing_key_is_rejected():
    assert check_api_key(None, "s3cret-demo-key") is False
    assert check_api_key("", "s3cret-demo-key") is False


def test_a_prefix_of_the_key_is_rejected():
    assert check_api_key("s3cret", "s3cret-demo-key") is False


def test_a_non_ascii_key_is_rejected_rather_than_crashing():
    """`hmac.compare_digest` raises TypeError on a str holding a codepoint above U+00FF, and
    Starlette decodes header bytes as latin-1, so an attacker can put one there for free. An
    unhandled TypeError inside the gate middleware is a 500 with no rejection counted, which
    turns the auth check into a cheap way to generate server errors."""
    assert check_api_key("s3cret-demo-k中", "s3cret-demo-key") is False


def test_comparison_is_constant_time():
    """A byte-by-byte `==` on a secret is a timing oracle. This asserts the implementation
    uses hmac.compare_digest rather than trying to measure nanoseconds in CI."""
    source = __import__("inspect").getsource(check_api_key)
    assert "compare_digest" in source
    assert re.search(r"presented\s*==\s*expected", source) is None


def test_fingerprint_is_stable_short_and_not_the_key():
    fingerprint = client_fingerprint("s3cret-demo-key")
    assert fingerprint == client_fingerprint("s3cret-demo-key")
    assert len(fingerprint) == 16
    assert "s3cret" not in fingerprint
    assert client_fingerprint("another-key") != fingerprint
