"""Reviewer session tokens.

One reviewer behind a shared secret, which the design already records as not being a real
authentication system. What this module does guarantee is the property the delivery spec
(section 6.3) makes normative: `reviewer_id` is derived server-side. The identity returned
is the process's configured REVIEWER_ID, and a token only ever decides whether to return it
or None. No code path parses an identity out of a client-held value.

Three details are load-bearing rather than stylistic.

* The identity and the expiry are both inside the signed message, so neither can be edited
  by the holder, and the claimed identity is then *also* compared against server
  configuration. A validly signed token for another identity therefore authenticates
  nobody, which is what makes the returned value a server fact rather than a client claim.
* Every comparison is `hmac.compare_digest` over **bytes**. On `str` operands it raises
  TypeError for any non-ASCII character, and the token is attacker-supplied, so comparing
  the raw string would turn one chosen byte into a 500 on an internet-facing endpoint.
* An unconfigured server (missing secret or missing reviewer id) authenticates nobody
  rather than accepting the empty string as an identity.
"""

import datetime as dt
import hashlib
import hmac


def _signature(reviewer_id: str, expiry: int, secret: str) -> str:
    message = f"{reviewer_id}.{expiry}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def issue_session_token(
    now: dt.datetime, secret: str, reviewer_id: str, ttl_seconds: int = 43200
) -> str:
    if not secret:
        raise ValueError("reviewer shared secret must not be empty")
    if not reviewer_id:
        raise ValueError("reviewer_id must not be empty")
    expiry = int(now.timestamp()) + ttl_seconds
    return f"{reviewer_id}.{expiry}.{_signature(reviewer_id, expiry, secret)}"


def current_reviewer(
    token: str | None, now: dt.datetime, secret: str, reviewer_id: str
) -> str | None:
    if not token or not secret or not reviewer_id:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    claimed, raw_expiry, signature = parts
    try:
        expiry = int(raw_expiry)
    except ValueError:
        return None
    expected = _signature(claimed, expiry, secret)
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        return None
    if expiry <= int(now.timestamp()):
        return None
    if not hmac.compare_digest(claimed.encode(), reviewer_id.encode()):
        return None
    return reviewer_id  # the server's value, never the token's
