"""CUT-1. Cut-line item 1 must be deletable in one commit with no other consequence.

The SBOM and AIBOM are ungraded and cheap to append later, which is exactly why they are on
the cut list. Planning them is right; letting them become load-bearing is not. The tests
that matter most here are the four severability ones -- they were green before the artifacts
existed and must stay green after, because their whole job is to prove that `git rm sbom.json
aibom.json scripts/make_sbom.py` is a complete change.
"""

import json
import re
import subprocess
from pathlib import Path

MAKEFILE = Path("Makefile")
SBOM = Path("sbom.json")
AIBOM = Path("aibom.json")
CARD = Path("MODEL_CARD.md")
GATE_TARGETS = ("test", "lint", "aws-up", "aws-down", "deploy-verify", "rollback", "db-dump")


def _prereqs(target: str) -> list[str]:
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"^{re.escape(target)}\s*:\s*(.*)$", line)
        if match:
            return match.group(1).split()
    return []


# ------------------------------------------------------------------ severability


def test_no_gate_target_depends_on_the_sbom():
    for target in GATE_TARGETS:
        prereqs = _prereqs(target)
        assert "sbom" not in prereqs, f"{target} depends on the SBOM"
        assert "aibom" not in prereqs, f"{target} depends on the AIBOM"


def test_no_workflow_requires_the_sbom():
    for path in Path(".github/workflows").glob("*.yml"):
        body = path.read_text(encoding="utf-8")
        assert "sbom.json" not in body, path
        assert "aibom.json" not in body, path


def test_no_test_outside_this_file_imports_or_reads_the_sbom():
    hits = subprocess.run(
        ["grep", "-rl", "--binary-files=without-match", "--exclude-dir=__pycache__",
         "-e", "sbom.json", "-e", "aibom.json", "tests/"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert hits == ["tests/unit/test_sbom_severability.py"], hits


def test_no_application_code_reads_either_artifact():
    """A dashboard panel or a health check that renders the SBOM would make cutting it a
    user-visible change rather than a deletion."""
    hits = subprocess.run(
        ["grep", "-rl", "--binary-files=without-match", "--exclude-dir=__pycache__",
         "-e", "sbom.json", "-e", "aibom.json",
         "backend", "frontend", "monitoring", "rescorer", "model", "infra"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert hits == [], hits


# ------------------------------------------------------------------ the artifacts


def test_the_sbom_is_valid_cyclonedx():
    doc = json.loads(SBOM.read_text(encoding="utf-8"))
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] >= "1.5"
    assert doc["components"], "an empty SBOM is worse than no SBOM"


def test_every_sbom_component_carries_a_version_and_a_purl():
    """A component with no version answers no question anyone asks an SBOM."""
    for component in json.loads(SBOM.read_text(encoding="utf-8"))["components"]:
        assert component.get("version"), f"{component['name']} has no version"
        assert component.get("purl", "").startswith("pkg:pypi/"), component["name"]


def test_the_sbom_carries_the_hashes_the_lock_pins():
    """The point of generating from a --generate-hashes lock rather than from a resolver is
    that the SBOM describes the bytes that actually get installed."""
    components = json.loads(SBOM.read_text(encoding="utf-8"))["components"]
    hashed = [c for c in components if c.get("hashes")]
    assert len(hashed) == len(components), "some components lost their hashes"
    for component in components:
        for entry in component["hashes"]:
            assert entry["alg"] == "SHA-256"
            assert re.fullmatch(r"[0-9a-f]{64}", entry["content"]), component["name"]


def test_the_sbom_covers_the_serving_lock_completely():
    """Generated from requirements/serve.txt, so it must account for every pin in it. A
    partial SBOM is the failure mode that looks like success."""
    lock = Path("requirements/serve.txt").read_text(encoding="utf-8")
    pinned = {
        m.group(1).lower().replace("_", "-")
        for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", lock, re.M)
    }
    named = {
        c["name"].lower().replace("_", "-")
        for c in json.loads(SBOM.read_text(encoding="utf-8"))["components"]
    }
    assert pinned <= named, f"missing from the SBOM: {sorted(pinned - named)}"


def test_the_aibom_names_the_model_the_data_and_the_digest():
    doc = json.loads(AIBOM.read_text(encoding="utf-8"))
    names = {component["name"] for component in doc["components"]}
    assert "toxic-clf" in names
    assert any("jigsaw" in name.lower() for name in names)
    body = AIBOM.read_text(encoding="utf-8")
    assert re.search(r"\b[0-9a-f]{64}\b", body), "the AIBOM does not pin the artifact digest"


def test_the_aibom_digest_matches_the_model_card():
    card = re.search(r"\b[0-9a-f]{64}\b", CARD.read_text(encoding="utf-8"))
    assert card and card.group(0) in AIBOM.read_text(encoding="utf-8")


def test_the_aibom_names_the_registry_entity_that_actually_exists():
    """The plan's draft named entity `rocklambros`, which does not exist -- the same 404 the
    README carried. Runs live under `rockcyber`, the registry under `rockcyber-org`."""
    body = AIBOM.read_text(encoding="utf-8")
    assert "rocklambros" not in body, "names a W&B entity that 404s"
    assert "rockcyber" in body


def test_the_aibom_records_the_data_version_the_card_records():
    """An AIBOM whose data component has no version cannot answer 'what was it trained on'."""
    card = Path("MODEL_CARD.md").read_text(encoding="utf-8")
    composite = re.search(r"composite `data_version`\s*\|\s*`([0-9a-f]{64})`", card)
    assert composite, "MODEL_CARD.md no longer records a composite data_version"
    assert composite.group(1) in AIBOM.read_text(encoding="utf-8")


def test_regenerating_on_the_same_commit_produces_the_same_bytes():
    """A wall-clock timestamp would make every regeneration a diff, and a diff nobody can
    explain is a diff nobody reviews. The stamp is the HEAD commit date."""
    before = SBOM.read_bytes(), AIBOM.read_bytes()
    subprocess.run(["python", "scripts/make_sbom.py"], check=True, capture_output=True)
    assert (SBOM.read_bytes(), AIBOM.read_bytes()) == before, "generation is not deterministic"
