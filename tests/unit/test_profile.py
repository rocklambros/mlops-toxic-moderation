from pathlib import Path

import pytest

from model.data.dedup import dedup
from model.data.load import load_raw
from model.data.profile import assert_label_hierarchy, profile, write_profile
from model.labels import LABELS

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def test_profile_counts_match_the_frame():
    clean = dedup(load_raw(FIXTURE))
    prof = profile(clean)
    assert prof.n_rows == len(clean)
    for label in LABELS:
        assert prof.label_counts[label] == int(clean[label].sum())
        assert prof.label_rates[label] == pytest.approx(prof.label_counts[label] / len(clean))


def test_cooccurrence_is_six_by_six_symmetric_with_counts_on_the_diagonal():
    clean = dedup(load_raw(FIXTURE))
    prof = profile(clean)
    assert prof.cooccurrence.shape == (6, 6)
    assert (prof.cooccurrence == prof.cooccurrence.T).all()
    for i, label in enumerate(LABELS):
        assert prof.cooccurrence[i, i] == prof.label_counts[label]


def test_label_hierarchy_assertion_catches_a_violation():
    df = load_raw(FIXTURE)
    assert_label_hierarchy(df)
    broken = df.copy()
    broken.loc[0, "severe_toxic"] = 1
    broken.loc[0, "toxic"] = 0
    with pytest.raises(AssertionError, match="severe_toxic <= toxic"):
        assert_label_hierarchy(broken)


def test_write_profile_emits_markdown_with_every_label_and_the_digest(tmp_path):
    clean = dedup(load_raw(FIXTURE))
    out = tmp_path / "data-profile.md"
    write_profile(clean, out, source=str(FIXTURE), raw_sha256="deadbeef")
    text = out.read_text()
    assert "deadbeef" in text
    for label in LABELS:
        assert f"`{label}`" in text
    assert "Co-occurrence" in text


def test_write_profile_refuses_a_corpus_that_breaks_the_hierarchy(tmp_path):
    df = load_raw(FIXTURE)
    df.loc[0, "severe_toxic"] = 1
    df.loc[0, "toxic"] = 0
    with pytest.raises(AssertionError):
        write_profile(df, tmp_path / "p.md", source="x", raw_sha256="y")
