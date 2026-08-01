"""The classical cross-validation as it runs on a paid pod, checked from a free box.

Ten launches on 2026-07-31 each diagnosed something that was discoverable here for nothing: a
wrong bundle shape, a missing wheel, a flag the receiving parser does not define. This file is
the part of that list that belongs to `run_phase1.py` and to the `--classical` branch of the
launcher, and every test in it names the money it saves.

The four failures asserted against, in the order they would have fired on a billing GPU:

1. `load_bundle` read `PHASE1_CACHE/bundle.pkl`. Nothing in this repo has ever written one --
   `write_bundle_cache` writes a *directory* of open formats -- so the very first statement
   after `main()` raised `FileNotFoundError` at second zero of a rented hour.
2. The banner then read `bundle.test_df`, which `CachedBundle` raises on by design when the
   held-out rows were withheld. That is precisely the cache that ships to a pod, so the second
   statement would have killed what the first no longer does.
3. The classical command carried no `podenv` shim, so `WANDB_API_KEY` -- present in the
   container the whole time -- was invisible to the `sshd` session that runs the job. Rubric
   1.2 is graded on the run page that key creates.
4. `run_finetune` ran the ONNX export and then demanded a `.safetensors` file, on a workload
   that produces neither. A finished cross-validation would have been reported as a failure
   and, worse, torn down before `fetch_checkpoint` ever ran.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from infra.runpod import deploy_runpod as dep
from infra.runpod import run_phase1_gpu as drv
from model.data.prepare import DatasetBundle, SplitConfig
from model.labels import LABELS
from model.train_distilbert import write_bundle_cache


def _frame(n: int, *, start: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for i in range(n):
        labels = {label: int(rng.random() < 0.3) for label in LABELS}
        labels["severe_toxic"] &= labels["toxic"]
        rows.append(
            {"id": f"{start + i:016x}", "comment_text": f"comment number {start + i}", **labels}
        )
    return pd.DataFrame(rows)


def _bundle(n_train: int = 20, n_test: int = 6, n_folds: int = 2) -> DatasetBundle:
    idx = np.arange(n_train)
    return DatasetBundle(
        train_df=_frame(n_train),
        test_df=_frame(n_test, start=1000),
        fold_indices=[
            (idx[idx % n_folds != k].copy(), idx[idx % n_folds == k].copy())
            for k in range(n_folds)
        ],
        raw_sha256="a" * 64,
        split_version="b" * 64,
        env_version="c" * 64,
        config=SplitConfig(seed=42, test_size=0.15, n_folds=n_folds),
    )


def _pod() -> dep.Pod:
    return dep.Pod(
        pod_id="p1",
        name="toxic-finetune-classical",
        raw={"publicIp": "1.2.3.4", "portMappings": {"22": 10022}, "costPerHr": 0.44},
    )


# ---------------------------------------------------------------------------
# The bundle shape that actually ships
# ---------------------------------------------------------------------------


def test_the_loader_reads_the_bundle_shape_the_launcher_delivers(tmp_path, monkeypatch) -> None:
    """`deliver_dataset` scps the directory `build_pod_bundle` staged. `run_phase1` has to be
    able to open exactly that, because nothing else will ever be at `PHASE1_CACHE`."""
    import run_phase1

    cache = write_bundle_cache(_bundle(), tmp_path / "cache" / "bundle")
    shipped = tmp_path / "pod" / "bundle"
    drv.build_pod_bundle(cache, shipped)

    monkeypatch.setattr(run_phase1, "CACHE", shipped)
    bundle = run_phase1.load_bundle()

    assert len(bundle.train_df) == 20
    assert len(bundle.fold_indices) == 2
    assert bundle.split_version == "b" * 64
    assert bundle.raw_sha256 == "a" * 64


def test_the_loader_never_pulls_the_held_out_rows_into_memory(tmp_path, monkeypatch) -> None:
    """The local cache still carries them. Loading them here would put the held-out set in the
    same process as the fit, which is the leak the firewall exists to make impossible."""
    import run_phase1

    cache = write_bundle_cache(_bundle(), tmp_path / "cache" / "bundle")
    assert (cache / "test.csv.gz").is_file(), "the local cache is supposed to keep them"

    monkeypatch.setattr(run_phase1, "CACHE", cache)
    assert run_phase1.load_bundle().held_out is None


def test_the_banner_does_not_reach_for_rows_that_were_withheld(tmp_path, monkeypatch) -> None:
    """`CachedBundle.test_df` raises `HeldOutTestAccess` on a pod bundle. The startup banner
    used to call it, so the run died one statement after the loader."""
    import run_phase1

    cache = write_bundle_cache(_bundle(), tmp_path / "cache" / "bundle")
    shipped = tmp_path / "pod" / "bundle"
    drv.build_pod_bundle(cache, shipped)

    monkeypatch.setattr(run_phase1, "CACHE", shipped)
    assert run_phase1.n_held_out(run_phase1.load_bundle()) == "withheld from this copy"


def test_a_hand_built_pickle_cache_still_loads_and_still_counts(tmp_path, monkeypatch) -> None:
    """The pickle branch is the only reader of a cache built before the directory format
    existed, so it keeps working -- and a `DatasetBundle` really does carry its held-out rows,
    which is why the banner reports a number for it and a word for the pod's copy."""
    import pickle

    import run_phase1

    cache = tmp_path / "legacy"
    cache.mkdir()
    (cache / "bundle.pkl").write_bytes(pickle.dumps(_bundle()))

    monkeypatch.setattr(run_phase1, "CACHE", cache)
    bundle = run_phase1.load_bundle()

    assert len(bundle.train_df) == 20
    assert run_phase1.n_held_out(bundle) == 6


def test_a_missing_cache_names_the_command_that_builds_it(tmp_path, monkeypatch) -> None:
    """A pod that received the wrong `--dataset` must say so in the first second, in words
    that name the fix, rather than raising a bare FileNotFoundError about a pickle."""
    import run_phase1
    from model.train_distilbert import BundleCacheError

    monkeypatch.setattr(run_phase1, "CACHE", tmp_path / "nothing-here")
    with pytest.raises(BundleCacheError, match="--build-cache"):
        run_phase1.load_bundle()


# ---------------------------------------------------------------------------
# The command the pod receives
# ---------------------------------------------------------------------------


def test_the_classical_command_restores_the_credential_the_run_needs() -> None:
    """RunPod hands the payload's `env` to PID 1; an `sshd` session inherits none of it. Every
    launch on 2026-07-31 carried a valid `WANDB_API_KEY` and logged zero runs."""
    command = dep.FinetuneSpec(classical=True).classical_command()

    assert " ".join(dep.POD_ENV_SHIM) in command
    assert "--require WANDB_API_KEY" in command
    assert "=" not in command.split("--require")[1].split("--")[0], "no value may travel"


def test_the_classical_command_points_at_the_directory_the_bundle_lands_in() -> None:
    """`deliver_dataset` puts `<staging>/bundle` at `REMOTE_CACHE_DIR`. A `PHASE1_CACHE` that
    named anything else is a "no bundle cache at ..." after the pod is already billing."""
    command = dep.FinetuneSpec(classical=True).classical_command()

    assert f"PHASE1_CACHE={dep.REMOTE_CACHE_DIR}" in command
    assert f"PHASE1_OUT={dep.REMOTE_OUTPUT_DIR}" in command


def test_the_classical_command_asks_run_phase1_for_stages_it_defines() -> None:
    """argparse exits 2 on an unrecognised choice, and it does it on a paid pod."""
    import argparse

    import run_phase1

    argv = shlex.split(dep.FinetuneSpec(classical=True).classical_command())
    target = argv[argv.index("--") + 1 :]
    assert target[:2] == ["python", "run_phase1.py"]

    parser = argparse.ArgumentParser()
    parser.add_argument("stages", nargs="+", choices=[*run_phase1.STAGES, "all"])
    assert parser.parse_args(target[2:]).stages == ["cv", "thresholds"]


def test_the_classical_command_carries_no_distilbert_flags() -> None:
    """The two jobs have disjoint vocabularies; `run_phase1.py` defines no options at all."""
    command = dep.FinetuneSpec(classical=True).classical_command()
    for flag in ("--epochs", "--weight-decay", "--fold", "--max-length", "--problem-type"):
        assert flag not in command


def test_the_out_of_fold_matrix_is_moved_where_retrieval_will_find_it() -> None:
    """`stage_cv` checkpoints into `PHASE1_CACHE` so a killed run resumes; `fetch_checkpoint`
    copies `PHASE1_OUT`. Without this the 150 fits die with the pod's disk."""
    command = dep.FinetuneSpec(classical=True).classical_retrieval_command()

    assert f"{dep.REMOTE_CACHE_DIR}/oof.npz" in command
    assert f"{dep.REMOTE_CACHE_DIR}/fold_*.npz" in command
    assert command.endswith(f"{dep.REMOTE_OUTPUT_DIR}/")


# ---------------------------------------------------------------------------
# What the launcher does with it
# ---------------------------------------------------------------------------


class _Recorder:
    """Every remote call, in order, with the pod and the ssh key thrown away."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def __call__(self, _pod: dep.Pod, command: str, **_kw: Any) -> str:
        self.commands.append(command)
        return ""


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Recorder:
    """`run_finetune` with every billable and networked seam replaced."""
    recorder = _Recorder()
    monkeypatch.setattr(dep, "run_remote", recorder)
    monkeypatch.setattr(dep, "wait_for_bootstrap", lambda *_a, **_k: None)
    monkeypatch.setattr(dep, "deliver_code", lambda *_a, **_k: None)
    monkeypatch.setattr(dep, "deliver_dataset", lambda *_a, **_k: None)
    monkeypatch.setattr(dep, "make_code_archive", lambda dest, **_k: Path(dest))
    monkeypatch.setattr(dep, "fetch_checkpoint", lambda pod, out, **kw: Path(out))
    monkeypatch.setattr(
        dep, "build_pod_env", lambda **_k: {"WANDB_API_KEY": "x", "HF_TOKEN": "y"}
    )

    class _Lease:
        def __init__(self, **_kw: Any) -> None:
            pass

        def __enter__(self) -> dep.Pod:
            return _pod()

        def __exit__(self, *_exc: Any) -> bool:
            return False

    monkeypatch.setattr(dep, "PodLease", _Lease)
    key = tmp_path / "key"
    key.write_text("private")
    Path(f"{key}.pub").write_text("ssh-ed25519 AAAA test")
    recorder.ssh_key = key  # type: ignore[attr-defined]
    return recorder


def test_the_classical_run_never_exports_onnx(wired: _Recorder, tmp_path: Path) -> None:
    """There are no torch weights to quantise. `model.export_onnx --model-dir outputs/final`
    fails on a directory that does not exist -- after the cross-validation is finished and
    before `fetch_checkpoint` ever runs, so the whole run is lost to the teardown."""
    dep.run_finetune(
        run_name="toxic-finetune-classical",
        dataset_path=tmp_path / "bundle",
        output_path=tmp_path / "out",
        spec=dep.FinetuneSpec(classical=True),
        ssh_key=wired.ssh_key,  # type: ignore[attr-defined]
        registry_path=tmp_path / "registry.json",
    )

    assert not [c for c in wired.commands if "export_onnx" in c]
    assert any("run_phase1.py cv thresholds" in c for c in wired.commands)
    assert any("oof.npz" in c for c in wired.commands)


def test_the_classical_run_is_not_asked_for_a_safetensors_checkpoint(
    wired: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`.skops`, JSON and `.npz` is the whole output. Demanding `.safetensors` turns a
    finished multi-hour run into `CheckpointError`."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dep,
        "fetch_checkpoint",
        lambda pod, out, **kw: seen.update(kw) or Path(out),
    )

    dep.run_finetune(
        run_name="toxic-finetune-classical",
        dataset_path=tmp_path / "bundle",
        output_path=tmp_path / "out",
        spec=dep.FinetuneSpec(classical=True),
        ssh_key=wired.ssh_key,  # type: ignore[attr-defined]
        registry_path=tmp_path / "registry.json",
    )

    assert seen["require_safetensors"] is False


def test_the_fine_tune_is_still_held_to_safetensors(
    wired: _Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The relaxation is for the classical branch and only for it: `torch.load` on a `.bin`
    executes arbitrary code, and that artifact is bound for a public registry."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        dep,
        "fetch_checkpoint",
        lambda pod, out, **kw: seen.update(kw) or Path(out),
    )

    dep.run_finetune(
        run_name="toxic-finetune-distilbert",
        dataset_path=tmp_path / "bundle",
        output_path=tmp_path / "out",
        spec=dep.FinetuneSpec(),
        ssh_key=wired.ssh_key,  # type: ignore[attr-defined]
        registry_path=tmp_path / "registry.json",
    )

    assert seen["require_safetensors"] is True
    assert any("export_onnx" in c for c in wired.commands)


def test_nothing_is_sent_before_the_wheels_are_installed(
    monkeypatch: pytest.MonkeyPatch, wired: _Recorder, tmp_path: Path
) -> None:
    """`build_bootstrap` starts sshd first and pip second, so "the readiness probe passed" and
    "scikit-learn exists" are minutes apart. `run_finetune` used to go straight from one to a
    training command."""
    order: list[str] = []
    monkeypatch.setattr(dep, "wait_for_bootstrap", lambda *_a, **_k: order.append("bootstrap"))
    monkeypatch.setattr(dep, "deliver_code", lambda *_a, **_k: order.append("code"))
    monkeypatch.setattr(dep, "deliver_dataset", lambda *_a, **_k: order.append("dataset"))

    dep.run_finetune(
        run_name="toxic-finetune-classical",
        dataset_path=tmp_path / "bundle",
        output_path=tmp_path / "out",
        spec=dep.FinetuneSpec(classical=True),
        ssh_key=wired.ssh_key,  # type: ignore[attr-defined]
        registry_path=tmp_path / "registry.json",
    )

    assert order[0] == "bootstrap", f"the bootstrap wait must come first, got {order}"


def test_the_sentinel_wait_lives_in_one_place() -> None:
    """Two copies of it would be two things to keep in step with `build_bootstrap`."""
    assert drv.wait_for_bootstrap is dep.wait_for_bootstrap


def test_every_launcher_agrees_on_the_project_the_grader_will_open() -> None:
    """`deploy_runpod` defaulted to `toxic-moderation` and everything else to
    `mlops-toxic-moderation`, so a classical run launched from there landed in a second, empty
    project -- and rubric 3.2's dashboard is a single project."""
    import model.train_distilbert as td

    project = dep.FinetuneSpec().wandb_project
    assert project == "mlops-toxic-moderation"
    for parser in (dep._build_parser(), drv.build_arg_parser(), td.build_arg_parser()):
        assert parser.get_default("wandb_project") == project


def test_the_staging_parent_is_refused_before_a_byte_moves(tmp_path) -> None:
    """`scp -r <local> <remote>/` lands the directory under its own basename. Pointing this at
    `artifacts/pod-bundle` delivers `/workspace/data/pod-bundle` and leaves the run looking at
    `/workspace/data/bundle` -- a mistake only the pod could report, and only while billing."""
    staging = tmp_path / "pod-bundle"
    (staging / "bundle").mkdir(parents=True)
    (staging / "bundle" / "manifest.json").write_text("{}")

    with pytest.raises(dep.LaunchError, match="must be a directory named 'bundle'"):
        dep.deliver_dataset(_pod(), staging, key_path=tmp_path / "k")


def test_a_directory_that_is_not_a_bundle_cache_is_refused(tmp_path) -> None:
    empty = tmp_path / "bundle"
    empty.mkdir()
    with pytest.raises(dep.LaunchError, match="no manifest.json"):
        dep.deliver_dataset(_pod(), empty, key_path=tmp_path / "k")


def test_the_real_staged_bundle_passes_the_shape_check(tmp_path, monkeypatch) -> None:
    """The shape the launcher will actually be handed, built by the code that builds it."""
    sent: list[list[str]] = []
    monkeypatch.setattr(dep, "_run_local", lambda cmd, **_k: sent.append(cmd) or "")

    cache = write_bundle_cache(_bundle(), tmp_path / "cache" / "bundle")
    shipped = tmp_path / "pod-bundle" / "bundle"
    drv.build_pod_bundle(cache, shipped)

    dep.deliver_dataset(_pod(), shipped, key_path=tmp_path / "k")

    scp = next(cmd for cmd in sent if cmd[0] == "scp")
    assert scp[-2] == str(shipped)
    assert scp[-1].endswith(f":{dep.REMOTE_DATA_DIR}/")


# ---------------------------------------------------------------------------
# Experiment tracking -- rubric 1.2 is graded on the run page
# ---------------------------------------------------------------------------


class _FakeRun:
    url = "https://wandb.ai/fake/run"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.logged: list[dict[str, Any]] = []
        self.summary: dict[str, Any] = {}

    def log(self, payload: dict[str, Any]) -> None:
        self.logged.append(payload)

    def finish(self) -> None:
        self.finished = True


@pytest.fixture
def fake_wandb(monkeypatch: pytest.MonkeyPatch) -> list[_FakeRun]:
    import sys
    import types

    runs: list[_FakeRun] = []
    module = types.ModuleType("wandb")
    module.init = lambda **kwargs: runs.append(_FakeRun(**kwargs)) or runs[-1]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wandb", module)
    monkeypatch.setenv("WANDB_API_KEY", "not-a-real-key")
    monkeypatch.delenv("PHASE1_NO_WANDB", raising=False)
    return runs


def test_the_run_carries_every_field_rubric_1_2_asks_for(
    tmp_path, monkeypatch, fake_wandb: list[_FakeRun]
) -> None:
    """Code version, hyperparameters, and data version**s** -- three fields, not one composite,
    so a moved number can be attributed to the corpus, the split, or the environment."""
    import run_phase1

    monkeypatch.setattr(
        run_phase1, "CACHE", write_bundle_cache(_bundle(), tmp_path / "cache" / "bundle")
    )
    run = run_phase1.start_run(run_phase1.load_bundle())

    config = run.kwargs["config"]
    for key in ("git_sha", "seed", "raw_sha256", "split_version", "env_version", "data_version"):
        assert config[key], f"{key} is missing from the run config"
    for key in ("solver", "max_iter", "calibration_folds", "word_max_features"):
        assert key in config, f"hyperparameter {key} is not logged"


def test_no_comment_text_can_reach_a_public_run_page(
    tmp_path, monkeypatch, fake_wandb: list[_FakeRun]
) -> None:
    """The project is public by owner decision, so a payload is the last place a user comment
    could escape into a graded artifact. `track` drops everything that is not a number."""
    import run_phase1

    monkeypatch.setattr(run_phase1, "_RUN", _FakeRun())
    run_phase1.track("oof", {"macro_f1": 0.71, "note": "comment number 3", "nan": float("nan")})

    assert run_phase1._RUN.logged == [{"oof/macro_f1": 0.71}]


def test_an_untracked_run_is_a_choice_and_never_an_accident(
    tmp_path, monkeypatch, fake_wandb: list[_FakeRun]
) -> None:
    """A missing key must not degrade to "log nothing and train for an hour anyway": that is
    what `podenv --require WANDB_API_KEY` fails on, one second in, for free."""
    import run_phase1

    monkeypatch.setattr(
        run_phase1, "CACHE", write_bundle_cache(_bundle(), tmp_path / "cache" / "bundle")
    )
    monkeypatch.setenv("PHASE1_NO_WANDB", "1")
    assert run_phase1.start_run(run_phase1.load_bundle()) is None

    monkeypatch.delenv("PHASE1_NO_WANDB")
    monkeypatch.delenv("WANDB_API_KEY")
    monkeypatch.setattr(
        "model.train_distilbert.load_secret",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("pass is not installed")),
    )
    with pytest.raises(RuntimeError, match="pass is not installed"):
        run_phase1.start_run(run_phase1.load_bundle())
