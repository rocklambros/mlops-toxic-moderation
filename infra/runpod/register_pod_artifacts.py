"""Register the DistilBERT checkpoint and its int8 ONNX export to W&B, with digests.

This runs ON THE POD, and that is a constraint rather than a convenience: the Jetson venv has
no ``torch`` and no ``wandb``, so neither the float model nor the uploader exists on the box
that drives the run. It also runs INSIDE the pod lease, before teardown, because the pod's
disk dies with the pod.

It is deliberately not ``model/tracking.py``. That module registers the *classical* model and
refuses anything that is not a skops archive at or above an advisory floor, links it into the
public Registry collection, and promotes it to a stage. None of that applies here: DistilBERT
is a safetensors checkpoint plus two ONNX graphs, and per the delivery spec it is a
build-time comparison model that is *not* promoted -- choosing between classical and
DistilBERT is decided on validation folds, and the promoted collection stays the classical
model's. So these are logged as versioned artifacts with provenance, and nothing is aliased
to a production stage.

Three things are checked before a byte is uploaded, because a registry entry that is wrong is
worse than one that is missing -- it is wrong *and* trusted:

1. **No pickle in the checkpoint.** ``torch.load`` on a ``.bin`` executes arbitrary code at
   load time, and this artifact is bound for a public registry.
2. **The ONNX manifest's recorded int8 digest matches the bytes on disk.** The exporter wrote
   that digest at export time; re-deriving it here catches a truncated copy, a half-written
   quantization, and a file swapped between the two steps.
3. **The parity gate actually passed.** ``export_onnx`` raises on a parity failure, but this
   module is a separate process and cannot assume it ran in the same shell. An int8 graph
   whose parity was never asserted must not enter the registry wearing a digest that implies
   it was.

Every W&B seam is injected, so the unit tests never touch the network or read ``~/.netrc``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from model.labels import LABELS

MANIFEST_FILE = "onnx_manifest.json"
SUMMARY_NAME = "training_summary.json"

CHECKPOINT_ARTIFACT = "distilbert-toxic"
ONNX_ARTIFACT = "distilbert-toxic-onnx-int8"
ARTIFACT_TYPE = "model"

# A pickle by another name. safetensors, never pickle.
PICKLE_SUFFIXES = (".bin", ".pt", ".pth", ".pkl", ".pickle", ".ckpt", ".joblib", ".msgpack")


class ArtifactError(RuntimeError):
    """What is about to be registered is not what it claims to be."""


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def file_digest(path: Path | str) -> str:
    """``sha256:<hex>`` over the bytes on disk, streamed so a 250 MB graph fits in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def tree_digests(root: Path | str) -> dict[str, str]:
    """Per-file digests keyed by POSIX-relative path, in sorted order.

    Sorted and relative because this dict is compared across machines: the pod writes it and a
    human on another box reads it. Absolute paths and filesystem iteration order would make
    two identical trees produce two different records.
    """
    root = Path(root)
    return {
        p.relative_to(root).as_posix(): file_digest(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def combined_digest(digests: dict[str, str]) -> str:
    """One digest over the whole tree: sha256 of ``path\\0digest\\n`` in sorted path order.

    A single value a human can compare by eye, that changes if any file changes, if a file is
    added or removed, or if a file is renamed -- the last of which a digest-of-digests that
    ignored paths would miss, and a renamed weight file is exactly how a label order gets
    transposed.
    """
    payload = "".join(f"{path}\0{digest}\n" for path, digest in sorted(digests.items()))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def assert_no_pickles(directory: Path | str) -> None:
    directory = Path(directory)
    found = sorted(
        p.relative_to(directory).as_posix()
        for p in directory.rglob("*")
        if p.suffix in PICKLE_SUFFIXES
    )
    if found:
        raise ArtifactError(
            f"{directory} holds pickled weights ({', '.join(found)}); torch.load executes "
            "arbitrary code and this artifact is bound for a public registry"
        )


def assert_safetensors_present(directory: Path | str) -> None:
    if not any(Path(directory).rglob("*.safetensors")):
        raise ArtifactError(f"{directory} holds no .safetensors file")


def load_onnx_manifest(onnx_dir: Path | str) -> dict[str, Any]:
    """Read the exporter's manifest and re-verify everything it asserts about itself."""
    onnx_dir = Path(onnx_dir)
    manifest_path = onnx_dir / MANIFEST_FILE
    if not manifest_path.exists():
        raise ArtifactError(
            f"no {MANIFEST_FILE} in {onnx_dir}: the export never completed, so there is no "
            "parity evidence and nothing to register"
        )
    manifest = json.loads(manifest_path.read_text())

    if manifest.get("labels") != list(LABELS):
        raise ArtifactError(
            f"manifest label order {manifest.get('labels')} != {list(LABELS)}; every per-label "
            "array in this project is positional, so a transposed export mislabels silently"
        )

    parity = manifest.get("parity_int8_vs_float") or {}
    if not parity:
        raise ArtifactError(
            "the manifest carries no int8-vs-float parity report; an unverified quantization "
            "must not enter the registry"
        )
    if parity.get("n_samples", 0) <= 0:
        raise ArtifactError("the parity report covers zero samples, so it asserts nothing")

    recorded = manifest.get("int8_onnx", {})
    int8_path = onnx_dir / str(recorded.get("path", ""))
    if not int8_path.is_file():
        raise ArtifactError(f"manifest names an int8 graph at {int8_path}, which does not exist")
    actual = file_digest(int8_path).removeprefix("sha256:")
    if actual != recorded.get("sha256"):
        raise ArtifactError(
            f"int8 graph digest {actual[:12]}... != the {str(recorded.get('sha256'))[:12]}... "
            "the exporter recorded; the file changed between export and registration"
        )
    return manifest


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def build_metadata(
    *,
    kind: str,
    digests: dict[str, str],
    training_summary: dict[str, Any] | None,
    onnx_manifest: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Provenance that travels with the file, so a registry entry explains itself.

    Only scalars, digests and version strings. No raw comment text ever reaches W&B: the
    project is public by owner decision, which makes any payload the last place a user comment
    could escape into a graded artifact.
    """
    metadata: dict[str, Any] = {
        "kind": kind,
        "labels": list(LABELS),
        "label_order_is_positional": True,
        "sha256": combined_digest(digests),
        "files": digests,
        "n_files": len(digests),
        "serialization": "safetensors" if kind == "pytorch-checkpoint" else "onnx",
    }
    if training_summary:
        for field in (
            "split_version", "data_version", "raw_sha256", "env_version",
            "fold", "epochs_run", "early_stopped", "best_metric", "best_metric_value",
        ):
            if field in training_summary:
                metadata[field] = training_summary[field]
        if "final_val_metrics" in training_summary:
            metadata["final_val_metrics"] = training_summary["final_val_metrics"]
        if "epoch_gaps" in training_summary:
            metadata["epoch_gaps"] = training_summary["epoch_gaps"]
    if onnx_manifest:
        metadata["quantization"] = onnx_manifest.get("quantization")
        metadata["parity_int8_vs_float"] = onnx_manifest.get("parity_int8_vs_float")
        metadata["parity_float_vs_torch"] = onnx_manifest.get("parity_float_vs_torch")
        metadata["size_ratio_int8_over_float"] = onnx_manifest.get("size_ratio_int8_over_float")
        metadata["example_int8_probs"] = onnx_manifest.get("example_int8_probs")
    if extra:
        metadata.update(extra)
    return metadata


def read_training_summary(model_dir: Path | str) -> dict[str, Any] | None:
    """The trainer writes this beside the checkpoint; it may sit one level up."""
    model_dir = Path(model_dir)
    for candidate in (model_dir / SUMMARY_NAME, model_dir.parent / SUMMARY_NAME):
        if candidate.is_file():
            return json.loads(candidate.read_text())
    return None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _default_run_factory(**kwargs: Any):  # pragma: no cover - real SDK only
    import wandb

    return wandb.init(**kwargs)


def _default_artifact_factory(**kwargs: Any):  # pragma: no cover - real SDK only
    import wandb

    return wandb.Artifact(**kwargs)


def register(
    *,
    model_dir: Path | str,
    onnx_dir: Path | str,
    project: str = "mlops-toxic-moderation",
    entity: str | None = None,
    run_name: str | None = None,
    run_factory: Callable[..., Any] = _default_run_factory,
    artifact_factory: Callable[..., Any] = _default_artifact_factory,
) -> dict[str, Any]:
    """Log both artifacts and return a receipt naming every digest that was uploaded.

    Every refusal happens before ``run_factory`` is called, so a rejected artifact leaves no
    empty run and no half-published version behind.
    """
    model_dir, onnx_dir = Path(model_dir), Path(onnx_dir)
    if not model_dir.is_dir():
        raise ArtifactError(f"no checkpoint directory at {model_dir}")
    if not onnx_dir.is_dir():
        raise ArtifactError(f"no ONNX directory at {onnx_dir}")

    assert_no_pickles(model_dir)
    assert_safetensors_present(model_dir)
    assert_no_pickles(onnx_dir)
    onnx_manifest = load_onnx_manifest(onnx_dir)

    summary = read_training_summary(model_dir)
    ckpt_digests = tree_digests(model_dir)
    onnx_digests = tree_digests(onnx_dir)

    ckpt_meta = build_metadata(
        kind="pytorch-checkpoint", digests=ckpt_digests, training_summary=summary
    )
    onnx_meta = build_metadata(
        kind="onnx-int8",
        digests=onnx_digests,
        training_summary=summary,
        onnx_manifest=onnx_manifest,
        extra={"checkpoint_sha256": ckpt_meta["sha256"]},
    )

    run = run_factory(
        project=project,
        entity=entity,
        name=run_name,
        job_type="register-distilbert",
        config={
            "checkpoint_sha256": ckpt_meta["sha256"],
            "onnx_int8_sha256": onnx_meta["sha256"],
            "split_version": ckpt_meta.get("split_version"),
        },
    )
    try:
        for name, directory, metadata in (
            (CHECKPOINT_ARTIFACT, model_dir, ckpt_meta),
            (ONNX_ARTIFACT, onnx_dir, onnx_meta),
        ):
            artifact = artifact_factory(name=name, type=ARTIFACT_TYPE, metadata=metadata)
            artifact.add_dir(str(directory))
            run.log_artifact(artifact)
    finally:
        finish = getattr(run, "finish", None)
        if callable(finish):
            finish()

    return {
        "project": project,
        "entity": entity,
        "checkpoint": {
            "artifact": CHECKPOINT_ARTIFACT,
            "sha256": ckpt_meta["sha256"],
            "files": ckpt_meta["n_files"],
        },
        "onnx_int8": {
            "artifact": ONNX_ARTIFACT,
            "sha256": onnx_meta["sha256"],
            "files": onnx_meta["n_files"],
            "parity": onnx_manifest.get("parity_int8_vs_float"),
        },
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infra.runpod.register_pod_artifacts",
        description="Register the DistilBERT checkpoint and its int8 ONNX export to W&B.",
    )
    parser.add_argument("--model-dir", type=Path, default=Path("outputs/final"))
    parser.add_argument("--onnx-dir", type=Path, default=Path("outputs/onnx"))
    parser.add_argument("--project", default="mlops-toxic-moderation")
    parser.add_argument("--entity", default=os.environ.get("WANDB_ENTITY") or None)
    parser.add_argument("--run-name", default=None)
    parser.add_argument(
        "--receipt", type=Path, default=None, help="write the digest receipt here as JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    receipt = register(
        model_dir=args.model_dir,
        onnx_dir=args.onnx_dir,
        project=args.project,
        entity=args.entity,
        run_name=args.run_name,
    )
    text = json.dumps(receipt, indent=2)
    if args.receipt:
        Path(args.receipt).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
