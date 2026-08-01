"""Moderation policy: calibrated probabilities plus tuned thresholds -> flags and a decision.

Toxicity is asymmetric-cost, so the thresholds are per label and tuned on validation only
(Phase 1). This module makes two guarantees the rest of the system relies on. Flags are
hierarchically coherent - severe_toxic implies toxic - and coherence is applied here, before
the response is built, so the contract validator is a backstop rather than the only control.
And `decide` is pure: same inputs, same outputs, no clock, no I/O.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from model.labels import LABELS

# Severe labels block outright, but only well clear of their threshold: a near-threshold
# severe score is exactly the case a human should see rather than one the machine should
# silently suppress.
BLOCK_LABELS: tuple[str, ...] = ("severe_toxic", "threat", "identity_hate")
BLOCK_MARGIN: float = 0.15
REVIEW_MARGIN: float = 0.10


@dataclass(frozen=True)
class DecisionResult:
    flags: dict[str, bool]
    decision: str
    max_prob: float


def decide(probs: dict[str, float], thresholds: dict[str, float]) -> DecisionResult:
    missing = [
        label for label in LABELS if label not in probs or label not in thresholds
    ]
    if missing:
        raise ValueError(f"probs/thresholds missing labels: {missing}")

    flags = {label: probs[label] >= thresholds[label] for label in LABELS}
    if flags["severe_toxic"]:
        flags["toxic"] = True

    max_prob = max(probs[label] for label in LABELS)

    if any(probs[label] >= thresholds[label] + BLOCK_MARGIN for label in BLOCK_LABELS):
        decision = "block"
    elif any(flags.values()):
        decision = "review"
    elif any(probs[label] >= thresholds[label] - REVIEW_MARGIN for label in LABELS):
        decision = "review"
    else:
        decision = "allow"

    return DecisionResult(flags=flags, decision=decision, max_prob=max_prob)


def load_thresholds(path: Path) -> dict[str, float]:
    """Load the Phase 1 `thresholds.json` artifact, rejecting anything unusable at startup."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [label for label in LABELS if label not in raw]
    if missing:
        raise ValueError(f"{path} is missing thresholds for: {missing}")
    thresholds: dict[str, float] = {}
    for label in LABELS:
        value = float(raw[label])
        if not 0.0 < value < 1.0:
            raise ValueError(f"{path}: threshold for {label} must be in (0, 1), got {value}")
        thresholds[label] = value
    return thresholds
