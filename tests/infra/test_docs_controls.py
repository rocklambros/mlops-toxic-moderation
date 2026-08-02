"""Assertions about the documents that record an infrastructure decision.

Every case here reads a committed file and nothing else. They live under `tests/infra`
because that is where the plans put the infrastructure documentation controls, and the
directory marker (`awsapply`) is applied by the root conftest -- but none of them opens a
socket, so they run anywhere the repository is checked out.
"""

from pathlib import Path

DOCS = Path("docs")
COST = DOCS / "cost-model.md"
DELIVERY_SPEC = DOCS / "superpowers" / "specs" / "2026-07-30-delivery-plan-design.md"


def test_the_delivery_spec_no_longer_carries_the_superseded_hourly_figure():
    """Remediation 0.2: corrections are made at source. A superseding document plus an
    unedited original is a supersession table, which is what that remediation rejected."""
    spec = DELIVERY_SPEC.read_text(encoding="utf-8")
    assert "$0.101/hr" not in spec
    assert "docs/cost-model.md" in spec, "point the reader at the document of record"


def test_the_cost_model_is_the_document_of_record_and_states_both_halves():
    """H7. The finding was not that the number was wrong; it was that the number quoted
    one half of a two-half cost and read as if it were the whole."""
    text = COST.read_text(encoding="utf-8")
    assert "Fixed monthly subtotal" in text
    assert "Variable, per running hour" in text
    for scenario in ("Scenario A", "Scenario B", "Scenario C"):
        assert scenario in text
    assert "$100" in text


def test_the_cost_model_prices_every_line_item_the_superseded_figure_omitted():
    text = COST.read_text(encoding="utf-8")
    for item in (
        "Elastic IP",
        "EBS",
        "RDS storage",
        "RDS backup",
        "CloudTrail",
        "GuardDuty",
        "ECR",
        "Secrets Manager",
        "CloudWatch",
        "SNS",
        "Data transfer",
        "EventBridge",
        "Terraform state",
    ):
        assert item in text, f"{item} is still missing from the cost model"


def test_the_cost_model_names_the_control_that_makes_the_ceiling_hold():
    """A budget alert is a notification. The scheduled stop is the thing that acts."""
    assert "nightly_stop_enabled" in COST.read_text(encoding="utf-8")
