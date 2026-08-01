"""The pip-audit suppression ledger (premortem H35).

A scanner with an unbounded ignore list converges on ignoring everything, because the cheapest
response to a red build at 1 a.m. is one more line in the ignore list. Every suppression here
carries a reason and an expiry, and an expired suppression fails the build -- which forces the
decision to be re-made rather than inherited.

**Why the last four cases exist.** The committed ledger is empty, and an empty ledger has no
expired rows, so `test_the_committed_ledger_parses_and_has_no_expired_row` is green whether the
expiry rule works or has been deleted. That is the same vacuity the workflow-hygiene suite was
written to avoid. So the expiry rule is exercised against the committed document's own format,
and then end to end: the script is run with a deliberately stale ledger and must exit non-zero,
and run again with a fresh one and must not. Without the second half, "it failed" would not
distinguish an enforced expiry from a broken harness.
"""

import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.vuln_ledger import active_ids, expired, parse_ledger

REPO = Path(__file__).resolve().parents[2]
LEDGER = REPO / "docs" / "security" / "pip-audit-ignores.md"
AUDIT = REPO / "scripts" / "run_pip_audit.sh"

SAMPLE = """
| Vulnerability | Package | Reason it is not exploitable here | Expires |
|---|---|---|---|
| GHSA-aaaa-bbbb-cccc | urllib3 | Proxy-only code path; this project sets no proxy | 2026-09-30 |
| PYSEC-2026-1 | jinja2 | Sandbox escape; no user-controlled template is rendered | 2026-08-01 |
"""
STALE_ROW = "| GHSA-dead-beef-cafe | urllib3 | deliberately stale probe row | 2020-01-01 |\n"


def test_parse_ledger_reads_id_package_reason_and_expiry():
    rows = parse_ledger(SAMPLE)
    assert [row.vuln_id for row in rows] == ["GHSA-aaaa-bbbb-cccc", "PYSEC-2026-1"]
    assert rows[0].package == "urllib3"
    assert rows[0].expires == dt.date(2026, 9, 30)
    assert "proxy" in rows[0].reason.lower()


def test_parse_ledger_ignores_the_header_and_separator_rows():
    assert len(parse_ledger(SAMPLE)) == 2


def test_a_suppression_with_no_reason_is_rejected():
    bad = SAMPLE.replace("Proxy-only code path; this project sets no proxy", "  ")
    with pytest.raises(ValueError, match="reason"):
        parse_ledger(bad)


def test_a_suppression_with_an_unparseable_expiry_is_rejected():
    bad = SAMPLE.replace("2026-09-30", "soon")
    with pytest.raises(ValueError, match="expiry"):
        parse_ledger(bad)


def test_a_suppression_with_no_expiry_is_rejected():
    bad = SAMPLE.replace("| 2026-09-30 |", "|  |")
    with pytest.raises(ValueError, match="expiry"):
        parse_ledger(bad)


def test_expired_suppressions_are_reported_and_not_applied():
    rows = parse_ledger(SAMPLE)
    today = dt.date(2026, 8, 15)
    assert [row.vuln_id for row in expired(rows, today)] == ["PYSEC-2026-1"]
    assert active_ids(rows, today) == ["GHSA-aaaa-bbbb-cccc"]


def test_the_committed_ledger_parses_and_has_no_expired_row():
    assert LEDGER.exists(), "docs/security/pip-audit-ignores.md is missing"
    rows = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    stale = expired(rows, dt.date.today())
    assert not stale, (
        "these suppressions have outlived their justification and must be re-decided: "
        + ", ".join(f"{row.vuln_id} ({row.package}, expired {row.expires})" for row in stale)
    )


def test_the_audit_script_reads_the_ledger_rather_than_hardcoding_ignores():
    script = AUDIT.read_text(encoding="utf-8")
    assert "vuln_ledger" in script, "the ignore list must come from the reviewed ledger"
    assert "--ignore-vuln" not in script.split("vuln_ledger")[0], (
        "a hardcoded --ignore-vuln bypasses the ledger and its expiry rule"
    )


# --------------------------------------------------------------------------------------
# the expiry rule, exercised rather than merely present
# --------------------------------------------------------------------------------------


def test_the_expiry_rule_bites_on_the_committed_document_s_own_format():
    """The case above passes on an empty ledger whether or not `expired` still works. This one
    feeds the committed document a row in its own table syntax and requires the rule to catch
    it, so the format and the rule are checked together."""
    text = LEDGER.read_text(encoding="utf-8") + STALE_ROW
    rows = parse_ledger(text)
    ids = [row.vuln_id for row in rows]
    assert "GHSA-dead-beef-cafe" in ids, (
        "the committed ledger's table syntax no longer parses into rows, so every assertion "
        f"about its contents is vacuous; parsed: {ids}"
    )
    today = dt.date.today()
    assert [row.vuln_id for row in expired(rows, today)] == ["GHSA-dead-beef-cafe"]
    assert "GHSA-dead-beef-cafe" not in active_ids(rows, today)


def _fake_toolchain(root: Path) -> Path:
    """A PATH holding this interpreter as `python` and a pip-audit that always succeeds.

    The stub matters: with a real pip-audit the run could fail for a reason that has nothing
    to do with the ledger, and the test would report a passing expiry rule it never proved.
    """
    binaries = root / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    (binaries / "python").symlink_to(sys.executable)
    stub = binaries / "pip-audit"
    stub.write_text('#!/bin/sh\necho "stub pip-audit: $*"\nexit 0\n', encoding="utf-8")
    stub.chmod(0o755)
    return binaries


def _run_audit(tmp_path: Path, ledger_text: str) -> subprocess.CompletedProcess:
    workspace = tmp_path / "workspace"
    (workspace / "docs" / "security").mkdir(parents=True)
    (workspace / "docs" / "security" / "pip-audit-ignores.md").write_text(
        ledger_text, encoding="utf-8"
    )
    shutil.copytree(REPO / "requirements", workspace / "requirements")
    (workspace / "scripts").mkdir()
    shutil.copy2(AUDIT, workspace / "scripts" / AUDIT.name)
    binaries = _fake_toolchain(tmp_path)
    return subprocess.run(
        ["bash", str(workspace / "scripts" / AUDIT.name)],
        cwd=workspace,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{binaries}:/usr/bin:/bin",
            "PYTHONPATH": str(REPO),
            "PYTHONHASHSEED": "0",
            "HOME": str(tmp_path),
        },
    )


def test_an_expired_suppression_fails_the_build(tmp_path):
    """The whole point of the ledger. `python -m scripts.vuln_ledger` exits 1 on a stale row,
    and the audit script must propagate that rather than carrying on with an empty ignore
    list -- which is what `mapfile -t X < <(cmd)` does, silently, because a process
    substitution's exit status is invisible to `set -e` and to `pipefail`."""
    result = _run_audit(tmp_path, LEDGER.read_text(encoding="utf-8") + STALE_ROW)
    assert result.returncode != 0, (
        "an expired suppression did not fail the audit; the ledger is decorative:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "GHSA-dead-beef-cafe" in result.stdout + result.stderr, (
        "the run failed without naming the expired row, so the operator cannot act on it"
    )


def test_the_same_audit_run_succeeds_once_the_stale_row_is_gone(tmp_path):
    """The control for the case above. Without it, a harness that could never succeed would
    look exactly like an enforced expiry rule."""
    result = _run_audit(tmp_path, LEDGER.read_text(encoding="utf-8"))
    assert result.returncode == 0, (
        "the audit fails even with a clean ledger, so the failure above proves nothing:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_the_audit_covers_every_lock_this_repository_ships():
    """A surface added with its own lock and left out of this script is a dependency set
    nothing scans. Discovered from the requirements directory rather than read out of the
    script, so the script cannot define its own coverage."""
    script = AUDIT.read_text(encoding="utf-8")
    shipped = sorted(
        path.name
        for path in (REPO / "requirements").iterdir()
        if path.is_file() and "--hash=sha256:" in path.read_text(encoding="utf-8")
    )
    assert shipped, "no hashed lock found, so this assertion certifies nothing"
    missing = [name for name in shipped if f"requirements/{name}" not in script]
    assert not missing, f"these locks are never audited: {missing}"


def test_the_audit_script_is_executable():
    assert os.access(AUDIT, os.X_OK), "chmod +x scripts/run_pip_audit.sh"
