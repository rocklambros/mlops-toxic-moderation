"""Tests for the W&B tracking surface.

Nothing here touches the network and nothing here imports the `wandb` SDK. The run, the
artifact constructor, and the registry link are all injected, which is the only way this
module can be exercised on a build box that holds a live `WANDB_API_KEY`: an accidental
`wandb.init()` in a unit test would create real runs under the graded public project.

The corpus-leak cases are the sharp end. The W&B project and the Registry page are public by
owner decision (delivery spec sections 11 and 13), so a payload is the last place a raw user
comment could escape into a graded public artifact.
"""

import ast
import hashlib
import importlib
import sys
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest
import skops.io as sio
from sklearn.linear_model import LogisticRegression

from model.data.prepare import SplitConfig, prepare_dataset
from model.labels import LABELS
from model.tracking import (
    ARTIFACT_TYPE,
    FORBIDDEN_PROMOTION_KEYS,
    MODEL_COLLECTION,
    PRODUCTION_ALIAS,
    PROMOTION_METRIC,
    REGISTRY_PROJECT,
    ArtifactDigestMismatch,
    ForbiddenPromotionMetric,
    RawTextLeak,
    UnsafeArtifact,
    assert_no_raw_text,
    assert_safe_model_artifact,
    build_run_config,
    build_run_summary,
    log_model_artifact,
    log_run,
    public_registry_url,
)

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


# --------------------------------------------------------------------------------------
# fixtures and fakes
# --------------------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _bundle(seed: int = 42):
    """The real Phase 0 DatasetBundle. Cached: every call re-runs dedup and the split."""
    return prepare_dataset(FIXTURE, SplitConfig(seed=seed))


def _config(bundle=None):
    return build_run_config(
        git_sha="9f1c2ab",
        seed=42,
        bundle=bundle if bundle is not None else _bundle(),
        model_name="classical-tfidf-ovr-lr",
        hyperparameters={
            "C": 1.0,
            "solver": "liblinear",
            "word_max_features": 200_000,
            "char_max_features": 100_000,
            "calibration_method": "sigmoid",
            "calibration_folds": 5,
        },
        thresholds={label: 0.5 for label in LABELS},
    )


@dataclass(frozen=True)
class _CI:
    """The shape `model.metrics.CIResult` has. Duplicated so this file does not depend on
    another task landing first; `test_the_real_ciresult_flattens_identically` proves the two
    agree once `model/metrics.py` exists."""

    point: float
    lo: float | None
    hi: float | None
    n_pos: int
    n_neg: int
    n_boot: int
    low_power: bool
    reason: str | None


class FakeArtifact:
    def __init__(self, name=None, type=None, description=None, metadata=None):
        self.name = name
        self.type = type
        self.description = description
        self.metadata = dict(metadata or {})
        self.files: list[str] = []

    def add_file(self, local_path, name=None):
        self.files.append(str(local_path))
        return self


class FakeRun:
    """Just the surface `model.tracking` touches: config, summary, log, artifacts, links."""

    def __init__(self, entity="rocklambros", project="mlops-toxic-moderation"):
        self.entity = entity
        self.project = project
        self.id = "run-abc"
        self.config: dict = {}
        self.summary: dict = {}
        self.logged: list[dict] = []
        self.logged_artifacts: list[tuple] = []
        self.links: list[tuple] = []

    def log(self, payload):
        self.logged.append(dict(payload))

    def log_artifact(self, artifact, aliases=None):
        self.logged_artifacts.append((artifact, aliases))
        return artifact

    def link_artifact(self, artifact, target_path, aliases=None):
        self.links.append((artifact, target_path, aliases))
        return artifact


def _skops_file(tmp_path: Path, name: str = "toxic-clf.skops") -> Path:
    x = np.random.default_rng(0).random((30, 4))
    y = (x[:, 0] > 0.5).astype(int)
    model = LogisticRegression(solver="liblinear").fit(x, y)
    path = tmp_path / name
    sio.dump(model, path)
    return path


def _metrics():
    return {"macro_f1": 0.7412, "accuracy": 0.9721, "macro_pr_auc": 0.681}


# --------------------------------------------------------------------------------------
# run config: rubric 1.2 (code version, hyperparameters, metrics, data versions)
# --------------------------------------------------------------------------------------


def test_config_carries_every_field_rubric_1_2_names():
    cfg = _config()
    assert cfg["git_sha"] == "9f1c2ab"
    assert len(cfg["data_version"]) == 64
    assert cfg["seed"] == 42
    assert cfg["model_name"] == "classical-tfidf-ovr-lr"
    assert cfg["hyperparameters"]["solver"] == "liblinear"
    assert cfg["hyperparameters"]["word_max_features"] == 200_000
    assert set(cfg["thresholds"]) == set(LABELS)


def test_config_carries_all_three_provenance_fields_not_one_composite():
    """Remediation 3.12. One `data_version` string cannot answer the question anyone asks
    when a number moves: did the corpus change, did the split change, or did the environment
    change? Rubric 1.2 says 'data versions', plural."""
    bundle = _bundle()
    cfg = _config(bundle)
    assert cfg["raw_sha256"] == bundle.raw_sha256
    assert cfg["split_version"] == bundle.split_version
    assert cfg["env_version"] == bundle.env_version
    assert cfg["data_version"] == bundle.data_version
    assert len({cfg["raw_sha256"], cfg["split_version"], cfg["env_version"]}) == 3


def test_a_seed_change_moves_split_version_alone_on_the_run_page():
    ca, cb = _config(_bundle(seed=42)), _config(_bundle(seed=7))
    assert ca["split_version"] != cb["split_version"]
    assert ca["raw_sha256"] == cb["raw_sha256"]
    assert ca["env_version"] == cb["env_version"]


def test_the_composite_is_derived_from_the_three_and_cannot_drift():
    cfg = _config()
    joined = f"{cfg['raw_sha256']}:{cfg['split_version']}:{cfg['env_version']}"
    assert cfg["data_version"] == hashlib.sha256(joined.encode()).hexdigest()


def test_build_run_config_refuses_a_bare_string_data_version():
    """Passing the composite alone is the regression this guard exists to prevent."""
    with pytest.raises(TypeError, match="requires the DatasetBundle"):
        build_run_config(
            git_sha="x", seed=42, bundle="d" * 64, model_name="m",
            hyperparameters={}, thresholds={},
        )


def test_config_copies_its_inputs_so_a_later_mutation_cannot_rewrite_history():
    hyper = {"C": 1.0}
    thresholds = {label: 0.5 for label in LABELS}
    cfg = build_run_config(
        git_sha="x", seed=42, bundle=_bundle(), model_name="m",
        hyperparameters=hyper, thresholds=thresholds,
    )
    hyper["C"] = 99.0
    thresholds["toxic"] = 0.99
    assert cfg["hyperparameters"]["C"] == 1.0
    assert cfg["thresholds"]["toxic"] == 0.5


# --------------------------------------------------------------------------------------
# run summary: accuracy is logged, intervals are flattened
# --------------------------------------------------------------------------------------


def test_summary_includes_accuracy_and_flattens_confidence_intervals():
    metrics = {"macro_f1": 0.74, "accuracy": 0.91, "pr_auc/threat": 0.31}
    cis = {"pr_auc/threat": _CI(0.31, 0.18, 0.44, 72, 23_859, 1000, False, None)}
    summary = build_run_summary(metrics, cis)
    assert summary["accuracy"] == 0.91  # rubric 1.2 and 3.2 name it explicitly
    assert summary["macro_f1"] == 0.74
    assert summary["pr_auc/threat"] == 0.31
    assert summary["pr_auc/threat.ci_lo"] == 0.18
    assert summary["pr_auc/threat.ci_hi"] == 0.44
    assert summary["pr_auc/threat.n_pos"] == 72
    assert summary["pr_auc/threat.low_power"] is False


def test_a_low_power_interval_keeps_none_bounds_instead_of_a_silent_zero():
    """A label with no positives has no interval. Coercing that to 0.0 would put a floor of
    zero on the dashboard and make an unmeasurable slice look measured and terrible."""
    cis = {"pr_auc/threat": _CI(float("nan"), None, None, 0, 120, 0, True, "0 positives")}
    summary = build_run_summary({"macro_f1": 0.5}, cis)
    assert summary["pr_auc/threat.ci_lo"] is None
    assert summary["pr_auc/threat.ci_hi"] is None
    assert summary["pr_auc/threat.low_power"] is True


def test_summary_does_not_mutate_the_metrics_it_was_handed():
    metrics = {"macro_f1": 0.74}
    build_run_summary(metrics, {"macro_f1": _CI(0.74, 0.7, 0.8, 100, 900, 1000, False, None)})
    assert metrics == {"macro_f1": 0.74}


def test_summary_refuses_an_object_that_is_not_an_interval():
    with pytest.raises(TypeError, match="confidence interval"):
        build_run_summary({"macro_f1": 0.74}, {"macro_f1": 0.74})


def _symbol(name: str):
    """Find a symbol in whichever sibling module ended up owning it.

    The plan puts `CIResult` and `PROMOTION_METRIC` in `model/metrics.py`; the implementation
    landed them in `model/evaluate.py`. Searching both keeps this a drift guard rather than a
    guess about someone else's file layout.
    """
    for module_name in ("model.metrics", "model.evaluate"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if hasattr(module, name):
            return getattr(module, name)
    pytest.skip(f"no sibling module defines {name} yet")


def test_the_real_ciresult_flattens_identically():
    """Drift guard against the module that owns the real interval type."""
    real = _symbol("CIResult")(0.31, 0.18, 0.44, 72, 23_859, 1000, False, None)
    fake = _CI(0.31, 0.18, 0.44, 72, 23_859, 1000, False, None)
    assert build_run_summary({}, {"pr_auc/threat": real}) == build_run_summary(
        {}, {"pr_auc/threat": fake}
    )


# --------------------------------------------------------------------------------------
# the raw-text leak check
# --------------------------------------------------------------------------------------


def test_a_corpus_comment_in_the_payload_is_caught_before_it_is_sent():
    corpus = ["you are an idiot", "have a nice day friend"]
    with pytest.raises(RawTextLeak, match="raw comment text"):
        assert_no_raw_text({"worst_example": "you are an idiot"}, corpus)
    with pytest.raises(RawTextLeak):
        assert_no_raw_text({"nested": {"rows": ["have a nice day friend"]}}, corpus)


def test_a_comment_quoted_inside_a_longer_string_is_still_caught():
    corpus = ["have a nice day friend"]
    with pytest.raises(RawTextLeak):
        assert_no_raw_text({"note": "sample row: have a nice day friend (id c000)"}, corpus)


def test_a_comment_used_as_a_dict_key_is_caught():
    """Per-example panels key on the example. Scanning values only would miss it."""
    corpus = ["have a nice day friend"]
    with pytest.raises(RawTextLeak):
        assert_no_raw_text({"have a nice day friend": 0.98}, corpus)


def test_a_clean_payload_passes_the_leak_check():
    corpus = ["you are an idiot", "have a nice day friend"]
    assert_no_raw_text({"macro_f1": 0.74, "git_sha": "9f1c", "note": "no user text here"}, corpus)


def test_short_corpus_entries_do_not_produce_false_positives():
    """A one-word comment would otherwise match half the metric names."""
    assert_no_raw_text({"decision": "allow"}, ["allow", "hi"])


# --------------------------------------------------------------------------------------
# log_run
# --------------------------------------------------------------------------------------


def test_log_run_sends_the_config_and_the_summary():
    run = FakeRun()
    log_run(run, config=_config(), summary={"macro_f1": 0.74, "accuracy": 0.91}, corpus=["idiot"])
    assert run.config["git_sha"] == "9f1c2ab"
    assert run.config["split_version"] == _bundle().split_version
    assert run.logged[0]["macro_f1"] == 0.74
    assert run.logged[0]["accuracy"] == 0.91
    assert run.summary["accuracy"] == 0.91


def test_log_run_refuses_a_leaking_summary_without_sending_anything():
    """The check must run before the first write, or the leak is already public when it
    raises: `run.config.update` is a network side effect, not a local one."""
    run = FakeRun()
    corpus = ["you are an idiot"]
    with pytest.raises(RawTextLeak):
        log_run(run, config=_config(), summary={"sample": "you are an idiot"}, corpus=corpus)
    assert run.config == {}
    assert run.logged == []
    assert run.summary == {}


# --------------------------------------------------------------------------------------
# artifact logging, digest recording, and the promoted registry stage (rubric 1.3)
# --------------------------------------------------------------------------------------


def test_the_artifact_carries_the_digest_and_the_provenance_as_metadata(tmp_path):
    run, path = FakeRun(), _skops_file(tmp_path)
    promoted = log_model_artifact(
        run, path, config=_config(), metrics=_metrics(), artifact_factory=FakeArtifact
    )
    artifact, aliases = run.logged_artifacts[0]
    assert artifact.name == MODEL_COLLECTION
    assert artifact.type == ARTIFACT_TYPE
    assert artifact.files == [str(path)]
    assert aliases == [PRODUCTION_ALIAS]
    assert artifact.metadata["model_digest"] == promoted.digest
    for field in ("git_sha", "raw_sha256", "split_version", "env_version", "data_version"):
        assert artifact.metadata[field] == _config()[field]
    assert artifact.metadata["promotion_metric"] == PROMOTION_METRIC
    assert artifact.metadata[PROMOTION_METRIC] == pytest.approx(0.7412)


def test_the_digest_also_lands_on_the_run_page_that_gets_graded(tmp_path):
    """The digest in the artifact ties the artifact to itself. The digest in the run summary
    is what lets a grader (or an incident) walk run -> artifact -> deployed model."""
    run, path = FakeRun(), _skops_file(tmp_path)
    promoted = log_model_artifact(
        run, path, config=_config(), metrics=_metrics(), artifact_factory=FakeArtifact
    )
    assert run.summary["model_digest"] == promoted.digest
    assert run.summary["registry_target"] == promoted.registry_target
    assert run.summary["registry_url"] == promoted.url
    assert run.summary["promoted_aliases"] == list(promoted.aliases)


def test_the_recorded_digest_is_the_sha256_of_the_bytes_on_disk(tmp_path):
    run, path = FakeRun(), _skops_file(tmp_path)
    promoted = log_model_artifact(
        run, path, config=_config(), metrics=_metrics(), artifact_factory=FakeArtifact
    )
    assert promoted.digest == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_digest_that_disagrees_with_the_file_is_refused(tmp_path):
    """H14: the digest is recorded independently of the artifact. If the file that reaches
    W&B is not the file that was measured, that must stop the upload, not be papered over."""
    run, path = FakeRun(), _skops_file(tmp_path)
    with pytest.raises(ArtifactDigestMismatch, match="does not match"):
        log_model_artifact(
            run, path, config=_config(), metrics=_metrics(),
            expected_digest="sha256:" + "0" * 64, artifact_factory=FakeArtifact,
        )
    assert run.logged_artifacts == []
    assert run.links == []


def test_a_matching_expected_digest_is_accepted_in_either_form(tmp_path):
    path = _skops_file(tmp_path)
    bare = hashlib.sha256(path.read_bytes()).hexdigest()
    for expected in (bare, f"sha256:{bare}"):
        promoted = log_model_artifact(
            FakeRun(), path, config=_config(), metrics=_metrics(),
            expected_digest=expected, artifact_factory=FakeArtifact,
        )
        assert promoted.digest == f"sha256:{bare}"


def test_the_artifact_is_linked_to_the_registry_at_a_promoted_stage(tmp_path):
    """Rubric 1.3 is graded on the Registry page showing a promoted stage, not on the run.

    The link target carries no entity prefix. W&B migrated the per-entity `model-registry`
    project away and the backend answers `<team>/model-registry/<collection>` with a 400; the
    migrated registry is org-scoped and resolves the organization from the source artifact.
    """
    run, path = FakeRun(), _skops_file(tmp_path)
    promoted = log_model_artifact(
        run, path, config=_config(), metrics=_metrics(), artifact_factory=FakeArtifact
    )
    artifact, target_path, aliases = run.links[0]
    assert artifact is run.logged_artifacts[0][0]
    assert target_path == f"{REGISTRY_PROJECT}/{MODEL_COLLECTION}"
    assert "rocklambros" not in target_path
    assert aliases == [PRODUCTION_ALIAS]
    assert promoted.registry_target == target_path
    assert promoted.url == public_registry_url("rocklambros")
    assert promoted.url.endswith(f"/{REGISTRY_PROJECT}/artifacts/model/{MODEL_COLLECTION}")


def test_the_registry_entity_overrides_the_runs_team_for_the_public_url(tmp_path):
    """The registry page lives under the ORGANIZATION, which is not the team the run is in.

    On this account the run's entity is `rockcyber` and the registry is `rockcyber-org`.
    Recording the team's name yields a URL that 404s while still looking like evidence.
    """
    run, path = FakeRun(), _skops_file(tmp_path)
    promoted = log_model_artifact(
        run, path, config=_config(), metrics=_metrics(),
        registry_entity="rocklambros-org", artifact_factory=FakeArtifact,
    )
    assert promoted.url == public_registry_url("rocklambros-org")


def test_the_legacy_registry_project_is_named_but_not_used():
    """Kept as a constant so the migration is documented where the reader looks for it."""
    from model.tracking import LEGACY_REGISTRY_PROJECT

    assert LEGACY_REGISTRY_PROJECT == "model-registry"
    assert REGISTRY_PROJECT != LEGACY_REGISTRY_PROJECT


def test_linking_without_a_promoted_stage_is_refused(tmp_path):
    run, path = FakeRun(), _skops_file(tmp_path)
    with pytest.raises(ValueError, match="rubric 1.3"):
        log_model_artifact(
            run, path, config=_config(), metrics=_metrics(),
            aliases=("latest",), artifact_factory=FakeArtifact,
        )
    assert run.logged_artifacts == []


def test_a_staging_alias_is_also_a_promoted_stage(tmp_path):
    """Rubric 1.3 accepts Staging or Production; the project promotes to Production."""
    run, path = FakeRun(), _skops_file(tmp_path)
    promoted = log_model_artifact(
        run, path, config=_config(), metrics=_metrics(),
        aliases=("staging",), artifact_factory=FakeArtifact,
    )
    assert promoted.aliases == ("staging",)
    assert run.links[0][2] == ["staging"]


def test_a_run_without_an_entity_cannot_guess_the_registry_path(tmp_path):
    """`f"{None}/model-registry/toxic-clf"` would link into nowhere and still look like it
    worked. `wandb.init(entity=None)` is the default, so this is reachable by omission."""
    run, path = FakeRun(entity=None), _skops_file(tmp_path)
    with pytest.raises(ValueError, match="entity"):
        log_model_artifact(
            run, path, config=_config(), metrics=_metrics(), artifact_factory=FakeArtifact
        )
    assert run.logged_artifacts == []


def test_an_explicit_target_path_overrides_the_legacy_registry_project(tmp_path):
    """The newer W&B registry addresses collections as `wandb-registry-model/<collection>`."""
    run, path = FakeRun(entity=None), _skops_file(tmp_path)
    promoted = log_model_artifact(
        run, path, config=_config(), metrics=_metrics(),
        target_path=f"wandb-registry-model/{MODEL_COLLECTION}", artifact_factory=FakeArtifact,
    )
    assert run.links[0][1] == f"wandb-registry-model/{MODEL_COLLECTION}"
    assert promoted.registry_target == f"wandb-registry-model/{MODEL_COLLECTION}"


def test_a_pickle_or_joblib_artifact_is_refused(tmp_path):
    """Safe serialization is normative (delivery spec section 6.3): skops only, never pickle."""
    run = FakeRun()
    for name in ("model.pkl", "model.joblib"):
        bad = tmp_path / name
        bad.write_bytes(b"anything")
        with pytest.raises(UnsafeArtifact, match="skops"):
            log_model_artifact(
                run, bad, config=_config(), metrics=_metrics(), artifact_factory=FakeArtifact
            )
    assert run.logged_artifacts == []


def test_a_renamed_file_that_is_not_a_skops_archive_is_refused(tmp_path):
    """A suffix check alone would pass a pickle renamed to .skops."""
    bad = tmp_path / "toxic-clf.skops"
    bad.write_bytes(b"\x80\x05not-a-zip")
    with pytest.raises(UnsafeArtifact, match="not a skops archive"):
        assert_safe_model_artifact(bad)


def test_a_skops_archive_missing_its_schema_is_refused(tmp_path):
    bad = tmp_path / "toxic-clf.skops"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("payload.npy", b"\x00")
    with pytest.raises(UnsafeArtifact, match="not a skops archive"):
        assert_safe_model_artifact(bad)


def test_a_real_skops_archive_passes(tmp_path):
    assert_safe_model_artifact(_skops_file(tmp_path))


def test_a_skops_below_the_advisory_floor_refuses_to_upload(tmp_path, monkeypatch):
    """skops 0.13.0 closed three high-severity advisories in the loader Phase 2 runs against
    this artifact. Publishing a model built by an older skops to a public registry is the
    failure this guard removes."""
    monkeypatch.setattr("model.tracking.installed_skops_version", lambda: "0.12.0")
    with pytest.raises(UnsafeArtifact, match="0.13.0"):
        assert_safe_model_artifact(_skops_file(tmp_path))


def test_promotion_on_accuracy_is_refused(tmp_path):
    """An all-negative predictor scores about 90% accuracy on this corpus."""
    run, path = FakeRun(), _skops_file(tmp_path)
    with pytest.raises(ForbiddenPromotionMetric, match="banned as a promotion metric"):
        log_model_artifact(
            run, path, config=_config(), metrics=_metrics(),
            promotion_metric="accuracy", artifact_factory=FakeArtifact,
        )
    with pytest.raises(ForbiddenPromotionMetric):
        log_model_artifact(
            run, path, config=_config(), metrics={"accuracy/threat": 0.99},
            promotion_metric="accuracy/threat", artifact_factory=FakeArtifact,
        )
    assert run.logged_artifacts == []


def test_promotion_requires_the_metric_it_promotes_on_to_be_present(tmp_path):
    run, path = FakeRun(), _skops_file(tmp_path)
    with pytest.raises(ValueError, match="macro_f1"):
        log_model_artifact(
            run, path, config=_config(), metrics={"accuracy": 0.97},
            artifact_factory=FakeArtifact,
        )


def test_artifact_metadata_is_leak_checked_against_the_corpus(tmp_path):
    run, path = FakeRun(), _skops_file(tmp_path)
    with pytest.raises(RawTextLeak):
        log_model_artifact(
            run, path, config=_config(), metrics=_metrics(),
            extra_metadata={"worst_false_negative": "you are an idiot and i mean it"},
            corpus=["you are an idiot and i mean it"], artifact_factory=FakeArtifact,
        )
    assert run.logged_artifacts == []


def test_a_config_without_provenance_cannot_be_attached_to_an_artifact(tmp_path):
    run, path = FakeRun(), _skops_file(tmp_path)
    with pytest.raises(TypeError, match="build_run_config"):
        log_model_artifact(
            run, path, config={"git_sha": "9f1c"}, metrics=_metrics(),
            artifact_factory=FakeArtifact,
        )


# --------------------------------------------------------------------------------------
# hygiene and drift guards
# --------------------------------------------------------------------------------------


def test_importing_tracking_does_not_import_the_wandb_sdk():
    """The SDK is injected, not imported at module scope: importing `wandb` reads ~/.netrc
    and WANDB_API_KEY, and this suite runs on the box that holds the live key.

    The source is inspected rather than only `sys.modules`, so the guard still means
    something on a machine where wandb happens to be installed and already imported."""
    assert "model.tracking" in sys.modules
    assert "wandb" not in sys.modules
    tree = ast.parse(Path("model/tracking.py").read_text())
    top_level_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    names = {
        alias.name.split(".")[0]
        for node in top_level_imports
        for alias in getattr(node, "names", [])
    } | {getattr(node, "module", "") or "" for node in top_level_imports}
    assert "wandb" not in names


def test_the_registry_constants_match_model_registry_when_it_lands():
    """Drift guard against `model/registry.py` (plan Task 15), whose anonymous public-page
    check queries exactly these names. Two spellings of `production` would pass both test
    suites and still fail rubric 1.3 on the graded page."""
    registry = pytest.importorskip("model.registry")
    assert registry.COLLECTION == MODEL_COLLECTION
    assert registry.PROMOTED_ALIAS == PRODUCTION_ALIAS
    assert registry.REGISTRY_PROJECT == REGISTRY_PROJECT


def test_the_promotion_metric_matches_the_module_that_selects_runs():
    """If run selection promotes on `macro_f1` while the artifact records something else as
    its promotion reason, the registry page and the decision no longer describe each other."""
    assert _symbol("PROMOTION_METRIC") == PROMOTION_METRIC
    assert _symbol("FORBIDDEN_PROMOTION_KEYS") == FORBIDDEN_PROMOTION_KEYS
