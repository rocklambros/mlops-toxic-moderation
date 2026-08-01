"""Serving-path input preparation.

Train/serve skew resolution (premortem H25). The serving normalizer is
`model.normalize.normalize_for_serving`: the FROZEN corpus normalizer plus confusable
folding, combining-mark stripping, and the length cap. It is a strict superset applied
AFTER the corpus normalizer, so `model/data/dedup.py` never imports it, dedup output
never moves, `split_version` never moves, and the locked 15% test set stays locked.

Folding at serving time maps an evasion ONTO the training distribution rather than away
from it: `уou` becomes `you`, a token the model was fitted on. The residual skew is
bounded to inputs containing confusables or combining marks, which is the population this
exists to canonicalise.

Named limitation for MODEL_CARD.md: combining marks are stripped, so `händbuch` serves as
`handbuch` while the corpus keeps `händbuch`. Residual cross-script and paraphrase evasion
remains a model-card limitation, and the review queue does not mitigate it because a
successful evasion is never flagged.
"""

from model.normalize import MAX_INPUT_CHARS
from model.normalize import normalize_for_serving as normalize

__all__ = ["MAX_INPUT_CHARS", "normalize", "prepare_input"]


def prepare_input(text: str) -> str:
    """Normalize one comment for scoring. Raises on oversize input.

    The pydantic layer already rejects oversize text with 422; this is the second gate, for
    internal callers such as the spool drainer and the Phase 3 re-scorer. It raises BEFORE
    calling `normalize`, so the serving normalizer's internal truncation is never reached on
    this path and no oversize input is ever silently shortened.
    """
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"input exceeds {MAX_INPUT_CHARS} characters")
    return normalize(text)
