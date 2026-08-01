"""The env-restoration shim: it must fix the SSH gap without putting a key on disk or in argv."""

from __future__ import annotations

from pathlib import Path

import pytest

from infra.runpod import podenv


def _environ_file(tmp_path: Path, pairs: dict[str, str]) -> Path:
    path = tmp_path / "environ"
    path.write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in pairs.items()) + b"\0")
    return path


def test_the_container_environment_is_parsed_from_nul_delimited_bytes(tmp_path: Path) -> None:
    path = _environ_file(tmp_path, {"WANDB_API_KEY": "k", "PATH": "/usr/bin"})
    assert podenv.read_container_environ(path) == {"WANDB_API_KEY": "k", "PATH": "/usr/bin"}


def test_a_missing_environ_file_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    """On a box where /proc is not mounted as it is on the pod, the variable may already be
    set, in which case there was nothing to do anyway."""
    assert podenv.read_container_environ(tmp_path / "nope") == {}


def test_a_value_containing_an_equals_sign_survives(tmp_path: Path) -> None:
    """Base64 and JWT-shaped secrets carry `=` padding, and a naive split loses the tail --
    producing a key that is silently one character short and a 401 that reads like a bad key."""
    path = _environ_file(tmp_path, {"HF_TOKEN": "abc=def=="})
    assert podenv.read_container_environ(path)["HF_TOKEN"] == "abc=def=="


def test_only_the_named_prefixes_are_copied(tmp_path: Path) -> None:
    """A blanket merge would drag PATH, LD_LIBRARY_PATH and PYTHONPATH out of an init process
    that started before the bootstrap ran, which is a good way to make a working interpreter
    unimportable."""
    source = {
        "WANDB_API_KEY": "k",
        "HF_TOKEN": "t",
        "PYTHONHASHSEED": "0",
        "TOKENIZERS_PARALLELISM": "false",
        "PATH": "/init/bin",
        "LD_LIBRARY_PATH": "/init/lib",
        "PYTHONPATH": "/init/py",
        "HOME": "/root-init",
    }
    assert set(podenv.copyable(source)) == {
        "WANDB_API_KEY", "HF_TOKEN", "PYTHONHASHSEED", "TOKENIZERS_PARALLELISM"
    }


def test_an_existing_value_is_never_overwritten() -> None:
    """A caller that set WANDB_PROJECT on the command it is about to run means it; PID 1 holds
    whatever the pod was created with, which may be a launch ago."""
    target = {"WANDB_PROJECT": "chosen-now"}
    added = podenv.merge_missing(target, {"WANDB_PROJECT": "from-pid-1", "WANDB_API_KEY": "k"})
    assert target["WANDB_PROJECT"] == "chosen-now"
    assert added == ["WANDB_API_KEY"]


def test_an_empty_string_counts_as_unset() -> None:
    """`WANDB_API_KEY=""` produces `Authorization: Bearer ` and a 401, which reads exactly like
    a revoked key rather than like a missing one."""
    target = {"WANDB_API_KEY": ""}
    podenv.merge_missing(target, {"WANDB_API_KEY": "real"})
    assert target["WANDB_API_KEY"] == "real"


def test_a_required_variable_that_is_nowhere_fails_before_the_command_runs(
    tmp_path: Path,
) -> None:
    """Discovering this after `execvp` means discovering it from inside a training run that
    has already loaded a corpus onto a GPU."""
    environ: dict[str, str] = {}
    with pytest.raises(podenv.MissingPodEnv, match="WANDB_API_KEY"):
        podenv.prepare(
            ("WANDB_API_KEY",), environ=environ, container_path=_environ_file(tmp_path, {})
        )


def test_the_container_value_satisfies_the_requirement(tmp_path: Path) -> None:
    """This is the whole bug: the key is in the container and absent from the SSH session."""
    environ: dict[str, str] = {}
    added = podenv.prepare(
        ("WANDB_API_KEY",),
        environ=environ,
        container_path=_environ_file(tmp_path, {"WANDB_API_KEY": "k", "PATH": "/x"}),
    )
    assert environ["WANDB_API_KEY"] == "k"
    assert added == ["WANDB_API_KEY"]
    assert "PATH" not in environ


def test_the_cli_reports_names_but_never_values(tmp_path: Path, capsys, monkeypatch) -> None:
    """This line goes to a log that is read by a human and, in CI, is public."""
    monkeypatch.setattr(podenv, "CONTAINER_ENVIRON", str(_environ_file(tmp_path, {})))
    monkeypatch.setenv("WANDB_API_KEY", "sk-do-not-print-this")

    assert podenv.main(["--require", "WANDB_API_KEY"]) == 0

    captured = capsys.readouterr()
    assert "sk-do-not-print-this" not in captured.out + captured.err
    assert "podenv: restored" in captured.err


def test_the_cli_exits_nonzero_without_execing_when_a_requirement_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(podenv, "CONTAINER_ENVIRON", str(_environ_file(tmp_path, {})))
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    execs: list[list[str]] = []
    monkeypatch.setattr(podenv.os, "execvp", lambda f, a: execs.append([f, *a]))

    assert podenv.main(["--require", "WANDB_API_KEY", "--", "python", "-c", "1"]) == 2
    assert execs == [], "nothing may run when a required credential is absent"


def test_the_cli_execs_the_command_after_the_double_dash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(podenv, "CONTAINER_ENVIRON", str(_environ_file(tmp_path, {})))
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(podenv.os, "execvp", lambda f, a: calls.append((f, a)))

    podenv.main(["--", "python", "-m", "model.train_distilbert", "--epochs", "3"])

    assert calls == [("python", ["python", "-m", "model.train_distilbert", "--epochs", "3"])]


def test_no_secret_is_ever_written_to_disk_by_this_module() -> None:
    """The alternative fixes -- /etc/profile.d, ~/.bashrc, ~/.ssh/environment -- all leave the
    key on a filesystem that outlives the process and lands in any image built from the pod."""
    source = Path(podenv.__file__).read_text()
    for writer in ("write_text", "write_bytes", "open(", "NamedTemporaryFile"):
        assert writer not in source.split('"""')[-1], f"{writer} appears in module code"
