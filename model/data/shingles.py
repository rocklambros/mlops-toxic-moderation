"""Shingles, exact Jaccard, and a process-local MinHash signature cache.

The cache exists because `make data` runs the MinHash stage twice -- once in dedup, once
in the leakage gate -- and MinHash is the dominant cost on the real corpus. Keyed on the
normalized text, so the gate gets a ~100% hit rate on a corpus dedup already signed.

Only signatures are cached. Shingle sets are recomputed on demand because caching ~400
short strings per row for 160k rows costs gigabytes, while recomputing one is microseconds.
"""

from dataclasses import dataclass

from datasketch import MinHash

SHINGLE_K = 5
NUM_PERM = 128

_CACHE: dict[str, MinHash] = {}
_HITS = 0
_MISSES = 0


@dataclass(frozen=True)
class CacheStats:
    hits: int
    misses: int


def shingle_set(text: str, k: int = SHINGLE_K) -> frozenset[str]:
    """Character k-shingles. Texts shorter than k become a single whole-text shingle."""
    if len(text) <= k:
        return frozenset({text}) if text else frozenset()
    return frozenset(text[i : i + k] for i in range(len(text) - k + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Exact set Jaccard. Two empty sets are identical, not undefined."""
    if not a and not b:
        return 1.0
    union = len(a | b)
    return 0.0 if union == 0 else len(a & b) / union


def signature(norm_text: str, num_perm: int = NUM_PERM) -> MinHash:
    """Cached MinHash over the shingles of an ALREADY-NORMALIZED text."""
    global _HITS, _MISSES
    cached = _CACHE.get(norm_text)
    if cached is not None:
        _HITS += 1
        return cached
    _MISSES += 1
    m = MinHash(num_perm=num_perm)
    shingles = shingle_set(norm_text)
    if shingles:
        m.update_batch([s.encode("utf-8") for s in sorted(shingles)])
    _CACHE[norm_text] = m
    return m


def cache_stats() -> CacheStats:
    return CacheStats(hits=_HITS, misses=_MISSES)


def clear_cache() -> None:
    global _HITS, _MISSES
    _CACHE.clear()
    _HITS = 0
    _MISSES = 0
