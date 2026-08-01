"""What must be true before a byte reaches a public W&B registry.

Every W&B seam is injected, so this suite needs no network, no `~/.netrc` and no key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from infra.runpod import register_pod_artifacts as reg
from model.labels import LABELS

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeArtifact:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.dirs: list[str] = []

    def add_dir(self, path: str) -> None:
        self.dirs.append(path)


class FakeRun:
    entity = "rock"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.logged: list[FakeArtifact] = []
        self.finished = False

    def log_artifact(self, artifact: FakeArtifact) -> None:
        self.logged.append(artifact)

    def finish(self) -> None:
        self.finished = True


@pytest.fixture
def runs() -> list[FakeRun]:
    return []


@pytest.fixture
def artifacts() -> list[FakeArtifact]:
    return []


@pytest.fixture
def factories(runs: list[FakeRun], artifacts: list[FakeArtifact]):
    def run_factory(**kwargs: Any) -> FakeRun:
        run = FakeRun(**kwargs)
        runs.append(run)
        return run

    def artifact_factory(**kwargs: Any) -> FakeArtifact:
        artifact = FakeArtifact(**kwargs)
        artifacts.append(artifact)
        return artifact

    return {"run_factory": run_factory, "artifact_factory": artifact_factory}


def _write_checkpoint(root: Path) -> Path:
    model_dir = root / "final"
    model_dir.mkdir(parents=True)
    (model_dir / "model.safetensors").write_bytes(b"weights")
    (model_dir / "config.json").write_text(json.dumps({"id2label": dict(enumerate(LABELS))}))
    (root / reg.SUMMARY_NAME).write_text(
        json.dumps(
            {
                "split_version": "a24b8dd6",
                "data_version": "d1",
                "fold": 0,
                "epochs_run": 3,
                "final_val_metrics": {"macro_pr_auc": 0.8},
                "epoch_gaps": [{"epoch": 1, "train_val_loss_gap": 0.01}],
            }
        )
    )
    return model_dir


def _write_onnx(root: Path, *, mutate: dict[str, Any] | None = None) -> Path:
    onnx_dir = root / "onnx"
    (onnx_dir / "int8").mkdir(parents=True)
    int8 = onnx_dir / "int8" / "model.onnx"
    int8.write_bytes(b"int8-graph")
    manifest = {
        "labels": list(LABELS),
        "quantization": {"kind": "dynamic int8", "target": "avx512_vnni"},
        "int8_onnx": {
            "path": "int8/model.onnx",
            "sha256": reg.file_digest(int8).removeprefix("sha256:"),
        },
        "parity_int8_vs_float": {"n_samples": 512, "max_abs_logit_delta": 0.01},
        "parity_float_vs_torch": {"n_samples": 512},
        "size_ratio_int8_over_float": 0.26,
        "example_int8_probs": dict.fromkeys(LABELS, 0.1),
    }
    manifest.update(mutate or {})
    (onnx_dir / reg.MANIFEST_FILE).write_text(json.dumps(manifest))
    return onnx_dir


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def test_the_combined_digest_changes_when_a_file_is_renamed() -> None:
    """A digest-of-digests that ignored paths would call these two trees identical. A renamed
    weight file is one of the ways a positional label order gets transposed."""
    a = {"model.safetensors": "sha256:aa", "config.json": "sha256:bb"}
    b = {"model-old.safetensors": "sha256:aa", "config.json": "sha256:bb"}
    assert reg.combined_digest(a) != reg.combined_digest(b)


def test_the_combined_digest_is_insensitive_to_iteration_order() -> None:
    a = {"a": "sha256:1", "b": "sha256:2"}
    b = {"b": "sha256:2", "a": "sha256:1"}
    assert reg.combined_digest(a) == reg.combined_digest(b)


def test_tree_digests_are_relative_posix_paths(tmp_path: Path) -> None:
    """These are compared across machines: the pod writes them and a human elsewhere reads
    them, so an absolute path would make two identical trees look different."""
    model_dir = _write_checkpoint(tmp_path)
    digests = reg.tree_digests(model_dir)
    assert set(digests) == {"model.safetensors", "config.json"}
    assert all(value.startswith("sha256:") for value in digests.values())


# ---------------------------------------------------------------------------
# Refusals -- all of them before the first network call
# ---------------------------------------------------------------------------


def test_a_pickled_checkpoint_is_refused(tmp_path: Path, factories, runs) -> None:
    """torch.load executes arbitrary code and this artifact is bound for a public registry."""
    model_dir = _write_checkpoint(tmp_path)
    (model_dir / "pytorch_model.bin").write_bytes(b"pickle")
    onnx_dir = _write_onnx(tmp_path)

    with pytest.raises(reg.ArtifactError, match="pytorch_model.bin"):
        reg.register(model_dir=model_dir, onnx_dir=onnx_dir, **factories)

    assert runs == [], "a rejected artifact must not leave an empty run behind"


def test_a_checkpoint_with_no_safetensors_is_refused(tmp_path: Path, factories) -> None:
    model_dir = tmp_path / "final"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}")
    with pytest.raises(reg.ArtifactError, match="safetensors"):
        reg.register(model_dir=model_dir, onnx_dir=_write_onnx(tmp_path), **factories)


def test_an_int8_graph_whose_digest_drifted_is_refused(tmp_path: Path, factories, runs) -> None:
    """The exporter recorded a digest; the bytes on disk no longer match it. That is a
    truncated copy, a half-written quantization, or a swapped file -- and a registry entry
    that is wrong is worse than one that is missing, because it is wrong and trusted."""
    model_dir = _write_checkpoint(tmp_path)
    onnx_dir = _write_onnx(tmp_path)
    (onnx_dir / "int8" / "model.onnx").write_bytes(b"different-bytes")

    with pytest.raises(reg.ArtifactError, match="digest"):
        reg.register(model_dir=model_dir, onnx_dir=onnx_dir, **factories)
    assert runs == []


def test_an_export_with_no_parity_report_is_refused(tmp_path: Path, factories) -> None:
    """An unverified quantization must not enter the registry. The ONNX export is the single
    highest-risk site for a silent label transposition."""
    model_dir = _write_checkpoint(tmp_path)
    onnx_dir = _write_onnx(tmp_path, mutate={"parity_int8_vs_float": {}})
    with pytest.raises(reg.ArtifactError, match="parity"):
        reg.register(model_dir=model_dir, onnx_dir=onnx_dir, **factories)


def test_a_parity_report_over_zero_samples_is_refused(tmp_path: Path, factories) -> None:
    model_dir = _write_checkpoint(tmp_path)
    onnx_dir = _write_onnx(tmp_path, mutate={"parity_int8_vs_float": {"n_samples": 0}})
    with pytest.raises(reg.ArtifactError, match="zero samples"):
        reg.register(model_dir=model_dir, onnx_dir=onnx_dir, **factories)


def test_a_transposed_label_order_is_refused(tmp_path: Path, factories) -> None:
    model_dir = _write_checkpoint(tmp_path)
    onnx_dir = _write_onnx(tmp_path, mutate={"labels": list(reversed(LABELS))})
    with pytest.raises(reg.ArtifactError, match="label order"):
        reg.register(model_dir=model_dir, onnx_dir=onnx_dir, **factories)


def test_a_missing_manifest_is_refused(tmp_path: Path, factories) -> None:
    model_dir = _write_checkpoint(tmp_path)
    onnx_dir = _write_onnx(tmp_path)
    (onnx_dir / reg.MANIFEST_FILE).unlink()
    with pytest.raises(reg.ArtifactError, match="export never completed"):
        reg.register(model_dir=model_dir, onnx_dir=onnx_dir, **factories)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_both_artifacts_are_logged_with_their_digests(
    tmp_path: Path, factories, runs, artifacts
) -> None:
    model_dir = _write_checkpoint(tmp_path)
    onnx_dir = _write_onnx(tmp_path)

    receipt = reg.register(
        model_dir=model_dir, onnx_dir=onnx_dir, project="p", entity="rock", **factories
    )

    assert [a.kwargs["name"] for a in artifacts] == [reg.CHECKPOINT_ARTIFACT, reg.ONNX_ARTIFACT]
    assert [a.dirs for a in artifacts] == [[str(model_dir)], [str(onnx_dir)]]
    assert receipt["checkpoint"]["sha256"].startswith("sha256:")
    assert receipt["onnx_int8"]["sha256"].startswith("sha256:")
    assert receipt["onnx_int8"]["sha256"] != receipt["checkpoint"]["sha256"]
    assert receipt["onnx_int8"]["parity"]["n_samples"] == 512
    assert runs[0].finished, "the run must be closed or the pod teardown races the upload"


def test_the_onnx_artifact_names_the_checkpoint_it_came_from(
    tmp_path: Path, factories, artifacts
) -> None:
    """Two artifacts that cannot be tied together are two artifacts nobody can audit."""
    reg.register(
        model_dir=_write_checkpoint(tmp_path), onnx_dir=_write_onnx(tmp_path), **factories
    )
    ckpt_meta, onnx_meta = (a.kwargs["metadata"] for a in artifacts)
    assert onnx_meta["checkpoint_sha256"] == ckpt_meta["sha256"]


def test_provenance_travels_with_the_artifact(tmp_path: Path, factories, artifacts) -> None:
    reg.register(
        model_dir=_write_checkpoint(tmp_path), onnx_dir=_write_onnx(tmp_path), **factories
    )
    metadata = artifacts[0].kwargs["metadata"]
    assert metadata["split_version"] == "a24b8dd6"
    assert metadata["labels"] == list(LABELS)
    assert metadata["epoch_gaps"], "the per-epoch train/val gap is the overfit evidence"


def test_no_raw_comment_text_reaches_the_artifact_metadata(
    tmp_path: Path, factories, artifacts
) -> None:
    """The W&B project is public by owner decision, which makes artifact metadata the last
    place a user comment could escape into a graded artifact."""
    reg.register(
        model_dir=_write_checkpoint(tmp_path), onnx_dir=_write_onnx(tmp_path), **factories
    )
    for artifact in artifacts:
        blob = json.dumps(artifact.kwargs["metadata"])
        assert "comment_text" not in blob
        # Nothing prose-shaped: every string value is a digest, a label, or a short token.
        for value in artifact.kwargs["metadata"].values():
            if isinstance(value, str):
                assert " " not in value.strip() or value.startswith("dynamic int8")


def test_the_run_is_finished_even_when_an_upload_raises(tmp_path: Path, runs) -> None:
    """An unfinished W&B run on a pod that is about to be destroyed loses the upload."""

    def run_factory(**kwargs: Any) -> FakeRun:
        run = FakeRun(**kwargs)
        runs.append(run)
        return run

    def exploding_artifact_factory(**kwargs: Any):
        raise RuntimeError("upload failed")

    with pytest.raises(RuntimeError, match="upload failed"):
        reg.register(
            model_dir=_write_checkpoint(tmp_path),
            onnx_dir=_write_onnx(tmp_path),
            run_factory=run_factory,
            artifact_factory=exploding_artifact_factory,
        )
    assert runs[0].finished
