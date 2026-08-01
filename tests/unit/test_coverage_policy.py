"""The coverage floor is 80% branch coverage on the code that decides outcomes (rubric 4.1).

A coverage number that lives only in a CI command is lowered the first time it is
inconvenient, at 1 a.m., in the same commit as the fix that broke it. So the floor is stated
in three places -- the coverage configuration, the Makefile target, and here -- and the value
is asserted, not just its presence. Lowering it is then a visible, reviewable act rather than
one character in a shell line nobody reads.

Streamlit entry points are omitted deliberately: their only meaningful test is the Phase 3
end-to-end traversal, and counting them pushes the project toward coverage theatre against a
UI. `rescorer` is absent from the source list because it sits behind the day-8 cut line, and a
source entry for a package that was cut fails the run for the wrong reason.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FLOOR = 80
FLOOR_FLAG = f"--cov-fail-under={FLOOR}"
MEASURED = {"model", "backend", "monitoring"}


def pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def makefile() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def test_the_coverage_floor_is_declared_where_it_is_enforced():
    assert FLOOR_FLAG in makefile(), (
        f"{FLOOR_FLAG} is not in the Makefile; the floor exists only in whatever CI happens "
        "to run, and CI is not in this repository until Task 8"
    )


def test_the_floor_is_the_only_one_declared_anywhere():
    """Two different floors in two files is how a floor gets lowered without being lowered:
    the Makefile keeps 80, CI quietly runs 60, and both are 'the' floor."""
    declared = set()
    for path in sorted(REPO_ROOT.rglob("*")):
        parts = set(path.parts)
        if not path.is_file() or {".git", ".venv", ".venv-lock", "docs", "claudedocs"} & parts:
            continue
        if path.suffix not in {".toml", ".cfg", ".ini", ".yml", ".yaml", ".sh"} and (
            path.name != "Makefile"
        ):
            continue
        declared |= set(
            re.findall(r"--cov-fail-under[= ](\d+)", path.read_text(encoding="utf-8"))
        )
    assert declared, "no coverage floor is declared anywhere"
    assert declared == {str(FLOOR)}, f"conflicting coverage floors declared: {sorted(declared)}"


def test_the_coverage_run_is_appended_across_both_halves_of_the_suite():
    """Rubric 4.1 asks for unit AND integration tests. Measuring only the unit half and
    calling the result the project's coverage understates the FastAPI endpoints to zero and
    overstates how much of `model` a single run touched; measuring only the integration half
    hides every pure function. `--cov-append` is what makes the number mean the whole suite.
    """
    recipe = makefile()
    assert re.search(r"(?m)^test-cov:", recipe), "no `make test-cov` target"
    body = recipe.split("\ntest-cov:", 1)[1].split("\n\n", 1)[0]
    joined = re.sub(r"\\\s*\n\s*", " ", body)
    runs = [line for line in joined.splitlines() if "pytest" in line]
    assert len(runs) == 2, f"expected a unit run and an integration run, found {len(runs)}"
    assert any('-m "not integration"' in run or "-m 'not integration'" in run for run in runs)
    assert any(re.search(r"-m\s+integration\b", run) for run in runs)
    assert sum("--cov-append" in run for run in runs) == 1, (
        "without --cov-append the second run discards the first one's data"
    )
    floor_runs = [run for run in runs if FLOOR_FLAG in run]
    assert len(floor_runs) == 1 and "--cov-append" in floor_runs[0], (
        "the floor must be checked on the appended total, not on one half of the suite"
    )


def test_coverage_measures_the_code_that_decides_outcomes():
    run = pyproject()["tool"]["coverage"]["run"]
    assert run["branch"] is True, "line coverage alone hides an untaken policy branch"
    assert set(run["source"]) == MEASURED, run["source"]


def test_the_streamlit_entry_points_are_omitted_on_purpose_not_by_accident():
    omit = pyproject()["tool"]["coverage"]["run"]["omit"]
    assert "frontend/ui.py" in omit
    assert "monitoring/dashboard.py" in omit


def test_nothing_that_is_measured_is_then_omitted_wholesale():
    """An omit entry that swallows a whole measured package is a floor of 80% on whatever is
    left. `monitoring/dashboard.py` is one file with a written reason; `monitoring/*` would
    not be."""
    omit = pyproject()["tool"]["coverage"]["run"]["omit"]
    offenders = [
        pattern
        for pattern in omit
        if pattern.split("/", 1)[0] in MEASURED and not pattern.endswith(".py")
    ]
    assert not offenders, f"these omit patterns remove a measured package wholesale: {offenders}"


def test_no_module_is_excluded_by_a_blanket_pragma():
    """`# pragma: no cover` on a whole module is how a coverage floor is defeated without
    changing the number."""
    offenders = []
    for package in sorted(MEASURED):
        root = REPO_ROOT / package
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            head = path.read_text(encoding="utf-8").splitlines()[:5]
            if any("pragma: no cover" in line for line in head):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"module-level coverage exclusions: {offenders}"


def test_the_exclusion_list_hides_no_reachable_branch():
    """`exclude_also` is a regex list applied to every source line. An entry like `^\\s*if `
    or `except` would delete real branches from the denominator and lift the number without
    lifting the testing."""
    excluded = pyproject()["tool"]["coverage"]["report"]["exclude_also"]
    assert excluded, "the exclusion list is empty, so this certifies nothing"
    allowed = {
        "if __name__ == .__main__.:",
        "if TYPE_CHECKING:",
        "raise NotImplementedError",
    }
    assert set(excluded) <= allowed, (
        f"unreviewed coverage exclusions: {sorted(set(excluded) - allowed)}. Each one removes "
        "lines from the denominator, so the floor stops meaning what it says"
    )
