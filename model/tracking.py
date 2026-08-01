"""W&B run payloads, model-artifact logging, and the promoted Registry stage.

Rubric 1.2 requires every run to log the code version (git commit), the hyperparameters, the
performance metrics including accuracy, and the data version**s**. Rubric 1.3 is graded on the
Registry page itself, publicly visible while logged out, showing `toxic-clf` at a promoted
stage -- the instructor confirmed on 2026-07-31 that a public *project* does not satisfy it
(delivery spec sections 11 and 13).

Four properties are load-bearing here, and each one is enforced by something that raises:

- **Three provenance fields, not one composite.** Phase 0 split `data_version` into
  `raw_sha256`, `split_version`, and `env_version` so that a moved number can be attributed:
  did the corpus change, did the split change, or did the environment change? A single opaque
  hash answers none of those. `build_run_config` therefore takes the `DatasetBundle` itself,
  so a caller cannot silently regress to the composite string.
- **No raw comment text ever leaves for a public surface.** The W&B project and the Registry
  page are public by owner decision, which makes the payload the last place a user comment
  could escape into a graded artifact. Every payload and every piece of artifact metadata is
  scanned against the corpus *before* the first write.
- **The artifact digest is recorded with the artifact, and cross-checked against the bytes on
  disk.** SHA-256 proves integrity in transit, not provenance (premortem H14); recording the
  digest in the immutable artifact version, in the git-committed model card, and refusing to
  upload when the two disagree is what closes the poisoned-artifact path.
- **Accuracy is logged, never promoted on.** An all-negative predictor scores about 90% on
  this corpus, so `log_model_artifact` refuses to record an accuracy-flavoured metric as the
  reason a version was promoted.

The W&B SDK is injected, never imported at module scope: importing `wandb` reads `~/.netrc`
and `WANDB_API_KEY`, and this module is unit-tested on a box that holds a live key.
"""

import hashlib
import re
import zipfile
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

# The Registry surface rubric 1.3 is graded on. `scripts/verify_public_registry.py` verifies
# exactly these names anonymously; its checks and this module cross-check the constants so two
# spellings of "production" cannot pass both and still fail on the graded page.
#
# `model-registry` is the LEGACY per-entity registry project. W&B migrated it away on this
# organization and the backend now answers a link into it with
# `400 The model registry has been migrated for teams in your organization. You may no longer
# make changes.` -- observed 2026-08-01 against `rockcyber/model-registry/toxic-clf`. The
# migrated registry is org-scoped and is addressed WITHOUT an entity prefix, because W&B
# resolves the organization from the source artifact rather than from the path.
REGISTRY_PROJECT = "wandb-registry-model"
LEGACY_REGISTRY_PROJECT = "model-registry"
MODEL_COLLECTION = "toxic-clf"
PRODUCTION_ALIAS = "production"
ARTIFACT_TYPE = "model"
PROMOTED_STAGES = frozenset({"production", "staging"})

# Selection metric. Mirrors model/metrics.py, which owns run selection.
PROMOTION_METRIC = "macro_f1"
FORBIDDEN_PROMOTION_KEYS = frozenset({"accuracy", "subset_accuracy", "macro_accuracy"})

# skops 0.13.0 closed three high-severity advisories in the loader Phase 2 runs against this
# artifact. Publishing a model serialized by an older skops to a public registry is exactly
# the supply-chain shape this project is graded on avoiding.
MIN_SKOPS_VERSION = (0, 13, 0)
MODEL_SUFFIX = ".skops"
_SKOPS_SCHEMA_MEMBER = "schema.json"
_PROVENANCE_FIELDS = ("git_sha", "raw_sha256", "split_version", "env_version", "data_version")
_CI_FIELDS = ("lo", "hi", "n_pos", "low_power")


class RawTextLeak(RuntimeError):
    """A payload bound for a public surface contains a raw corpus comment."""


class UnsafeArtifact(RuntimeError):
    """The file about to be uploaded is not a skops archive written by a patched skops."""


class ArtifactDigestMismatch(RuntimeError):
    """The bytes on disk are not the bytes whose digest was recorded elsewhere."""


class ForbiddenPromotionMetric(ValueError):
    """Promotion was justified by a metric the design bans for selection."""


@dataclass(frozen=True)
class PromotedArtifact:
    """What was published, under which digest, at which stage, and where to look at it."""

    collection: str
    aliases: tuple[str, ...]
    digest: str
    registry_target: str
    url: str | None  # None only when an explicit target_path bypassed the entity
    promotion_metric: str
    promotion_value: float


def build_run_config(
    *,
    git_sha: str,
    seed: int,
    bundle,
    model_name: str,
    hyperparameters: dict[str, Any],
    thresholds: dict[str, float],
) -> dict[str, Any]:
    """Rubric 1.2 says "data version**s**". Log all three, plus the composite for display.

    The bundle is required rather than a string so a caller cannot silently regress to the
    composite: that regression is exactly what happened between Phase 0 and Phase 1 and it was
    invisible because nothing typed the argument (remediation 3.12).
    """
    for field in ("raw_sha256", "split_version", "env_version", "data_version"):
        if not hasattr(bundle, field):
            raise TypeError(
                "build_run_config requires the DatasetBundle, not a bare data_version "
                f"string: {field!r} is missing"
            )
    return {
        "git_sha": git_sha,
        "seed": seed,
        "raw_sha256": bundle.raw_sha256,
        "split_version": bundle.split_version,
        "env_version": bundle.env_version,
        "data_version": bundle.data_version,
        "model_name": model_name,
        "hyperparameters": dict(hyperparameters),
        "thresholds": dict(thresholds),
    }


def build_run_summary(metrics: dict[str, float], cis: dict[str, Any]) -> dict[str, Any]:
    """Flatten metrics and their intervals into scalars W&B can chart.

    `accuracy` passes straight through: rubric 1.2 lists it among the metrics each run logs
    and rubric 3.2 puts it on the dashboard. `model.metrics.select_best_run` is what stops it
    becoming a decision input.

    A low-power interval keeps `None` bounds rather than being coerced to 0.0. Zero is a
    number a reader trusts; `None` is a gap they ask about, and for a label with no positives
    the gap is the truth.
    """
    summary: dict[str, Any] = dict(metrics)
    for key, ci in cis.items():
        missing = [field for field in _CI_FIELDS if not hasattr(ci, field)]
        if missing:
            raise TypeError(
                f"{key!r} is not a confidence interval: {type(ci).__name__} is missing "
                f"{missing}. Pass model.metrics.CIResult"
            )
        summary[f"{key}.ci_lo"] = ci.lo
        summary[f"{key}.ci_hi"] = ci.hi
        summary[f"{key}.n_pos"] = ci.n_pos
        summary[f"{key}.low_power"] = ci.low_power
    return summary


def _strings(value: Any) -> list[str]:
    """Every string anywhere in a payload, keys included.

    Keys matter: a per-example panel keyed on the example itself would slip past a
    values-only scan while publishing the comment verbatim.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.extend(_strings(key))
            out.extend(_strings(item))
        return out
    if isinstance(value, list | tuple | set | frozenset):
        return [s for item in value for s in _strings(item)]
    return []


def assert_no_raw_text(payload: Any, corpus, *, min_length: int = 12) -> None:
    """Refuse to send a payload containing any corpus comment.

    Entries shorter than `min_length` are skipped: a one-word comment would match ordinary
    metric names and turn the check into noise nobody reads.
    """
    haystacks = _strings(payload)
    if not haystacks:
        return
    for comment in corpus:
        text = str(comment)
        if len(text) < min_length:
            continue
        for haystack in haystacks:
            if text in haystack:
                raise RawTextLeak(
                    f"payload contains raw comment text ({len(text)} chars) and the W&B "
                    f"project is public; log ids and aggregates, never the comment"
                )


def log_run(run, *, config: dict, summary: dict, corpus) -> None:
    """Attach the config and the metrics to an already-initialised run.

    Both payloads are leak-checked before the first write. `run.config.update` is a network
    side effect: checking after it would mean the leak is already public when this raises.
    """
    assert_no_raw_text(config, corpus)
    assert_no_raw_text(summary, corpus)
    run.config.update(config)
    run.log(summary)
    run.summary.update(summary)


def installed_skops_version() -> str:
    """Separate function so the floor check is testable without a downgrade."""
    try:
        return version("skops")
    except PackageNotFoundError:  # pragma: no cover - skops is a pinned base requirement
        return "0"


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def assert_safe_model_artifact(path) -> None:
    """The file must be a real skops archive written by a skops at or above the floor.

    A suffix check alone is not enough: a pickle renamed to `.skops` would sail through it,
    and the loader Phase 2 runs is the one place this project cannot afford arbitrary code
    execution. skops archives are zip files carrying a `schema.json`.
    """
    installed = installed_skops_version()
    if _version_tuple(installed) < MIN_SKOPS_VERSION:
        floor = ".".join(str(part) for part in MIN_SKOPS_VERSION)
        raise UnsafeArtifact(
            f"skops {installed} is installed but {floor} is the floor: it closed three "
            f"high-severity advisories in the loader Phase 2 runs against this artifact"
        )
    path = Path(path)
    if not path.is_file():
        raise UnsafeArtifact(f"no artifact at {path}")
    if path.suffix != MODEL_SUFFIX:
        raise UnsafeArtifact(
            f"{path.name} is not a {MODEL_SUFFIX} file; the classical model is serialized "
            f"with skops only, never pickle or joblib (delivery spec section 6.3)"
        )
    if not zipfile.is_zipfile(path):
        raise UnsafeArtifact(f"{path.name} is not a skops archive: it is not a zip container")
    with zipfile.ZipFile(path) as archive:
        if _SKOPS_SCHEMA_MEMBER not in archive.namelist():
            raise UnsafeArtifact(
                f"{path.name} is not a skops archive: no {_SKOPS_SCHEMA_MEMBER}. A pickle "
                f"renamed to {MODEL_SUFFIX} would pass a suffix check"
            )


def file_digest(path) -> str:
    """`sha256:<hex>` over the bytes on disk."""
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def public_registry_url(entity: str, collection: str = MODEL_COLLECTION) -> str:
    """The page rubric 1.3 is graded on, which must open with no credentials.

    `entity` here is the ORGANIZATION that owns the migrated registry (`rockcyber-org`), not
    the team the run belongs to (`rockcyber`). They differ, and using the team's name yields a
    URL that 404s in the app while still looking plausible in a receipt.
    """
    return f"https://wandb.ai/{entity}/{REGISTRY_PROJECT}/artifacts/{ARTIFACT_TYPE}/{collection}"


def _wandb_artifact(**kwargs):  # pragma: no cover - exercised only against the real SDK
    import wandb

    return wandb.Artifact(**kwargs)


def log_model_artifact(
    run,
    model_path,
    *,
    config: dict,
    metrics: dict,
    expected_digest: str | None = None,
    promotion_metric: str = PROMOTION_METRIC,
    collection: str = MODEL_COLLECTION,
    aliases: tuple[str, ...] = (PRODUCTION_ALIAS,),
    corpus=(),
    extra_metadata: dict | None = None,
    target_path: str | None = None,
    registry_entity: str | None = None,
    artifact_factory=None,
) -> PromotedArtifact:
    """Log the skops model as a W&B artifact and link it into the Registry at a promoted stage.

    Every refusal below happens before the first network call, in this order, so a rejected
    upload leaves no half-published version behind:

    1. the promotion metric is not accuracy, and it is actually present in `metrics`;
    2. the aliases contain a promoted stage, because rubric 1.3 grades the stage, not the run;
    3. the config is the one `build_run_config` returns, so provenance travels with the file;
    4. an entity is known, so the recorded public URL points somewhere rather than at `None`;
    5. the file is a skops archive from a skops at or above the advisory floor, not a pickle;
    6. the bytes on disk match the digest recorded independently (premortem H14);
    7. the artifact metadata carries no raw comment text.

    The default link target is the migrated org-scoped registry,
    `wandb-registry-model/<collection>`, with no entity prefix: W&B resolves the organization
    from the source artifact's entity and rejects a path that names the team instead.
    `target_path` overrides it.

    `registry_entity` is the organization whose registry page is being published
    (`rockcyber-org`). It defaults to the run's entity, which is correct only when the team and
    the organization share a name -- they do not here, so the caller passes it.
    """
    if promotion_metric in FORBIDDEN_PROMOTION_KEYS or promotion_metric.startswith("accuracy"):
        raise ForbiddenPromotionMetric(
            f"{promotion_metric!r} is logged for rubric 1.2 and 3.2 but is banned as a "
            f"promotion metric: an all-negative predictor scores about 90% on this corpus. "
            f"Use {PROMOTION_METRIC!r}"
        )
    if promotion_metric not in metrics:
        raise ValueError(
            f"metrics carry no {promotion_metric!r}, so nothing justifies promoting this "
            f"version; got {sorted(metrics)}"
        )
    aliases = tuple(aliases)
    if not PROMOTED_STAGES & set(aliases):
        raise ValueError(
            f"aliases {list(aliases)} contain no promoted stage {sorted(PROMOTED_STAGES)}; "
            f"rubric 1.3 is graded on the Registry page showing a promoted stage"
        )
    missing = [field for field in _PROVENANCE_FIELDS if field not in config]
    if missing:
        raise TypeError(
            f"config is missing {missing}; pass the dict build_run_config returns so the "
            f"artifact carries its own provenance"
        )
    entity = registry_entity or getattr(run, "entity", None)
    if target_path is None and not entity:
        raise ValueError(
            "the run has no entity and no registry_entity was given, so the recorded registry "
            f"URL would be 'https://wandb.ai/None/{REGISTRY_PROJECT}/...' -- a receipt that "
            "looks like evidence and opens nothing; pass entity= to wandb.init, pass "
            "registry_entity=, or pass an explicit target_path"
        )

    assert_safe_model_artifact(model_path)
    digest = file_digest(model_path)
    if expected_digest is not None:
        wanted = expected_digest if expected_digest.startswith("sha256:") else (
            f"sha256:{expected_digest}"
        )
        if wanted != digest:
            raise ArtifactDigestMismatch(
                f"{Path(model_path).name} hashes to {digest} which does not match the "
                f"recorded {wanted}; the file that would reach W&B is not the file that "
                f"was measured"
            )

    metadata: dict[str, Any] = {field: config[field] for field in _PROVENANCE_FIELDS}
    metadata.update(
        {
            "model_name": config.get("model_name"),
            "model_digest": digest,
            "promotion_metric": promotion_metric,
            promotion_metric: float(metrics[promotion_metric]),
            "thresholds": dict(config.get("thresholds") or {}),
            "aliases": list(aliases),
        }
    )
    if extra_metadata:
        metadata.update(extra_metadata)
    assert_no_raw_text(metadata, corpus)

    factory = artifact_factory or _wandb_artifact
    artifact = factory(name=collection, type=ARTIFACT_TYPE, metadata=metadata)
    artifact.add_file(str(model_path))
    logged = run.log_artifact(artifact, aliases=list(aliases)) or artifact
    target = target_path or f"{REGISTRY_PROJECT}/{collection}"
    run.link_artifact(logged, target_path=target, aliases=list(aliases))
    url = public_registry_url(entity, collection) if entity else None
    # The digest on the run page is what lets a grader, or an incident responder, walk
    # run -> artifact -> deployed model without trusting the artifact to describe itself.
    run.summary.update(
        {
            "model_digest": digest,
            "registry_target": target,
            "registry_url": url,
            "promoted_aliases": list(aliases),
        }
    )
    return PromotedArtifact(
        collection=collection,
        aliases=aliases,
        digest=digest,
        registry_target=target,
        url=url,
        promotion_metric=promotion_metric,
        promotion_value=float(metrics[promotion_metric]),
    )
