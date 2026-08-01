"""Run a command on the pod with the container's own environment restored.

RunPod delivers `env` from the pod-creation payload to the container's **PID 1**. A shell
started by `sshd` is not a child of PID 1 and does not inherit it, so `WANDB_API_KEY` is
present in the container and absent in every SSH session -- which is how a run that has a
valid key still dies on `load_secret: pass is not installed and WANDB_API_KEY is not set`,
after the pod is created, bootstrapped, and billing.

This is the smallest fix that does not weaken the credential rules:

- **Never to disk.** The alternative fixes -- `/etc/profile.d/…`, `~/.bashrc`, `~/.ssh/
  environment` -- all write the key to the pod's filesystem, where it outlives the process and
  lands in any snapshot or image built from the pod.
- **Never in argv.** Passing `WANDB_API_KEY=… python -m …` over SSH puts the secret in the
  remote command line, which is `ps`-readable by every process on the pod, and in the local
  `ssh` argv, which is `ps`-readable here.
- **No new exposure.** `/proc/1/environ` already holds these values, put there by RunPod at
  creation. Reading them adds no copy anywhere; it only moves them from one process's memory
  into another's.

Usage on the pod::

    python -m infra.runpod.podenv --require WANDB_API_KEY -- python -m model.train_distilbert …

The command is replaced via `execvp`, so the child is the process the caller waits on and its
exit status is the real one, with no wrapper swallowing a signal.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

CONTAINER_ENVIRON = "/proc/1/environ"

# Only these are ever copied. A blanket merge of PID 1's environment would also drag in
# `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH` and `HOME` from an init process that was started
# before the bootstrap ran, which is a good way to make a working interpreter unimportable.
COPYABLE_PREFIXES: tuple[str, ...] = ("WANDB_", "HF_", "HUGGING_FACE_", "TOKENIZERS_")
COPYABLE_NAMES: tuple[str, ...] = ("PYTHONHASHSEED",)


class MissingPodEnv(RuntimeError):
    """A variable the command needs is in neither this environment nor the container's."""


def read_container_environ(path: str | Path = CONTAINER_ENVIRON) -> dict[str, str]:
    """Parse a NUL-delimited `environ` file. Unreadable or absent is `{}`, never an error.

    This runs on hosts where `/proc` may not be mounted the way it is on the pod, and a
    missing file must degrade to "nothing to add" rather than to a crash: the variable may
    well already be set, in which case there is nothing to do anyway.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        key, sep, value = entry.partition(b"=")
        if not sep:
            continue
        out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def copyable(environ: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if key.startswith(COPYABLE_PREFIXES) or key in COPYABLE_NAMES
    }


def merge_missing(target: dict[str, str], source: dict[str, str]) -> list[str]:
    """Fill only the names `target` does not already have. Returns the names that were added.

    Existing values win, deliberately: a caller that set `WANDB_PROJECT` on the command it is
    about to run means it, and PID 1 holds whatever the pod was created with, which may be a
    launch ago.
    """
    added = []
    for key, value in sorted(source.items()):
        if not target.get(key):
            target[key] = value
            added.append(key)
    return added


def prepare(required: tuple[str, ...], *, environ: dict[str, str] | None = None,
            container_path: str | Path = CONTAINER_ENVIRON) -> list[str]:
    """Restore the copyable container variables, then enforce `required`. Returns added names."""
    target = os.environ if environ is None else environ
    added = merge_missing(target, copyable(read_container_environ(container_path)))
    missing = [name for name in required if not target.get(name)]
    if missing:
        raise MissingPodEnv(
            f"{missing} is set neither in this environment nor in {container_path}. The pod "
            "was created without it, or the name in the pod payload differs from the name the "
            "command expects. Nothing was run."
        )
    return added


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infra.runpod.podenv",
        description="Restore the container's env from PID 1, then exec the given command.",
    )
    parser.add_argument(
        "--require", nargs="*", default=[], metavar="NAME",
        help="fail before exec if these are still unset",
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="the command to run, after a literal --",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    command = [part for part in args.command if part != "--"]
    try:
        added = prepare(tuple(args.require))
    except MissingPodEnv as exc:
        print(f"podenv: {exc}", file=sys.stderr)
        return 2
    # Names only. The values are the whole point of not printing this line.
    print(f"podenv: restored {added} from PID 1", file=sys.stderr)
    if not command:
        return 0
    os.execvp(command[0], command)  # noqa: S606 - argv list from the caller, no shell
    return 127  # pragma: no cover - execvp does not return


if __name__ == "__main__":
    sys.exit(main())
