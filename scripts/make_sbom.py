#!/usr/bin/env python3
"""Generate a CycloneDX SBOM for the serving image, and an AIBOM for the model it serves.

SEVERABLE. Cut-line item 1. Nothing gates on the output: no Make target depends on it, no
workflow reads it, no application code opens it, and no test outside
`tests/unit/test_sbom_severability.py` mentions it. Deleting `sbom.json`, `aibom.json` and
this file is a complete change, and four tests in that module exist to keep it that way.

**Why this reads the lock instead of running `cyclonedx-py`.** Two reasons, and the second
is the load-bearing one.

1. `requirements/serve.txt` is pip-compiled with `--generate-hashes`. It already holds every
   field an SBOM needs -- name, exact version, and the SHA-256 of each distribution -- so a
   resolver would be a second opinion about bytes we have already pinned. Generating from
   the lock means the SBOM describes what actually gets installed rather than what a fresh
   resolution would install today.
2. `tests/unit/test_install_commands.py` forbids any unhashed package installation outside
   `docs/` and `tests/`, and `scripts/` is in its scan roots. Fetching `cyclonedx-bom` at
   run time from a committed script would put an unverified download into the supply chain
   of a document whose entire purpose is describing that supply chain. That is not an
   obstacle to work around; it is the control working.

Determinism: the metadata timestamp is the HEAD commit's committer date, not the wall clock,
so regenerating on the same commit produces byte-identical files. A generator that rewrites
its own output on every run produces diffs nobody can explain, and diffs nobody can explain
are diffs nobody reviews.

Usage:  python scripts/make_sbom.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVE_LOCK = REPO / "requirements/serve.txt"
CARD = REPO / "MODEL_CARD.md"
SBOM = REPO / "sbom.json"
AIBOM = REPO / "aibom.json"

# `pkg-name==1.2.3 \` followed by one or more `--hash=sha256:<hex>` continuation lines.
PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s\\;]+)", re.MULTILINE)
HASH = re.compile(r"--hash=sha256:(?P<digest>[0-9a-f]{64})")


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def _pinned_packages(lock_text: str) -> list[dict]:
    """Every pin in the lock, with the hashes that belong to it.

    A pin owns every `--hash=` line between itself and the next pin. Parsing by position
    rather than by regexing the whole file at once is what keeps hashes attached to the
    right package when a wheel and an sdist are both listed.
    """
    matches = list(PIN.finditer(lock_text))
    packages = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(lock_text)
        block = lock_text[match.end() : end]
        digests = [m.group("digest") for m in HASH.finditer(block)]
        name = match.group("name")
        packages.append(
            {
                "type": "library",
                "name": name,
                "version": match.group("version"),
                "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{match.group('version')}",
                "hashes": [{"alg": "SHA-256", "content": d} for d in digests],
            }
        )
    return packages


def _first_sha256(text: str) -> str:
    match = re.search(r"\b[0-9a-f]{64}\b", text)
    if match is None:
        raise SystemExit("MODEL_CARD.md carries no 64-hex digest; refusing to write a blank AIBOM")
    return match.group(0)


def _composite_data_version(card_text: str) -> str:
    match = re.search(r"composite `data_version`\s*\|\s*`([0-9a-f]{64})`", card_text)
    if match is None:
        raise SystemExit("MODEL_CARD.md no longer records a composite data_version")
    return match.group(1)


def main() -> int:
    if not SERVE_LOCK.is_file():
        raise SystemExit(f"{SERVE_LOCK} is missing; the SBOM is generated from the lock")

    sha = _git("rev-parse", "HEAD")
    # Committer date of HEAD, ISO-8601 UTC. Stable across regenerations of the same commit.
    stamp = _git("show", "-s", "--format=%cd", "--date=format-local:%Y-%m-%dT%H:%M:%SZ", "HEAD")

    card_text = CARD.read_text(encoding="utf-8")
    components = _pinned_packages(SERVE_LOCK.read_text(encoding="utf-8"))
    if not components:
        raise SystemExit(
            "parsed zero packages from the serving lock; refusing to write an empty SBOM"
        )

    metadata = {
        "timestamp": stamp,
        "component": {
            "type": "application",
            "name": "mlops-toxic-moderation",
            "version": sha,
            "description": "Multi-label toxic comment moderation service: FastAPI backend, "
            "Streamlit interfaces, Postgres, deployed to three EC2 instances.",
        },
        "properties": [
            {"name": "source", "value": "requirements/serve.txt (pip-compile --generate-hashes)"},
            {"name": "scope", "value": "the serving image only; UI, monitoring, rescorer and "
                                       "development locks are separate files"},
        ],
    }

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": metadata,
        "components": components,
    }

    digest = _first_sha256(card_text)
    data_version = _composite_data_version(card_text)

    aibom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "timestamp": stamp,
            "component": {
                "type": "application",
                "name": "mlops-toxic-moderation",
                "version": sha,
            },
        },
        "components": [
            {
                "type": "machine-learning-model",
                "name": "toxic-clf",
                "version": "v0",
                "description": "TF-IDF word 1-2 plus char_wb 3-5 into a OneVsRest calibrated "
                "logistic regression, six independent labels.",
                "hashes": [{"alg": "SHA-256", "content": digest}],
                "properties": [
                    {"name": "serialization",
                     "value": "skops, loaded against a static trusted-type allowlist"},
                    {"name": "registry",
                     "value": "https://wandb.ai/rockcyber-org/wandb-registry-model/artifacts/model/toxic-clf"},
                    {"name": "registry-entity", "value": "rockcyber-org"},
                    {"name": "run-entity", "value": "rockcyber"},
                    {"name": "promoted-stage", "value": "production"},
                    {"name": "model-card", "value": "MODEL_CARD.md"},
                    {"name": "digest-of-record",
                     "value": "the block in the git-committed MODEL_CARD.md; MODEL_DIGEST is a "
                              "cross-check the loader refuses to load past on mismatch"},
                ],
            },
            {
                "type": "data",
                "name": "jigsaw-toxic-comment-classification",
                "version": data_version,
                "description": "Jigsaw Toxic Comment Classification Challenge, English, six "
                "labels. Splits are derived from it and are not redistributed here.",
                "properties": [
                    {"name": "license",
                     "value": "per the Kaggle competition terms; not redistributed here"},
                    {"name": "composite-data-version", "value": data_version},
                    {"name": "provenance", "value": "docs/data-provenance.md"},
                ],
            },
        ],
    }

    for path, doc in ((SBOM, sbom), (AIBOM, aibom)):
        path.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    print(f"wrote sbom.json ({len(components)} components) and aibom.json for {sha[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
