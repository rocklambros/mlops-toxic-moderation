"""Rewrite GitHub Actions `uses:` references from mutable tags to full commit SHAs.

Premortem H35: any action in any job can mint the OIDC token the deploy role trusts, so a tag
that its owner can move is a supply-chain hole with production blast radius. This resolves
each tag once, writes the 40-character commit SHA into the workflow, and leaves the tag behind
as a trailing comment so the pin stays auditable and upgradable by a human -- a bare 40-hex
string is a pin nobody can read, so nobody upgrades it, so it rots into an unpatched
dependency.

Local (`./...`) and `docker://` references are left alone: neither is resolved from a tag the
action's owner controls.

Usage:  python -m scripts.pin_actions [workflow ...]
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

USES_RE = re.compile(
    r"^(?P<indent>\s*(?:-\s+)?)uses:\s*"
    r"(?P<owner>[A-Za-z0-9][\w.-]*)/(?P<repo>[\w.-]+)"
    r"(?P<subpath>(?:/[\w.-]+)*)"
    r"@(?P<ref>[\w.\-/]+)"
    r"(?P<trailer>\s*#.*)?$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

Resolver = Callable[[str, str, str], str]


def resolve_with_gh(owner: str, repo: str, ref: str) -> str:
    """Resolve a tag or branch to the commit it currently points at.

    `check=True` on purpose: a failed lookup must raise rather than hand back an empty
    string, which `pin_text` would then reject as a non-sha with a confusing message.
    """
    completed = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/commits/{ref}", "--jq", ".sha"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def pin_text(text: str, resolve: Resolver) -> str:
    out: list[str] = []
    for line in text.splitlines():
        match = USES_RE.match(line)
        if match is None or SHA_RE.match(match.group("ref")):
            out.append(line)
            continue
        owner, repo, ref = match.group("owner"), match.group("repo"), match.group("ref")
        sha = resolve(owner, repo, ref)
        if not SHA_RE.match(sha):
            raise ValueError(f"resolver returned a non-sha ref for {owner}/{repo}@{ref}: {sha!r}")
        out.append(
            f"{match.group('indent')}uses: {owner}/{repo}{match.group('subpath')}@{sha}  # {ref}"
        )
    body = "\n".join(out)
    return body + "\n" if text.endswith("\n") else body


def main(argv: list[str], resolve: Resolver = resolve_with_gh) -> int:
    paths = [Path(a) for a in argv[1:]] or sorted(Path(".github/workflows").glob("*.yml"))
    changed = 0
    for path in paths:
        before = path.read_text(encoding="utf-8")
        after = pin_text(before, resolve)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed += 1
            print(f"pinned {path}")
    print(f"{changed} workflow(s) rewritten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
