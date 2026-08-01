"""A rate-limit key for a schema that had no submitter identifier.

Phase 2 stores `predictions.client_fp`, but that is a digest of the demo API key: every
request the shared frontend proxies carries the same one, so it cannot key a per-submitter
quota. This module supplies the key that can.

What is stored is the first 16 hex characters of HMAC-SHA256(key, "identity|utc_date"):
not reversible to an address without the per-deploy key, not linkable across days because
the date is inside the message, not linkable across deploys because the key rotates, and
purged with `predictions.input_text` on the same 30-day TTL. It carries strictly less
information than an ordinary web access log, and without it the review queue is an
unbounded anonymous write path into a graded metric.

`identity` is the TCP peer for direct callers. Traffic proxied by the user UI all shares
one peer address, so the frontend passes its own server-side session token (never sent to
the browser) in X-Session-Fp -- accepted only when the caller also presents the frontend's
API key. The proxy header a load balancer would set is never consulted: these functions
have no parameter for it, so a spoofed hop cannot reach them at all.

The two namespaces are prefixed because they share one key space. Without the prefix a
session token spelled like an address would land in that address's quota bucket.
"""

import datetime as dt
import hashlib
import hmac
import re

# \A and \Z, not ^ and $: in Python `$` also matches immediately before a trailing newline,
# so "deadbeefdeadbeef\n" and "deadbeefdeadbeef" would be two spellings of one bucket.
_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")


def caller_identity(peer_ip: str, session_fp_header: str | None, api_key_ok: bool) -> str:
    if api_key_ok and session_fp_header and _HEX16.match(session_fp_header):
        return f"session:{session_fp_header}"
    return f"peer:{peer_ip}"


def submitter_fp(identity: str, day: dt.date, key: bytes) -> str:
    if not key:
        raise ValueError("submitter fingerprint key must not be empty")
    message = f"{identity}|{day.isoformat()}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16]
