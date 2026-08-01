"""The three things the driver adds on top of the lifecycle, and each one costs money to get
wrong: the held-out rows must not travel, "SSH answers" must not be mistaken for "the pod is
usable", and the smoke run must exercise the same command the real run sends.

No network, no `pass`, no key: every seam is patched.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from infra.runpod import deploy_runpod as dep
from infra.runpod import run_phase1_gpu as drv


def _write_cache(root: Path) -> Path:
    cache = root / "bundle"
    cache.mkdir(parents=True)
    (cache / "train.csv.gz").write_bytes(b"train")
    (cache / drv.TEST_FILE).write_bytes(b"HELD-OUT-ROWS")
    (cache / "folds.npz").write_bytes(b"folds")
    (cache / drv.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "format": "mtm-bundle-cache/1",
                "split_version": "a24b8dd6cafe",
                "n_train": 180633,
                "n_test": 31877,
                "n_folds": 5,
                "has_test": True,
                "files": {
                    "train.csv.gz": "sha256:aaa",
                    drv.TEST_FILE: "sha256:bbb",
                    "folds.npz": "sha256:ccc",
                },
            }
        )
    )
    return cache


# ---------------------------------------------------------------------------
# The leakage firewall
# ---------------------------------------------------------------------------


def test_the_held_out_rows_never_reach_the_pod_bundle(tmp_path: Path) -> None:
    """Choosing between classical and DistilBERT on test numbers is selection on the test set.
    The firewall is a property of the bytes that travel, not a flag on the far side: a pod that
    does not have the rows cannot be talked into scoring them."""
    manifest = drv.build_pod_bundle(_write_cache(tmp_path), tmp_path / "pod" / "bundle")

    shipped = tmp_path / "pod" / "bundle"
    assert not (shipped / drv.TEST_FILE).exists()
    assert b"HELD-OUT-ROWS" not in b"".join(
        p.read_bytes() for p in shipped.rglob("*") if p.is_file()
    )
    assert manifest["has_test"] is False


def test_the_pod_manifest_drops_the_digest_of_the_file_it_no_longer_ships(tmp_path: Path) -> None:
    """`load_bundle_cache` verifies every digest in `manifest["files"]`. Leaving the entry for
    a file that is not there fails the *pod*, after the GPU is already billing, with a message
    about a corrupt cache rather than about a deliberate omission."""
    drv.build_pod_bundle(_write_cache(tmp_path), tmp_path / "pod" / "bundle")
    manifest = json.loads((tmp_path / "pod" / "bundle" / drv.MANIFEST_NAME).read_text())
    assert drv.TEST_FILE not in manifest["files"]
    assert set(manifest["files"]) == {"train.csv.gz", "folds.npz"}
    assert manifest["held_out_withheld_from_pod"] is True


def test_the_local_cache_keeps_its_held_out_rows(tmp_path: Path) -> None:
    """Phase 1 evaluates on them exactly once, on this machine, under the git-tracked log."""
    cache = _write_cache(tmp_path)
    drv.build_pod_bundle(cache, tmp_path / "pod" / "bundle")
    assert (cache / drv.TEST_FILE).read_bytes() == b"HELD-OUT-ROWS"
    assert json.loads((cache / drv.MANIFEST_NAME).read_text())["has_test"] is True


def test_a_rebuild_over_a_stale_staging_dir_does_not_resurrect_the_test_file(
    tmp_path: Path,
) -> None:
    """A previous run left a bundle behind. `copytree` into an existing directory would fail,
    and a plain merge would leave last time's `test.csv.gz` sitting in the tree that ships."""
    stale = tmp_path / "pod" / "bundle"
    stale.mkdir(parents=True)
    (stale / drv.TEST_FILE).write_bytes(b"STALE-HELD-OUT")

    drv.build_pod_bundle(_write_cache(tmp_path), stale)
    assert not (stale / drv.TEST_FILE).exists()


def test_a_missing_cache_names_the_command_that_builds_it(tmp_path: Path) -> None:
    with pytest.raises(drv.PreparationError, match="--build-cache"):
        drv.build_pod_bundle(tmp_path / "nope", tmp_path / "pod")


# ---------------------------------------------------------------------------
# Readiness is not the same as usable
# ---------------------------------------------------------------------------


def _pod() -> dep.Pod:
    return dep.Pod(
        pod_id="p1", name="toxic-finetune-x",
        raw={"publicIp": "1.2.3.4", "portMappings": {"22": 10022}},
    )


def test_the_driver_waits_for_the_wheels_not_just_for_sshd(monkeypatch, tmp_path: Path) -> None:
    """`dockerStartCmd` starts sshd first and pip second, so the readiness probe passes minutes
    before `transformers` exists. A training command sent into that window dies on
    ModuleNotFoundError with the pod already billing."""
    attempts: list[str] = []

    def fake_run_remote(_pod, command: str, **_kw: Any) -> str:
        attempts.append(command)
        if len(attempts) < 3:
            raise dep.LaunchError("ssh failed (exit 1)")
        return ""

    monkeypatch.setattr(dep, "run_remote", fake_run_remote)
    drv.wait_for_bootstrap(_pod(), key_path=tmp_path / "k", sleep=lambda _s: None)

    assert len(attempts) == 3
    assert dep.READY_SENTINEL in attempts[0]


def test_a_bootstrap_that_never_finishes_raises_rather_than_polling_forever(
    monkeypatch, tmp_path: Path
) -> None:
    """An unbounded poll holds the process, and therefore the teardown, hostage while the pod
    bills."""
    monkeypatch.setattr(
        dep, "run_remote",
        lambda *_a, **_k: (_ for _ in ()).throw(dep.LaunchError("ssh failed")),
    )
    clock = iter([0.0, 0.0, 10_000.0, 10_000.0])
    with pytest.raises(dep.ReadinessTimeout, match="bootstrap"):
        drv.wait_for_bootstrap(
            _pod(), key_path=tmp_path / "k", sleep=lambda _s: None,
            monotonic=lambda: next(clock),
        )


# ---------------------------------------------------------------------------
# The smoke run
# ---------------------------------------------------------------------------


def test_the_smoke_run_uses_the_same_flag_vocabulary_as_the_real_run() -> None:
    """A smoke test that hand-writes its own command proves that the hand-written command
    works. This one is the real command plus three overrides, so a flag the trainer does not
    parse fails at minute three for two cents rather than at minute fifty."""
    spec = dep.FinetuneSpec()
    smoke = drv.smoke_command(spec)
    real = spec.train_command()

    for flag in ("--cache", "--model-name", "--lr", "--weight-decay", "--patience"):
        assert flag in smoke and flag in real
    assert f"--max-train-rows {drv.SMOKE_ROWS}" in smoke
    assert "--no-wandb" in smoke, "a two-minute smoke run must not create a W&B run"
    assert drv.SMOKE_OUTPUT in smoke
    assert dep.REMOTE_OUTPUT_DIR not in shlex.split(smoke), "the smoke run must not overwrite it"


def test_the_smoke_command_parses_against_the_real_trainer() -> None:
    train_distilbert = pytest.importorskip("model.train_distilbert")
    parser = train_distilbert.build_arg_parser()
    argv = shlex.split(drv.smoke_command(dep.FinetuneSpec()))
    target = argv[argv.index("--") + 1:]
    assert target[:3] == ["python", "-m", "model.train_distilbert"]
    args = parser.parse_args(target[3:])
    assert args.max_train_rows == drv.SMOKE_ROWS
    assert args.no_wandb is True
    assert args.epochs == 1


def test_the_credential_check_asks_only_for_names(monkeypatch, tmp_path: Path) -> None:
    """A pod that cannot see the W&B key must be found before the corpus is on the GPU, and
    the check itself must not put a value on the wire."""
    sent: list[str] = []
    monkeypatch.setattr(dep, "run_remote", lambda _p, cmd, **_k: sent.append(cmd) or "")

    drv.assert_pod_credentials(_pod(), key_path=tmp_path / "k")

    assert "--require WANDB_API_KEY" in sent[0]
    assert "infra.runpod.podenv" in sent[0]
    assert "=" not in sent[0].split("--require")[1], "no value may travel with the name"


# ---------------------------------------------------------------------------
# Proof
# ---------------------------------------------------------------------------


def test_a_pod_that_costs_more_than_the_quote_is_still_checked_against_the_ceiling() -> None:
    """Observed live: preflight quoted an A40 at $0.300/hr from `lowestPrice.minimumBidPrice`
    and the created pod came back at $0.440/hr, because that field is the lowest bid across
    every cloud type while this project pins `cloudType: SECURE`. A quote that can be 47% low
    is a quote no ceiling should be enforced against."""
    pod = dep.Pod(pod_id="p1", name="toxic-finetune-x", raw={"costPerHr": 0.44})
    assert drv.assert_realized_price(pod, quoted_usd_per_hr=0.30, max_hours=3.0) == 0.44


def test_a_pod_above_the_hourly_ceiling_is_refused_after_creation() -> None:
    """The lease still owns teardown: raising here destroys the pod instead of running on it."""
    pod = dep.Pod(pod_id="p1", name="toxic-finetune-x", raw={"costPerHr": 9.99})
    with pytest.raises(dep.LaunchError, match="ceiling"):
        drv.assert_realized_price(pod, quoted_usd_per_hr=0.30, max_hours=3.0)


def test_a_long_dead_man_window_at_a_high_rate_is_refused() -> None:
    pod = dep.Pod(pod_id="p1", name="toxic-finetune-x", raw={"costPerHr": 1.40})
    with pytest.raises(dep.LaunchError, match="run ceiling"):
        drv.assert_realized_price(pod, quoted_usd_per_hr=1.40, max_hours=48.0)


def test_the_code_archive_is_owned_by_root_so_the_pod_can_unpack_it(tmp_path: Path) -> None:
    """Observed live: the pod is root in a container without CAP_CHOWN, GNU tar as root
    restores ownership by default, and `tar xzf` exited 2 with "Cannot change ownership to uid
    1000" -- after the pod was created, ready, and billing."""
    import tarfile

    archive = dep.make_code_archive(tmp_path / "code.tar.gz")
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
    assert members, "the archive is empty"
    assert {m.uid for m in members} == {0}
    assert {m.gid for m in members} == {0}


def test_the_code_archive_carries_no_data_and_no_git(tmp_path: Path) -> None:
    import tarfile

    with tarfile.open(dep.make_code_archive(tmp_path / "code.tar.gz")) as tar:
        names = tar.getnames()
    assert not [n for n in names if n.startswith("data/") or n.startswith(".git")]
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert "model/train_distilbert.py" in names
    assert "infra/runpod/register_pod_artifacts.py" in names


# The Phase 0 pipeline. `prepare_dataset` costs 13.6 minutes of CPU and is reachable from
# exactly one place -- `--build-cache`, which runs on the Jetson -- so `datasketch` and
# `iterstrat` are build-box requirements, not pod requirements. The exclusion is only sound
# because nothing imports this package at module scope on a pod entrypoint, which is what
# `test_the_pod_never_imports_the_build_box_pipeline_at_module_scope` asserts.
# WAS ("model.data",). That invariant held while the only pod workload was DistilBERT,
# which reads a prepared bundle without reconstructing it. It became false when the
# classical CV moved to a pod: pickle.load rebuilds DatasetBundle, which imports
# model/data/prepare.py -> dedup -> datasketch ON THE POD. Keeping the exclusion made the
# requirements test structurally unable to see the packages whose absence killed ten pods.
BUILD_BOX_ONLY: tuple[str, ...] = ()


def _third_party_imports(entrypoints: tuple[str, ...]) -> dict[str, set[str]]:
    """Every non-stdlib top-level module reachable from `entrypoints` through local imports.

    Static, not `importlib`: the point is to answer "what will the pod need" from a box where
    torch and wandb are not installed and importing the entrypoint would fail immediately.
    """
    import ast
    import sys

    stdlib = set(sys.stdlib_module_names)
    local_roots = {"model", "infra"}
    seen: set[Path] = set()
    queue = [Path(e) for e in entrypoints]
    found: dict[str, set[str]] = {}

    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                root = module.split(".")[0]
                if not root or root in stdlib:
                    continue
                if root in local_roots:
                    if module.startswith(BUILD_BOX_ONLY):
                        continue
                    candidate = Path(*module.split(".")).with_suffix(".py")
                    package_init = Path(*module.split(".")) / "__init__.py"
                    queue.extend([candidate, package_init])
                    continue
                found.setdefault(root, set()).add(str(path))
    return found


def test_the_pod_installs_every_third_party_module_its_entrypoints_import() -> None:
    """`pip install` on the pod runs from a hand-written list, and the code it has to serve is
    in another file entirely. That gap cost a live pod: `model/export_onnx.py` imports
    `model/contract.py`, which imports `pydantic`, which was on nobody's list -- and the
    failure surfaced as ModuleNotFoundError on a GPU that was already billing.
    """
    needed = _third_party_imports(
        (
            "model/train_distilbert.py",
            "model/export_onnx.py",
            "infra/runpod/register_pod_artifacts.py",
            # The classical CV entrypoint. Added after ten paid launches diagnosed a
            # missing pip package: unpickling DatasetBundle imports model/data/prepare.py,
            # which imports dedup, which imports datasketch -- absent from the pod list,
            # so the pod died before touching the GPU. Every one of those failures was
            # discoverable here, for free.
            "run_phase1.py",
            # Reachable only through pickle. `run_phase1` calls pickle.load on the bundle,
            # which reconstructs DatasetBundle and therefore imports model/data/prepare.py
            # -> dedup -> datasketch at RUNTIME. No static walker can see that edge, so the
            # module is named explicitly. This is the edge that cost ten paid launches.
            "model/data/prepare.py",
        )
    )
    installed = {
        req.split("==")[0].split("[")[0].lower().replace("_", "-")
        for req in dep.POD_REQUIREMENTS
    }
    provided = {name.lower() for name in dep.POD_IMAGE_PROVIDES}

    missing = {
        module: sorted(sources)
        for module, sources in needed.items()
        if module.lower() not in provided
        and dep.MODULE_TO_DISTRIBUTION.get(module, module).lower().replace("_", "-")
        not in installed
    }
    assert not missing, (
        "these modules are imported by code that runs on the pod but are neither in "
        f"POD_REQUIREMENTS nor provided by POD_IMAGE: {missing}"
    )


def test_the_pod_never_imports_the_build_box_pipeline_at_module_scope() -> None:
    """This is the invariant that makes `BUILD_BOX_ONLY` sound, and it is also the leakage
    firewall: `model.data.prepare` is the only route to `prepare_dataset`, recomputing the
    split on a pod risks a different realised split for the same seed, and the module that
    reaches it is the same one that holds the held-out rows.
    """
    import ast

    for entrypoint in (
        "model/train_distilbert.py",
        "model/export_onnx.py",
        "infra/runpod/register_pod_artifacts.py",
    ):
        tree = ast.parse(Path(entrypoint).read_text())
        for node in tree.body:  # module scope only
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            offenders = [m for m in modules if m.startswith(BUILD_BOX_ONLY)]
            assert not offenders, f"{entrypoint} imports {offenders} at module scope"


def test_every_pod_requirement_is_pinned() -> None:
    """An unpinned wheel resolves differently on the pod than it did in the last run, and the
    thing that changes is a numeric result nobody diffed."""
    unpinned = [req for req in dep.POD_REQUIREMENTS if "==" not in req]
    assert not unpinned, f"unpinned pod requirements: {unpinned}"


def test_the_pod_does_not_reinstall_what_the_image_already_ships() -> None:
    """Re-resolving torch on top of a CUDA image is how a working image becomes a CPU build."""
    installed = {req.split("==")[0].lower() for req in dep.POD_REQUIREMENTS}
    assert not installed & {name.lower() for name in dep.POD_IMAGE_PROVIDES}


def test_an_entity_this_key_cannot_write_to_is_refused_before_a_pod_exists(monkeypatch) -> None:
    """`wandb.init` authenticates against the *key*, not the entity, then fails with
    `failed to upsert bucket: 404 Not Found` at the first log call. That happened live, on a
    GPU, at minute five, with an entity that simply did not exist."""
    monkeypatch.setattr(drv, "wandb_entities", lambda **_k: ("rockcyber", {"rockcyber", "rockl"}))
    with pytest.raises(drv.PreparationError, match="rocklambros"):
        drv.resolve_wandb_entity("rocklambros")


def test_the_default_entity_is_used_when_none_is_given(monkeypatch) -> None:
    monkeypatch.setattr(drv, "wandb_entities", lambda **_k: ("rockcyber", {"rockcyber", "rockl"}))
    assert drv.resolve_wandb_entity(None) == "rockcyber"


def test_a_team_the_key_belongs_to_is_accepted(monkeypatch) -> None:
    monkeypatch.setattr(drv, "wandb_entities", lambda **_k: ("rockl", {"rockl", "rockcyber"}))
    assert drv.resolve_wandb_entity("rockcyber") == "rockcyber"


def test_an_account_with_no_entity_at_all_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(drv, "wandb_entities", lambda **_k: ("", set()))
    with pytest.raises(drv.PreparationError, match="no entity"):
        drv.resolve_wandb_entity(None)


def test_teardown_proof_reports_the_live_count_from_the_api(monkeypatch) -> None:
    """`PodLease` already verified the pods it created are gone. This asks the account, so a
    pod left by a crashed earlier attempt shows up too."""
    monkeypatch.setattr(drv, "list_live_pods", lambda: [{"id": "leftover"}])
    monkeypatch.setattr(drv, "pod_spend", lambda **_k: (0.42, [{"amount": 0.42}]))
    proof = drv.prove_no_pods_live()
    assert proof.count == 1
    assert proof.spend_usd == 0.42


def test_an_uncapped_account_refuses_to_launch(monkeypatch) -> None:
    from infra.runpod.runpod_client import AccountStatus

    monkeypatch.setattr(
        drv, "account_status",
        lambda: AccountStatus(user_id="u", spend_limit=None, client_balance=10.0,
                              current_spend_per_hr=0.0),
    )
    with pytest.raises(drv.PreparationError, match="spending cap"):
        drv.report_spend_cap()


def test_launching_on_top_of_a_live_pod_is_refused(monkeypatch, tmp_path: Path) -> None:
    """Launching on top of a leak doubles the leak and buries the evidence: from then on you
    cannot tell the new pod from the old one."""
    monkeypatch.setattr(
        drv, "reconcile",
        lambda *_a, **_k: {"live_and_ours": [{"pod_id": "p1"}], "orphans": []},
    )
    with pytest.raises(drv.PreparationError, match="already live"):
        drv.assert_nothing_is_running(tmp_path / "registry.json")
