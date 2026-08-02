"""A PATH of fake CLIs, so a shell script's control flow can be tested without AWS.

The scripts under `infra/` are the deploy. They decide whether a registry outage falls back
safely, whether a tampered artifact is installed, and whether a SendCommand that matched
nothing reports success -- and every one of those decisions is unreachable from Python. The
only honest way to exercise them is to run them, with the AWS-facing commands replaced by
stubs whose behaviour the test chooses.

`REAL_TOOLS` is an allowlist rather than "the system PATH plus stubs", and the distinction is
load-bearing: with the real PATH in front, a stub that was never installed silently resolves
to the real `aws` and the test measures the wrong thing -- or, far worse on a developer
machine with live credentials, makes a real API call. Everything a script legitimately needs
is enumerated here; anything not enumerated is `command not found`, which is a loud failure.

`python3` is on the list because the AWS stub in `test_ssm_run.py` is a Python program with a
`#!/usr/bin/env python3` shebang. Without it `env` cannot resolve the interpreter, the stub
exits 127 before parsing a single argument, and every test in that file "fails" for a reason
that has nothing to do with the script under test.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

# Coreutils and interpreters the deploy scripts legitimately call. `printf`, `test` and
# `unset` are bash builtins and resolve without a PATH entry; the rest are real binaries.
REAL_TOOLS = (
    "bash", "sh", "python3",
    "date", "sleep", "printf", "cat", "grep", "sed", "awk", "sort", "cut", "tee",
    "sha256sum", "mkdir", "rm", "mv", "cp", "ln", "head", "tail", "tr", "test", "env",
    "install", "chmod", "chown", "mktemp", "dirname", "basename", "seq", "id", "wc",
    "curl", "jq",
)


def shell_code(path: Path) -> str:
    """A shell script with its commentary removed, for the assertions that must read source.

    Every script under `infra/` is more comment than code by design, and the comments name the
    exact things the checks forbid: the paragraph explaining why nothing is recorded under
    /toxic/boot/ contains the string `/toxic/boot/`, and the one explaining why there is no
    localhost default contains `localhost`. A check that fires on its own rationale is a check
    somebody deletes, so every source-reading assertion goes through here.

    Whole-line comments only. A trailing `#` inside a string or a parameter expansion is not a
    comment, and a scanner that guessed would be the thing this module exists to avoid.
    """
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def make_stub(bin_dir: Path, name: str, script: str) -> Path:
    """Write an executable stub named `name` into `bin_dir`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def stub_path(bin_dir: Path) -> str:
    """A PATH holding only the stubs plus the coreutils the script legitimately needs.

    The stub directory comes FIRST, so a stub always wins over the real tool of the same
    name. Nothing else is on the path at all.
    """
    real = bin_dir / "_real"
    real.mkdir(parents=True, exist_ok=True)
    for tool in REAL_TOOLS:
        found = shutil.which(tool)
        if found and not (real / tool).exists():
            (real / tool).symlink_to(found)
    return f"{bin_dir}:{real}"


def run(script: Path, args: list[str], bin_dir: Path, env: dict[str, str] | None = None,
        cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `script` with a stubbed PATH and a deliberately minimal environment.

    The environment is built from scratch rather than inherited. An inherited AWS_PROFILE,
    AWS_ACCESS_KEY_ID or AWS_CONTAINER_CREDENTIALS_* would be visible to any stub that chose
    to read it, and -- if a stub were ever missing -- to the real CLI.
    """
    full_env = {"PATH": stub_path(bin_dir), "HOME": str(bin_dir), "AWS_REGION": "us-west-2"}
    full_env.update(env or {})
    return subprocess.run(
        ["bash", str(script), *args],
        env=full_env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
