import re

import pytest

from backend.model_loader import load_model, sha256_file
from tests.fixtures.make_model import build_demo_artifact

HEX64 = re.compile(r"[0-9a-f]{64}")


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    path, digest = build_demo_artifact(tmp_path_factory.mktemp("artifact") / "toxic-clf.skops")
    assert digest == sha256_file(path)
    return load_model(path, digest, artifact_name="toxic-clf", registry_version=3)


def test_public_version_carries_no_digest(model):
    """H14. Delivery spec section 6.3 strips the digest from /health specifically so the
    exact model cannot be fingerprinted by an attacker crafting evasions. Returning it on
    every /predict response makes that control inert."""
    assert model.public_version == "toxic-clf:v3"
    assert "sha256" not in model.public_version
    assert not HEX64.search(model.public_version)


def test_full_version_is_retained_for_logs_and_the_database(model):
    assert model.model_version.startswith("toxic-clf:v3@sha256:")
    assert HEX64.search(model.model_version)


def test_the_two_labels_are_distinct(model):
    assert model.public_version != model.model_version
    assert model.model_version.startswith(model.public_version)
