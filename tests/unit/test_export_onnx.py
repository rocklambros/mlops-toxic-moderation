"""The ONNX export is where column order can genuinely change, so this file is mostly about
proving that a re-ordering cannot pass.

The heavy end-to-end cases build a two-layer DistilBERT from a config, with a five-word
vocabulary written to disk, so they need no network and no pretrained download. They skip
when torch/optimum/onnxruntime are absent, which is the state of the aarch64 build box's
project venv; the parity arithmetic they wrap is tested unconditionally above them, because
the arithmetic is the part that decides whether an artifact ships.
"""

import functools
import json
import subprocess
import sys

import numpy as np
import pytest

from model.contract import probs_to_dict
from model.export_onnx import (
    FLAG_FLIP_FRAC,
    LOGIT_ATOL,
    PROB_ATOL,
    RANK_MARGIN,
    ExportError,
    LabelOrderError,
    ParityError,
    _load_thresholds,
    _sole_onnx,
    _thresh_vector,
    assert_label_order,
    assert_parity,
    compare_logits,
    default_quant_target,
    id2label_in_order,
    logits_to_dicts,
)
from model.labels import LABELS

LOOSE = dict(logit_atol=1e9, logit_mean_atol=1e9, prob_atol=1e9, flag_flip_frac=1.0)


def _logits(n=64, seed=0):
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, 2.0, size=(n, len(LABELS)))


# ---------------------------------------------------------------------------------------
# The array-to-dict seam
# ---------------------------------------------------------------------------------------


def test_logits_to_dicts_uses_the_authoritative_adapter_in_labels_order():
    row = np.array([[3.0, -3.0, -2.0, -1.0, 0.0, 1.0]])
    out = logits_to_dicts(row)[0]
    assert list(out.keys()) == list(LABELS)
    expected = probs_to_dict(1.0 / (1.0 + np.exp(-row[0])))
    assert out == pytest.approx(expected)
    assert out["toxic"] > 0.95 and out["severe_toxic"] < 0.05
    assert all(type(v) is float for v in out.values())


def test_probabilities_are_sigmoid_per_label_and_never_softmax():
    """Six independent labels. A row that softmaxed would sum to 1.0 and cap co-occurrence."""
    row = np.full((1, len(LABELS)), 4.0)
    out = logits_to_dicts(row)[0]
    assert sum(out.values()) == pytest.approx(len(LABELS) * 0.982, abs=0.01)
    assert all(value > 0.98 for value in out.values())


def test_logits_to_dicts_rejects_a_wrong_width_matrix():
    with pytest.raises(ValueError, match=r"expected \(n, 6\)"):
        logits_to_dicts(np.zeros((3, 5)))


# ---------------------------------------------------------------------------------------
# Label-order guards
# ---------------------------------------------------------------------------------------


def test_label_order_guard_accepts_labels_order_with_int_or_str_keys():
    assert_label_order({i: label for i, label in enumerate(LABELS)})
    assert_label_order({str(i): label for i, label in enumerate(LABELS)})
    assert id2label_in_order({str(i): la for i, la in enumerate(LABELS)}) == list(LABELS)


def test_label_order_guard_rejects_a_permuted_head():
    permuted = list(LABELS)
    permuted[3], permuted[4] = permuted[4], permuted[3]
    with pytest.raises(LabelOrderError, match="must be"):
        assert_label_order(dict(enumerate(permuted)))


def test_label_order_guard_rejects_the_huggingface_default_labels():
    """`LABEL_0..LABEL_5` means the head was built without id2label; every name is then lost."""
    with pytest.raises(LabelOrderError):
        assert_label_order({i: f"LABEL_{i}" for i in range(len(LABELS))})


def test_label_order_guard_reads_a_config_object_not_only_a_dict():
    class _Config:
        id2label = {i: label for i, label in enumerate(LABELS)}

    assert_label_order(_Config())


def test_label_order_guard_reads_a_parsed_config_json_with_string_keys():
    """The shape that actually lands on disk next to the .onnx file. JSON has no int keys."""
    config = {
        "architectures": ["DistilBertForSequenceClassification"],
        "id2label": {str(i): label for i, label in enumerate(LABELS)},
    }
    assert id2label_in_order(config) == list(LABELS)
    assert_label_order(config)

    config["id2label"] = {str(i): label for i, label in enumerate(sorted(LABELS))}
    with pytest.raises(LabelOrderError):
        assert_label_order(config)


# ---------------------------------------------------------------------------------------
# Parity: the happy path and the tolerance boundaries
# ---------------------------------------------------------------------------------------


def test_identical_logits_pass_with_zero_deltas():
    reference = _logits()
    report = compare_logits(reference, reference.copy())
    assert report.passed is True
    assert report.failures == ()
    assert report.max_abs_logit_delta == 0.0
    assert report.max_abs_prob_delta == 0.0
    assert report.n_flag_flips == 0
    assert report.n_argmax_disagreements == 0
    assert report.n_decisions == reference.size


def test_noise_inside_the_stated_tolerance_passes():
    reference = _logits()
    rng = np.random.default_rng(7)
    candidate = reference + rng.uniform(-0.02, 0.02, reference.shape)
    report = compare_logits(reference, candidate)
    assert report.passed is True
    assert report.max_abs_logit_delta < LOGIT_ATOL


def test_a_logit_shift_past_the_tolerance_fails_and_names_the_number():
    reference = np.zeros((8, len(LABELS)))
    candidate = reference.copy()
    candidate[0, 2] += LOGIT_ATOL + 0.01
    report = compare_logits(reference, candidate)
    assert report.passed is False
    assert any("max |logit delta|" in failure for failure in report.failures)
    with pytest.raises(ParityError, match="max \\|logit delta\\|"):
        assert_parity(report)


def test_the_mean_gate_catches_a_uniform_drift_no_single_logit_gate_would():
    """Every logit off by 0.10: under the max gate, over the mean gate. Bias, not noise."""
    reference = _logits()
    report = compare_logits(reference, reference + 0.10)
    assert report.max_abs_logit_delta < LOGIT_ATOL
    assert report.passed is False
    assert any("mean |logit delta|" in failure for failure in report.failures)


def test_the_probability_gate_is_separate_from_the_logit_gate():
    """Near zero the sigmoid is steepest, so a small logit move is a large probability move."""
    reference = np.zeros((4, len(LABELS)))
    candidate = reference.copy()
    candidate[:, 1] = 0.20  # ~0.05 in probability, over PROB_ATOL, under LOGIT_ATOL
    report = compare_logits(reference, candidate, logit_mean_atol=1e9)
    assert report.max_abs_logit_delta < LOGIT_ATOL
    assert report.max_abs_prob_delta > PROB_ATOL
    assert any("max |prob delta|" in failure for failure in report.failures)


def test_decision_flips_are_counted_against_the_tuned_thresholds_not_against_a_half():
    thresholds = {label: 0.9 for label in LABELS}
    reference = np.full((100, len(LABELS)), 2.5)      # p ~ 0.924, flagged at 0.9
    candidate = np.full((100, len(LABELS)), 2.0)      # p ~ 0.881, not flagged at 0.9
    lenient = compare_logits(reference, candidate, logit_mean_atol=1e9)
    strict = compare_logits(reference, candidate, thresholds=thresholds, logit_mean_atol=1e9)
    assert lenient.n_flag_flips == 0                  # nothing crosses 0.5
    assert strict.n_flag_flips == 100 * len(LABELS)
    assert strict.flag_flip_fraction > FLAG_FLIP_FRAC
    assert any("decisions flipped" in failure for failure in strict.failures)
    assert strict.per_label_flag_flips["threat"] == 100


def test_one_boundary_flip_on_a_small_sample_does_not_fail_the_gate():
    """At 12 rows a single flip is 1.4% of the decisions: a pure-fraction rule fails everything.

    The allowance is max(FLAG_FLIP_MIN, floor(frac x decisions)), and the report marks the
    gate under-powered so a passing small-sample export is not mistaken for evidence.
    """
    reference = np.full((12, len(LABELS)), -0.002)   # p just under 0.5: nothing flagged
    candidate = reference.copy()
    candidate[0, 0] = 0.002                          # crosses 0.5 by a hair: one decision moves
    report = compare_logits(reference, candidate)
    assert report.n_flag_flips == 1
    assert report.flag_flip_fraction > FLAG_FLIP_FRAC
    assert report.flag_flips_allowed == 1
    assert report.flag_gate_low_power is True
    assert report.passed is True

    candidate[1, 0] = 0.002
    two_flips = compare_logits(reference, candidate)
    assert two_flips.n_flag_flips == 2
    assert two_flips.passed is False


def test_a_large_sample_gets_a_proportional_allowance_and_is_not_low_power():
    reference = np.full((500, len(LABELS)), -0.002)
    candidate = reference.copy()
    candidate[:14, 0] = 0.002  # 14 flips against an allowance of floor(0.005 * 3000) = 15
    report = compare_logits(reference, candidate)
    assert report.n_decisions == 3000
    assert report.flag_flips_allowed == 15
    assert report.flag_gate_low_power is False
    assert report.passed is True

    candidate[:16, 0] = 0.002
    assert compare_logits(reference, candidate).passed is False


def test_missing_threshold_keys_are_refused_rather_than_defaulted():
    with pytest.raises(ValueError, match="missing"):
        compare_logits(_logits(), _logits(), thresholds={"toxic": 0.5})


def test_default_thresholds_are_one_half_for_every_label():
    assert list(_thresh_vector(None)) == [0.5] * len(LABELS)


# ---------------------------------------------------------------------------------------
# The transposition canary. This is the reason the module exists.
# ---------------------------------------------------------------------------------------


def test_a_column_swap_is_invisible_to_every_aggregate_statistic():
    """The premise: no macro metric, mean logit, or loss can see a permutation."""
    reference = _logits()
    swapped = reference[:, [0, 1, 2, 4, 3, 5]]  # threat <-> insult
    assert swapped.mean() == pytest.approx(reference.mean())
    assert swapped.std() == pytest.approx(reference.std())
    assert sorted(np.sort(swapped, axis=1).ravel()) == sorted(np.sort(reference, axis=1).ravel())
    # And the output contract's key check is order-blind, so it passes on the wrong mapping.
    assert set(logits_to_dicts(swapped)[0]) == set(logits_to_dicts(reference)[0])


def test_a_column_swap_fails_parity_on_the_argmax_gate_alone():
    """Loosened numeric gates, so only the ranking check can fire. It does."""
    reference = _logits(n=200, seed=3)
    swapped = reference[:, [0, 1, 2, 4, 3, 5]]
    report = compare_logits(reference, swapped, **LOOSE)
    assert report.n_rank_eligible > 0
    assert report.n_argmax_disagreements > 0
    assert report.passed is False
    assert any("column re-ordering" in failure for failure in report.failures)
    with pytest.raises(ParityError, match="column re-ordering"):
        assert_parity(report)


def test_a_full_column_rotation_fails_parity():
    reference = _logits(n=200, seed=5)
    rotated = reference[:, [5, 0, 1, 2, 3, 4]]
    assert compare_logits(reference, rotated, **LOOSE).passed is False


def test_honest_quantization_noise_never_trips_the_ranking_gate():
    """RANK_MARGIN > PROB_ATOL is what makes the canary specific rather than merely loud."""
    reference = _logits(n=500, seed=11)
    rng = np.random.default_rng(2)
    candidate = reference + rng.uniform(-0.05, 0.05, reference.shape)
    report = compare_logits(reference, candidate)
    assert report.n_rank_eligible > 100
    assert report.n_argmax_disagreements == 0
    assert report.passed is True
    assert RANK_MARGIN > PROB_ATOL


def test_rows_the_reference_barely_separates_are_excluded_from_the_ranking_gate():
    reference = np.zeros((32, len(LABELS)))  # every probability identical: no ranking to keep
    report = compare_logits(reference, reference[:, ::-1], **LOOSE)
    assert report.n_rank_eligible == 0
    assert report.n_argmax_disagreements == 0


# ---------------------------------------------------------------------------------------
# Input validation and reporting
# ---------------------------------------------------------------------------------------


def test_mismatched_shapes_are_a_hard_error():
    with pytest.raises(ValueError, match="shape mismatch"):
        compare_logits(_logits(n=4), _logits(n=5))


def test_a_wrong_number_of_columns_is_a_hard_error():
    with pytest.raises(ValueError, match=r"expected \(n, 6\)"):
        compare_logits(np.zeros((4, 5)), np.zeros((4, 5)))


def test_an_empty_sample_is_refused_rather_than_passing_vacuously():
    with pytest.raises(ValueError, match="at least one sample"):
        compare_logits(np.zeros((0, len(LABELS))), np.zeros((0, len(LABELS))))


def test_non_finite_logits_are_refused():
    reference = _logits(n=4)
    candidate = reference.copy()
    candidate[0, 0] = np.nan
    with pytest.raises(ValueError, match="NaN or inf"):
        compare_logits(reference, candidate)


def test_the_report_serializes_to_json_for_the_artifact_manifest():
    report = compare_logits(_logits(), _logits() + 0.001)
    payload = json.loads(json.dumps(report.to_dict()))
    assert list(payload["per_label_max_abs_logit_delta"]) == list(LABELS)
    assert list(payload["per_label_flag_flips"]) == list(LABELS)
    assert payload["tolerances"]["rank_margin"] == RANK_MARGIN
    assert isinstance(payload["failures"], list)


def test_assert_parity_returns_the_report_when_it_passes():
    report = compare_logits(_logits(), _logits().copy())
    passing = compare_logits(np.zeros((4, 6)), np.zeros((4, 6)))
    assert assert_parity(passing) is passing
    assert report.n_samples == 64


# ---------------------------------------------------------------------------------------
# Small helpers with real consequences
# ---------------------------------------------------------------------------------------


def test_the_quantization_target_defaults_to_the_graviton_serving_instance():
    assert default_quant_target("aarch64") == "arm64"
    assert default_quant_target("arm64") == "arm64"
    assert default_quant_target("x86_64") == "avx512_vnni"


def test_thresholds_load_from_either_the_bare_or_the_wrapped_json_shape(tmp_path):
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({label: 0.4 for label in LABELS}))
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps({"data_version": "x" * 64, "thresholds": {la: 0.4 for la in LABELS}})
    )
    assert _load_thresholds(bare) == _load_thresholds(wrapped)
    assert list(_load_thresholds(wrapped)) == list(LABELS)
    assert _load_thresholds(None) is None


def test_an_ambiguous_export_directory_is_refused(tmp_path):
    with pytest.raises(ExportError, match="exactly one"):
        _sole_onnx(tmp_path)
    (tmp_path / "a.onnx").write_bytes(b"")
    (tmp_path / "b.onnx").write_bytes(b"")
    with pytest.raises(ExportError, match="exactly one"):
        _sole_onnx(tmp_path)


# ---------------------------------------------------------------------------------------
# Orchestration, with the runtime faked out. Runs everywhere, including where onnxruntime
# cannot start a session at all.
# ---------------------------------------------------------------------------------------


def _fake_runtime(monkeypatch, *, int8_logits_from):
    """Replace export, quantization and inference so the ORDER OF THE GATES can be tested.

    The gates are the product here: which check runs, against which pair of matrices, and
    what lands in the manifest. Those are decisions in this repo's code, not in optimum's.
    """
    from model import export_onnx as module

    reference = _logits(n=12, seed=21)

    def fake_export(model_dir, out_dir):
        out_dir = _mkdir(out_dir)
        (out_dir / "config.json").write_text(
            json.dumps({"id2label": {str(i): la for i, la in enumerate(LABELS)}})
        )
        path = out_dir / "model.onnx"
        path.write_bytes(b"float-onnx-bytes")
        return path

    def fake_quantize(float_dir, out_dir, *, target=None, per_channel=False):
        out_dir = _mkdir(out_dir)
        path = out_dir / "model_quantized.onnx"
        path.write_bytes(b"int8")
        return path

    class FakeScorer:
        def __init__(self, path, **_kw):
            self.path = path

        def logits(self, encodings, **_kw):
            if "quantized" in self.path.name:
                return int8_logits_from(reference)
            return reference

    monkeypatch.setattr(module, "export_float_onnx", fake_export)
    monkeypatch.setattr(module, "quantize_int8", fake_quantize)
    monkeypatch.setattr(module, "OnnxScorer", FakeScorer)
    monkeypatch.setattr(module, "encode", lambda texts, model_dir, **_kw: {"input_ids": [[1]]})
    monkeypatch.setattr(module, "torch_logits", lambda model_dir, encodings: reference)
    return reference


def _mkdir(path):
    from pathlib import Path

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_export_and_verify_runs_both_gates_and_writes_an_auditable_manifest(tmp_path, monkeypatch):
    from model.export_onnx import export_and_verify

    rng = np.random.default_rng(4)
    _fake_runtime(
        monkeypatch, int8_logits_from=lambda ref: ref + rng.uniform(-0.02, 0.02, ref.shape)
    )

    manifest = export_and_verify(tmp_path / "model", tmp_path / "onnx", ["a", "b"], target="arm64")

    assert manifest["parity_float_vs_torch"]["passed"] is True
    assert manifest["parity_int8_vs_float"]["passed"] is True
    assert manifest["quantization"]["target"] == "arm64"
    assert manifest["quantization"]["kind"].startswith("dynamic int8")
    assert list(manifest["example_int8_probs"]) == list(LABELS)
    assert len(manifest["int8_onnx"]["sha256"]) == 64
    assert manifest["int8_onnx"]["sha256"] != manifest["float_onnx"]["sha256"]

    written = json.loads((tmp_path / "onnx" / "manifest.json").read_text())
    assert written["labels"] == list(LABELS)
    assert list(written["example_float_probs"]) == list(LABELS), (
        "the manifest is the human-readable record of the label mapping, so it must not be "
        "written with sorted keys"
    )
    assert (tmp_path / "onnx" / "parity.json").exists()


def test_a_transposed_quantized_model_stops_the_export(tmp_path, monkeypatch):
    """The failure this whole module exists to produce. Nothing else in the run would fail."""
    from model.export_onnx import export_and_verify

    _fake_runtime(monkeypatch, int8_logits_from=lambda ref: ref[:, [0, 1, 2, 4, 3, 5]])
    with pytest.raises(ParityError):
        export_and_verify(tmp_path / "model", tmp_path / "onnx", ["a", "b"])
    assert not (tmp_path / "onnx" / "manifest.json").exists(), (
        "a failed parity gate must not leave a manifest a later step could register"
    )


def test_a_quantization_that_drifts_past_tolerance_stops_the_export(tmp_path, monkeypatch):
    from model.export_onnx import export_and_verify

    _fake_runtime(monkeypatch, int8_logits_from=lambda ref: ref + 0.6)
    with pytest.raises(ParityError, match="int8 ONNX vs float32 ONNX"):
        export_and_verify(tmp_path / "model", tmp_path / "onnx", ["a", "b"])


def test_an_export_that_moves_the_columns_fails_before_quantization_is_blamed(
    tmp_path, monkeypatch
):
    """Separating the two gates is what makes the failure diagnosable rather than mysterious."""
    from model.export_onnx import export_and_verify

    _fake_runtime(monkeypatch, int8_logits_from=lambda ref: ref)
    from model import export_onnx as module

    monkeypatch.setattr(
        module, "torch_logits", lambda model_dir, encodings: _logits(n=12, seed=21)[:, ::-1]
    )
    with pytest.raises(ParityError, match="float32 ONNX vs PyTorch"):
        export_and_verify(tmp_path / "model", tmp_path / "onnx", ["a", "b"])


def test_an_empty_parity_sample_is_refused(tmp_path, monkeypatch):
    from model.export_onnx import export_and_verify

    _fake_runtime(monkeypatch, int8_logits_from=lambda ref: ref)
    with pytest.raises(ValueError, match="non-empty sample"):
        export_and_verify(tmp_path / "model", tmp_path / "onnx", [])


# ---------------------------------------------------------------------------------------
# End to end on a tiny synthetic model, against the real runtime.
#
# onnxruntime aborts the interpreter outright on some aarch64 hosts -- on this build box its
# CPU-topology probe trips `Assertion '__n < this->size()' failed` inside the thread-pool
# affinity setup and calls abort(), which no try/except and no importorskip can catch. The
# capability probe therefore runs in a SUBPROCESS: a crash there costs one exit code instead
# of the whole test session. On the GPU pod and on x86 CI these tests run for real.
# ---------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _onnxruntime_runs_here() -> bool:
    probe = (
        "import numpy as np, onnx, onnxruntime, tempfile, os;"
        "from onnx import helper, TensorProto;"
        "x = helper.make_tensor_value_info('x', TensorProto.FLOAT, [None, 4]);"
        "y = helper.make_tensor_value_info('y', TensorProto.FLOAT, [None, 4]);"
        "g = helper.make_graph([helper.make_node('Relu', ['x'], ['y'])], 'g', [x], [y]);"
        "m = helper.make_model(g, opset_imports=[helper.make_opsetid('', 14)]);"
        "m.ir_version = 9;"
        "p = os.path.join(tempfile.mkdtemp(), 'relu.onnx');"
        "onnx.save(m, p);"
        "onnxruntime.InferenceSession(p, providers=['CPUExecutionProvider'])"
    )
    try:
        return subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, timeout=120
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_onnxruntime = pytest.mark.skipif(
    not _onnxruntime_runs_here(),
    reason="onnxruntime cannot create an InferenceSession on this host (aarch64 build box)",
)


def _tiny_model_dir(tmp_path):
    """A two-layer DistilBERT with a toy vocabulary, saved as safetensors. No network."""
    from transformers import (
        DistilBertConfig,
        DistilBertForSequenceClassification,
        DistilBertTokenizerFast,
    )

    vocab = [
        "[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]",
        "you", "are", "an", "idiot", "thanks", "for", "the", "edit", "nice", "work",
    ]
    model_dir = tmp_path / "tiny"
    model_dir.mkdir(parents=True, exist_ok=True)
    vocab_file = tmp_path / "vocab.txt"
    vocab_file.write_text("\n".join(vocab) + "\n")

    config = DistilBertConfig(
        vocab_size=len(vocab), dim=32, n_layers=2, n_heads=2, hidden_dim=64,
        max_position_embeddings=64, num_labels=len(LABELS),
        problem_type="multi_label_classification",
        id2label={i: label for i, label in enumerate(LABELS)},
        label2id={label: i for i, label in enumerate(LABELS)},
    )
    model = DistilBertForSequenceClassification(config)
    model.save_pretrained(str(model_dir), safe_serialization=True)
    DistilBertTokenizerFast(vocab_file=str(vocab_file)).save_pretrained(str(model_dir))
    return model_dir


TINY_TEXTS = [
    "you are an idiot",
    "thanks for the edit",
    "nice work",
    "you are an idiot idiot idiot",
    "thanks for the nice edit",
    "idiot",
]


@requires_onnxruntime
def test_export_quantize_and_parity_end_to_end(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("optimum")
    from model.export_onnx import export_and_verify

    model_dir = _tiny_model_dir(tmp_path)
    manifest = export_and_verify(
        model_dir, tmp_path / "onnx", TINY_TEXTS, max_length=16, target="arm64"
    )

    assert manifest["labels"] == list(LABELS)
    assert manifest["parity_float_vs_torch"]["passed"] is True
    assert manifest["parity_int8_vs_float"]["passed"] is True
    assert manifest["parity_int8_vs_float"]["n_samples"] == len(TINY_TEXTS)
    assert manifest["parity_int8_vs_float"]["n_argmax_disagreements"] == 0
    assert list(manifest["example_int8_probs"]) == list(LABELS)
    assert len(manifest["int8_onnx"]["sha256"]) == 64
    assert manifest["int8_onnx"]["sha256"] != manifest["float_onnx"]["sha256"]
    assert (tmp_path / "onnx" / "manifest.json").exists()
    assert (tmp_path / "onnx" / "parity.json").exists()
    assert (tmp_path / "onnx" / "int8" / "model_quantized.onnx").exists()


@requires_onnxruntime
def test_the_exported_onnx_reproduces_the_torch_logits_column_for_column(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("optimum")
    from model.export_onnx import OnnxScorer, encode, export_float_onnx, torch_logits

    model_dir = _tiny_model_dir(tmp_path)
    onnx_path = export_float_onnx(model_dir, tmp_path / "float")
    encodings = encode(TINY_TEXTS, model_dir, max_length=16)
    reference = torch_logits(model_dir, encodings)
    exported = OnnxScorer(onnx_path).logits(encodings)

    assert reference.shape == (len(TINY_TEXTS), len(LABELS))
    assert compare_logits(reference, exported, logit_atol=1e-3, logit_mean_atol=1e-3,
                          prob_atol=1e-3, flag_flip_frac=0.0).passed is True


def test_the_torch_reference_path_produces_six_columns_in_labels_order(tmp_path):
    """No ONNX involved: the reference side of the parity gate, on a real transformers model."""
    pytest.importorskip("torch")
    from model.export_onnx import encode, torch_logits

    model_dir = _tiny_model_dir(tmp_path)
    encodings = encode(TINY_TEXTS, model_dir, max_length=16)
    assert set(encodings) >= {"input_ids", "attention_mask"}
    assert encodings["input_ids"].shape == (len(TINY_TEXTS), 16)

    reference = torch_logits(model_dir, encodings)
    assert reference.shape == (len(TINY_TEXTS), len(LABELS))
    assert np.isfinite(reference).all()
    assert list(logits_to_dicts(reference)[0]) == list(LABELS)


def test_a_permuted_head_is_caught_by_the_exported_config(tmp_path):
    """The realistic transposition: a config whose id2label was rebuilt from a sorted list."""
    pytest.importorskip("torch")
    from model.export_onnx import assert_label_order

    model_dir = _tiny_model_dir(tmp_path)
    config_path = model_dir / "config.json"
    config = json.loads(config_path.read_text())
    assert_label_order({int(k): v for k, v in config["id2label"].items()})

    config["id2label"] = {str(i): label for i, label in enumerate(sorted(LABELS))}
    with pytest.raises(LabelOrderError, match="must be"):
        assert_label_order({int(k): v for k, v in config["id2label"].items()})
