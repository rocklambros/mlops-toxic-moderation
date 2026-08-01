"""Read the expected artifact digest from the git-committed model card.

Provenance, not merely integrity (premortem tail risk 1). SHA-256 proves an artifact arrived
unaltered; it proves nothing about who produced it. Today the artifact and its digest both
come from Weights & Biases under one API key that is deliberately shared with RunPod pods,
so whoever holds that key can publish a poisoned artifact and a matching digest. Reading the
expected digest from MODEL_CARD.md - committed to git, protected by branch protection -
splits the two trust domains, and the cost of doing so is one regex.
"""

import re
from pathlib import Path

DIGEST_LINE = re.compile(r"^-\s*MODEL_DIGEST:\s*sha256:([0-9a-f]{64})\s*$", re.MULTILINE)


def read_expected_digest(card_path: Path) -> str:
    """Return the 64-character hex digest declared by the model card.

    Raises ValueError when the card declares none, or declares more than one distinct value.
    """
    text = Path(card_path).read_text(encoding="utf-8")
    found = DIGEST_LINE.findall(text)
    if not found:
        raise ValueError(
            f"{card_path} carries no `- MODEL_DIGEST: sha256:<64 lowercase hex>` line"
        )
    distinct = sorted(set(found))
    if len(distinct) > 1:
        raise ValueError(f"{card_path} declares {len(distinct)} conflicting MODEL_DIGEST values")
    return distinct[0]
