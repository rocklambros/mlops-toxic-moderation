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


def test_readme_states_the_availability_window():
    """Delivery spec section 12: the live URL carries its availability window in the README."""
    body = _text()
    assert "Availability window" in body
    assert re.search(r"20\d\d-\d\d-\d\d", body), "state real dates, not 'during work sessions'"


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
    body = _text()
    assert "$100" in body
    assert "nightly stop" in body.lower() or "stops the instances" in body.lower(), (
        "a budget alert is a notification; name the control that actually stops spend"
    )
