from pathlib import Path

import pytest

from backend.model_card import DIGEST_LINE, read_expected_digest

DIGEST = "3f" * 32
CARD = f"""# Model Card: toxic-clf

## Artifact digest of record

- MODEL_ARTIFACT: toxic-clf
- MODEL_REGISTRY_VERSION: 3
- MODEL_DIGEST: sha256:{DIGEST}
"""


def test_reads_the_digest_from_a_well_formed_card(tmp_path):
    card = tmp_path / "MODEL_CARD.md"
    card.write_text(CARD, encoding="utf-8")
    assert read_expected_digest(card) == DIGEST


def test_missing_digest_line_raises(tmp_path):
    card = tmp_path / "MODEL_CARD.md"
    card.write_text("# Model Card\n\nNo digest here.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="MODEL_DIGEST"):
        read_expected_digest(card)


def test_conflicting_digests_raise(tmp_path):
    card = tmp_path / "MODEL_CARD.md"
    card.write_text(CARD + f"- MODEL_DIGEST: sha256:{'ab' * 32}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting"):
        read_expected_digest(card)


def test_truncated_digest_is_not_accepted(tmp_path):
    card = tmp_path / "MODEL_CARD.md"
    card.write_text("- MODEL_DIGEST: sha256:abc123\n", encoding="utf-8")
    with pytest.raises(ValueError, match="MODEL_DIGEST"):
        read_expected_digest(card)


def test_the_repositorys_own_model_card_declares_a_digest():
    """TAIL-1. The digest of record must be in the repository, under branch protection, so
    that forging it requires compromising git as well as the registry credential."""
    assert DIGEST_LINE.search(Path("MODEL_CARD.md").read_text(encoding="utf-8"))
