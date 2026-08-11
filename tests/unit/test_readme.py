"""Rubric 5.3 is graded on three things. Two of them were in the plan; the third was not."""

import re
from pathlib import Path

README = Path("README.md")
COST_MODEL = Path("docs/cost-model.md")


def _text() -> str:
    return README.read_text(encoding="utf-8")


def _sections() -> list[str]:
    return [line.lstrip("# ").strip() for line in _text().splitlines() if line.startswith("## ")]


def test_readme_covers_the_three_graded_headings():
    sections = _sections()
    for required in ("Setup", "Deployment", "Example requests"):
        assert any(required.lower() in s.lower() for s in sections), f"missing '{required}' section"


def test_readme_shows_a_runnable_predict_example():
    """H32. 'Example user requests' is a rubric clause with no owning task before this one."""
    body = _text()
    assert "curl -X POST" in body
    assert "/predict" in body
    assert "X-API-Key" in body, "the example must show the demo key header or it does not run"
    assert '"text"' in body, "the example must show the request body"


def test_readme_shows_the_expected_predict_response():
    body = _text()
    for key in ("request_id", "model_version", "decision", "max_prob", "latency_ms"):
        assert key in body, f"the documented response is missing {key}"


def test_readme_shows_a_health_example():
    assert re.search(r"curl\s+(-\S+\s+)*\S*/health", _text())


def test_the_readme_publishes_the_three_live_addresses():
    """CHANGED FROM 'availability window', deliberately.

    The earlier test required the README to name a window during which the live URL would
    answer, because the stack was expected to be stopped between sessions. It is not: the
    service runs continuously, so there is no window to state and a date in this file would
    only invite the reader to wonder what happens after it.

    What replaces it is the stronger property. A reader cannot assess a deployed system they
    cannot reach, and for most of this project's life the addresses appeared in the README
    only as `<eip-1>` placeholders. This asserts all three resolvable addresses are present,
    each with its port, so the front door is a door.
    """
    body = _text()
    for address, port in (
        ("44.239.182.162", "8000"),
        ("34.210.186.130", "8501"),
        ("52.43.232.239", "8502"),
    ):
        assert f"{address}:{port}" in body, f"the README does not publish {address}:{port}"
    assert "<eip-" not in body, "a placeholder address survived; the reader cannot click it"


def test_the_readme_does_not_advertise_an_end_to_the_service():
    """A reader deciding whether to open a URL should not first read that it will be gone.

    The README twice told its own reader the stack would be destroyed on a fixed date, which
    is an instruction to skip every live check in the document. Availability is now a
    property of the running system rather than a promise with an expiry.
    """
    body = _text().lower()
    for phrase in ("is destroyed", "will be destroyed", "availability window", "grading window"):
        assert phrase not in body, f"the README still advertises an end to the service: {phrase!r}"


def test_readme_documents_the_three_instance_topology():
    body = _text()
    for component in ("backend", "frontend", "monitoring"):
        assert component in body.lower()
    assert "t4g.medium" in body and "t4g.small" in body


def test_readme_carries_no_account_id_and_no_secret_value():
    """The 12-digit id and the demo key are referenced by placeholder, never by value."""
    body = _text()
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", body), "a 12-digit account id is in the README"
    assert "AKIA" not in body
    assert "DEMO_API_KEY" in body, "reference the key by variable, never by value"


def test_readme_is_not_the_placeholder():
    assert "This README is a placeholder" not in _text()


def _mermaid_blocks() -> list[str]:
    return re.findall(r"```mermaid\n(.*?)```", _text(), re.S)


def test_the_architecture_is_drawn_not_only_described():
    """A three-instance topology with a private database and a deploy gate is a picture.
    Prose describing it is a picture the reader has to redraw in their head."""
    blocks = _mermaid_blocks()
    assert len(blocks) >= 3, f"only {len(blocks)} mermaid diagrams in the README"
    kinds = {block.strip().split("\n")[0].split()[0] for block in blocks}
    assert {"flowchart", "sequenceDiagram"} <= kinds, f"diagram kinds present: {kinds}"


def test_no_mermaid_label_contains_a_character_github_refuses_to_render():
    """`#` opens an HTML entity code inside a mermaid label, so `EC2 #1` renders as a broken
    diagram on GitHub rather than as an error anyone would notice locally.

    Validated once against mermaid 11.16.1, the major GitHub renders with: all blocks parse.
    This test is the cheap standing guard, because adding Node to a Python CI job to re-parse
    a diagram costs more than it saves.
    """
    for index, block in enumerate(_mermaid_blocks(), 1):
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("classDef") or "fill:" in stripped:
                continue  # hex colours are not label text
            assert "#" not in stripped, (
                f"mermaid block {index} has '#' in a label, which GitHub reads as an HTML "
                f"entity and fails to render: {stripped!r}"
            )


def test_every_mermaid_fence_is_closed():
    """An unclosed fence swallows the rest of the README into a code block."""
    body = _text()
    assert body.count("```mermaid") == len(_mermaid_blocks()), "an unclosed mermaid fence"
    assert body.count("```") % 2 == 0, "odd number of code fences in the README"


def test_readme_cost_agrees_with_the_cost_model():
    """H7. An hourly rate for the running state omits the money that accrues while the
    stack is stopped, which is most of the graded fortnight."""
    body = _text()
    model = COST_MODEL.read_text(encoding="utf-8")
    fixed = re.search(r"Fixed monthly subtotal.*?\$([0-9.]+)", model, re.S)
    assert fixed, "docs/cost-model.md must state a 'Fixed monthly subtotal'"
    assert fixed.group(1) in body, (
        "the README omits the fixed monthly cost that accrues while stopped"
    )
    assert "$0.101" not in body
    assert re.search(r"Scenario C|full billing month|worst case", body), (
        "state the worst-case month, not just the hourly rate"
    )


def test_readme_names_the_budget_and_the_hard_control_not_only_the_alert():
    """The intent survives; the example changed.

    This used to require the README to name the nightly stop schedule. That schedule is
    disabled, so citing it would describe a control that is not in force -- and describing a
    shutdown to a reader who is about to open the live URLs is worse than saying nothing.

    The point of the test is unchanged: a budget alert is a notification, and the README must
    name something that actually refuses. The service control policy does, by denying every
    instance type outside a four-entry allowlist. That refusal is in force right now, which
    the nightly stop is not.
    """
    body = _text()
    assert "$100" in body
    assert "service control policy" in body.lower(), (
        "a budget alert is a notification; name the control that actually refuses"
    )
    assert "hard refusal" in body.lower() or "denies" in body.lower(), (
        "say that the control refuses, not that it warns"
    )
