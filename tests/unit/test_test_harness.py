"""The test harness enforces its own preconditions (premortem H1, rubric 4.1).

Three properties, each of which was a memo before it was a test:

  1. PYTHONHASHSEED=0 is pinned for EVERY pytest invocation, not only `make test`, and the
     guard reads the interpreter flag rather than the environment variable it was set from.
  2. Markers follow the directory layout, so `-m "not integration"` is trustworthy without
     anyone remembering to mark a new file.
  3. A green CI job has to have proved something. A run that executed no test, and an
     integration run in which every integration test skipped, both exit 0 today.

The fake-green guard is tested from both directions on purpose. Asserting only that it
refuses is satisfied by a guard that refuses everything, which would be discovered the first
time a real CI job went red for no reason; asserting only that a healthy run passes is
satisfied by no guard at all.
"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import fake_green_reasons, running_in_ci

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_TEST = (
    "tests/unit/test_run_cli.py"
    "::test_real_corpus_is_present_and_matches_recorded_provenance"
)


def run_pytest(args: list[str], env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Set explicitly, never inherited: this suite's own behaviour must not depend on whether
    # the developer running it happens to be inside CI.
    env.setdefault("CI", "")
    env.update(env_overrides)
    env = {key: value for key, value in env.items() if value != ""}
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# --- the seed pin, premortem H1 ---------------------------------------------------------


def test_the_seed_guard_reads_the_interpreter_flag_not_the_environment(tmp_path):
    """PYTHONHASHSEED has to be set before the interpreter starts; by the time any Python
    code runs it is already too late to fix. A guard written as
    `os.environ.get("PYTHONHASHSEED") != "0"` -- which is how the Phase 4 plan wrote it --
    is satisfied by anything that exports the variable into an already-randomised
    interpreter: a wrapper script, a tox config, a plugin. This plants exactly that plugin.
    """
    plugin = tmp_path / "spoofseed.py"
    plugin.write_text("import os\nos.environ['PYTHONHASHSEED'] = '0'\n", encoding="utf-8")
    result = run_pytest(
        ["tests/unit/test_labels.py", "-p", "spoofseed"],
        {"PYTHONHASHSEED": "random", "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode != 0, (
        "the environment variable said 0 and the interpreter was still randomised; the guard "
        "believed the variable"
    )
    assert "PYTHONHASHSEED=0 is required" in (result.stdout + result.stderr)


# --- markers, rubric 4.1 -----------------------------------------------------------------


def test_markers_are_declared_and_strict():
    options = pyproject()["tool"]["pytest"]["ini_options"]
    assert "--strict-markers" in options["addopts"], (
        "without --strict-markers a typo'd marker silently selects nothing"
    )
    declared = " ".join(options["markers"])
    for marker in ("unit:", "integration:", "perf:", "awsapply:"):
        assert marker in declared, f"{marker} is not declared"
    assert options.get("pythonpath") == ["."], "scripts/ must be importable by its tests"


def test_every_marker_used_in_the_tree_is_declared():
    """--strict-markers turns an undeclared marker into a COLLECTION error, so one
    @pytest.mark.perf in a file nobody re-reads takes the whole suite down."""
    options = pyproject()["tool"]["pytest"]["ini_options"]
    declared = set(re.findall(r"^(\w+):", "\n".join(options["markers"]), re.M))
    assert declared, "no marker names parsed out of pyproject.toml"
    used = set(
        re.findall(
            r"@?pytest\.mark\.(\w+)",
            "\n".join(
                path.read_text(encoding="utf-8") for path in (REPO_ROOT / "tests").rglob("*.py")
            ),
        )
    )
    builtin = {"parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings"}
    assert used, "no marker uses found, so this test certifies nothing"
    assert used <= declared | builtin, sorted(used - declared - builtin)


def test_directory_layout_drives_the_markers():
    unit = run_pytest(["--collect-only", "-m", "unit"], {"PYTHONHASHSEED": "0"})
    assert unit.returncode == 0, unit.stdout + unit.stderr
    assert "tests/unit/test_labels.py" in unit.stdout, "unit tests were not auto-marked"
    assert "tests/integration/" not in unit.stdout, (
        "an integration test was collected under -m unit; the marker hook mis-classified it"
    )

    integration = run_pytest(
        ["--collect-only", "-m", "integration"], {"PYTHONHASHSEED": "0"}
    )
    assert integration.returncode == 0, integration.stdout + integration.stderr
    assert "tests/integration/test_health.py" in integration.stdout, (
        "integration tests were not auto-marked; no file under tests/integration declares "
        "the mark by hand in a way this would still see"
    )


def test_a_hand_marked_test_outside_the_integration_directory_is_not_called_a_unit_test():
    """tests/unit/test_run_cli.py's real-corpus check lives under tests/unit and declares
    `@pytest.mark.integration` for itself: it reads a 2 GB download that is not in the
    repository. The directory hook must not hand it a `unit` marker as well, or `-m unit`
    silently means "and also some things that need the network"."""
    unit = run_pytest(["--collect-only", "-m", "unit"], {"PYTHONHASHSEED": "0"})
    assert CORPUS_TEST not in unit.stdout, "a network-dependent test was collected as a unit test"

    integration = run_pytest(
        ["--collect-only", "-m", "integration"], {"PYTHONHASHSEED": "0"}
    )
    assert CORPUS_TEST in integration.stdout, (
        "the hand-applied integration marker was lost; the directory hook must be additive"
    )


# --- the fake-green guard, rubric 4.1 ----------------------------------------------------


def test_the_guard_is_scoped_to_ci_and_reads_the_runner_variable():
    assert running_in_ci({"CI": "true"}) is True
    assert running_in_ci({"CI": "True"}) is True
    assert running_in_ci({}) is False
    assert running_in_ci({"CI": ""}) is False


@pytest.mark.parametrize(
    ("executed", "selected", "passed", "expected"),
    [
        (0, 0, 0, "no test executed"),
        (0, 12, 0, "no test executed"),
        (900, 12, 0, "not one of them passed"),
        (900, 12, 1, None),
        (900, 0, 0, None),
        (1, 1, 1, None),
    ],
)
def test_the_guard_names_a_reason_for_every_shape_it_refuses(executed, selected, passed, expected):
    reasons = " ".join(
        fake_green_reasons(
            executed=executed, integration_selected=selected, integration_passed=passed
        )
    )
    if expected is None:
        assert reasons == "", f"a run that proved something was refused: {reasons}"
    else:
        assert expected in reasons


def test_a_ci_run_that_executes_no_test_is_not_green():
    """`--collect-only` is the cheapest way to reach this shape, and it is not hypothetical:
    it is one character away from `--co` in a debugging session that got committed."""
    result = run_pytest(
        ["tests/unit/test_labels.py", "--collect-only"],
        {"PYTHONHASHSEED": "0", "CI": "true"},
    )
    assert result.returncode != 0, "CI reported green on a run that executed nothing"
    assert "FAKE-GREEN GUARD" in (result.stdout + result.stderr)
    assert "no test executed" in (result.stdout + result.stderr)


def test_a_ci_run_in_which_every_test_skipped_is_not_green(tmp_path):
    """The same fake green with no integration marker anywhere near it: `pytest -m unit`
    reporting "57 skipped" and exiting 0 is a job that measured nothing. Only the
    skipped-is-not-executed distinction catches this one, and a guard that counted a skip as
    an execution would still pass every other test in this file.

    The skip is injected in the call phase by a throwaway plugin rather than borrowed from
    whichever test happens to skip today, so this cannot quietly stop testing anything the
    day that test's precondition is satisfied."""
    plugin = tmp_path / "skipincall.py"
    plugin.write_text(
        "import pytest\n\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_runtest_call(item):\n"
        "    pytest.skip('forced skip inside the call phase')\n",
        encoding="utf-8",
    )
    result = run_pytest(
        ["tests/unit/test_labels.py", "-p", "skipincall"],
        {"PYTHONHASHSEED": "0", "CI": "true", "PYTHONPATH": str(tmp_path)},
    )
    output = result.stdout + result.stderr
    assert "skipped" in result.stdout, output
    assert re.search(r"\d+ passed", result.stdout) is None, (
        f"the forced skip did not take; this asserts nothing about skips:\n{output}"
    )
    assert result.returncode != 0, "CI reported green on a run in which every test skipped"
    assert "no test executed" in output


def test_a_ci_run_whose_integration_tests_all_skip_is_not_green():
    """The shape the plan was aiming at, reached without a database: this selects the one
    integration-marked test whose precondition is a file that is not in the repository, so
    it skips. One skipped integration test, zero passed, exit 0 -- until the guard."""
    result = run_pytest(
        ["tests/unit/test_run_cli.py", "-m", "integration"],
        {"PYTHONHASHSEED": "0", "CI": "true", "TEST_DATABASE_URL": "postgresql://set/but-unused"},
    )
    output = result.stdout + result.stderr
    assert "1 skipped" in output, (
        "the fixture for this test changed; it no longer reaches the skipped-integration "
        f"shape this asserts:\n{output}"
    )
    assert result.returncode != 0, (
        "CI reported green on an integration run that connected to nothing"
    )
    assert "1 integration tests were selected and not one of them passed" in output


def test_integration_tests_cannot_run_in_ci_without_a_database_url():
    result = run_pytest(
        ["tests/integration/test_health.py"],
        {"PYTHONHASHSEED": "0", "CI": "true", "TEST_DATABASE_URL": ""},
    )
    assert result.returncode != 0, "CI would have reported green without a database"
    assert "TEST_DATABASE_URL is unset" in (result.stdout + result.stderr)


def test_the_guard_stays_out_of_the_way_of_a_healthy_ci_unit_run():
    """The other direction, and the one that keeps this from being a guard that refuses
    everything. A unit job inside CI selects no integration test, needs no database, and
    must exit 0 with no guard output at all."""
    result = run_pytest(
        ["tests/unit/test_labels.py"],
        {"PYTHONHASHSEED": "0", "CI": "true", "TEST_DATABASE_URL": ""},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAKE-GREEN GUARD" not in (result.stdout + result.stderr)


def test_the_guard_stays_out_of_the_way_outside_ci():
    result = run_pytest(
        ["tests/unit/test_labels.py", "--collect-only"],
        {"PYTHONHASHSEED": "0", "CI": ""},
    )
    assert result.returncode == 0, (
        "a local --collect-only was refused; the guard is not scoped to CI and every "
        "developer now has to work around it"
    )
