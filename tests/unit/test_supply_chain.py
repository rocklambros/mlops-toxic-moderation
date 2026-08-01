"""The build box holds four live credentials at once, so install-time code execution is
the highest-severity thing Phase 0 can get wrong. A wheel cannot run code at install time;
an sdist can. These assertions keep both controls wired."""

import re
from pathlib import Path

MAKEFILE = Path("Makefile")
LOCK = Path("requirements/dev.lock")
BASE = Path("requirements/base.txt")
# Every per-surface lock: the development venv, the serving image, the two Streamlit
# images. Discovered rather than listed, so a fifth surface cannot be added with an
# unhashed lock and no test noticing.
SURFACE_LOCKS = sorted(
    path
    for path in Path("requirements").glob("*.txt")
    if path.name != "dev.txt" and "pip-compile" in path.read_text()
)


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


def test_every_per_surface_lock_is_fully_hashed():
    """serve.txt and ui.txt install into containers that hold the demo key and, for the
    reviewer console, the write path to the graded metric. An unhashed pin there is the
    same supply-chain hole as an unhashed pin in dev.lock."""
    assert {path.name for path in SURFACE_LOCKS} >= {"serve.txt", "ui.txt"}, SURFACE_LOCKS
    for path in SURFACE_LOCKS:
        text = path.read_text()
        pins = re.findall(r"(?m)^[A-Za-z0-9_.\-]+==", text)
        assert pins, f"{path} has no pinned distributions"
        assert text.count("--hash=sha256:") >= len(pins), f"{path} has an unhashed pin"


def test_every_surface_requirement_input_is_pinned_exactly():
    for path in Path("requirements").glob("*.in"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-r "):
                continue
            assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[0-9][^\s]*", stripped), f"{path}: {line}"


def test_the_ui_lock_is_regenerated_wheels_only_like_every_other_lock():
    recipe = MAKEFILE.read_text()
    assert re.search(r"^ui-lock:.*check-py", recipe, re.M), "no ui-lock target, or it is unguarded"
    ui_recipe = recipe.split("ui-lock:", 1)[1]
    assert "--only-binary=:all:" in ui_recipe.split("\n\n", 1)[0]


def test_the_ui_surface_carries_no_database_driver():
    """H16: a Streamlit container that can import psycopg is one leaked DSN away from
    writing the graded metric directly. It reaches Postgres only through the backend API."""
    ui = Path("requirements/ui.txt").read_text()
    for driver in ("psycopg", "sqlalchemy", "asyncpg", "pg8000"):
        assert not re.search(rf"(?mi)^{driver}[=\[]", ui), f"{driver} is installed in the UI image"


def test_makefile_refuses_a_prerelease_interpreter():
    """/usr/bin/python3.11 on this build box is 3.11.0rc1. CI's setup-python fetches a
    release build, so an unguarded `PY ?= python3.11` puts local and CI on different
    interpreters and any divergence is debugged in the wrong place. The guard makes the
    mismatch loud at `make venv` instead of silent for nineteen days."""
    recipe = MAKEFILE.read_text()
    assert "check-py" in recipe, "no interpreter guard target"
    assert "releaselevel" in recipe, "guard does not reject pre-release builds"
    assert re.search(r"^venv:.*check-py", recipe, re.M), "venv does not depend on check-py"
