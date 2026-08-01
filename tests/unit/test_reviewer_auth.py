import datetime as dt
import hashlib
import hmac

import pytest

from backend.reviewer_auth import current_reviewer, issue_session_token

SECRET = "s3cr3t-shared-with-the-reviewer"
REVIEWER = "rock"
NOW = dt.datetime(2026, 8, 12, 9, 0, tzinfo=dt.UTC)


def test_issued_token_resolves_to_the_configured_reviewer():
    token = issue_session_token(NOW, SECRET, REVIEWER)
    assert current_reviewer(token, NOW, SECRET, REVIEWER) == REVIEWER


def test_no_token_is_no_reviewer():
    assert current_reviewer(None, NOW, SECRET, REVIEWER) is None
    assert current_reviewer("", NOW, SECRET, REVIEWER) is None


def test_forged_token_is_rejected():
    assert current_reviewer("rock.9999999999.deadbeef", NOW, SECRET, REVIEWER) is None


def test_token_from_a_different_secret_is_rejected():
    token = issue_session_token(NOW, "some-other-secret", REVIEWER)
    assert current_reviewer(token, NOW, SECRET, REVIEWER) is None


def test_expired_token_is_rejected():
    token = issue_session_token(NOW, SECRET, REVIEWER, ttl_seconds=60)
    later = NOW + dt.timedelta(seconds=61)
    assert current_reviewer(token, later, SECRET, REVIEWER) is None


def test_an_expiry_rewritten_by_the_holder_does_not_extend_the_session():
    """The expiry is inside the signed message, so moving it invalidates the signature
    rather than buying another twelve hours."""
    token = issue_session_token(NOW, SECRET, REVIEWER, ttl_seconds=60)
    claimed, expiry, signature = token.split(".")
    extended = f"{claimed}.{int(expiry) + 86400}.{signature}"
    later = NOW + dt.timedelta(seconds=61)
    assert current_reviewer(extended, later, SECRET, REVIEWER) is None


def test_a_token_minted_for_another_identity_cannot_borrow_this_one():
    """The identity is inside the signed payload AND compared to server config, so a valid
    token for 'mallory' does not authenticate as the configured reviewer."""
    token = issue_session_token(NOW, SECRET, "mallory")
    assert current_reviewer(token, NOW, SECRET, REVIEWER) is None


def test_identity_is_never_taken_from_the_token_alone():
    """current_reviewer returns the SERVER's reviewer_id, never a value parsed out of a
    client-held string. Renaming the configured reviewer changes what a fixed token
    resolves to -- to None, never to the token's own claim."""
    token = issue_session_token(NOW, SECRET, REVIEWER)
    assert current_reviewer(token, NOW, SECRET, "someone-else") is None


def _forge(reviewer_id: str, secret: str, ttl_seconds: int = 3600) -> str:
    """Mint a token the way an attacker would, so the server-side guards are tested against
    a correctly signed forgery rather than against garbage the signature check catches."""
    expiry = int(NOW.timestamp()) + ttl_seconds
    message = f"{reviewer_id}.{expiry}".encode()
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"{reviewer_id}.{expiry}.{signature}"


def test_a_server_with_no_shared_secret_authenticates_nobody():
    """An unset REVIEWER_SHARED_SECRET is an empty HMAC key, which anyone can compute. The
    server must refuse to authenticate at all rather than validate the forgery."""
    assert current_reviewer(_forge(REVIEWER, ""), NOW, "", REVIEWER) is None


def test_a_server_with_no_configured_reviewer_id_authenticates_nobody():
    """An unset REVIEWER_ID must not make the empty string a valid identity: a correctly
    signed token claiming '' would otherwise resolve, and every review row would then be
    attributed to nobody."""
    assert current_reviewer(_forge("", SECRET), NOW, SECRET, "") is None


def test_a_non_ascii_signature_is_rejected_rather_than_raising():
    """hmac.compare_digest refuses non-ASCII str operands with a TypeError, so comparing
    the raw client string would turn one attacker-chosen byte into a 500 on an
    internet-facing endpoint."""
    assert current_reviewer(f"rock.9999999999.{'ÿ' * 64}", NOW, SECRET, REVIEWER) is None
    assert current_reviewer(f"roсk.9999999999.{'ÿ' * 64}", NOW, SECRET, REVIEWER) is None


def test_a_token_with_extra_separators_is_rejected():
    token = issue_session_token(NOW, SECRET, REVIEWER)
    assert current_reviewer(token + ".extra", NOW, SECRET, REVIEWER) is None


def test_token_comparison_is_constant_time():
    import inspect

    import backend.reviewer_auth as module

    assert "compare_digest" in inspect.getsource(module)


def test_empty_secret_is_refused():
    with pytest.raises(ValueError, match="secret"):
        issue_session_token(NOW, "", REVIEWER)


def test_empty_reviewer_id_is_refused():
    with pytest.raises(ValueError, match="reviewer_id"):
        issue_session_token(NOW, SECRET, "")
