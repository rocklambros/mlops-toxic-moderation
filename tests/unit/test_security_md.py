"""H33. Every claim in a public security policy has to be true of the built system.

The practices section used to be nine sentences in the present tense, written before any
code existed. All nine were aspirations at the time, and two were contradicted by the
project's own plan: ingress "restricted to a single operator address" while the deliverable
requires a grader-reachable URL, and "holds no third-party user data" while `/predict` is a
public endpoint that stores submitted comments for thirty days.

A public security policy that is wrong is worse than no security policy, because a reader
has no way to tell which sentences were checked. So each claim now carries a status and a
path, and these tests assert the shape rather than the prose: a status from a closed set, an
evidence reference, and -- the one that actually bites -- that every path cited as evidence
exists on disk.
"""

import re
from pathlib import Path

SECURITY = Path("SECURITY.md")
ALLOWED_STATUS = {"Enforced", "Implemented", "Partial", "Planned", "Not true"}


def _body() -> str:
    return SECURITY.read_text(encoding="utf-8")


def _section() -> str:
    return _body().split("## Practices in this repository")[1].split("\n## ")[0]


def _practice_rows() -> list[list[str]]:
    rows = []
    for line in _section().splitlines():
        if line.startswith("|") and "---" not in line:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and cells[0].lower() not in {"claim", "practice"}:
                rows.append(cells)
    return rows


def test_the_practices_section_is_a_table_not_a_bullet_list():
    section = _section()
    assert "|" in section, "the practices are still unqualified prose"
    assert "Status" in section and "Evidence" in section


def test_every_practice_row_has_a_status_and_evidence():
    rows = _practice_rows()
    assert len(rows) >= 9, f"only {len(rows)} practices are accounted for"
    for claim, status, evidence in ((r[0], r[1], r[2]) for r in rows):
        bare = status.replace("*", "").split("--")[0].split("—")[0].strip()
        assert bare in ALLOWED_STATUS, f"{claim}: bad status '{status}'"
        assert evidence, f"{claim}: no evidence"
        assert re.search(r"[`/.]", evidence), f"{claim}: evidence is not a path or a command"


def test_evidence_paths_that_look_like_files_exist():
    """The one that bites. An earlier draft of this table cited six paths that had never
    been written, including a runbook a shipping script prints as the operator's only
    recovery route."""
    for row in _practice_rows():
        for token in re.findall(r"`([^`]+)`", row[2]):
            if "/" in token and " " not in token and not token.startswith("aws "):
                path = token.split("::")[0].split(":")[0]
                assert Path(path).exists(), f"evidence path missing: {token}"


def test_the_two_contradicted_claims_are_corrected():
    body = _body()
    assert "restricted to a single operator address" not in body, (
        "contradicted by the grader-reachable demo window"
    )
    assert "holds no third-party user data" not in body, (
        "contradicted by a public /predict that stores submitted comments"
    )


def test_the_demo_window_is_described_honestly():
    body = _body().lower()
    assert "demo window" in body
    assert "8503" in body, "say which port is NEVER opened"


def test_the_data_handling_is_described_honestly():
    body = _body().lower()
    assert "/predict" in body
    assert "30 days" in body or "input_text_retention_days" in body


def test_the_model_card_it_cites_exists():
    assert "MODEL_CARD.md" in _body()
    assert Path("MODEL_CARD.md").exists()


def test_no_present_tense_security_claim_survives_outside_the_table():
    """The failure mode was nine unqualified assertions. They do not come back as prose."""
    for line in _section().splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and re.search(r"\b(is|are|runs|uses|exists)\b", stripped):
            raise AssertionError(f"unqualified claim outside the table: {stripped}")


def test_the_cleartext_claim_is_marked_not_true_rather_than_omitted():
    """The temptation is to drop the row. A security policy that lists only the controls
    that pass is an advertisement."""
    rows = [r for r in _practice_rows() if "encrypt" in r[0].lower()]
    assert rows, "no row about transport encryption at all"
    assert any("Not true" in r[1] for r in rows), (
        "the endpoints serve cleartext HTTP; the row must say so"
    )


def test_the_ingress_row_reflects_the_demo_window_being_open():
    """`demo.auto.tfvars` opened the three graded listeners to 0.0.0.0/0 on 2026-08-10. A
    row still claiming operator-only ingress would be false the moment it was written."""
    rows = [r for r in _practice_rows() if "ingress" in r[0].lower()]
    assert rows, "no row about ingress"
    assert any("Partial" in r[1] for r in rows), (
        "ingress is open to the internet during the demo window; 'Enforced' overstates it"
    )
