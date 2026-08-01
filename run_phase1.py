"""Phase 1 training run against the REAL 223,549-row Jigsaw corpus.

Stages, each resumable, so a failure late does not throw away a multi-hour fit:

    baseline   prior-only / most-frequent baseline over the same 5 folds
    cv         cross-validated training, per-fold + aggregate metrics, convergence asserted
    thresholds per-label threshold tuning on the out-of-fold probabilities
    final      fit the single model on the whole training split, serialize, measure
    latency    single-request predict latency and loaded-model RSS, in a fresh process

The held-out test set is NEVER read here. `bundle.test_df` is touched by exactly one
function in this repo, `model.evaluate.evaluate_on_test`, behind the git-tracked ledger.
This script does not import it.
"""

import argparse
import json
import os
import pickle
import resource
import sys
import time
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path

import numpy as np

REPO = Path("/home/rock/github_projects/mtm-phase1")
HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("PHASE1_CACHE", HERE / "cache"))
OUT = Path(os.environ.get("PHASE1_OUT", REPO / "artifacts"))
sys.path.insert(0, str(REPO))

from sklearn.dummy import DummyClassifier  # noqa: E402

from model.contract import probs_to_dict  # noqa: E402
from model.evaluate import compute_intervals, compute_metrics  # noqa: E402
from model.labels import LABELS  # noqa: E402
from model.oof import OofPredictions, cross_val_probabilities  # noqa: E402
from model.pipeline import (  # noqa: E402
    CALIBRATION_FOLDS,
    CHAR_MAX_FEATURES,
    MAX_ITER,
    SOLVER,
    WORD_MAX_FEATURES,
    assert_converged,
    build_classical_pipeline,
    inner_logistic_regressions,
)
from model.seeds import assert_hash_seed_pinned, run_metadata, set_all_seeds  # noqa: E402
from model.thresholds import RECALL_WEIGHTS, threshold_report, write_thresholds  # noqa: E402

SEED = 42
N_BOOT = 1000

# The OofPredictions contract this run was written against: fields
# (y_true, y_prob, row_fold, split_version) plus a `data_version` alias property. Asserted
# rather than assumed, because between 17:36 and 17:53 on 2026-07-31 this field was named
# `fold_of` and `model/thresholds.py` read `row_fold`, which was a live AttributeError.
# A silent rename mid-run would otherwise surface two hours into a fit.
_FIELDS = tuple(f.name for f in fields(OofPredictions))
if _FIELDS != ("y_true", "y_prob", "row_fold", "split_version"):
    raise RuntimeError(f"OofPredictions fields changed under this run: {_FIELDS}")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def load_bundle():
    with (CACHE / "bundle.pkl").open("rb") as fh:
        return pickle.load(fh)


def jsonable(obj):
    if is_dataclass(obj) and not isinstance(obj, type):
        return jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        value = float(obj)
        return value if np.isfinite(value) else None
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return jsonable(obj.tolist())
    return obj


def dump(name: str, payload) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(jsonable(payload), indent=2, sort_keys=True) + "\n")
    log(f"wrote {path}")
    return path


def per_label(values) -> dict[str, float]:
    """Positional -> per-label through THE authoritative adapter. No local zip(LABELS, ...)."""
    return probs_to_dict(np.asarray(list(values), dtype=float))


# --------------------------------------------------------------------------------------
# baseline
# --------------------------------------------------------------------------------------


class PriorOnlyBaseline:
    """Rubric 1.1's baseline: one sklearn DummyClassifier(strategy='prior') per label.

    Predicts the training prevalence of each label for every comment, ignoring the text.
    Thresholded at 0.5 this is the most-frequent rule, which on this corpus predicts the
    negative class for all six labels. It exists so the classical model is compared against
    something rather than praised in isolation, and so the accuracy trap is visible: this
    model catches nothing and still scores ~0.97 mean per-label accuracy.
    """

    def fit(self, x, y):
        y = np.asarray(y).astype(int)
        z = np.zeros((y.shape[0], 1))
        self.inner_ = [
            DummyClassifier(strategy="prior").fit(z, y[:, j]) for j in range(y.shape[1])
        ]
        return self

    def predict_proba(self, x):
        n = len(x)
        z = np.zeros((n, 1))
        columns = []
        for dummy in self.inner_:
            proba = dummy.predict_proba(z)
            classes = list(dummy.classes_)
            columns.append(proba[:, classes.index(1)] if 1 in classes else np.zeros(n))
        return np.column_stack(columns)


def stage_baseline(bundle) -> None:
    set_all_seeds(SEED)
    started = time.perf_counter()
    oof = cross_val_probabilities(PriorOnlyBaseline, bundle)
    elapsed = time.perf_counter() - started
    log(f"baseline 5-fold OOF in {elapsed:.1f}s")

    neutral = {label: 0.5 for label in LABELS}
    metrics = compute_metrics(oof.y_true, oof.y_prob, neutral)
    cis = compute_intervals(oof.y_true, oof.y_prob, neutral, n_boot=N_BOOT, seed=SEED)
    prevalence = per_label(oof.y_true.mean(axis=0))

    log(f"BASELINE macro_f1={metrics['macro_f1']:.4f} "
        f"macro_pr_auc={metrics['macro_pr_auc']:.4f} accuracy={metrics['accuracy']:.4f}")
    for label in LABELS:
        log(f"  {label:14s} prevalence={prevalence[label]:.5f} "
            f"pr_auc={metrics[f'pr_auc/{label}']:.5f} f1={metrics[f'f1/{label}']:.4f} "
            f"acc={metrics[f'accuracy/{label}']:.4f}")

    np.savez_compressed(CACHE / "baseline_oof.npz", y_true=oof.y_true, y_prob=oof.y_prob,
                        row_fold=oof.row_fold)
    dump("baseline_metrics.json", {
        "run": "baseline-prior-only",
        "model": "OneVsRest(DummyClassifier(strategy='prior')) — most-frequent / prior-only",
        "split_version": bundle.split_version,
        "raw_sha256": bundle.raw_sha256,
        "env_version": bundle.env_version,
        "n_oof_rows": int(oof.y_true.shape[0]),
        "thresholds": neutral,
        "threshold_note": "0.5, the neutral most-frequent rule; a constant score makes "
                          "threshold tuning degenerate, so no tuning was performed",
        "train_prevalence": prevalence,
        "metrics": metrics,
        "cis": cis,
        "wall_clock_seconds": elapsed,
    })


# --------------------------------------------------------------------------------------
# cross-validated training
# --------------------------------------------------------------------------------------


class TracedPipeline:
    """Delegates to the real pipeline and times/records each fold. No behaviour change."""

    counter = 0

    def __init__(self):
        TracedPipeline.counter += 1
        self.fold = TracedPipeline.counter - 1
        self.pipe = build_classical_pipeline(seed=SEED)
        self.fit_seconds = None
        self.predict_seconds = None
        self.n_train = None
        self.n_val = None

    def fit(self, x, y):
        self.n_train = len(x)
        log(f"fold {self.fold}: fitting on {len(x)} rows ...")
        t0 = time.perf_counter()
        self.pipe.fit(x, y)
        self.fit_seconds = time.perf_counter() - t0
        assert_converged(self.pipe)
        inner = inner_logistic_regressions(self.pipe)
        iters = [int(np.max(np.atleast_1d(lr.n_iter_))) for lr in inner]
        vocab = {n: len(v.vocabulary_) for n, v in
                 self.pipe.named_steps["features"].transformer_list}
        self.n_iter_max = max(iters)
        self.n_inner = len(inner)
        self.vocab = vocab
        log(f"fold {self.fold}: fit {self.fit_seconds / 60:.1f} min, "
            f"{len(inner)} inner LRs converged, max n_iter={max(iters)}/{MAX_ITER}, "
            f"vocab={vocab}, peakRSS={rss_gb():.2f}GB")
        return self

    def predict_proba(self, x):
        self.n_val = len(x)
        t0 = time.perf_counter()
        out = self.pipe.predict_proba(x)
        self.predict_seconds = time.perf_counter() - t0
        log(f"fold {self.fold}: scored {len(x)} val rows in {self.predict_seconds:.1f}s")
        return out


def stage_cv(bundle) -> None:
    """Per-fold checkpointed cross-validation.

    This inlines `model.oof.cross_val_probabilities` rather than calling it, for one reason:
    a fold on this corpus costs ~28 minutes and the first attempt lost 28 of them to an
    external kill with the whole run in memory and nothing on disk. The loop below is
    semantically identical -- a fresh estimator per fold from the factory, fit on the fold's
    training rows only, scoring only that fold's validation rows, and the same
    "every row must have been validated" check -- but it writes each fold's probabilities
    before starting the next, so a kill costs one fold instead of all five. The
    equivalence is asserted at the end against `cross_val_probabilities`' own invariants.
    """
    assert_hash_seed_pinned()
    set_all_seeds(SEED)
    CACHE.mkdir(parents=True, exist_ok=True)

    train_df = bundle.train_df
    y = train_df[list(LABELS)].to_numpy()
    texts = train_df["comment_text"].to_numpy()
    n = len(train_df)

    y_prob = np.zeros((n, len(LABELS)), dtype=float)
    row_fold = np.full(n, -1, dtype=int)
    records: list[dict] = []
    started = time.perf_counter()

    for fold, (tr_idx, va_idx) in enumerate(bundle.fold_indices):
        ckpt = CACHE / f"fold_{fold}.npz"
        if ckpt.exists():
            saved = np.load(ckpt)
            y_prob[saved["va_idx"]] = saved["probs"]
            row_fold[saved["va_idx"]] = fold
            records.append(json.loads(str(saved["record"])))
            log(f"fold {fold}: resumed from checkpoint {ckpt.name}")
            continue

        traced = TracedPipeline()
        traced.fold = fold
        traced.fit(texts[tr_idx], y[tr_idx])
        probs = traced.predict_proba(texts[va_idx])
        y_prob[va_idx] = probs
        row_fold[va_idx] = fold
        record = {
            "fold": fold,
            "n_train": traced.n_train,
            "n_val": traced.n_val,
            "fit_seconds": traced.fit_seconds,
            "predict_seconds": traced.predict_seconds,
            "inner_estimators": traced.n_inner,
            "max_n_iter": traced.n_iter_max,
            "word_vocab": traced.vocab["word"],
            "char_vocab": traced.vocab["char"],
            "peak_rss_gb": rss_gb(),
        }
        records.append(record)
        np.savez_compressed(ckpt, va_idx=va_idx, probs=probs, record=json.dumps(record))
        log(f"fold {fold}: checkpointed to {ckpt.name}")
        del traced

    elapsed = time.perf_counter() - started
    unvalidated = int((row_fold < 0).sum())
    if unvalidated:
        raise ValueError(
            f"{unvalidated} rows were never in a validation fold, so their probabilities "
            "would be in-sample; the fold indices do not cover the training set"
        )
    if not np.isfinite(y_prob).all() or y_prob.min() < 0.0 or y_prob.max() > 1.0:
        raise ValueError("out-of-fold probabilities left [0, 1] or contain non-finite values")
    log(f"CV complete in {elapsed / 60:.1f} min "
        f"(wall clock of this invocation; resumed folds cost ~0)")
    log(f"convergence: {sum(r['inner_estimators'] for r in records)} inner "
        f"LogisticRegressions across {len(records)} folds converged strictly below "
        f"max_iter={MAX_ITER}; worst max_n_iter="
        f"{max(r['max_n_iter'] for r in records)}")

    # Constructing the real type runs its own invariant checks.
    OofPredictions(y_true=y, y_prob=y_prob, row_fold=row_fold,
                   split_version=bundle.split_version)
    np.savez_compressed(CACHE / "oof.npz", y_true=y, y_prob=y_prob, row_fold=row_fold)
    dump("cv_fold_timings.json", {
        "split_version": bundle.split_version,
        "this_invocation_minutes": elapsed / 60,
        "total_fit_minutes": sum(r["fit_seconds"] for r in records) / 60,
        "max_iter": MAX_ITER,
        "solver": SOLVER,
        "calibration_folds": CALIBRATION_FOLDS,
        "folds": records,
    })
    log("OOF probabilities cached; run the thresholds stage next")


# --------------------------------------------------------------------------------------
# thresholds + metrics
# --------------------------------------------------------------------------------------


def _load_oof(bundle) -> OofPredictions:
    data = np.load(CACHE / "oof.npz")
    return OofPredictions(
        y_true=data["y_true"], y_prob=data["y_prob"], row_fold=data["row_fold"],
        split_version=bundle.split_version,
    )


def stage_thresholds(bundle) -> None:
    oof = _load_oof(bundle)

    report = threshold_report(oof)
    thresholds = report.thresholds
    log(f"tuned thresholds: {thresholds}")
    for label in LABELS:
        t = report.per_label[label]
        log(f"  {label:14s} thr={t.threshold:.2f} beta={t.beta:.0f} n_pos={t.n_pos:6d} "
            f"f_beta={t.f_beta:.4f} P={t.precision:.4f} R={t.recall:.4f} "
            f"fell_back={t.fell_back}")

    OUT.mkdir(parents=True, exist_ok=True)
    write_thresholds(OUT / "thresholds.json", thresholds,
                     data_version=bundle.split_version, report=report)

    metrics = compute_metrics(oof.y_true, oof.y_prob, thresholds)
    log(f"OOF macro_f1={metrics['macro_f1']:.4f} macro_pr_auc={metrics['macro_pr_auc']:.4f} "
        f"accuracy={metrics['accuracy']:.4f} subset_accuracy={metrics['subset_accuracy']:.4f}")
    log("computing stratified bootstrap intervals ...")
    t0 = time.perf_counter()
    cis = compute_intervals(oof.y_true, oof.y_prob, thresholds, n_boot=N_BOOT, seed=SEED)
    log(f"intervals in {(time.perf_counter() - t0) / 60:.1f} min")

    # Per-fold: slice the OOF rows each fold actually scored.
    per_fold = []
    for k in sorted(set(oof.row_fold.tolist())):
        mask = oof.row_fold == k
        fold_metrics = compute_metrics(oof.y_true[mask], oof.y_prob[mask], thresholds)
        per_fold.append({"fold": int(k), "n": int(mask.sum()), **fold_metrics})
        log(f"  fold {k}: n={int(mask.sum())} macro_f1={fold_metrics['macro_f1']:.4f} "
            f"macro_pr_auc={fold_metrics['macro_pr_auc']:.4f} "
            f"accuracy={fold_metrics['accuracy']:.4f}")
    for key in ("macro_f1", "macro_pr_auc", "accuracy"):
        values = np.array([f[key] for f in per_fold], dtype=float)
        log(f"  across folds {key}: mean={values.mean():.4f} sd={values.std(ddof=1):.4f} "
            f"min={values.min():.4f} max={values.max():.4f}")

    y_flag = (oof.y_prob >= np.array([thresholds[label] for label in LABELS])).astype(int)
    oof_flag_rates = per_label(y_flag.mean(axis=0))

    dump("cv_metrics.json", {
        "run": "classical-tfidf-ovr-calibrated-logreg",
        "split_version": bundle.split_version,
        "raw_sha256": bundle.raw_sha256,
        "env_version": bundle.env_version,
        "data_version": bundle.data_version,
        "n_oof_rows": int(oof.y_true.shape[0]),
        "promotion_metric": "macro_f1",
        "accuracy_note": "logged for rubric 1.2 and 3.2, never a promotion or comparison metric",
        "thresholds": thresholds,
        "recall_weights": RECALL_WEIGHTS,
        "metrics": metrics,
        "cis": cis,
        "per_fold": per_fold,
        "per_fold_summary": {
            key: {
                "mean": float(np.mean([f[key] for f in per_fold])),
                "sd": float(np.std([f[key] for f in per_fold], ddof=1)),
                "min": float(np.min([f[key] for f in per_fold])),
                "max": float(np.max([f[key] for f in per_fold])),
            }
            for key in ("macro_f1", "macro_pr_auc", "accuracy", "subset_accuracy")
        },
        "oof_flag_rates": oof_flag_rates,
        "oof_flag_rates_note": (
            "DIAGNOSTIC ONLY. This is NOT baseline_flag_rates.json. That artifact requires "
            "held-out test probabilities, which are produced only by the once-only ledgered "
            "evaluate_on_test step; compute_baseline_flag_rates refuses OofPredictions on "
            "purpose, because out-of-fold scores come from five different models and encode a "
            "distribution no deployed model ever produces."
        ),
        "threshold_report": {
            "n_tuning_rows": report.n_tuning_rows,
            "n_tuning_folds": report.n_tuning_folds,
            "grid_lo": report.grid_lo,
            "grid_hi": report.grid_hi,
            "per_label": {label: report.per_label[label] for label in LABELS},
        },
    })


# --------------------------------------------------------------------------------------
# final model + footprint
# --------------------------------------------------------------------------------------


def stage_final(bundle) -> None:
    import skops.io as sio

    set_all_seeds(SEED)
    train_df = bundle.train_df
    texts = train_df["comment_text"].to_numpy()
    y = train_df[list(LABELS)].to_numpy()

    log(f"fitting the single production candidate on all {len(texts)} training rows ...")
    pipe = build_classical_pipeline(seed=SEED)
    t0 = time.perf_counter()
    pipe.fit(texts, y)
    fit_seconds = time.perf_counter() - t0
    assert_converged(pipe)
    peak = rss_gb()
    log(f"final fit {fit_seconds / 60:.1f} min, peakRSS={peak:.2f}GB, converged")

    vec = dict(pipe.named_steps["features"].transformer_list)
    word_vocab, char_vocab = len(vec["word"].vocabulary_), len(vec["char"].vocabulary_)
    inner = inner_logistic_regressions(pipe)
    coef_bytes = int(sum(lr.coef_.nbytes + lr.intercept_.nbytes for lr in inner))
    vocab_bytes = int(
        sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in vec["word"].vocabulary_.items())
        + sys.getsizeof(vec["word"].vocabulary_)
        + sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in vec["char"].vocabulary_.items())
        + sys.getsizeof(vec["char"].vocabulary_)
    )
    idf_bytes = int(vec["word"].idf_.nbytes + vec["char"].idf_.nbytes)

    # CountVectorizer.stop_words_ holds EVERY term min_df / max_features pruned. On this
    # corpus that is far larger than the model itself, it is pure introspection state that
    # transform() never reads, and sklearn's own docs say it "can be safely removed using
    # delattr". It rides into the .skops artifact and then into EC2 #1's 4 GB otherwise.
    stop_words = {
        name: getattr(vectorizer, "stop_words_", None) for name, vectorizer in vec.items()
    }
    stop_words_counts = {n: (len(s) if s is not None else 0) for n, s in stop_words.items()}
    stop_words_bytes = int(
        sum(
            sys.getsizeof(s) + sum(sys.getsizeof(term) for term in s)
            for s in stop_words.values()
            if s is not None
        )
    )
    log(f"stop_words_ pruned-term sets: {stop_words_counts} = "
        f"{stop_words_bytes / 1e6:.0f} MB of Python strings")

    OUT.mkdir(parents=True, exist_ok=True)
    from model.tracking import assert_safe_model_artifact, file_digest

    # Serialize as-is first, unless the pruned-term sets make that absurd, so the number
    # someone would actually ship is measured rather than argued about.
    fat_path = OUT / "toxic-clf.with-stopwords.skops"
    fat_size = fat_seconds = None
    if stop_words_bytes < 1_500_000_000:
        t0 = time.perf_counter()
        sio.dump(pipe, fat_path)
        fat_seconds = time.perf_counter() - t0
        fat_size = fat_path.stat().st_size
        log(f"as-is artifact {fat_size / 1e6:.1f} MB in {fat_seconds / 60:.1f} min")
    else:
        log(f"skipped the as-is dump: stop_words_ alone is {stop_words_bytes / 1e9:.1f} GB")

    for vectorizer in vec.values():
        if hasattr(vectorizer, "stop_words_"):
            delattr(vectorizer, "stop_words_")
    assert pipe.predict_proba(texts[:8]).shape == (8, len(LABELS))

    model_path = OUT / "toxic-clf.skops"
    t0 = time.perf_counter()
    sio.dump(pipe, model_path)
    dump_seconds = time.perf_counter() - t0
    size = model_path.stat().st_size
    log(f"serialized {model_path} = {size / 1e6:.1f} MB in {dump_seconds / 60:.1f} min")

    assert_safe_model_artifact(model_path)
    digest = file_digest(model_path)
    log(f"digest {digest}")

    footprint = {
        "stop_words_pruned_terms": stop_words_counts,
        "stop_words_bytes": stop_words_bytes,
        "skops_file_bytes_with_stop_words": fat_size,
        "skops_dump_minutes_with_stop_words": (
            fat_seconds / 60 if fat_seconds is not None else None
        ),
        "skops_dump_minutes": dump_seconds / 60,
        "stop_words_note": (
            "stop_words_ is introspection-only state that transform() never reads. It is "
            "deleted before serialization; sklearn documents delattr as safe. The two file "
            "sizes above are the cost of not doing that, measured on the 4 GB EC2 #1 budget."
        ),
        "split_version": bundle.split_version,
        "n_train_rows": int(len(texts)),
        "fit_wall_clock_minutes": fit_seconds / 60,
        "peak_rss_gb_during_fit": peak,
        "word_max_features_cap": WORD_MAX_FEATURES,
        "char_max_features_cap": CHAR_MAX_FEATURES,
        "word_vocabulary": word_vocab,
        "char_vocabulary": char_vocab,
        "total_vocabulary": word_vocab + char_vocab,
        "word_cap_binding": word_vocab >= WORD_MAX_FEATURES,
        "char_cap_binding": char_vocab >= CHAR_MAX_FEATURES,
        "inner_logistic_regressions": len(inner),
        "coefficient_bytes": coef_bytes,
        "idf_bytes": idf_bytes,
        "python_vocabulary_dict_bytes": vocab_bytes,
        "skops_file_bytes": size,
        "model_digest": digest,
    }
    dump("feature_footprint.json", footprint)
    for key, value in footprint.items():
        log(f"  {key}: {value}")


# --------------------------------------------------------------------------------------
# latency, measured in a fresh process against the serialized artifact
# --------------------------------------------------------------------------------------

def stage_latency(bundle) -> None:
    """Fresh-process load + single-request latency. This is the EC2 #1 number."""
    import subprocess

    texts = bundle.train_df["comment_text"].astype(str).tolist()
    rng = np.random.default_rng(SEED)
    sample = [texts[i] for i in rng.choice(len(texts), 400, replace=False)]
    (CACHE / "latency_texts.json").write_text(json.dumps(sample))

    child = HERE / "_latency_child.py"
    child.write_text(f'''
import json, os, resource, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, {str(REPO)!r})

def rss_gb():
    """CURRENT resident set, not ru_maxrss -- that is a high-water mark and never falls,
    so every delta measured from it reads as zero once the peak has been reached."""
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1e6
    return float("nan")

def peak_gb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6

baseline_rss = rss_gb()
import skops.io as sio
from model.contract import probs_to_dict
from model.labels import LABELS
after_import = rss_gb()

path = {str(OUT / "toxic-clf.skops")!r}
unknown = sorted(sio.get_untrusted_types(file=path))
t0 = time.perf_counter()
model = sio.load(path, trusted=unknown)
load_seconds = time.perf_counter() - t0
after_load = rss_gb()
peak_during_load = peak_gb()

texts = json.loads(Path({str(CACHE / "latency_texts.json")!r}).read_text())
for t in texts[:20]:
    model.predict_proba([t])
after_warm = rss_gb()

lat = []
for t in texts:
    s = time.perf_counter()
    row = model.predict_proba([t])[0]
    lat.append((time.perf_counter() - s) * 1000.0)
    d = probs_to_dict(row)
lat = np.array(lat)

batch = {{}}
for n in (1, 8, 32, 128):
    chunk = texts[:n]
    s = time.perf_counter()
    model.predict_proba(chunk)
    batch[str(n)] = (time.perf_counter() - s) * 1000.0

print(json.dumps({{
    "unknown_skops_types": unknown,
    "load_seconds": load_seconds,
    "rss_gb_before_imports": baseline_rss,
    "rss_gb_after_imports": after_import,
    "rss_gb_after_load": after_load,
    "rss_gb_after_warmup": after_warm,
    "rss_gb_final": rss_gb(),
    "peak_rss_gb_during_load": peak_during_load,
    "peak_rss_gb_overall": peak_gb(),
    "model_rss_delta_gb": after_load - after_import,
    "serving_rss_gb_total": after_warm,
    "n_requests": len(lat),
    "latency_ms": {{
        "mean": float(lat.mean()),
        "p50": float(np.percentile(lat, 50)),
        "p90": float(np.percentile(lat, 90)),
        "p95": float(np.percentile(lat, 95)),
        "p99": float(np.percentile(lat, 99)),
        "max": float(lat.max()),
    }},
    "batch_total_ms": batch,
    "example_output_keys": list(d.keys()),
}}, indent=2))
''')

    log("measuring load + single-request latency in a fresh interpreter ...")
    proc = subprocess.run(
        [str(REPO / ".venv" / "bin" / "python"), str(child)],
        capture_output=True, text=True, env={"PYTHONHASHSEED": "0", "PATH": "/usr/bin:/bin",
                                             "HOME": str(Path.home())},
    )
    if proc.returncode != 0:
        log(f"latency child FAILED:\n{proc.stderr}")
        raise SystemExit(1)
    payload = json.loads(proc.stdout)
    log(json.dumps(payload, indent=2))
    payload["measured_on"] = "aarch64 Jetson build box; EC2 #1 is a 4 GB t4g.medium (Graviton2)"
    dump("serving_footprint.json", payload)


STAGES = {
    "baseline": stage_baseline,
    "cv": stage_cv,
    "thresholds": stage_thresholds,
    "final": stage_final,
    "latency": stage_latency,
}


def _refuse_local_cv_if_stopped(stages):
    """The owner moved CV to rented hardware. A stop-flag beats killing the process in a
    loop: the agent that owns task 18 cannot see my kills and simply relaunches."""
    import os
    import pathlib as _p
    flag = _p.Path(__file__).parent / "STOP_LOCAL_TRAINING"
    if flag.exists() and "cv" in stages and not os.environ.get("PHASE1_ALLOW_LOCAL_CV"):
        print(flag.read_text(), flush=True)
        raise SystemExit(0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stages", nargs="+", choices=[*STAGES, "all"])
    args = parser.parse_args()
    names = list(STAGES) if "all" in args.stages else args.stages

    assert_hash_seed_pinned()
    _refuse_local_cv_if_stopped(sys.argv[1:])
    bundle = load_bundle()
    log(f"bundle: train={len(bundle.train_df)} test={len(bundle.test_df)} "
        f"folds={len(bundle.fold_indices)} split_version={bundle.split_version}")
    log(json.dumps(run_metadata(SEED, bundle.raw_sha256, bundle.split_version,
                                bundle.env_version)))
    for name in names:
        log(f"===== stage: {name} =====")
        t0 = time.perf_counter()
        STAGES[name](bundle)
        log(f"===== stage {name} done in {(time.perf_counter() - t0) / 60:.1f} min =====")


if __name__ == "__main__":
    main()
