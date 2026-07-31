"""The build box holds four live credentials at once, so install-time code execution is
the highest-severity thing Phase 0 can get wrong. A wheel cannot run code at install time;
an sdist can. These assertions keep both controls wired."""

import re
from pathlib import Path

MAKEFILE = Path("Makefile")
LOCK = Path("requirements/dev.lock")
BASE = Path("requirements/base.txt")


def test_every_base_requirement_is_pinned_exactly():
    for line in BASE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][^\s]*", stripped), stripped


def test_venv_target_requires_hashes_and_refuses_source_distributions():
    recipe = MAKEFILE.read_text()
    assert "--require-hashes" in recipe
    assert "--only-binary=:all:" in recipe
    assert "-r requirements/dev.lock" in recipe


def test_lock_exists_and_every_pin_carries_a_hash():
    assert LOCK.is_file(), "run `make lock` and commit requirements/dev.lock"
    text = LOCK.read_text()
    pins = re.findall(r"(?m)^[A-Za-z0-9_.\-]+==", text)
    assert pins, "the lock has no pinned distributions"
    assert text.count("--hash=sha256:") >= len(pins)


def test_makefile_refuses_a_prerelease_interpreter():
    """/usr/bin/python3.11 on this build box is 3.11.0rc1. CI's setup-python fetches a
    release build, so an unguarded `PY ?= python3.11` puts local and CI on different
    interpreters and any divergence is debugged in the wrong place. The guard makes the
    mismatch loud at `make venv` instead of silent for nineteen days."""
    recipe = MAKEFILE.read_text()
    assert "check-py" in recipe, "no interpreter guard target"
    assert "releaselevel" in recipe, "guard does not reject pre-release builds"
    assert re.search(r"^venv:.*check-py", recipe, re.M), "venv does not depend on check-py"
