"""Every Python module on disk must be tracked by git.

An unanchored `data/` in .gitignore matches a directory named data at ANY depth, so it
silently excluded model/data/ -- load, dedup, split, prepare, firewall_check, profile,
provenance, shingles, run -- from ten commits. The full suite passed the whole time,
because pytest imports from the working tree and the files were present locally. Only a
fresh clone would have revealed it, and the first fresh clone was going to be CI.

This test asserts the property directly rather than trusting the ignore rules.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _tracked() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def test_every_python_module_is_tracked():
    tracked = _tracked()
    on_disk = {
        str(p.relative_to(REPO))
        for p in REPO.rglob("*.py")
        if ".venv" not in p.parts and "__pycache__" not in p.parts and ".git" not in p.parts
    }
    missing = sorted(on_disk - tracked)
    assert not missing, (
        "these Python files exist on disk but git does not track them, so a fresh clone "
        f"would not have them: {missing}"
    )


def test_no_gitignore_directory_pattern_can_match_a_nested_source_directory():
    """Anchor-or-justify. A bare `foo/` matches at any depth; `/foo/` matches only at the
    repository root. Patterns that legitimately recur at any depth are allow-listed."""
    nestable_by_design = {
        "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/",
        "htmlcov/", ".idea/", ".vscode/", "*.egg-info/",
    }
    offenders = []
    for line in (REPO / ".gitignore").read_text().splitlines():
        p = line.strip()
        if not p or p.startswith("#") or not p.endswith("/"):
            continue
        if p.startswith("/") or p in nestable_by_design:
            continue
        offenders.append(p)
    assert not offenders, (
        f"unanchored directory patterns can swallow nested source dirs: {offenders}. "
        "Prefix with / to anchor to the repository root."
    )


def test_committed_test_fixtures_are_tracked():
    """`*.csv` is unanchored too, and it swallowed tests/fixtures/mini_jigsaw.csv -- the
    fixture the Phase 0 plan calls a committed source artifact. Every test that reads it
    passed locally and would have failed on the first CI run."""
    tracked = _tracked()
    fixtures = {
        str(p.relative_to(REPO))
        for p in (REPO / "tests" / "fixtures").glob("*")
        if p.is_file() and p.suffix in {".csv", ".json", ".parquet"}
    }
    missing = sorted(fixtures - tracked)
    assert not missing, f"committed fixtures are not tracked: {missing}"
