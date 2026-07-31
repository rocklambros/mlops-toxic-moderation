import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from model.contract import PredictionResponse, enforce_hierarchy, probs_to_dict
from model.labels import LABELS


def _payload(**overrides):
    probs = {label: 0.10 for label in LABELS}
    probs["toxic"] = 0.80
    payload = {
        "request_id": "uuid",
        "model_version": "toxic-clf:v3@sha256:abcd",
        "labels": {label: {"prob": probs[label], "flag": label == "toxic"} for label in LABELS},
        "decision": "review",
        "max_prob": 0.80,
        "latency_ms": 42,
    }
    payload.update(overrides)
    return payload


def test_valid_payload_parses():
    resp = PredictionResponse(**_payload())
    assert tuple(resp.labels.keys()) == LABELS


def test_rejects_unknown_decision():
    with pytest.raises(ValidationError):
        PredictionResponse(**_payload(decision="delete"))


def test_rejects_wrong_label_keys():
    payload = _payload()
    payload["labels"].pop("threat")
    with pytest.raises(ValidationError):
        PredictionResponse(**payload)


def test_rejects_out_of_order_label_keys():
    payload = _payload()
    payload["labels"] = dict(reversed(list(payload["labels"].items())))
    with pytest.raises(ValidationError, match="exact order"):
        PredictionResponse(**payload)


@pytest.mark.parametrize("bad", [-5.0, 42.0, 1.0001, -0.0001])
def test_rejects_probability_outside_zero_one(bad):
    payload = _payload()
    payload["labels"]["obscene"]["prob"] = bad
    with pytest.raises(ValidationError):
        PredictionResponse(**payload)


def test_rejects_negative_latency():
    with pytest.raises(ValidationError):
        PredictionResponse(**_payload(latency_ms=-7))


def test_rejects_max_prob_inconsistent_with_labels():
    with pytest.raises(ValidationError, match="max_prob"):
        PredictionResponse(**_payload(max_prob=0.99))


def test_rejects_severe_toxic_probability_above_toxic():
    payload = _payload(max_prob=0.99)
    payload["labels"]["severe_toxic"]["prob"] = 0.99
    payload["labels"]["toxic"]["prob"] = 0.01
    with pytest.raises(ValidationError, match="severe_toxic"):
        PredictionResponse(**payload)


def test_rejects_severe_toxic_flag_without_toxic_flag():
    payload = _payload()
    payload["labels"]["severe_toxic"] = {"prob": 0.80, "flag": True}
    payload["labels"]["toxic"] = {"prob": 0.80, "flag": False}
    with pytest.raises(ValidationError, match="severe_toxic flagged"):
        PredictionResponse(**payload)


def test_probs_to_dict_maps_positionally_in_label_order():
    row = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    out = probs_to_dict(row)
    assert list(out.keys()) == list(LABELS)
    assert out == {
        "toxic": 0.1, "severe_toxic": 0.2, "obscene": 0.3,
        "threat": 0.4, "insult": 0.5, "identity_hate": 0.6,
    }


def test_probs_to_dict_rejects_wrong_length():
    with pytest.raises(ValueError, match="expected 6 probabilities"):
        probs_to_dict(np.array([0.1, 0.2, 0.3]))


def test_probs_to_dict_rejects_a_two_dimensional_row():
    """A (2, 6) matrix must be a dimensionality error, not a length error: ravel() would
    turn it into a plausible-looking 12-vector and report the wrong cause."""
    with pytest.raises(ValueError, match="1-D"):
        probs_to_dict(np.zeros((2, len(LABELS))))


def test_enforce_hierarchy_clamps_severe_toxic():
    assert enforce_hierarchy({**{lb: 0.0 for lb in LABELS}, "toxic": 0.2,
                              "severe_toxic": 0.9})["severe_toxic"] == 0.2


def test_protected_namespaces_is_disabled():
    assert PredictionResponse.model_config.get("protected_namespaces") == ()


def test_importing_the_contract_emits_no_pydantic_warning():
    result = subprocess.run(
        [sys.executable, "-W", "error::UserWarning", "-c", "import model.contract"],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
             "PYTHONHASHSEED": "0"},
    )
    assert result.returncode == 0, result.stderr
