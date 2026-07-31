"""Two normalizers, deliberately different, in one file so the difference is visible.

`normalize` is the CORPUS normalizer. Dedup, the leakage gate, and `split_version` all
depend on it. It is FROZEN: changing it changes which rows collapse, which changes the
locked 15% test set, which invalidates every registered model. `test_corpus_normalizer_is_frozen`
pins a golden table so an edit cannot land silently.

`normalize_for_serving` is the SERVING normalizer, a strict superset: `normalize` plus
confusable/homoglyph folding plus a max-length cap. It is NOT used by dedup.
"""

import re
import unicodedata

CORPUS_NORMALIZER_ID = "nfkc-casefold-ws-v1"
SERVING_NORMALIZER_ID = "corpus-v1+confusables+cap5000"
MAX_INPUT_CHARS = 5000

_WS = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[​‌‍⁠﻿]")

_CONFUSABLES = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
        "х": "x", "у": "y", "і": "i", "ј": "j", "һ": "h",
        "ԁ": "d", "ο": "o", "α": "a", "ε": "e", "ρ": "p",
        "υ": "u", "χ": "x", "ɡ": "g", "ı": "i", "‐": "-",
        "‑": "-", "‒": "-", "–": "-", "—": "-", "‘": "'",
        "’": "'", "“": '"', "”": '"',
    }
)


def normalize(text: str) -> str:
    """FROZEN corpus normalizer. Do not edit without a new split_version."""
    text = unicodedata.normalize("NFKC", str(text)).casefold().strip()
    return _WS.sub(" ", text)


def normalize_for_serving(text: str) -> str:
    """Serving normalizer: corpus normalizer plus confusable folding and a length cap."""
    text = str(text)[:MAX_INPUT_CHARS]
    text = _ZERO_WIDTH.sub("", text)
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(_CONFUSABLES)
    text = "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))
    return _WS.sub(" ", unicodedata.normalize("NFKC", text).strip())
