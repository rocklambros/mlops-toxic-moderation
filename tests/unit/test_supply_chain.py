"""The build box holds four live credentials at once, so install-time code execution is
the highest-severity thing Phase 0 can get wrong. A wheel cannot run code at install time;
an sdist can. These assertions keep both controls wired."""

import re
from pathlib import Path

MAKEFILE = Path("Makefile")
LOCK = Path("requirements/dev.lock")
BASE = Path("requirements/base.txt")
# Every per-surface lock: the serving image, the two Streamlit images, the monitoring
# dashboard, the challenger re-scorer. Discovered rather than listed, so a sixth surface
# cannot be added with an unhashed lock and no test noticing.
SURFACE_LOCKS = sorted(
    path
    for path in Path("requirements").glob("*.txt")
    if path.name != "dev.txt" and "pip-compile" in path.read_text()
)
# Discovered the same way, and from the other direction: an input with no compiled lock is
# a surface whose image installs something nobody hashed.
SURFACE_INPUTS = sorted(Path("requirements").glob("*.in"))


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


def test_every_requirement_input_has_a_compiled_lock_beside_it():
    """The scan above discovers locks. This one discovers *inputs*, so a surface added with
    a hand-written `requirements/<name>.txt` -- which is what Phase 3's plan said to do for
    the re-scorer -- cannot slip past by simply not looking like a compiled lock. The image
    would then run `pip install --require-hashes` against a file with no hashes in it."""
    assert SURFACE_INPUTS, "the input scan found nothing, so it certifies nothing"
    for source in SURFACE_INPUTS:
        compiled = source.with_suffix(".txt")
        assert compiled.is_file(), f"{source} has no compiled lock; add a `make` target"
        assert compiled in SURFACE_LOCKS, f"{compiled} was not produced by pip-compile"


def test_every_surface_requirement_input_is_pinned_exactly():
    for path in Path("requirements").glob("*.in"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-r "):
                continue
            assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[0-9][^\s]*", stripped), f"{path}: {line}"


def test_every_surface_lock_is_regenerated_wheels_only_and_on_a_release_interpreter():
    """Discovered from the Makefile rather than named, for the same reason SURFACE_LOCKS is
    discovered: a lock target added without `--only-binary=:all:` resolves the whole surface
    on a box holding four live credentials, and an sdist runs its setup.py while doing it."""
    recipe = MAKEFILE.read_text()
    targets = re.findall(r"(?m)^([a-z0-9]+(?:-[a-z0-9]+)*-lock):(.*)$", recipe)
    assert {name for name, _ in targets} >= {"ui-lock"}, targets
    for name, dependencies in targets:
        assert "check-py" in dependencies, f"{name} is not guarded by check-py"
        body = recipe.split(f"\n{name}:", 1)[1].split("\n\n", 1)[0]
        assert "--only-binary=:all:" in body, f"{name} may resolve a source distribution"
        assert "--generate-hashes" in body, f"{name} produces an unhashed lock"


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
