"""Deterministic synthetic multi-label corpus for Phase 1 unit tests.

Real Jigsaw is 223,549 raw rows (212,510 after dedup, 180,633 in train) and lives outside the
repo (it is gitignored under data/raw/). These tests need a corpus that is small, seeded,
learnable, and carries the same shape of imbalance as the real thing, including a rare
`threat`-like label at a few percent.
"""

import numpy as np

from model.labels import LABELS

_CLEAN = (
    "thanks for the edit",
    "great work on the article",
    "i disagree politely",
    "nice sourcing here",
    "the weather is lovely",
    "please add a citation",
)
_CUES = {
    "toxic": "idiot",
    "severe_toxic": "vile",
    "obscene": "filth",
    "threat": "killyou",
    "insult": "moron",
    "identity_hate": "yourkind",
}
_RATES = {
    "toxic": 0.30,
    "severe_toxic": 0.10,
    "obscene": 0.20,
    "threat": 0.04,
    "insult": 0.25,
    "identity_hate": 0.08,
}


def make_corpus(n: int = 800, seed: int = 0) -> tuple[list[str], np.ndarray]:
    rng = np.random.default_rng(seed)
    texts: list[str] = []
    y = np.zeros((n, len(LABELS)), dtype=int)
    for i in range(n):
        parts = [_CLEAN[i % len(_CLEAN)], f"comment {i}"]
        for j, label in enumerate(LABELS):
            if rng.random() < _RATES[label]:
                y[i, j] = 1
                parts.append(_CUES[label])
        rng.shuffle(parts)
        texts.append(" ".join(parts))
    return texts, y
