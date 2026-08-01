"""Load the DistilBERT ONNX challenger, and refuse it unless it is what it claims.

Four gates, all fail-closed:

1. SHA-256 against a digest recorded independently in the model card and the W&B version
   alias, never derived from the artifact being loaded.
2. `problem_type == "multi_label_classification"` and `id2label` in exactly LABELS order.
   HF Trainer silently defaults to softmax cross-entropy on a six-column target, which
   trains the wrong objective; and a permuted `id2label` mislabels every probability while
   producing a perfectly valid-looking (n, 6) array.
3. Logit parity against a fixture shipped with the artifact. Quantization changes outputs,
   so parity is verified where the model is used, not only where it was exported.
4. The parity tolerance the artifact declares may only be *tighter* than MAX_PARITY_ATOL.
   An artifact that sets its own threshold is not gated by one.

Gate 3 is not hypothetical. Phase 1's first int8 export failed it at max |logit delta|
2.7206 against a 0.25 tolerance, worst label `identity_hate`, because the quantizer ran
per-tensor and targeted the exporting host's x86 architecture rather than the arm64 (t4g
Graviton) serving fleet. The float32 export passed. Which file is loaded is a filename and
a digest -- `model_filename` here, `CHALLENGER_MODEL_FILE` in the worker -- so promoting a
re-exported int8 artifact is configuration, not a code change.

onnxruntime and tokenizers are imported lazily inside the concrete adapters, so importing
this module costs nothing on a machine where the re-scorer has been cut (premortem C8).
"""

import hashlib
import hmac
import json
from pathlib import Path

import numpy as np
from scipy.special import expit

from model.labels import LABELS

EXPECTED_PROBLEM_TYPE = "multi_label_classification"
DEFAULT_MODEL_FILENAME = "model.onnx"
# The ceiling on the parity tolerance, in logit units. An artifact may declare a smaller
# `atol` in its parity fixture; it may not declare a larger one, because then the thing
# being gated would be choosing the gate.
MAX_PARITY_ATOL = 0.05


class ChallengerContractError(RuntimeError):
    """The artifact is not the model this system agreed to run."""


class Challenger:
    def __init__(self, session, tokenizer):
        self._session = session
        self._tokenizer = tokenizer

    def logits(self, texts: list[str]) -> np.ndarray:
        input_ids, attention_mask = self._tokenizer.encode(texts)
        raw = np.asarray(self._session.run(input_ids, attention_mask), dtype=np.float32)
        if raw.ndim != 2 or raw.shape[1] != len(LABELS):
            raise ChallengerContractError(
                f"challenger returned shape {raw.shape}, expected (n, {len(LABELS)})"
            )
        return raw

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        # Sigmoid, not softmax: the labels are independent, which is the same fact that
        # problem_type encodes at training time.
        return expit(self.logits(texts)).astype(np.float32)


def _verify_digest(model_path: Path, expected_sha256: str) -> None:
    if not model_path.is_file():
        raise ChallengerContractError(f"{model_path} not found")
    actual = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256.lower()):
        raise ChallengerContractError(
            f"sha256 mismatch for {model_path}: expected {expected_sha256}, got {actual}"
        )


def _verify_config(artifact_dir: Path) -> None:
    config_path = artifact_dir / "config.json"
    if not config_path.is_file():
        raise ChallengerContractError(f"{config_path} not found")
    config = json.loads(config_path.read_text())

    problem_type = config.get("problem_type")
    if problem_type != EXPECTED_PROBLEM_TYPE:
        raise ChallengerContractError(
            f"config.json declares problem_type={problem_type!r}; this system requires "
            f"{EXPECTED_PROBLEM_TYPE!r}. A model trained without it optimised softmax "
            "cross-entropy over six mutually exclusive classes, which is the wrong objective."
        )

    id2label = config.get("id2label") or {}
    ordered = tuple(id2label.get(str(index)) for index in range(len(LABELS)))
    if ordered != LABELS:
        raise ChallengerContractError(
            f"config.json id2label is {ordered}, expected {LABELS} in that exact order"
        )


def _parity_tolerance(declared) -> float:
    if declared is None:
        return MAX_PARITY_ATOL
    atol = float(declared)
    if atol > MAX_PARITY_ATOL:
        raise ChallengerContractError(
            f"parity fixture declares atol {atol:g}, above the ceiling {MAX_PARITY_ATOL:g}. "
            "An artifact may tighten its own tolerance and may not widen it."
        )
    return atol


def _check_parity(challenger: Challenger, parity: dict) -> None:
    reference = np.asarray(parity["logits"], dtype=np.float32)
    if reference.size == 0:
        raise ChallengerContractError(
            "parity fixture carries no reference logits; an empty fixture gates nothing"
        )
    atol = _parity_tolerance(parity.get("atol"))
    observed = challenger.logits(list(parity["texts"]))
    if observed.shape != reference.shape:
        raise ChallengerContractError(
            f"parity fixture shape {reference.shape} != observed {observed.shape}"
        )
    deltas = np.abs(observed - reference)
    worst = float(deltas.max())
    if worst > atol:
        label = LABELS[int(np.unravel_index(int(deltas.argmax()), deltas.shape)[1])]
        raise ChallengerContractError(
            f"logit parity failed: max |delta| = {worst:.4f} > atol {atol:g}, "
            f"worst label {label}"
        )


def load_challenger(
    artifact_dir: Path,
    expected_sha256: str,
    *,
    model_filename: str = DEFAULT_MODEL_FILENAME,
    session=None,
    tokenizer=None,
) -> Challenger:
    artifact_dir = Path(artifact_dir)
    model_path = artifact_dir / model_filename
    _verify_digest(model_path, expected_sha256)
    _verify_config(artifact_dir)

    parity_path = artifact_dir / "parity.json"
    if not parity_path.is_file():
        raise ChallengerContractError(
            f"{parity_path} not found; the ONNX export must ship reference logits"
        )
    parity = json.loads(parity_path.read_text())

    if session is None or tokenizer is None:
        from rescorer.onnx_session import build_session, build_tokenizer

        session = session or build_session(model_path)
        tokenizer = tokenizer or build_tokenizer(artifact_dir / "tokenizer.json")

    challenger = Challenger(session, tokenizer)
    _check_parity(challenger, parity)
    return challenger
