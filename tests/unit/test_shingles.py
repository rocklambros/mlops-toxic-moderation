from datasketch import MinHash

from model.data.shingles import (
    NUM_PERM,
    cache_stats,
    clear_cache,
    jaccard,
    shingle_set,
    signature,
)


def test_shingles_of_a_short_text_are_the_whole_text():
    assert shingle_set("abc") == frozenset({"abc"})
    assert shingle_set("") == frozenset()


def test_shingles_slide_by_one_character():
    assert shingle_set("abcdef") == frozenset({"abcde", "bcdef"})


def test_jaccard_boundaries():
    assert jaccard(frozenset(), frozenset()) == 1.0
    assert jaccard(frozenset({"a"}), frozenset()) == 0.0
    assert jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0
    assert jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == 1 / 3


def test_batched_signature_equals_the_per_shingle_loop():
    text = "the quick brown fox jumps over the lazy dog again and again"
    reference = MinHash(num_perm=NUM_PERM)
    for shingle in sorted(shingle_set(text)):
        reference.update(shingle.encode("utf-8"))
    assert (signature(text).hashvalues == reference.hashvalues).all()


def test_signature_is_cached_by_normalized_text():
    clear_cache()
    first = signature("a repeated comment body")
    before = cache_stats()
    second = signature("a repeated comment body")
    after = cache_stats()
    assert first is second
    assert after.hits == before.hits + 1
    assert after.misses == before.misses


def test_clear_cache_resets_counters():
    signature("something")
    clear_cache()
    assert cache_stats() == type(cache_stats())(hits=0, misses=0)
