#!/usr/bin/env python3
"""Re-run the offline half of the submission check, and print what a reader would see.

The test suite asserts these properties; this prints them. Both exist because they answer
different questions at different moments. `pytest` answers "did anything regress" in CI.
This answers "what am I about to hand in" at the keyboard, five minutes before submitting,
where a red dot in a list is more useful than a stack trace.

Exit status is non-zero if any check fails, so it is usable as a gate.

What it deliberately does NOT do: reach the network. Every online claim -- that the W&B
Report renders logged out, that the repository opens without a session, that the live URL
answers -- belongs to `tests/integration/test_submission_logged_out.py`, which strips
credentials properly. A convenience script that quietly used the operator's `~/.netrc` would
recreate the exact failure this manifest was written to catch.

Usage:  python scripts/verify_submission.py [--manifest docs/submission-manifest.yml]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.redact import scan  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# The three service addresses README.md publishes. Named here so the check below asserts
# "these and no others" rather than "none", which stopped being true when the README started
# publishing them and left this gate printing a green line the repository contradicted.
PUBLISHED_ENDPOINTS = ("44.239.182.162", "34.210.186.130", "52.43.232.239")


def _only_published_endpoints(live: dict) -> bool:
    """True when every address in the live-URL block is one of the three chosen hosts."""
    found = set()
    for field in ("url", "health_url"):
        found.update(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", str(live.get(field, ""))))
    return found <= set(PUBLISHED_ENDPOINTS)
REQUIRED_SCREENSHOTS = {
    "aws-console-three-ec2-and-rds",
    "live-prototype-on-ec2",
    "monitoring-dashboard-populated",
    "blocked-merge",
    "wandb-registry-promoted-stage",
}


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
        if not ok:
            self.failures.append(label)
        return ok

    def note(self, label: str, detail: str) -> None:
        print(f"  [note] {label} -- {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="docs/submission-manifest.yml")
    args = parser.parse_args()

    manifest = REPO / args.manifest
    if not manifest.is_file():
        print(f"missing manifest: {manifest}", file=sys.stderr)
        return 2

    doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    deliverables = doc["deliverables"]
    report = Report()

    print("\nDeliverables")
    for name in ("repository", "wandb", "screenshots", "live_url"):
        entry = deliverables.get(name, {})
        report.check(bool(entry.get("url")), f"{name}: has a URL")
        report.check(
            entry.get("verified_logged_out") is True,
            f"{name}: verified logged out",
            str(entry.get("verified_on", "no date")),
        )

    print("\nWeights & Biases")
    wandb = deliverables["wandb"]
    report.check("rocklambros" not in wandb["url"], "tracking URL avoids the 404 entity")
    report.check("/reports/" in wandb["url"], "tracking is a Report, which renders logged out")
    report.check(wandb["registry_url"] != wandb["url"], "registry is a separate surface")
    report.check(bool(wandb.get("promoted_stage")), "promoted stage recorded",
                 wandb.get("promoted_stage", ""))

    print("\nLive endpoint")
    live = deliverables["live_url"]
    report.check(
        _only_published_endpoints(live),
        "the only addresses here are the three service endpoints published on purpose",
    )
    report.check(bool(live.get("url_parameter")), "a resolvable source is named",
                 live.get("url_parameter", ""))
    report.check(bool(live.get("availability_window")), "availability window is stated")
    stop_start = live.get("survives_stop_start", {})
    if stop_start.get("status") == "verified":
        report.check(bool(stop_start.get("evidence")), "stop/start survival has evidence")
    else:
        report.note(
            "stop/start survival is UNVERIFIED",
            f"run `{stop_start.get('how_to_verify', 'make aws-down && make aws-up')}` to close it",
        )

    print("\nScreenshots")
    items = {shot["id"]: shot for shot in deliverables["screenshots"]["items"]}
    missing = REQUIRED_SCREENSHOTS - set(items)
    report.check(not missing, "every required screenshot is declared", ", ".join(sorted(missing)))
    for shot_id, shot in sorted(items.items()):
        path = REPO / shot["path"]
        if not report.check(path.is_file(), f"{shot_id}: file present", shot["path"]):
            continue
        data = path.read_bytes()
        report.check(data[:8] == PNG_MAGIC and len(data) > 10_000,
                     f"{shot_id}: is a non-blank PNG", f"{len(data) // 1024} KiB")
        report.check(shot.get("redacted_account_id") is True, f"{shot_id}: account id redacted")
        report.check(shot.get("contains_raw_user_text") is False, f"{shot_id}: no third-party text")

    declared = {Path(s["path"]).name for s in deliverables["screenshots"]["items"]}
    on_disk = {p.name for p in (REPO / "docs/evidence/screenshots").glob("*.png")}
    report.check(on_disk <= declared, "no undeclared screenshot in the evidence directory",
                 ", ".join(sorted(on_disk - declared)))

    print("\nPublication safety")
    targets = [manifest, *(REPO / "docs/evidence").rglob("*.md")]
    findings = scan(targets)
    report.check(
        findings == [],
        "no account id, address or credential in the manifest or evidence",
        "; ".join(f"{f.path}:{f.line_number} {f.kind}" for f in findings[:5]),
    )

    print()
    if report.failures:
        print(f"FAILED {len(report.failures)} check(s):")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    print("All offline submission checks passed.")
    print("The online half is: pytest tests/integration/test_submission_logged_out.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
