"""C9. The clause list is parsed from the rubric, so the matrix cannot silently drift.

The existing coverage matrix keys every row on a section of the *design spec*. That proves
the plan covers the design; it cannot prove the plan covers the grade, and four rubric
clauses turned out to have no owning task at all. This one is keyed on the rubric, and reads
the clauses out of the rubric file itself rather than from a transcription, so rewording the
assignment turns the suite red instead of silently invalidating the self-grade.
"""

import re
from pathlib import Path

RUBRIC = Path("docs/week9_FinalProject.md")
MATRIX = Path("docs/rubric-conformance.md")


# Nested sub-clauses carry real requirements and have no bullet of their own, so the three
# top-level regexes fold them into a parent row that never asserts them. Rubric 3.2's
# "(data exchanged via the database, not JSON files)" is the case that had no owner at all.
# Each entry is (clause id, a literal that must appear in the rubric).
SUB_CLAUSES = {
    "2.2-log-every-request": "must log every prediction request",
    "3.2-different-server": "on a different EC2 server",
    "3.2-not-json-files": "not JSON files",
    "3.2-latency": "Prediction latency over time",
    "3.2-target-drift": "target drift",
    "3.2-user-feedback": "collect user feedback",
    "4.1-unit": "Unit tests for individual functions",
    "4.1-integration": "Integration tests for FastAPI endpoints",
}


def rubric_clauses() -> set[str]:
    body = RUBRIC.read_text(encoding="utf-8")
    clauses = set(re.findall(r"^- \*\*(\d\.\d)\s", body, re.M))  # 1.1 .. 5.3
    clauses |= {f"Core {n}" for n in re.findall(r"^(\d)\. \*\*.+?\*\* —", body, re.M)}
    clauses |= set(re.findall(r"^- \*\*([A-Z][^:*]*):\*\*", body, re.M))  # deliverables
    for clause_id, literal in SUB_CLAUSES.items():
        assert literal in body, (
            f"{clause_id}: the rubric text this clause id tracks ({literal!r}) is gone; "
            "the rubric was reworded and this matrix is now measuring the wrong document"
        )
        clauses.add(clause_id)
    return clauses


def matrix_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 4 and cells[0].lower() not in {"rubric clause", "clause"}:
            rows[cells[0].strip("*` ")] = cells
    return rows


def test_the_rubric_parses_into_the_expected_clause_set():
    """Written out rather than counted. A `>= 20` floor against an actual 21 lets a single
    reworded bullet drop a clause without failing anything, which is the drift this matrix
    exists to prevent."""
    expected = {
        "1.1", "1.2", "1.3", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2", "5.3",
        "Core 1", "Core 2", "Core 3", "Core 4", "Core 5", "Core 6",
        "GitHub Repository URL", "Project Workflow Screenshots",
        "Experiment Tracking Dashboard URL",
        "2.2-log-every-request", "3.2-different-server", "3.2-not-json-files",
        "3.2-latency", "3.2-target-drift", "3.2-user-feedback",
        "4.1-unit", "4.1-integration",
    }
    clauses = rubric_clauses()
    assert clauses == expected, {
        "missing": sorted(expected - clauses),
        "unexpected": sorted(clauses - expected),
    }


def test_every_rubric_clause_has_an_owner_and_evidence():
    """C9. Four clauses previously had no owning task, and they were the boring ones."""
    rows = matrix_rows()
    missing = sorted(rubric_clauses() - set(rows))
    assert not missing, f"rubric clauses with no row: {missing}"
    for clause, cells in rows.items():
        assert cells[1], f"{clause}: no owning artifact"
        assert cells[2], f"{clause}: no evidence"
        assert cells[3] in {"PASS", "FAIL", "PARTIAL"}, f"{clause}: bad verdict '{cells[3]}'"


def test_the_self_grade_was_run_against_the_live_system():
    body = MATRIX.read_text(encoding="utf-8")
    assert re.search(r"Graded on[^\n]*20\d\d-\d\d-\d\d", body)
    assert "live" in body.lower()


def test_no_clause_is_left_failing():
    failing = [clause for clause, cells in matrix_rows().items() if cells[3] == "FAIL"]
    assert not failing, f"unremediated rubric failures: {failing}"


def test_partial_verdicts_carry_a_written_justification():
    for clause, cells in matrix_rows().items():
        if cells[3] == "PARTIAL":
            assert len(cells) >= 5 and cells[4], f"{clause}: PARTIAL with no justification"


def test_evidence_paths_in_the_matrix_exist():
    """Every repo-relative path cited as evidence has to be on disk.

    A leading slash excludes HTTP routes: `/predict` and `/health` are endpoints this
    project serves, not files it ships, and every evidence path in this repository is
    relative. Without that discriminator the check fails on the API surface it is supposed
    to be crediting.
    """
    for clause, cells in matrix_rows().items():
        for token in re.findall(r"`([^`]+)`", cells[2]):
            if token.startswith(("/", "http", "aws ")) or " " in token or "/" not in token:
                continue
            assert Path(token.split("::")[0]).exists(), f"{clause}: missing {token}"


def test_a_partial_verdict_is_not_quietly_upgraded_by_deleting_the_note():
    """The cheapest way to make a PARTIAL row look finished is to change the verdict and
    leave everything else. This asserts the two clauses known to be short of PASS are still
    reported as such, so upgrading one has to be a deliberate edit that says why."""
    rows = matrix_rows()
    promoted = rows.get("1.3")
    assert promoted, "rubric 1.3 has no row"
    if promoted[3] == "PASS":
        raise AssertionError(
            "1.3 says PASS. The promoted model is the classical toxic-clf at macro PR-AUC "
            "0.6632; the DistilBERT challenger reached 0.7268. If a higher-scoring model is "
            "now promoted, update MODEL_CARD.md section 5 in the same commit and say so here."
        )
