"""Stable model output contract, plus the single authoritative array->dict adapter."""

from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from model.labels import LABELS

MAX_PROB_TOLERANCE = 1e-6


def probs_to_dict(row: np.ndarray) -> dict[str, float]:
    """THE array->dict converter. Every call site uses this; nobody re-derives with zip().

    Phase 0 OWNS this function. Phase 1 Task 1 and Phase 2 Task 1 must import it, not
    redefine it: as originally written all three said "Append to model/contract.py" with
    three different bodies and three different messages, Python keeps the last def, and the
    two earlier phases' pytest.raises(match=...) cases go red without anyone touching them.
    That is premortem H23 recurring inside the remediation for H23. Phase 4 Task 11's
    test_probs_to_dict_is_defined_exactly_once is the guard.

    ravel() is deliberately NOT used: a (2, 6) matrix ravels to (12,) and would be reported
    as a length error rather than the dimensionality error it is.
    """
    arr = np.asarray(row, dtype=float)
    if arr.ndim != 1:
        raise ValueError(f"probs_to_dict takes a 1-D row, got shape {arr.shape}")
    if arr.shape[0] != len(LABELS):
        raise ValueError(f"expected {len(LABELS)} probabilities, got {arr.shape[0]}")
    return {label: float(arr[i]) for i, label in enumerate(LABELS)}


def enforce_hierarchy(probs: dict[str, float]) -> dict[str, float]:
    """severe_toxic can never exceed toxic. Phase 2 calls this before building a response."""
    out = dict(probs)
    out["severe_toxic"] = min(out["severe_toxic"], out["toxic"])
    return out


class LabelScore(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    prob: float = Field(ge=0.0, le=1.0)
    flag: bool


class PredictionResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    request_id: str
    model_version: str
    labels: dict[str, LabelScore]
    decision: Literal["allow", "review", "block"]
    max_prob: float = Field(ge=0.0, le=1.0)
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _labels_match_constant_in_order(self) -> "PredictionResponse":
        if tuple(self.labels.keys()) != LABELS:
            raise ValueError(f"labels keys must equal {LABELS} in that exact order")
        return self

    @model_validator(mode="after")
    def _max_prob_is_consistent(self) -> "PredictionResponse":
        observed = max(score.prob for score in self.labels.values())
        if abs(observed - self.max_prob) > MAX_PROB_TOLERANCE:
            raise ValueError(f"max_prob {self.max_prob} != max label prob {observed}")
        return self

    @model_validator(mode="after")
    def _severe_toxic_implies_toxic(self) -> "PredictionResponse":
        severe, toxic = self.labels["severe_toxic"], self.labels["toxic"]
        if severe.prob > toxic.prob + MAX_PROB_TOLERANCE:
            raise ValueError(
                f"severe_toxic prob {severe.prob} exceeds toxic prob {toxic.prob}"
            )
        if severe.flag and not toxic.flag:
            raise ValueError("severe_toxic flagged without toxic flagged")
        return self
