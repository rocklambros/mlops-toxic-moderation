import ast
from pathlib import Path

import pytest
import skops.io as sio
from sklearn.preprocessing import FunctionTransformer

from backend.config import load_settings
from backend.model_loader import (
    TRUSTED_TYPES,
    LoadedModel,
    ModelIntegrityError,
    load_from_settings,
    load_model,
    sha256_file,
)
from tests.fixtures.make_model import build_demo_artifact

SOURCE = Path("backend/model_loader.py")


def _shout(text):  # a plain function: exactly the payload the allowlist exists to refuse
    return [t.upper() for t in text]


@pytest.fixture(scope="module")
def artifact(tmp_path_factory):
    return build_demo_artifact(tmp_path_factory.mktemp("artifact") / "toxic-clf.skops")


def test_trusted_types_is_a_literal_tuple_of_strings():
    """REG-6.3d. The control is only real if it cannot be widened at deploy time. A tuple of
    string literals is auditable in a diff; a computed list is not."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    assigns = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "TRUSTED_TYPES"
    ]
    assert len(assigns) == 1, "TRUSTED_TYPES must be assigned exactly once at module level"
    literal = assigns[0].value
    assert isinstance(literal, ast.Tuple), "TRUSTED_TYPES must be a tuple literal"
    assert literal.elts, "TRUSTED_TYPES must not be empty"
    for element in literal.elts:
        assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
            "every TRUSTED_TYPES entry must be a string literal, not a call, name, or splat"
        )
    assert tuple(element.value for element in literal.elts) == TRUSTED_TYPES, (
        "the literal in the source is not what the module exports at runtime"
    )


def test_loader_never_trusts_whatever_the_artifact_contains():
    source = SOURCE.read_text(encoding="utf-8")
    assert "get_untrusted_types" not in source, (
        "get_untrusted_types()-then-trust-all silently voids the control"
    )
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "trusted":
            assert not (
                isinstance(node.value, ast.Constant) and node.value.value is True
            ), "trusted=True disables the allowlist"


def test_type_outside_the_allowlist_is_rejected(tmp_path):
    """The poisoned-artifact path: an arbitrary callable inside the artifact is remote code
    execution in a process that holds the instance role."""
    payload = tmp_path / "payload.skops"
    sio.dump(FunctionTransformer(func=_shout), payload)
    with pytest.raises(ModelIntegrityError, match="untrusted"):
        load_model(payload, sha256_file(payload), artifact_name="toxic-clf", registry_version=1)


def test_digest_mismatch_fails_closed(artifact):
    path, _ = artifact
    with pytest.raises(ModelIntegrityError, match="digest mismatch"):
        load_model(path, "0" * 64, artifact_name="toxic-clf", registry_version=3)


def test_malformed_expected_digest_fails_closed(artifact):
    path, _ = artifact
    with pytest.raises(ModelIntegrityError, match="64-character"):
        load_model(path, "not-a-digest", artifact_name="toxic-clf", registry_version=3)


def test_tampered_artifact_fails_closed(artifact, tmp_path):
    path, digest = artifact
    tampered = tmp_path / "tampered.skops"
    tampered.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(ModelIntegrityError, match="digest mismatch"):
        load_model(tampered, digest, artifact_name="toxic-clf", registry_version=3)


def test_valid_artifact_loads_and_scores(artifact):
    path, digest = artifact
    model = load_model(path, digest, artifact_name="toxic-clf", registry_version=3)
    assert isinstance(model, LoadedModel)
    probabilities = model.predict_proba(["you are an idiot", "have a nice day"])
    assert probabilities.shape == (2, 6)
    assert ((probabilities >= 0.0) & (probabilities <= 1.0)).all()


def test_digest_of_record_comes_from_the_committed_model_card(artifact, tmp_path):
    """TAIL-1. A MODEL_DIGEST that disagrees with the card means the two trust domains
    disagree, and the only safe response is to refuse to start."""
    path, digest = artifact
    card = tmp_path / "MODEL_CARD.md"
    card.write_text(f"- MODEL_DIGEST: sha256:{digest}\n", encoding="utf-8")
    env = {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/toxic",
        "DEMO_API_KEY": "k",
        "MODEL_ARTIFACT_PATH": str(path),
        "MODEL_CARD_PATH": str(card),
        "MODEL_DIGEST": digest,
        "MODEL_REGISTRY_VERSION": "3",
        "THRESHOLDS_PATH": "unused.json",
    }
    assert load_from_settings(load_settings(env)).public_version == "toxic-clf:v3"

    disagreeing = {**env, "MODEL_DIGEST": "b" * 64}
    with pytest.raises(ModelIntegrityError, match="model card"):
        load_from_settings(load_settings(disagreeing))
