import re

import pytest

from backend.db import Prediction, ReviewQueue
from backend.policy import DecisionResult
from model.labels import LABELS
from tests.integration.conftest import AUTH

pytestmark = pytest.mark.integration

HEX64 = re.compile(r"[0-9a-f]{64}")


def allow_decision(probs, thresholds):
    """A deterministic stand-in for `decide` that returns a coherent `allow`.

    The fixture estimator is fitted on eight rows, so which decision any given string draws
    is an accident of that fit rather than a property under test. Two tests below are about
    the enqueue branch the app takes GIVEN a decision, so the decision is pinned here instead
    of hoped for -- the original plan text guarded one of them with
    `if body["decision"] == "allow":`, and the fixture in fact decides `review` for that
    string, so the assertion never ran.
    """
    return DecisionResult(
        flags=dict.fromkeys(LABELS, False),
        decision="allow",
        max_prob=max(probs.values()),
    )


def test_predict_returns_a_contract_valid_response(client):
    response = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "request_id",
        "model_version",
        "labels",
        "decision",
        "max_prob",
        "latency_ms",
    }
    assert set(body["labels"]) == set(LABELS)
    assert body["decision"] in {"allow", "review", "block"}
    assert 0.0 <= body["max_prob"] <= 1.0
    assert body["latency_ms"] >= 0
    for score in body["labels"].values():
        assert 0.0 <= score["prob"] <= 1.0
        assert isinstance(score["flag"], bool)


def test_every_prediction_writes_exactly_one_row(client, session):
    """Rubric 2.2: the service must log every prediction request, its output, and a
    timestamp."""
    response = client.post("/predict", json={"text": "have a nice day friend"}, headers=AUTH)
    request_id = response.json()["request_id"]
    stored = session.get(Prediction, request_id)
    assert stored is not None
    assert stored.ts is not None
    assert stored.input_text == "have a nice day friend"
    assert stored.input_chars == len("have a nice day friend")
    assert stored.status == "ok"
    assert stored.persist_status == "direct"
    assert stored.prob_toxic is not None
    assert stored.latency_ms == response.json()["latency_ms"]


def test_the_database_row_carries_the_full_version_and_the_response_does_not(client, session):
    """H14. The digest belongs in the row and the log, where it supports incident response,
    and nowhere a client can read it."""
    body = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH).json()
    stored = session.get(Prediction, body["request_id"])
    assert stored.model_version.startswith("toxic-clf:v3@sha256:")
    assert body["model_version"] == "toxic-clf:v3"


def test_no_response_ever_carries_the_artifact_digest(client, artifact_bundle):
    digest = artifact_bundle["digest"]
    responses = [
        client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH),
        client.get("/health"),
    ]
    for response in responses:
        assert digest not in response.text
        assert not HEX64.search(response.text)


def test_a_reviewable_prediction_enqueues_a_flagged_review_row(client, session, monkeypatch):
    monkeypatch.setattr(
        "backend.app.decide",
        lambda probs, thresholds: DecisionResult(
            flags={label: label == "toxic" for label in LABELS},
            decision="review",
            max_prob=max(probs.values()),
        ),
    )
    body = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH).json()
    assert body["decision"] == "review"
    queued = session.get(ReviewQueue, body["request_id"])
    assert queued.source == "flagged"
    assert queued.sample_rate == 1.0
    assert queued.input_text_snapshot == "you are an idiot"
    assert queued.status == "pending"


def test_an_allowed_prediction_does_not_enqueue_when_the_audit_rate_is_zero(
    client, session, monkeypatch
):
    monkeypatch.setattr("backend.app.decide", allow_decision)
    body = client.post("/predict", json={"text": "have a nice day friend"}, headers=AUTH).json()
    assert body["decision"] == "allow"
    assert session.get(ReviewQueue, body["request_id"]) is None


def test_random_audit_enqueues_with_its_sample_rate(client, session, monkeypatch):
    """H8. The weight has to be on the row, or Phase 3 cannot correct the pooled estimate."""
    from dataclasses import replace

    monkeypatch.setattr("backend.app.decide", allow_decision)
    monkeypatch.setattr("backend.app.should_random_audit", lambda rate, rng: True)
    client.app.state.settings = replace(client.app.state.settings, random_audit_rate=0.05)
    body = client.post("/predict", json={"text": "have a nice day friend"}, headers=AUTH).json()
    queued = session.get(ReviewQueue, body["request_id"])
    assert queued.source == "random-audit"
    assert queued.sample_rate == pytest.approx(0.05)


def test_request_ids_are_unique_per_request(client):
    seen = {
        client.post("/predict", json={"text": f"comment {index}"}, headers=AUTH).json()[
            "request_id"
        ]
        for index in range(20)
    }
    assert len(seen) == 20


def test_severe_toxic_never_appears_without_toxic(client, monkeypatch):
    """H22, end to end. The policy enforces coherence; this asserts nothing downstream
    reintroduces the incoherent pair.

    The thresholds are pinned so that severe_toxic clears its own threshold while toxic does
    not clear the higher one. That is the only way to reach the incoherent pair once
    probabilities are hierarchy-clamped, and it is realistic: Phase 1 tunes a threshold per
    label, so `threshold[toxic] > threshold[severe_toxic]` is an ordinary outcome.
    """
    client.app.state.thresholds = {
        **dict.fromkeys(LABELS, 0.5),
        "toxic": 0.9,
        "severe_toxic": 0.3,
    }
    monkeypatch.setattr(
        "backend.app.probs_to_dict",
        lambda row: {
            "toxic": 0.50,
            "severe_toxic": 0.40,
            "obscene": 0.01,
            "threat": 0.01,
            "insult": 0.01,
            "identity_hate": 0.01,
        },
    )
    body = client.post("/predict", json={"text": "anything"}, headers=AUTH).json()
    assert body["labels"]["severe_toxic"]["flag"] is True
    assert body["labels"]["toxic"]["flag"] is True


def test_a_severe_probability_above_toxic_is_clamped_rather_than_served(client, session):
    """H22's probability half, and a live defect rather than a hypothetical one.

    `OneVsRestClassifier` fits the six labels independently, so `severe_toxic > toxic` comes
    out of the real estimator on ordinary input. `PredictionResponse` refuses that pair, so
    without `enforce_hierarchy` on the serving path the endpoint answers 500 to a benign
    comment. The clamp runs before `decide`, so the row, the flags and the response all carry
    the same coherent numbers.
    """
    response = client.post(
        "/predict", json={"text": "my home address is 221b baker street"}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["labels"]["severe_toxic"]["prob"] <= body["labels"]["toxic"]["prob"]
    stored = session.get(Prediction, body["request_id"])
    assert stored.prob_severe_toxic <= stored.prob_toxic
    assert stored.prob_severe_toxic == pytest.approx(body["labels"]["severe_toxic"]["prob"])
