"""The challenger artifact is refused unless it is the model this system agreed to run.

Delivery spec section 6.2 names two failure modes and this suite pins both. HF Trainer
defaults to softmax cross-entropy on a six-column target unless
`problem_type="multi_label_classification"` is set, which trains the wrong objective and
still produces an artifact that loads, scores, and looks fine. And int8 dynamic
quantization changes outputs, so parity has to be verified where the model is used rather
than only where it was exported.

The parity gate is not hypothetical. Phase 1's first int8 export failed it at
max |logit delta| 2.7206 against a 0.25 tolerance, worst label `identity_hate`, because the
quantizer ran per-tensor and targeted the exporting host's architecture rather than the
arm64 serving fleet. That is what a load-time parity check is for.
"""

import ast
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from model.labels import LABELS
from rescorer.challenger import (
    MAX_PARITY_ATOL,
    Challenger,
    ChallengerContractError,
    load_challenger,
)

OK = Path("tests/fixtures/challenger_ok")
BAD_OBJECTIVE = Path("tests/fixtures/challenger_bad_objective")
REFERENCE = np.array(json.loads((OK / "parity.json").read_text())["logits"], dtype=np.float32)
DIGEST = hashlib.sha256((OK / "model.onnx").read_bytes()).hexdigest()

# Every heavy dependency of the re-scorer. `rescorer.challenger` must import none of them at
# module scope, or the cut-line (C8) stops being severable: the rest of Phase 3 runs on a box
# where none of these is installed.
HEAVY = ("onnxruntime", "tokenizers", "torch", "transformers", "optimum")


class FakeSession:
    def __init__(self, logits: np.ndarray):
        self.logits = logits
        self.calls = 0

    def run(self, input_ids, attention_mask):
        self.calls += 1
        rows = len(input_ids)
        return np.resize(self.logits, (rows, len(LABELS))).astype(np.float32)


class FakeTokenizer:
    def encode(self, texts):
        ids = [[1] * 8 for _ in texts]
        mask = [[1] * 8 for _ in texts]
        return ids, mask


def _load(directory=OK, digest=DIGEST, logits=REFERENCE, **kwargs):
    return load_challenger(
        directory, digest, session=FakeSession(logits), tokenizer=FakeTokenizer(), **kwargs
    )


def _staged(tmp_path: Path) -> Path:
    """A writable copy of the conforming artifact.

    Every negative case mutates a copy under tmp_path. Staging into the committed fixture
    directory -- which is what this task's plan said to do for the bad-objective case --
    writes two untracked files into the repository on every test run.
    """
    staged = tmp_path / "artifact"
    shutil.copytree(OK, staged)
    return staged


def test_a_conforming_artifact_loads():
    challenger = _load()
    probs = challenger.predict_proba(["a", "b", "c"])
    assert probs.shape == (3, len(LABELS))
    assert probs.min() >= 0.0 and probs.max() <= 1.0


def test_probabilities_are_sigmoid_not_softmax():
    """Six independent labels. A softmax would make the row sum to 1 and would silently
    couple labels that the training objective treats as independent."""
    challenger = _load()
    row = challenger.predict_proba(["you are an idiot"])[0]
    assert row.sum() > 1.0
    # expit(2.10) for the first reference row's `toxic` logit.
    assert row[0] == pytest.approx(0.891, abs=1e-3)


def test_wrong_digest_fails_closed():
    with pytest.raises(ChallengerContractError, match="sha256"):
        _load(digest="0" * 64)


def test_a_missing_model_file_fails_closed(tmp_path):
    staged = _staged(tmp_path)
    (staged / "model.onnx").unlink()
    with pytest.raises(ChallengerContractError, match="not found"):
        _load(directory=staged)


def test_softmax_objective_is_refused(tmp_path):
    """Delivery spec section 6.2: HF Trainer defaults to softmax cross-entropy on a
    six-column target, which trains the wrong objective and still produces an artifact."""
    staged = _staged(tmp_path)
    shutil.copy(BAD_OBJECTIVE / "config.json", staged / "config.json")
    with pytest.raises(ChallengerContractError, match="multi_label_classification"):
        _load(directory=staged)


def test_missing_problem_type_is_refused(tmp_path):
    staged = _staged(tmp_path)
    config = json.loads((staged / "config.json").read_text())
    config.pop("problem_type")
    (staged / "config.json").write_text(json.dumps(config))
    with pytest.raises(ChallengerContractError, match="problem_type"):
        _load(directory=staged)


def test_label_order_mismatch_is_refused(tmp_path):
    staged = _staged(tmp_path)
    config = json.loads((staged / "config.json").read_text())
    config["id2label"]["0"], config["id2label"]["1"] = (
        config["id2label"]["1"],
        config["id2label"]["0"],
    )
    (staged / "config.json").write_text(json.dumps(config))
    with pytest.raises(ChallengerContractError, match="id2label"):
        _load(directory=staged)


def test_every_single_label_transposition_is_refused(tmp_path):
    """A transposition anywhere in the six is silent and catastrophic: the array shape is
    right, the probabilities are in range, and every label is misattributed. Checking one
    swap would leave five permutations that a positional check might not cover."""
    for first in range(len(LABELS)):
        for second in range(first + 1, len(LABELS)):
            staged = _staged(tmp_path / f"swap_{first}_{second}")
            config = json.loads((staged / "config.json").read_text())
            config["id2label"][str(first)], config["id2label"][str(second)] = (
                config["id2label"][str(second)],
                config["id2label"][str(first)],
            )
            (staged / "config.json").write_text(json.dumps(config))
            with pytest.raises(ChallengerContractError, match="id2label"):
                _load(directory=staged)


def test_a_short_id2label_is_refused(tmp_path):
    """Five heads mapped, six expected. `.get()` on the missing index yields None, which
    must not compare equal to a label name by accident."""
    staged = _staged(tmp_path)
    config = json.loads((staged / "config.json").read_text())
    config["id2label"].pop("5")
    (staged / "config.json").write_text(json.dumps(config))
    with pytest.raises(ChallengerContractError, match="id2label"):
        _load(directory=staged)


def test_int8_logit_drift_is_caught_at_load():
    """The parity fixture is checked when the worker starts, not only at export time, so a
    re-quantized or corrupted artifact cannot silently change the challenger's opinion."""
    drifted = REFERENCE + 0.6
    with pytest.raises(ChallengerContractError, match="parity"):
        _load(logits=drifted)


def test_the_parity_failure_names_the_worst_label_and_the_measured_delta():
    """Phase 1's failed export was diagnosable because the gate said which label and by how
    much. A bare "parity failed" sends the operator back to re-run the export to find out."""
    drifted = REFERENCE.copy()
    drifted[0, LABELS.index("identity_hate")] += 2.7206
    with pytest.raises(ChallengerContractError) as caught:
        _load(logits=drifted)
    assert "identity_hate" in str(caught.value)
    assert "2.72" in str(caught.value)


def test_parity_tolerance_admits_ordinary_quantization_noise():
    noise = REFERENCE + np.float32(0.02)
    challenger = _load(logits=noise)
    assert challenger.predict_proba(["a"]).shape == (1, len(LABELS))


def test_an_artifact_may_not_widen_its_own_parity_tolerance(tmp_path):
    """The gate is worthless if the thing being gated sets the threshold. Phase 1's failed
    export missed by 2.7206; an artifact shipping `"atol": 3.0` would have walked through."""
    staged = _staged(tmp_path)
    parity = json.loads((staged / "parity.json").read_text())
    parity["atol"] = 3.0
    (staged / "parity.json").write_text(json.dumps(parity))
    with pytest.raises(ChallengerContractError, match="atol"):
        _load(directory=staged, logits=REFERENCE + 2.5)


def test_an_artifact_may_tighten_its_own_parity_tolerance(tmp_path):
    """Mirror of the test above. A cap that refused every declared value would pass it and
    would also refuse a float32 export that legitimately asks for a stricter bound."""
    staged = _staged(tmp_path)
    parity = json.loads((staged / "parity.json").read_text())
    parity["atol"] = 0.001
    (staged / "parity.json").write_text(json.dumps(parity))
    _load(directory=staged, logits=REFERENCE)
    with pytest.raises(ChallengerContractError, match="parity"):
        _load(directory=staged, logits=REFERENCE + 0.01)


def test_an_artifact_with_no_declared_tolerance_gets_the_capped_default(tmp_path):
    staged = _staged(tmp_path)
    parity = json.loads((staged / "parity.json").read_text())
    parity.pop("atol")
    (staged / "parity.json").write_text(json.dumps(parity))
    _load(directory=staged, logits=REFERENCE + np.float32(MAX_PARITY_ATOL / 2))
    with pytest.raises(ChallengerContractError, match="parity"):
        _load(directory=staged, logits=REFERENCE + np.float32(MAX_PARITY_ATOL * 2))


def test_missing_parity_fixture_is_refused(tmp_path):
    staged = _staged(tmp_path)
    (staged / "parity.json").unlink()
    with pytest.raises(ChallengerContractError, match="parity.json"):
        _load(directory=staged)


def test_a_parity_fixture_of_the_wrong_shape_is_refused(tmp_path):
    staged = _staged(tmp_path)
    parity = json.loads((staged / "parity.json").read_text())
    parity["texts"] = parity["texts"][:1]
    (staged / "parity.json").write_text(json.dumps(parity))
    with pytest.raises(ChallengerContractError, match="shape"):
        _load(directory=staged)


def test_an_empty_parity_fixture_is_refused(tmp_path):
    """Zero reference rows makes `max(abs(diff))` an empty reduction, and the cheapest way
    to defeat this gate is to ship a parity file with nothing in it."""
    staged = _staged(tmp_path)
    parity = json.loads((staged / "parity.json").read_text())
    parity["texts"] = []
    parity["logits"] = []
    (staged / "parity.json").write_text(json.dumps(parity))
    with pytest.raises(ChallengerContractError, match="reference"):
        _load(directory=staged)


def test_a_challenger_that_returns_the_wrong_width_is_refused_at_load():
    class WrongWidth:
        def run(self, input_ids, attention_mask):
            return np.zeros((len(input_ids), len(LABELS) - 1), dtype=np.float32)

    with pytest.raises(ChallengerContractError, match="shape"):
        load_challenger(OK, DIGEST, session=WrongWidth(), tokenizer=FakeTokenizer())


@pytest.mark.parametrize(
    "returned",
    [
        np.zeros((1, len(LABELS) - 1), dtype=np.float32),
        np.zeros((1, len(LABELS) + 1), dtype=np.float32),
        np.zeros(len(LABELS), dtype=np.float32),
        np.zeros((1, 1, len(LABELS)), dtype=np.float32),
    ],
    ids=["too-narrow", "too-wide", "one-dimensional", "three-dimensional"],
)
def test_every_score_is_shape_checked_not_only_the_parity_batch(returned):
    """The load-time parity check happens once. A session that drifts afterwards -- a
    re-exported graph with a different output rank, a provider that squeezes the batch
    dimension away -- would otherwise be handed straight to `probs_to_dict`, which sees a
    length and not a meaning."""

    class Drifting:
        def run(self, input_ids, attention_mask):
            return returned

    challenger = Challenger(Drifting(), FakeTokenizer())
    with pytest.raises(ChallengerContractError, match="shape"):
        challenger.predict_proba(["a"])


def test_the_quantized_export_is_a_configuration_change_not_a_rewrite(tmp_path):
    """Phase 1's int8 export is named `model_quantized.onnx`, and the currently-valid
    artifact is the float32 `model.onnx`. Swapping one for the other must be a filename and
    a digest, not a code change (delivery spec section 6.2's cut-line, premortem C8)."""
    staged = _staged(tmp_path)
    (staged / "model.onnx").rename(staged / "model_quantized.onnx")
    with pytest.raises(ChallengerContractError, match="not found"):
        _load(directory=staged)
    challenger = _load(directory=staged, model_filename="model_quantized.onnx")
    assert challenger.predict_proba(["a"]).shape == (1, len(LABELS))


def _rescorer_modules() -> list[str]:
    found = sorted(
        f"rescorer.{path.stem}"
        for path in Path("rescorer").glob("*.py")
        if path.stem != "__init__"
    )
    assert found, "the severability scan found no modules to scan"
    return found


def test_no_rescorer_module_declares_a_heavy_import_at_module_scope():
    """C8 severability, asserted against the source rather than against sys.modules.

    A sys.modules check alone cannot fail on a machine where onnxruntime is not installed --
    which is every machine in the unit job -- so it certifies nothing there. This one reads
    the import statements and fails whether or not the package is present.
    """
    for module in _rescorer_modules():
        path = Path(module.replace(".", "/") + ".py")
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert name.split(".")[0] not in HEAVY, f"{path} imports {name} at module scope"


def _probe(imports: list[str], forbidden: tuple[str, ...]) -> subprocess.CompletedProcess:
    code = (
        f"import sys; import {', '.join(imports)}; "
        f"leaked = sorted(set({forbidden!r}) & set(sys.modules)); "
        "assert not leaked, leaked; print('ok')"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def test_importing_the_rescorer_pulls_in_no_inference_runtime():
    """The property the AST check is a proxy for, measured in a fresh interpreter."""
    result = _probe(_rescorer_modules(), HEAVY)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_the_severability_probe_would_notice_a_module_scope_import():
    """Non-vacuity for the test above: the probe has to be able to fail. `json` really is
    imported at module scope here, so it stands in for a dependency that is genuinely
    present -- a probe that only ever named absent packages could not fail either."""
    result = _probe(["rescorer.challenger"], ("json",))
    assert result.returncode != 0
    assert "json" in result.stderr
