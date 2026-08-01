"""Demo API key check for /predict.

This is not an identity system and the model card says so. It is the control that stops a
public moderation endpoint on a public repository from being free denial-of-service capacity,
and it is one of the three compensating controls delivery spec section 13 names for the
decision to make the W&B registry publicly visible.

Operational note that belongs with the code: the key is NOT published in the repository. The
README shows `curl -H "X-API-Key: $DEMO_API_KEY" ...` and the value travels in the Canvas
submission text entry, which is not public. It is rotated after grading. /health carries no
key requirement, so the grader, the deploy gate, and the container HEALTHCHECK all work.
"""

import hashlib
import hmac

API_KEY_HEADER = "X-API-Key"


def check_api_key(presented: str | None, expected: str) -> bool:
    """Constant-time comparison of the presented header value against the demo key.

    Both sides are encoded first. `hmac.compare_digest` raises TypeError when a `str`
    argument holds a codepoint above U+00FF, and header values are attacker-controlled, so
    comparing the raw strings turns any non-Latin-1 key into an unhandled exception inside
    the gate middleware -- a 500 with no rejection counted. On ASCII keys the encoded and
    unencoded comparisons are identical, so this costs nothing.
    """
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def client_fingerprint(api_key: str) -> str:
    """Stable per-key identifier for rate limiting and abuse forensics.

    The fingerprint, never the key, is what reaches the rate limiter, the log line, and
    `predictions.client_fp`, so a leaked log or a screenshot cannot replay traffic.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
