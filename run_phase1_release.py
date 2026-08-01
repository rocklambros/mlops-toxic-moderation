"""Phase 1 release: the once-only held-out evaluation, the fairness slice, and the registry.

Everything here happens AFTER the model has been chosen. Cross-validation chose it; the 31,877
held-out rows only *measure* it. Nothing in this file compares two candidates, and nothing in it
can be re-run against the same split: `model.evaluate.evaluate_on_test` refuses a second touch
through the git-tracked ledger at `docs/test-set-touch-log.md`.

Order is the control, not a convenience:

1. The W&B run opens first, so an authentication failure costs a second rather than burning the
   ledger entry that a failed run would leave unattached to any run id.
2. The held-out evaluation runs next, and its full result -- metrics, intervals, the fairness
   report, and the raw held-out probabilities -- is written to `artifacts/` **before** a single
   byte is uploaded. A network failure during a 400 MB artifact upload must not cost the one
   evaluation this split will ever get.
3. Re-invocation is safe by construction. If the ledger already carries this `split_version`,
   the saved result is reloaded and the held-out rows are never scored again. If the ledger
   carries it and the saved result is gone, this refuses rather than re-scoring.

The model evaluated is the classical `toxic-clf` skops artifact. DistilBERT is a build-time
comparison model that the delivery spec (section 6.1) deliberately does not promote and does not
evaluate on the held-out set: choosing between the two on test numbers is selection on the test
set, and it biases the winner upward.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

BUNDLE = Path(os.environ.get("PHASE1_BUNDLE", REPO / "data" / "cache" / "bundle"))
OUT = Path(os.environ.get("PHASE1_OUT", REPO / "artifacts"))
CV_DIR = Path(os.environ.get("PHASE1_CV_DIR", OUT / "classical-cv"))
MODEL_PATH = Path(os.environ.get("PHASE1_MODEL", OUT / "toxic-clf.skops"))

TEST_RESULT = OUT / "test_metrics.json"
TEST_PROBS = OUT / "test_probabilities.npz"
RECEIPT = OUT / "registry_receipt.json"
PUBLIC_CHECK = OUT / "registry_public_check.json"

SEED = 42
N_BOOT = 1000
WANDB_PROJECT = os.environ.get("WANDB_PROJECT") or "mlops-toxic-moderation"
WANDB_ENTITY = os.environ.get("WANDB_ENTITY") or "rockcyber"
# The registry rubric 1.3 is graded on is org-scoped, and the organization is NOT the team the
# run belongs to: runs live under `rockcyber`, the registry under `rockcyber-org`. Recording the
# team name yields a URL that 404s while still looking like evidence in a receipt.
WANDB_REGISTRY_ENTITY = os.environ.get("WANDB_REGISTRY_ENTITY") or "rockcyber-org"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------------------
# JSON round-tripping for CIResult
# --------------------------------------------------------------------------------------


def jsonable(obj):
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj) and not isinstance(obj, type):
        return jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating | float):
        value = float(obj)
        return value if np.isfinite(value) else None
    if isinstance(obj, np.bool_ | bool):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return jsonable(obj.tolist())
    return obj


def cis_from_json(payload: dict) -> dict:
    """Rebuild `CIResult` objects, because `build_run_summary` type-checks its argument.

    Reloading a saved evaluation must produce the same object graph the fresh path produces,
    or the reload path would silently skip the interval columns on the run page.
    """
    from model.evaluate import CIResult

    return {key: CIResult(**value) for key, value in payload.items()}


# --------------------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------------------


def load_full_bundle():
    """The ONLY place in this project that loads the cache `with_test=True`."""
    from model.train_distilbert import load_bundle_cache

    bundle = load_bundle_cache(BUNDLE, with_test=True)
    log(
        f"bundle: train={len(bundle.train_df)} test={len(bundle.test_df)} "
        f"split_version={bundle.split_version}"
    )
    return bundle


def load_thresholds() -> dict[str, float]:
    from model.labels import LABELS

    payload = json.loads((CV_DIR / "thresholds.json").read_text())
    thresholds = {label: float(payload[label]) for label in LABELS}
    log(f"thresholds tuned out-of-fold: {thresholds}")
    return thresholds


def load_model():
    import skops.io as sio

    from model.tracking import assert_safe_model_artifact, file_digest

    assert_safe_model_artifact(MODEL_PATH)
    digest = file_digest(MODEL_PATH)
    unknown = sorted(sio.get_untrusted_types(file=str(MODEL_PATH)))
    log(f"loading {MODEL_PATH.name} ({MODEL_PATH.stat().st_size / 1e6:.0f} MB), digest {digest}")
    log(f"skops types this load trusts explicitly: {unknown}")
    t0 = time.perf_counter()
    model = sio.load(str(MODEL_PATH), trusted=unknown)
    log(f"loaded in {time.perf_counter() - t0:.1f}s")
    return model, digest, unknown


def git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def env_version_check(bundle) -> None:
    """The final artifact was fitted on this split; refuse to score it against another."""
    footprint = json.loads((OUT / "feature_footprint.json").read_text())
    if footprint["split_version"] != bundle.split_version:
        raise SystemExit(
            f"the serialized model was fitted on split {footprint['split_version']} but the "
            f"bundle carries {bundle.split_version}; scoring one against the other is not an "
            f"evaluation of anything"
        )


# --------------------------------------------------------------------------------------
# the once-only held-out evaluation
# --------------------------------------------------------------------------------------


def evaluate_or_reload(bundle, thresholds, *, sha: str, run_id: str, allow_untracked: bool) -> dict:
    """Score the held-out set once, or reload the result the one permitted touch produced."""
    from model.evaluate import LEDGER_PATH, evaluate_on_test, read_touched_versions

    ledger = REPO / LEDGER_PATH
    touched = read_touched_versions(ledger)
    if bundle.split_version in touched:
        log(f"ledger already records split {bundle.split_version}; the rows are NOT re-scored")
        if not TEST_RESULT.is_file():
            raise SystemExit(
                f"{ledger} records this split but {TEST_RESULT} is gone. The held-out set is "
                f"evaluated once per split_version and this one is spent; recover the artifact "
                f"rather than re-scoring."
            )
        payload = json.loads(TEST_RESULT.read_text())
        payload["cis"] = cis_from_json(payload["cis"])
        payload["reloaded"] = True
        return payload

    log(f"evaluating the held-out {len(bundle.test_df)} rows -- once, for split "
        f"{bundle.split_version}")
    model, digest, _ = load_model()
    t0 = time.perf_counter()
    result = evaluate_on_test(
        bundle=bundle,
        model=model,
        thresholds=thresholds,
        git_sha=sha,
        run_id=run_id,
        ledger_path=ledger,
        n_boot=N_BOOT,
        seed=SEED,
        # The production ledger is normally required to be git-tracked, which is what makes the
        # guard survive a fresh clone. This invocation creates the file for the first time and is
        # not permitted to run `git add`, so the check is waived here and the commit is a
        # reported follow-up. The refusal itself is file-based and already live.
        require_tracked_ledger=not allow_untracked,
    )
    log(f"held-out evaluation in {(time.perf_counter() - t0) / 60:.1f} min")

    # Persist the raw predictions before anything else can fail. These are what a re-derivation
    # of any number in the model card runs against, and they cannot be regenerated.
    np.savez_compressed(
        TEST_PROBS,
        y_true=result["y_true"],
        y_prob=result["y_prob"],
        y_flag=result["y_flag"],
    )
    log(f"wrote {TEST_PROBS}")
    result["model_digest"] = digest
    result["reloaded"] = False
    return result


def write_test_result(result: dict, bundle) -> None:
    """`reloaded` is deliberately not persisted.

    It describes *this invocation*, not the evaluation. Writing it would make the artifact
    change on every re-read for no reason, and a file whose digest moves without its contents
    moving is a file nobody can use as evidence.
    """
    payload = {
        key: value
        for key, value in result.items()
        if key not in ("y_true", "y_prob", "y_flag", "reloaded")
    }
    payload["raw_sha256"] = bundle.raw_sha256
    payload["env_version"] = bundle.env_version
    payload["data_version"] = bundle.data_version
    payload["n_boot"] = N_BOOT
    payload["seed"] = SEED
    TEST_RESULT.write_text(json.dumps(jsonable(payload), indent=2, sort_keys=True) + "\n")
    log(f"wrote {TEST_RESULT}")


def write_fairness(result: dict) -> Path:
    from model.fairness import FAIRNESS_REPORT_PATH, write_fairness_report

    path = write_fairness_report(REPO / FAIRNESS_REPORT_PATH, result["fairness"])
    log(f"wrote {path}")
    return path


# --------------------------------------------------------------------------------------
# tracking + registry
# --------------------------------------------------------------------------------------


def hyperparameters() -> dict:
    from model.pipeline import (
        CALIBRATION_FOLDS,
        CALIBRATION_METHOD,
        CHAR_MAX_FEATURES,
        MAX_ITER,
        SOLVER,
        WORD_MAX_FEATURES,
    )

    return {
        "estimator": "OneVsRest(CalibratedClassifierCV(LogisticRegression))",
        "vectorizer": "FeatureUnion(word 1-2gram tfidf, char_wb 3-5gram tfidf)",
        "word_ngram_range": [1, 2],
        "word_min_df": 2,
        "word_max_features": WORD_MAX_FEATURES,
        "char_analyzer": "char_wb",
        "char_ngram_range": [3, 5],
        "char_min_df": 3,
        "char_max_features": CHAR_MAX_FEATURES,
        "sublinear_tf": True,
        "strip_accents": "unicode",
        "solver": SOLVER,
        "C": 1.0,
        "class_weight": "balanced",
        "max_iter": MAX_ITER,
        "calibration_method": CALIBRATION_METHOD,
        "calibration_folds": CALIBRATION_FOLDS,
        "inner_fits": 6 * 5 * CALIBRATION_FOLDS,
        "n_bootstrap": N_BOOT,
        "seed": SEED,
    }


def cv_summary() -> dict:
    """The out-of-fold numbers the promotion decision was actually made on."""
    payload = json.loads((CV_DIR / "cv_metrics.json").read_text())
    flat = {f"cv/{key}": value for key, value in payload["metrics"].items()
            if isinstance(value, int | float)}
    for key in ("macro_f1", "macro_pr_auc", "accuracy", "subset_accuracy"):
        stats = payload["per_fold_summary"][key]
        flat[f"cv_across_folds/{key}.mean"] = stats["mean"]
        flat[f"cv_across_folds/{key}.sd"] = stats["sd"]
    baseline = json.loads((OUT / "baseline_metrics.json").read_text())
    for key in ("macro_f1", "macro_pr_auc", "accuracy", "subset_accuracy"):
        flat[f"baseline/{key}"] = baseline["metrics"][key]
    return flat


def fairness_scalars(report: dict) -> dict:
    return {
        "fairness/background_fpr": report["background_fpr"],
        "fairness/background_flag_rate": report["background_flag_rate"],
        "fairness/max_fpr_gap": report["max_fpr_gap"],
        "fairness/max_f1_drop": report["max_f1_drop"],
        "fairness/four_fifths_ratio": report["four_fifths_ratio"],
        "fairness/n_terms_scored": report["n_terms_scored"],
        "fairness/n_terms_low_power": report["n_terms_low_power"],
        "fairness/n_terms_present": report["n_terms_present"],
        "fairness/material": report["material"],
    }


def publish(run, bundle, result, thresholds, *, sha: str, corpus) -> dict:
    from model.tracking import build_run_config, build_run_summary, log_model_artifact, log_run

    config = build_run_config(
        git_sha=sha,
        seed=SEED,
        bundle=bundle,
        model_name="classical-tfidf-ovr-calibrated-logreg",
        hyperparameters=hyperparameters(),
        thresholds=thresholds,
    )
    summary = build_run_summary(result["metrics"], result["cis"])
    summary = {f"test/{key}": value for key, value in summary.items()}
    summary.update(cv_summary())
    summary.update(fairness_scalars(result["fairness"]))
    summary["n_test_rows"] = result["n_test"]
    summary["n_bootstrap"] = N_BOOT
    log_run(run, config=config, summary=summary, corpus=corpus)
    log("run config and metrics logged")

    promoted = log_model_artifact(
        run,
        MODEL_PATH,
        config=config,
        metrics=result["metrics"],
        expected_digest=result.get("model_digest"),
        corpus=corpus,
        registry_entity=WANDB_REGISTRY_ENTITY,
        extra_metadata={
            "n_train_rows": int(len(bundle.train_df)),
            "n_test_rows": int(result["n_test"]),
            "test_macro_f1": result["metrics"]["macro_f1"],
            "test_macro_pr_auc": result["metrics"]["macro_pr_auc"],
            "test_accuracy": result["metrics"]["accuracy"],
            "cv_macro_f1_selection_basis": json.loads(
                (CV_DIR / "cv_metrics.json").read_text()
            )["metrics"]["macro_f1"],
            "fairness_max_fpr_gap": result["fairness"]["max_fpr_gap"],
            "serialization": "skops",
            "labels_are_positional": True,
        },
    )
    log(f"promoted {promoted.collection} -> {promoted.registry_target} "
        f"aliases={list(promoted.aliases)} digest={promoted.digest}")
    receipt = {
        "collection": promoted.collection,
        "aliases": list(promoted.aliases),
        "digest": promoted.digest,
        "registry_target": promoted.registry_target,
        "registry_url": promoted.url,
        "promotion_metric": promoted.promotion_metric,
        "promotion_value": promoted.promotion_value,
        "run_id": run.id,
        "run_url": run.url,
        "project_url": f"https://wandb.ai/{run.entity}/{run.project}",
        "git_sha": sha,
        "split_version": bundle.split_version,
        "raw_sha256": bundle.raw_sha256,
        "env_version": bundle.env_version,
    }
    RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    log(f"wrote {RECEIPT}")
    return receipt


# --------------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-wandb", action="store_true",
                        help="evaluate and write the reports, publish nothing")
    parser.add_argument("--allow-untracked-ledger", action="store_true",
                        help="waive the git-tracked ledger check, for the invocation that "
                             "creates the ledger for the first time")
    parser.add_argument("--run-name", default=os.environ.get("WANDB_RUN_NAME") or None)
    args = parser.parse_args()

    from model.seeds import assert_hash_seed_pinned, set_all_seeds

    assert_hash_seed_pinned()
    set_all_seeds(SEED)
    OUT.mkdir(parents=True, exist_ok=True)

    sha = git_sha()
    bundle = load_full_bundle()
    env_version_check(bundle)
    thresholds = load_thresholds()

    run = None
    if not args.no_wandb:
        if not os.environ.get("WANDB_API_KEY"):
            from model.train_distilbert import load_secret

            os.environ["WANDB_API_KEY"] = load_secret("wandb/api-key", "WANDB_API_KEY")
        import wandb

        run = wandb.init(
            project=WANDB_PROJECT,
            entity=WANDB_ENTITY,
            name=args.run_name,
            job_type="evaluate-register",
        )
        log(f"W&B run {run.id}: {run.url}")

    try:
        result = evaluate_or_reload(
            bundle, thresholds, sha=sha,
            run_id=(run.id if run else "no-wandb"),
            allow_untracked=args.allow_untracked_ledger,
        )
        write_test_result(result, bundle)
        write_fairness(result)

        metrics = result["metrics"]
        log(f"HELD-OUT macro_f1={metrics['macro_f1']:.4f} "
            f"macro_pr_auc={metrics['macro_pr_auc']:.4f} "
            f"accuracy={metrics['accuracy']:.4f} "
            f"subset_accuracy={metrics['subset_accuracy']:.4f}")
        fair = result["fairness"]
        log(f"FAIRNESS background_fpr={fair['background_fpr']:.4f} "
            f"max_fpr_gap={fair['max_fpr_gap']:.4f} ({fair['worst_term']}) "
            f"four_fifths={fair['four_fifths_ratio']}")

        if run is not None:
            corpus = bundle.test_df["comment_text"].astype(str).tolist()
            publish(run, bundle, result, thresholds, sha=sha, corpus=corpus)
    finally:
        if run is not None:
            run.finish()


if __name__ == "__main__":
    main()
