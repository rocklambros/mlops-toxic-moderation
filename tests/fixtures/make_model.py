"""Deterministic tiny classical artifact, shaped exactly like the Phase 1 Production model.

Every label carries both classes so OneVsRestClassifier fits a real LogisticRegression per
label rather than a _ConstantPredictor; that keeps the fixture's trusted-type set equal to
the production one.
"""

import hashlib
from pathlib import Path

import numpy as np
import skops.io as sio
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import Pipeline

TEXTS = [
    "have a nice day friend",
    "thanks for the thoughtful edit",
    "you are an idiot",
    "what a moron you are",
    "f*ck this garbage",
    "i will kill you",
    "people of that group are subhuman",
    "you vile disgusting worthless scum",
]

# columns: toxic, severe_toxic, obscene, threat, insult, identity_hate
Y = np.array(
    [
        [0, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 0],
        [1, 0, 0, 0, 1, 0],
        [1, 0, 0, 0, 1, 0],
        [1, 0, 1, 0, 0, 0],
        [1, 1, 0, 1, 0, 0],
        [1, 0, 0, 0, 0, 1],
        [1, 1, 1, 1, 1, 1],
    ]
)


def build_pipeline() -> Pipeline:
    return Pipeline(
        [
            ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=1)),
            (
                "clf",
                OneVsRestClassifier(
                    CalibratedClassifierCV(
                        LogisticRegression(class_weight="balanced", solver="liblinear"),
                        cv=2,
                        method="sigmoid",
                    )
                ),
            ),
        ]
    )


def build_demo_artifact(path: Path) -> tuple[Path, str]:
    """Fit, dump with skops, and return (path, sha256 hex digest)."""
    pipeline = build_pipeline().fit(TEXTS, Y)
    sio.dump(pipeline, path)
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return Path(path), digest
