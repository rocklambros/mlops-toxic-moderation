"""Serving-path input preparation.

Train/serve skew resolution (premortem H25). The serving normalizer IS
`model.data.dedup.normalize` - the same function object, not a superset. Delivery spec
section 6.2 described it as dedup's normalizer plus confusable/homoglyph folding, which
cannot hold: folding only here means the model scores text it was never fitted on, and
folding in `dedup` changes dedup's output, therefore `data_version`, therefore the locked
test set - after Phase 1 registered models against it. The gap is closed by making the two
identical. Residual cross-script and homoglyph evasion is a model-card limitation, and the
review queue does not mitigate it because a successful evasion is never flagged.
"""

from backend.config import MAX_INPUT_CHARS
from model.data.dedup import normalize

__all__ = ["MAX_INPUT_CHARS", "normalize", "prepare_input"]


def prepare_input(text: str) -> str:
    """Normalize one comment for scoring. Raises on oversize input.

    The pydantic layer already rejects oversize text with 422; this is the second gate, for
    internal callers such as the spool drainer and the Phase 3 re-scorer.
    """
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"input exceeds {MAX_INPUT_CHARS} characters")
    return normalize(text)
