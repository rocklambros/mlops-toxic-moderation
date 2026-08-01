"""Safe model loading. This is the trust boundary between the registry and the instance.

The registry hands an artifact over the network into a process holding the EC2 instance
profile, so a poisoned artifact is remote code execution against the account. Two independent
controls close that path, and both fail closed:

1. Provenance and integrity. The expected digest is read from the git-committed model card
   and cross-checked against the MODEL_DIGEST environment variable before anything is
   deserialized, so the artifact and its expected digest do not share one trust domain.
2. Deserialization under an explicit static allowlist. Asking the artifact which types it
   needs and then trusting the answer is not a control - it trusts whatever the attacker put
   in the file. The allowlist below is the answer, fixed in advance, and the name of that
   skops discovery helper is deliberately absent from this module so that
   `test_loader_never_trusts_whatever_the_artifact_contains` can enforce its absence by
   substring.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from skops.io import load as skops_load
from skops.io.exceptions import UntrustedTypesFoundException

from backend.config import Settings
from backend.model_card import read_expected_digest

# EXPLICIT STATIC ALLOWLIST. Every entry is a string literal. Widening it is a reviewable
# diff on this line, never a runtime decision. numpy and scipy containers are handled by
# skops' own persistence protocols and deliberately do not appear here.
TRUSTED_TYPES: tuple[str, ...] = (
    "sklearn.calibration.CalibratedClassifierCV",
    "sklearn.calibration._CalibratedClassifier",
    "sklearn.calibration._SigmoidCalibration",
    "sklearn.feature_extraction.text.TfidfTransformer",
    "sklearn.feature_extraction.text.TfidfVectorizer",
    "sklearn.isotonic.IsotonicRegression",
    "sklearn.linear_model._logistic.LogisticRegression",
    "sklearn.multiclass.OneVsRestClassifier",
    "sklearn.multiclass._ConstantPredictor",
    "sklearn.pipeline.FeatureUnion",
    "sklearn.pipeline.Pipeline",
    "sklearn.preprocessing._data.Normalizer",
    "sklearn.preprocessing._label.LabelBinarizer",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ModelIntegrityError(RuntimeError):
    """Provenance or integrity could not be established. Never recoverable at runtime."""


@dataclass
class LoadedModel:
    model_version: str
    public_version: str
    estimator: object = field(repr=False)

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.estimator.predict_proba(texts), dtype=float)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def load_model(
    artifact_path: Path,
    expected_sha256: str,
    artifact_name: str,
    registry_version: int,
) -> LoadedModel:
    if not _HEX64.match(expected_sha256 or ""):
        raise ModelIntegrityError(
            "expected_sha256 must be 64-character lowercase hex; refusing to load"
        )
    actual = sha256_file(artifact_path)
    if not hmac.compare_digest(actual, expected_sha256):
        raise ModelIntegrityError(
            f"artifact digest mismatch: expected {expected_sha256}, computed {actual}"
        )
    try:
        estimator = skops_load(artifact_path, trusted=list(TRUSTED_TYPES))
    except UntrustedTypesFoundException as exc:
        raise ModelIntegrityError(f"artifact contains untrusted types: {exc}") from exc
    return LoadedModel(
        model_version=f"{artifact_name}:v{registry_version}@sha256:{expected_sha256}",
        public_version=f"{artifact_name}:v{registry_version}",
        estimator=estimator,
    )


def load_from_settings(settings: Settings) -> LoadedModel:
    card_digest = read_expected_digest(settings.model_card_path)
    if not hmac.compare_digest(card_digest, settings.model_digest):
        raise ModelIntegrityError(
            "MODEL_DIGEST does not match the digest of record in the model card; "
            "the registry and the repository disagree about which artifact is Production"
        )
    return load_model(
        settings.model_artifact_path,
        card_digest,
        artifact_name=settings.artifact_name,
        registry_version=settings.model_registry_version,
    )
