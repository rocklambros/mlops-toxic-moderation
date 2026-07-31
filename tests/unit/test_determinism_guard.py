import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_suite_refuses_to_run_without_pythonhashseed():
    env = dict(os.environ)
    env.pop("PYTHONHASHSEED", None)
    env["PYTHONPATH"] = str(REPO)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_labels.py", "-q"],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "PYTHONHASHSEED=0 is required" in (result.stdout + result.stderr)


def test_guard_allows_run_with_pythonhashseed_zero():
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONPATH"] = str(REPO)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_labels.py", "-q"],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
