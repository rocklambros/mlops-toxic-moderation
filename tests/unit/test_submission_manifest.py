"""DELIV-1..4. The offline half: is the evidence complete, and is it safe to publish?

Every one of these deliverables can look fine to the person who built it and be broken for
the grader. A public repository can have a private Actions log. A public W&B *project* is a
different surface from the *Registry* page, and both are different again from a page that
renders. A screenshot can carry an account id nobody noticed.

Two assertions in the plan's draft of this file were changed rather than satisfied, because
satisfying them would have meant writing something untrue. Both changes are marked below
with the reason, since a test that was weakened without saying so is worse than no test.
"""

import re
from pathlib import Path

import pytest
import yaml

from scripts.redact import scan

MANIFEST = Path("docs/submission-manifest.yml")
# The three service addresses README.md publishes on purpose. scripts/redact.py exempts
# exactly these, and scripts/verify_submission.py checks against the same list.
PUBLISHED_ENDPOINTS = frozenset({"44.239.182.162", "34.210.186.130", "52.43.232.239"})

REQUIRED_SCREENSHOTS = {
    "aws-console-three-ec2-and-rds",
    "live-prototype-on-ec2",
    "monitoring-dashboard-populated",
    "blocked-merge",
    "wandb-registry-promoted-stage",
}


def _doc() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _shots() -> dict:
    return {shot["id"]: shot for shot in _doc()["deliverables"]["screenshots"]["items"]}


def test_manifest_covers_all_four_deliverables():
    keys = set(_doc()["deliverables"])
    assert keys == {"repository", "wandb", "screenshots", "live_url"}


def test_every_deliverable_records_a_url_and_a_logged_out_check():
    for name, entry in _doc()["deliverables"].items():
        assert entry.get("url"), f"{name} has no URL"
        assert entry.get("verified_logged_out") is True, f"{name} not verified logged out"
        assert entry.get("verified_on"), f"{name} has no verification date"


def test_the_wandb_entry_covers_the_registry_page_and_not_just_the_project():
    """H11. The submission checklist previously verified only that the PROJECT was public."""
    entry = _doc()["deliverables"]["wandb"]
    assert entry.get("registry_url"), "no registry URL"
    assert entry.get("promoted_stage"), "the promoted stage is not recorded"
    assert entry["registry_url"] != entry["url"], "the registry is a different surface"


def test_the_wandb_urls_are_the_ones_that_render_not_the_ones_that_merely_resolve():
    """The failure this project actually hit. wandb.ai returns 200 for a project that does
    not exist, one that is private, and one that is public with no saved workspace view."""
    entry = _doc()["deliverables"]["wandb"]
    assert "rocklambros" not in entry["url"], "the entity that 404s is back in the manifest"
    assert "rocklambros" not in entry["registry_url"]
    assert "/reports/" in entry["url"], (
        "a bare project URL renders an empty workspace to a logged-out visitor; the "
        "experiment-tracking deliverable must be a Report, which renders standalone"
    )


def test_the_live_url_records_its_availability_window():
    entry = _doc()["deliverables"]["live_url"]
    assert entry.get("availability_window")


def test_the_stop_start_claim_is_recorded_with_a_status_rather_than_asserted():
    """CHANGED FROM THE PLAN, deliberately.

    The plan asserted `survives_stop_start is True`. No stop/start cycle has been run
    against the deployed stack: the rollback rehearsal says in its own words that its health
    check "only proves the containers restarted", which is a weaker claim. Hardcoding True
    would have made this suite certify something nobody had done -- the exact failure the
    whole manifest exists to prevent.

    So the field carries a status, and this test enforces that an unverified claim says how
    to verify it and what partial evidence exists, rather than quietly reading as a pass.
    """
    entry = _doc()["deliverables"]["live_url"]["survives_stop_start"]
    assert entry["status"] in {"verified", "unverified"}, entry["status"]
    if entry["status"] == "verified":
        assert re.fullmatch(r"20\d\d-\d\d-\d\d", str(entry["verified_on"]))
        assert entry.get("evidence"), "a verified claim needs the evidence that verified it"
    else:
        assert entry.get("why_unverified"), "say why, or it reads as an oversight"
        assert entry.get("how_to_verify"), "an unverified claim with no procedure is a shrug"
        assert entry.get("partial_evidence"), "say what IS proven, so the gap is bounded"


def test_the_only_addresses_published_are_the_three_chosen_ones():
    """REPLACES `test_the_live_url_does_not_publish_a_public_address`.

    That test asserted a dotted-quad regex over two fields of this one file, under a name
    that made a claim about the whole repository. When the README started publishing the
    three service addresses on purpose, the name became false while the assertion stayed
    green, and `scripts/verify_submission.py` printed "no public address is published in the
    repository" to anyone running the gate.

    The property worth holding is not "no address" -- that ship sailed deliberately -- but
    "these three and no others", so a fourth host leaking in is still a finding rather than
    riding the precedent. `scripts/redact.py` exempts exactly the same three by value.
    """
    entry = _doc()["deliverables"]["live_url"]
    found = set()
    for field in ("url", "health_url"):
        found.update(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", str(entry[field])))
    assert found, "the live URL resolves to nothing a reader can open"
    unexpected = found - PUBLISHED_ENDPOINTS
    assert not unexpected, f"an unexpected address is published: {sorted(unexpected)}"
    assert entry.get("url_parameter", "").startswith("/toxic/"), "no resolvable source given"


def test_every_required_screenshot_is_present_and_declared_redacted():
    entries = _shots()
    assert REQUIRED_SCREENSHOTS <= set(entries), REQUIRED_SCREENSHOTS - set(entries)
    for shot_id, shot in entries.items():
        path = Path(shot["path"])
        assert path.exists(), f"{shot_id}: {path} is missing"
        assert path.suffix == ".png", shot_id
        assert shot["redacted_account_id"] is True, shot_id
        assert shot["contains_raw_user_text"] is False, shot_id


def test_every_screenshot_on_disk_is_declared_in_the_manifest():
    """The inverse of the check above. An undeclared PNG in the evidence directory is one
    nobody reviewed for an account id before it was pushed to a public repository."""
    declared = {Path(shot["path"]).name for shot in _doc()["deliverables"]["screenshots"]["items"]}
    on_disk = {p.name for p in Path("docs/evidence/screenshots").glob("*.png")}
    assert on_disk <= declared, f"undeclared screenshots: {sorted(on_disk - declared)}"


def test_every_screenshot_says_what_it_shows():
    """A path and a checkbox do not tell a reviewer whether the image proves the claim."""
    for shot_id, shot in _shots().items():
        assert shot.get("shows", "").strip(), f"{shot_id} does not say what it shows"


def test_the_dashboard_screenshot_records_chart_density():
    """C5. A dashboard screenshot with four points and one bar is the failure mode."""
    dashboard = _shots()["monitoring-dashboard-populated"]
    assert dashboard["prediction_count"] >= 2000
    assert dashboard["time_buckets"] >= 7
    assert dashboard["reviewed_items"] >= 200


def test_no_evidence_file_contains_an_account_id():
    """DELIV-3. The manifest and every evidence document are committed to a public repo."""
    targets = [MANIFEST, *Path("docs/evidence").rglob("*.md")]
    findings = scan(targets)
    assert findings == [], [f"{f.path}:{f.line_number} {f.kind}" for f in findings]


@pytest.mark.parametrize("name", sorted(REQUIRED_SCREENSHOTS))
def test_every_required_screenshot_is_a_real_png_with_pixels(name):
    """Guards the shape of the failure where a capture step silently wrote a zero-byte file
    and every path-existence check still passed."""
    path = Path(_shots()[name]["path"])
    header = path.read_bytes()[:8]
    assert header == b"\x89PNG\r\n\x1a\n", f"{name} is not a PNG"
    assert path.stat().st_size > 10_000, f"{name} is {path.stat().st_size} bytes; likely blank"
