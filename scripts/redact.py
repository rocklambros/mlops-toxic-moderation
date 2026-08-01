"""Redact the AWS account id, the Elastic IPs and credential values from anything public.

The repository is public, GitHub Actions logs on a public repository are world-readable, and
the submission checklist requires that no account id appear in any screenshot or evidence
file. This module is the single place that knows what has to disappear.

Three classes of identifier, and they are not interchangeable.

* **The account id.** Twelve digits. It appears inside ARNs and ECR URIs far more often than
  it appears bare, so the rule is anchored on digit boundaries rather than on whitespace.
* **The Elastic IPs.** The public address of the graded system. Only globally routable
  addresses are masked: the loopback bind of the reviewer console, the VPC private ranges,
  the instance metadata service address and the wildcard bind are all committed
  configuration, and a redactor that rewrites them corrupts the artifact it was asked to
  publish.
* **Credential values.** Two kinds. The vendor shapes -- an access key id, a personal access
  token -- are recognisable on their own. This project's own credentials (the demo API key,
  the reviewer shared secret, the fingerprint key, the database password) have no shape at
  all; the only thing that marks them is an assignment, so an assignment is what is read.
  A *reference* to a credential is not a credential: `$DEMO_API_KEY` and `${{ secrets.X }}`
  are how the documentation is supposed to talk about them and are deliberately preserved.

Two properties are easy to get wrong and are pinned by tests rather than by comment.

1. **Credential rules run before the account-id rule.** A token carrying twelve consecutive
   digits would otherwise be split by the account-id substitution, after which the
   credential rule no longer matches and the remainder of the token is published.
2. **A forty-hex string is only a credential in credential context.** A registry API key and
   a git commit sha are the same shape, and this project publishes commit shas on purpose --
   the model card records the training commit, the workflow pins every action to a full sha.
   A bare-shape rule masks the provenance record it exists to protect, and a scanner that
   reports committed evidence as a leak is a scanner somebody switches off.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# A 12-digit run that is not part of a longer HEX run. Anchored this way it catches the id
# inside an ARN ("iam::<id>:role") and inside an ECR URI, which is where it actually
# appears, without touching timestamps, latencies, or ISO dates.
#
# The boundary is hex, not decimal, and that is the whole point. The artifact digest of
# record in MODEL_CARD.md is 64 hex characters and carries a run of exactly twelve digits
# four characters in. A decimal boundary masks the middle of it -- corrupting the value the
# fail-closed loader checks against, and reporting the trust root as a leak every time
# `make submission-check` reads that file. An account id is never adjacent to a hex letter:
# it follows "::", a "/", a quote or whitespace, and is followed by ":" or ".".
ACCOUNT_ID = re.compile(r"(?<![0-9A-Fa-f])[0-9]{12}(?![0-9A-Fa-f])")

# A dotted quad that is not part of a longer dotted run, so a four-component version string
# is still a candidate but "1.2.3.4.5" is not. Whether it is masked is decided by
# _is_publishable_address, not by the pattern.
IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")

# Addresses that are configuration rather than location. Every one of these is committed
# somewhere in this repository and must survive redaction unchanged.
ADDRESSES_THAT_STAY = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",  # the wildcard bind in every compose port mapping
        "10.0.0.0/8",  # the VPC
        "127.0.0.0/8",  # the reviewer console loopback bind
        "169.254.0.0/16",  # the instance metadata service
        "172.16.0.0/12",
        "192.168.0.0/16",
        "224.0.0.0/4",  # multicast
        "255.255.255.255/32",
    )
)

# Names whose value is a credential wherever one is assigned to the other. Deliberately a
# suffix/prefix match: DEMO_API_KEY, REVIEWER_SHARED_SECRET, POSTGRES_PASSWORD and
# AWS_SESSION_TOKEN all land here without being enumerated.
# `[_-]KEY` rather than a bare `KEY`, so SUBMITTER_FP_KEY and PRIVATE_KEY are credentials
# and a YAML `key:` or the word "monkey" is not.
_CREDENTIAL_NAME = (
    r"[A-Za-z0-9_.-]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|[_-]KEY"
    r"|CREDENTIAL)[A-Za-z0-9_.-]*"
)
# A value is a literal, not a reference: it may not open with a shell expansion, a
# templating brace, or an angle-bracket placeholder, because those are how the
# documentation is supposed to name a credential it must not print.
_LITERAL_VALUE = r"(?![$<{])[^\s\"'`]{12,}"

# `*_KEY` is ambiguous in a way that `*_SECRET` and `*_TOKEN` are not: half the uses in this
# repository name a location rather than a credential. `make db-restore S3_KEY=db/...` is a
# documented operator command and redacting it makes the README's own instruction unusable.
# Kept as data, and exercised by a test, because an over-redacting scanner corrupts the
# artifact it was asked to publish -- which is the same failure as leaking, pointed the
# other way.
KEY_NAMES_THAT_LOCATE_RATHER_THAN_AUTHENTICATE = frozenset(
    {
        "s3_key",
        "object_key",
        "cache_key",
        "partition_key",
        "sort_key",
        "primary_key",
        "foreign_key",
        "idempotency_key",
        "routing_key",
        "dedup_key",
    }
)


def _mask_assigned_secret(match: re.Match[str]) -> str:
    if match.group("name").lower().lstrip("-_.") in KEY_NAMES_THAT_LOCATE_RATHER_THAN_AUTHENTICATE:
        return match.group(0)
    quote = match.group("quote")
    return f"{match.group('lead')}{quote}<redacted>{quote}"

Mask = str | Callable[[re.Match[str]], str]

SECRET_SHAPES: tuple[tuple[str, re.Pattern[str], Mask], ...] = (
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<aws-access-key-id>"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), "<github-token>"),
    (
        # Anchored on the assignment, the flag or the netrc field that says "credential".
        # See the module docstring for why the bare forty-hex shape is not enough.
        "wandb-key",
        re.compile(
            r"(?i)(?P<lead>(?:wandb[_-]?(?:api[_-]?)?key|api[_-]?key|password|machine\s+"
            r"api\.wandb\.ai.{0,80}?password)\s*[:=]?\s*[\"']?|wandb\s+login\s+)"
            r"(?P<value>[0-9a-f]{40})\b",
            re.S,
        ),
        r"\g<lead><wandb-key>",
    ),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"), "Bearer <token>"),
    (
        # A password inside a URL's userinfo. The host is diagnostic and is kept.
        "database-url-password",
        re.compile(r"(?P<lead>://[^\s:/@]+:)(?P<value>[^\s/@]+)(?P<tail>@)"),
        r"\g<lead><redacted>\g<tail>",
    ),
    (
        "assigned-secret",
        re.compile(
            rf"(?i)(?P<lead>\b(?P<name>{_CREDENTIAL_NAME})\s*[:=]\s*)(?P<quote>[\"']?)"
            rf"(?P<value>{_LITERAL_VALUE})(?P=quote)"
        ),
        _mask_assigned_secret,
    ),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line_number: int
    kind: str
    line: str


def _is_publishable_address(text: str) -> bool:
    """True when this dotted quad is configuration rather than the location of the stack."""
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        return True  # not an address at all, e.g. a four-component version string
    return any(address in network for network in ADDRESSES_THAT_STAY)


def _mask_address(match: re.Match[str]) -> str:
    found = match.group(0)
    return found if _is_publishable_address(found) else "<elastic-ip>"


def redact(text: str) -> str:
    """Return `text` with every identifier that must not be published masked.

    Credential shapes run FIRST. See the module docstring, property 1.
    """
    out = text
    for _name, pattern, mask in SECRET_SHAPES:
        out = pattern.sub(mask, out)
    out = IPV4.sub(_mask_address, out)
    return ACCOUNT_ID.sub("<account-id>", out)


def _kinds_in(line: str) -> list[str]:
    """Which rules would rewrite this line.

    A rule counts as a finding when applying it CHANGES the line, not when its pattern
    merely matches. The two are not the same for any rule that decides with a predicate --
    `S3_KEY=` matches the credential-assignment pattern and is deliberately left alone --
    and a scanner that reports what the redactor would not touch is a gate that goes red on
    a file nobody can make clean. Defined this way the two cannot drift.
    """
    kinds = ["account-id"] if ACCOUNT_ID.search(line) else []
    if IPV4.sub(_mask_address, line) != line:
        kinds.append("elastic-ip")
    kinds += [name for name, pattern, mask in SECRET_SHAPES if pattern.sub(mask, line) != line]
    return kinds


def _files_under(path: Path) -> list[Path]:
    """Every file this path names. A directory names all of its files, recursively.

    `make submission-check` passes `docs/evidence`, which is a directory. Skipping it would
    report the evidence tree clean without having opened one file in it.
    """
    if path.is_dir():
        return sorted(child for child in path.rglob("*") if child.is_file())
    return [path]


def scan(paths: list[Path]) -> list[Finding]:
    """Report every line in every path that would leak an identifier if published."""
    findings: list[Finding] = []
    for target in paths:
        for path in _files_under(target):
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError, PermissionError):
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                for kind in _kinds_in(line):
                    findings.append(Finding(path=path, line_number=number, kind=kind, line=line))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Redact identifiers from public artifacts.")
    parser.add_argument("--scan", action="store_true", help="report instead of rewriting")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)

    if args.scan:
        findings = scan(args.paths)
        for finding in findings:
            # The reported line is itself redacted: a scanner that echoes what it found into
            # a world-readable Actions log has published the identifier it exists to catch.
            print(
                f"{finding.path}:{finding.line_number}: {finding.kind}: "
                f"{redact(finding.line).strip()}"
            )
        return 1 if findings else 0

    if args.paths:
        for target in args.paths:
            for path in _files_under(target):
                path.write_text(redact(path.read_text(encoding="utf-8")), encoding="utf-8")
        return 0

    sys.stdout.write(redact(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
