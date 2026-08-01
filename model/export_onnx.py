"""ONNX export, int8 dynamic quantization, and the logit-parity gate.

This is the single highest-risk site in the whole model path for a **silent label
transposition**, and the reason is specific rather than superstitious: the export is the one
step where column order can genuinely change. The head's six output columns are only labelled
by ``config.id2label``; ONNX carries tensors, not names. Anything that re-orders
``id2label``, exports from a directory whose config was written by a different run, or
re-derives the mapping with its own ``zip(LABELS, row)`` produces a model that scores
``threat`` into the ``obscene`` column. Every metric still computes, every probability is
still in [0, 1], and the output contract's key-membership validator is order-blind, so
nothing anywhere goes red.

Three gates close that path:

1. ``assert_label_order`` — ``[id2label[i] for i in range(6)]`` must equal ``LABELS``,
   checked on the exported config, not on the training config.
2. A **float** parity gate. The exported float32 ONNX must reproduce the PyTorch logits to
   ``TORCH_ONNX_ATOL``. A permuted export fails here by a mile, which separates "the export
   moved the columns" from "quantization moved the numbers".
3. An **int8** parity gate with a stated tolerance, plus an argmax-agreement check restricted
   to rows whose top-two probability gap exceeds ``RANK_MARGIN``. Since ``RANK_MARGIN`` is
   larger than ``PROB_ATOL``, honest quantization noise cannot flip those rows and a
   transposition always does.

``probs_to_dict`` from ``model/contract.py`` is the only array-to-dict conversion used here.

optimum, onnxruntime and transformers are imported inside functions: the parity arithmetic is
unit-tested on a build box that does not have them, and the numeric gate is the part that has
to be right.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from model.contract import probs_to_dict
from model.labels import LABELS
from model.train_distilbert import (
    SUMMARY_NAME,
    load_bundle_cache,
    sha256_bytes,
    sigmoid,
)

FLOAT_SUBDIR = "float"
INT8_SUBDIR = "int8"
QUANTIZED_FILE = "model_quantized.onnx"
FLOAT_FILE = "model.onnx"
PARITY_FILE = "parity.json"
MANIFEST_FILE = "manifest.json"

# --------------------------------------------------------------------------------------
# Stated tolerances. These are the contract; changing one is a decision, not a tweak.
# --------------------------------------------------------------------------------------

# float32 ONNX against PyTorch. Same weights, same maths, different kernels: the only
# expected difference is fused-operation rounding, so this is tight on purpose.
TORCH_ONNX_ATOL = 1e-3

# int8 dynamic quantization against float32 ONNX. Weights are quantized per tensor to 8 bits
# and activations are quantized at run time, so a per-logit shift of a few hundredths is
# normal and a shift of a quarter is not.
LOGIT_ATOL = 0.25
LOGIT_MEAN_ATOL = 0.05

# Probability space is what the moderation policy thresholds, so it gets its own gate. A
# 0.03 shift cannot move a decision unless the score was already inside 0.03 of a threshold.
PROB_ATOL = 0.03

# Fraction of (row, label) decisions permitted to flip at the tuned thresholds. Zero is not
# achievable for scores that sit exactly on a boundary, and pretending otherwise produces a
# gate that is disabled the first time it fires.
FLAG_FLIP_FRAC = 0.005

# ...and a fraction alone is not a usable gate on a small sample: at 12 rows one boundary
# flip is 1.4% of the decisions, so a pure-fraction rule fails every honest export below ~200
# decisions. The allowance is therefore max(FLAG_FLIP_MIN, floor(frac x decisions)), and when
# the fraction cannot resolve even one flip the report says so instead of passing quietly.
FLAG_FLIP_MIN = 1

# The transposition canary. Rows whose top-two probability gap exceeds this cannot have
# their argmax moved by noise bounded at PROB_ATOL, so any argmax disagreement among them is
# a re-ordering, not quantization. RANK_MARGIN > PROB_ATOL is what makes that inference valid.
RANK_MARGIN = 0.05

DEFAULT_SAMPLE_SIZE = 256
DEFAULT_MAX_LENGTH = 192


class ParityError(RuntimeError):
    """The quantized model does not reproduce the float model within the stated tolerance."""


class LabelOrderError(RuntimeError):
    """The exported model's column order is not LABELS order."""


class ExportError(RuntimeError):
    """The export or quantization step did not produce the file it promised."""


# --------------------------------------------------------------------------------------
# Label-order guards and the one authoritative array-to-dict conversion
# --------------------------------------------------------------------------------------


def id2label_in_order(config_or_map) -> list[str]:
    """``id2label`` read positionally, from any of the three shapes it legitimately arrives in.

    A ``PretrainedConfig`` object, a parsed ``config.json`` (which nests the mapping under
    ``"id2label"`` with string keys, because JSON has no integer keys), or the bare mapping.
    Getting this wrong is not a cosmetic bug: a reader that silently sees no mapping reports
    ``[None, ...]``, and a guard that always fails is disabled within a day.
    """
    mapping = getattr(config_or_map, "id2label", None)
    if mapping is None and isinstance(config_or_map, dict):
        mapping = config_or_map.get("id2label", config_or_map)
    mapping = mapping or {}
    return [mapping.get(i, mapping.get(str(i))) for i in range(len(LABELS))]


def assert_label_order(config_or_map) -> None:
    ordered = id2label_in_order(config_or_map)
    if ordered != list(LABELS):
        raise LabelOrderError(
            f"exported id2label in index order is {ordered}, must be {list(LABELS)}. ONNX "
            "carries tensors, not names: column j IS LABELS[j] downstream, so a permuted "
            "config silently relabels every prediction the re-scorer makes."
        )


def logits_to_dicts(logits) -> list[dict[str, float]]:
    """Logits -> per-label probability dicts. The ONLY array-to-dict path in this module.

    Sigmoid per column, never softmax: the six labels are independent and a comment can carry
    ``toxic`` and ``obscene`` at once. ``probs_to_dict`` is imported rather than re-derived
    with ``zip(LABELS, row)`` because an independent re-derivation is precisely how a
    transposition survives review (premortem H23).
    """
    probs = sigmoid(np.asarray(logits, dtype=np.float64))
    if probs.ndim != 2 or probs.shape[1] != len(LABELS):
        raise ValueError(f"expected (n, {len(LABELS)}) logits, got {probs.shape}")
    return [probs_to_dict(row) for row in probs]


# --------------------------------------------------------------------------------------
# The parity gate
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ParityReport:
    n_samples: int
    max_abs_logit_delta: float
    mean_abs_logit_delta: float
    max_abs_prob_delta: float
    per_label_max_abs_logit_delta: dict[str, float]
    per_label_max_abs_prob_delta: dict[str, float]
    n_flag_flips: int
    n_decisions: int
    flag_flip_fraction: float
    flag_flips_allowed: int
    flag_gate_low_power: bool
    per_label_flag_flips: dict[str, int]
    n_rank_eligible: int
    n_argmax_disagreements: int
    tolerances: dict[str, float]
    failures: tuple[str, ...]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["failures"] = list(self.failures)
        return out


def _thresh_vector(thresholds: dict[str, float] | None) -> np.ndarray:
    if thresholds is None:
        return np.full(len(LABELS), 0.5)
    missing = [label for label in LABELS if label not in thresholds]
    if missing:
        raise ValueError(f"thresholds are missing {missing}")
    return np.array([float(thresholds[label]) for label in LABELS])


def compare_logits(
    reference,
    candidate,
    *,
    thresholds: dict[str, float] | None = None,
    logit_atol: float = LOGIT_ATOL,
    logit_mean_atol: float = LOGIT_MEAN_ATOL,
    prob_atol: float = PROB_ATOL,
    flag_flip_frac: float = FLAG_FLIP_FRAC,
    flag_flip_min: int = FLAG_FLIP_MIN,
    rank_margin: float = RANK_MARGIN,
) -> ParityReport:
    """Compare two ``(n, 6)`` logit matrices column by column and decide pass or fail.

    ``reference`` is the float model, ``candidate`` the quantized one. The comparison is
    positional and per column, which is the only way a transposition is visible at all: any
    aggregate over the whole matrix (mean logit, macro metric, loss) is invariant to a
    permutation of the columns.
    """
    reference = np.asarray(reference, dtype=np.float64)
    candidate = np.asarray(candidate, dtype=np.float64)
    if reference.shape != candidate.shape:
        raise ValueError(f"shape mismatch: reference {reference.shape} vs {candidate.shape}")
    if reference.ndim != 2 or reference.shape[1] != len(LABELS):
        raise ValueError(f"expected (n, {len(LABELS)}) logits, got {reference.shape}")
    if reference.shape[0] == 0:
        raise ValueError("parity needs at least one sample row")
    if not np.isfinite(reference).all() or not np.isfinite(candidate).all():
        raise ValueError("logits contain NaN or inf")

    delta = np.abs(reference - candidate)
    ref_probs, cand_probs = sigmoid(reference), sigmoid(candidate)
    prob_delta = np.abs(ref_probs - cand_probs)

    thresh = _thresh_vector(thresholds)
    ref_flags = ref_probs >= thresh
    cand_flags = cand_probs >= thresh
    flips = ref_flags != cand_flags
    n_decisions = int(flips.size)
    n_flips = int(flips.sum())

    # Only rows the reference separates by more than rank_margin can testify about ordering.
    ordered = np.sort(ref_probs, axis=1)
    top_gap = ordered[:, -1] - ordered[:, -2]
    eligible = top_gap > rank_margin
    disagreements = int(
        (np.argmax(ref_probs[eligible], axis=1) != np.argmax(cand_probs[eligible], axis=1)).sum()
    ) if eligible.any() else 0

    max_logit = float(delta.max())
    mean_logit = float(delta.mean())
    max_prob = float(prob_delta.max())
    flip_fraction = n_flips / n_decisions
    proportional_allowance = int(np.floor(flag_flip_frac * n_decisions))
    allowed_flips = max(int(flag_flip_min), proportional_allowance)
    low_power = proportional_allowance < 1

    failures: list[str] = []
    if max_logit > logit_atol:
        failures.append(f"max |logit delta| {max_logit:.4f} > {logit_atol}")
    if mean_logit > logit_mean_atol:
        failures.append(f"mean |logit delta| {mean_logit:.4f} > {logit_mean_atol}")
    if max_prob > prob_atol:
        failures.append(f"max |prob delta| {max_prob:.4f} > {prob_atol}")
    if n_flips > allowed_flips:
        failures.append(
            f"{n_flips}/{n_decisions} decisions flipped ({flip_fraction:.4f}), "
            f"allowance {allowed_flips} (max of {flag_flip_min} and "
            f"{flag_flip_frac} x {n_decisions})"
        )
    if disagreements:
        failures.append(
            f"{disagreements} of {int(eligible.sum())} well-separated rows changed argmax; "
            "noise bounded at prob_atol cannot do that across a gap of rank_margin, so this "
            "is a column re-ordering, not quantization"
        )

    return ParityReport(
        n_samples=int(reference.shape[0]),
        max_abs_logit_delta=max_logit,
        mean_abs_logit_delta=mean_logit,
        max_abs_prob_delta=max_prob,
        per_label_max_abs_logit_delta={
            label: float(delta[:, j].max()) for j, label in enumerate(LABELS)
        },
        per_label_max_abs_prob_delta={
            label: float(prob_delta[:, j].max()) for j, label in enumerate(LABELS)
        },
        n_flag_flips=n_flips,
        n_decisions=n_decisions,
        flag_flip_fraction=float(flip_fraction),
        flag_flips_allowed=allowed_flips,
        flag_gate_low_power=bool(low_power),
        per_label_flag_flips={label: int(flips[:, j].sum()) for j, label in enumerate(LABELS)},
        n_rank_eligible=int(eligible.sum()),
        n_argmax_disagreements=disagreements,
        tolerances={
            "logit_atol": logit_atol,
            "logit_mean_atol": logit_mean_atol,
            "prob_atol": prob_atol,
            "flag_flip_frac": flag_flip_frac,
            "flag_flip_min": float(flag_flip_min),
            "rank_margin": rank_margin,
        },
        failures=tuple(failures),
        passed=not failures,
    )


def assert_parity(report: ParityReport, *, what: str = "int8 ONNX vs float") -> ParityReport:
    if not report.passed:
        raise ParityError(
            f"{what} parity failed on {report.n_samples} samples: "
            + "; ".join(report.failures)
            + f" | per-label max |logit delta| {report.per_label_max_abs_logit_delta}"
        )
    return report


# --------------------------------------------------------------------------------------
# Export, quantize, score
# --------------------------------------------------------------------------------------


def default_quant_target(machine: str | None = None) -> str:
    """arm64 by default: the re-scorer runs on a Graviton ``t4g`` instance, not on x86.

    Quantizing for avx512_vnni and then serving on aarch64 still runs, but the operator set
    chosen for the wrong ISA is how an int8 model ends up slower than the float one it
    replaced.
    """
    machine = (machine or platform.machine()).lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return "avx512_vnni"


def export_float_onnx(model_dir: Path, out_dir: Path) -> Path:
    """torch -> ONNX float32 via optimum. No quantization here; that is a separate gate."""
    from optimum.onnxruntime import ORTModelForSequenceClassification  # noqa: PLC0415
    from transformers import AutoTokenizer  # noqa: PLC0415

    model_dir, out_dir = Path(model_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ort_model = ORTModelForSequenceClassification.from_pretrained(str(model_dir), export=True)
    assert_label_order(ort_model.config)
    ort_model.save_pretrained(str(out_dir))
    try:
        AutoTokenizer.from_pretrained(str(model_dir)).save_pretrained(str(out_dir))
    except (OSError, ValueError):  # a tokenizer-less fixture is legitimate in unit tests
        pass
    produced = _sole_onnx(out_dir)
    if produced.name != FLOAT_FILE:
        produced = produced.rename(out_dir / FLOAT_FILE)
    return produced


def quantize_int8(
    float_dir: Path,
    out_dir: Path,
    *,
    target: str | None = None,
    per_channel: bool = False,
) -> Path:
    """Dynamic int8 quantization via optimum/onnxruntime. Weights int8, activations at run time.

    Dynamic, not static: static quantization needs a calibration set, and a calibration set
    drawn from anything but the training rows is a quiet path for validation or held-out data
    to influence the shipped artifact.
    """
    from optimum.onnxruntime import ORTQuantizer  # noqa: PLC0415
    from optimum.onnxruntime.configuration import AutoQuantizationConfig  # noqa: PLC0415

    float_dir, out_dir = Path(float_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = target or default_quant_target()
    factory = getattr(AutoQuantizationConfig, target, None)
    if factory is None:
        raise ExportError(f"unknown quantization target {target!r}")

    quantizer = ORTQuantizer.from_pretrained(str(float_dir), file_name=_sole_onnx(float_dir).name)
    quantizer.quantize(
        save_dir=str(out_dir),
        quantization_config=factory(is_static=False, per_channel=per_channel),
    )
    for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "vocab.txt",
                 "special_tokens_map.json"):
        source = float_dir / name
        if source.exists() and not (out_dir / name).exists():
            shutil.copy2(source, out_dir / name)

    quantized = _sole_onnx(out_dir)
    if quantized.name != QUANTIZED_FILE:
        quantized = quantized.rename(out_dir / QUANTIZED_FILE)
    return quantized


def _sole_onnx(directory: Path) -> Path:
    files = sorted(p for p in Path(directory).glob("*.onnx"))
    if len(files) != 1:
        raise ExportError(
            f"expected exactly one .onnx in {directory}, found {[p.name for p in files]}"
        )
    return files[0]


class OnnxScorer:
    """Thin onnxruntime wrapper. Deliberately not optimum: this is what serving will use.

    The feed is built from the session's own declared inputs, so a model exported with
    ``token_type_ids`` works without a code change and a model missing ``attention_mask``
    fails loudly instead of being silently fed a zero tensor.
    """

    def __init__(self, onnx_path: Path, *, providers: list[str] | None = None) -> None:
        import onnxruntime  # noqa: PLC0415

        self.path = Path(onnx_path)
        self.session = onnxruntime.InferenceSession(
            str(self.path), providers=providers or ["CPUExecutionProvider"]
        )
        self.input_names = [i.name for i in self.session.get_inputs()]
        self.output_name = self.session.get_outputs()[0].name

    def logits(self, encodings: dict[str, np.ndarray], *, batch_size: int = 32) -> np.ndarray:
        missing = [name for name in self.input_names if name not in encodings]
        if missing:
            raise ExportError(f"{self.path.name} needs inputs {missing} that were not supplied")
        n = len(next(iter(encodings.values())))
        out = []
        for start in range(0, n, batch_size):
            stop = min(start + batch_size, n)
            feed = {
                name: np.asarray(encodings[name][start:stop], dtype=np.int64)
                for name in self.input_names
            }
            out.append(np.asarray(self.session.run([self.output_name], feed)[0], dtype=np.float64))
        return np.concatenate(out, axis=0)


def torch_logits(model_dir: Path, encodings: dict[str, np.ndarray]) -> np.ndarray:
    """Reference logits straight from the fine-tuned PyTorch model, in eval mode."""
    import torch  # noqa: PLC0415
    from transformers import AutoModelForSequenceClassification  # noqa: PLC0415

    model = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    assert_label_order(model.config)
    model.eval()
    tensors = {k: torch.tensor(np.asarray(v), dtype=torch.long) for k, v in encodings.items()}
    with torch.no_grad():
        return model(**tensors).logits.double().numpy()


def encode(
    texts, model_dir: Path, *, max_length: int = DEFAULT_MAX_LENGTH
) -> dict[str, np.ndarray]:
    from transformers import AutoTokenizer  # noqa: PLC0415

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    batch = tokenizer(
        list(texts), truncation=True, max_length=max_length, padding="max_length",
        return_tensors="np",
    )
    return {key: np.asarray(value, dtype=np.int64) for key, value in batch.items()}


def parity_sample_texts(
    cache: Path, *, fold: int = 0, size: int = DEFAULT_SAMPLE_SIZE
) -> list[str]:
    """Parity rows come from the VALIDATION fold, never from the held-out test set.

    ``load_bundle_cache`` is called with ``with_test=False``, so the held-out rows are not
    even in memory while the export runs.
    """
    bundle = load_bundle_cache(cache, with_test=False)
    _, _, val_texts, _ = bundle.fold(fold)
    return list(val_texts[:size])


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------


def export_and_verify(
    model_dir: Path,
    out_dir: Path,
    texts,
    *,
    thresholds: dict[str, float] | None = None,
    target: str | None = None,
    per_channel: bool = False,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> dict[str, Any]:
    """Export, quantize, and run both parity gates. Returns the manifest it writes."""
    model_dir, out_dir = Path(model_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    texts = list(texts)
    if not texts:
        raise ValueError("parity needs a non-empty sample")

    float_onnx = export_float_onnx(model_dir, out_dir / FLOAT_SUBDIR)
    target = target or default_quant_target()
    int8_onnx = quantize_int8(
        out_dir / FLOAT_SUBDIR, out_dir / INT8_SUBDIR, target=target, per_channel=per_channel
    )
    assert_label_order(json.loads((out_dir / FLOAT_SUBDIR / "config.json").read_text()))

    encodings = encode(texts, model_dir, max_length=max_length)
    reference = torch_logits(model_dir, encodings)
    float_out = OnnxScorer(float_onnx).logits(encodings)
    int8_out = OnnxScorer(int8_onnx).logits(encodings)

    export_report = assert_parity(
        compare_logits(
            reference,
            float_out,
            thresholds=thresholds,
            logit_atol=TORCH_ONNX_ATOL,
            logit_mean_atol=TORCH_ONNX_ATOL,
            prob_atol=TORCH_ONNX_ATOL,
            # Exact on the float gate: same weights, same maths. A single flipped decision
            # here is not boundary noise, it is a changed model.
            flag_flip_frac=0.0,
            flag_flip_min=0,
        ),
        what="float32 ONNX vs PyTorch",
    )
    int8_report = assert_parity(
        compare_logits(reference=float_out, candidate=int8_out, thresholds=thresholds),
        what="int8 ONNX vs float32 ONNX",
    )

    training_summary_path = model_dir / SUMMARY_NAME
    manifest = {
        "labels": list(LABELS),
        "quantization": {
            "kind": "dynamic int8 (weights int8, activations quantized at run time)",
            "target": target,
            "per_channel": per_channel,
        },
        "float_onnx": {
            "path": str(float_onnx.relative_to(out_dir)),
            "sha256": sha256_bytes(float_onnx),
            "bytes": float_onnx.stat().st_size,
        },
        "int8_onnx": {
            "path": str(int8_onnx.relative_to(out_dir)),
            "sha256": sha256_bytes(int8_onnx),
            "bytes": int8_onnx.stat().st_size,
        },
        "size_ratio_int8_over_float": round(
            int8_onnx.stat().st_size / float_onnx.stat().st_size, 4
        ),
        "parity_float_vs_torch": export_report.to_dict(),
        "parity_int8_vs_float": int8_report.to_dict(),
        # One row of each, through probs_to_dict, so a human reviewing the artifact can see
        # the label mapping rather than trust it.
        "example_float_probs": logits_to_dicts(float_out[:1])[0],
        "example_int8_probs": logits_to_dicts(int8_out[:1])[0],
        "thresholds": thresholds,
        "n_parity_samples": len(texts),
        "training_summary": (
            json.loads(training_summary_path.read_text())
            if training_summary_path.exists()
            else None
        ),
    }
    # sort_keys is deliberately off: the per-label dicts are written in LABELS order so a
    # human reading the manifest sees the mapping the model actually uses. The payload is
    # built deterministically, so the file is still byte-stable across runs.
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n")
    (out_dir / PARITY_FILE).write_text(json.dumps(int8_report.to_dict(), indent=2) + "\n")
    return manifest


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the fine-tuned DistilBERT to ONNX, quantize to int8, verify parity."
    )
    parser.add_argument("--model-dir", type=Path, default=Path("artifacts/distilbert/final"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/onnx"))
    parser.add_argument("--cache", type=Path, default=Path("data/cache/bundle"))
    parser.add_argument("--fold", type=int, default=0)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--texts-file", type=Path, default=None,
                        help="newline-delimited parity sample; defaults to the validation fold")
    parser.add_argument("--thresholds", type=Path, default=None,
                        help="thresholds.json; decisions are compared at these, not at 0.5")
    parser.add_argument("--target", default=None,
                        help="quantization target (arm64, avx512_vnni, avx2, ...)")
    parser.add_argument("--per-channel", action="store_true")
    parser.add_argument("--max-length", type=int, default=DEFAULT_MAX_LENGTH)
    return parser


def _load_thresholds(path: Path | None) -> dict[str, float] | None:
    if path is None:
        return None
    payload = json.loads(Path(path).read_text())
    thresholds = payload.get("thresholds", payload)
    return {label: float(thresholds[label]) for label in LABELS}


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.texts_file is not None:
        texts = [line for line in Path(args.texts_file).read_text().splitlines() if line.strip()]
    else:
        texts = parity_sample_texts(args.cache, fold=args.fold, size=args.sample_size)

    manifest = export_and_verify(
        args.model_dir,
        args.out,
        texts,
        thresholds=_load_thresholds(args.thresholds),
        target=args.target,
        per_channel=args.per_channel,
        max_length=args.max_length,
    )
    int8 = manifest["parity_int8_vs_float"]
    print(
        f"parity OK on {int8['n_samples']} samples: "
        f"max|dlogit|={int8['max_abs_logit_delta']:.4f} "
        f"mean|dlogit|={int8['mean_abs_logit_delta']:.4f} "
        f"max|dprob|={int8['max_abs_prob_delta']:.4f} "
        f"flips={int8['n_flag_flips']}/{int8['n_decisions']} "
        f"argmax_disagreements={int8['n_argmax_disagreements']}"
    )
    print(f"int8 sha256 {manifest['int8_onnx']['sha256']}")
    print(f"size ratio int8/float {manifest['size_ratio_int8_over_float']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
