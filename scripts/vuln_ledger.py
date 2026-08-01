"""Parse the pip-audit suppression ledger.

Premortem H35 asks for a dependency scan that fails the build. The failure mode of such a scan
is not that it misses something; it is that a growing ignore list quietly turns it off. Every
row here needs a reason a human wrote and a date after which the decision must be re-made.

Run as a module (`python -m scripts.vuln_ledger`) it prints the ids that are still active, one
per line, and exits 1 -- printing nothing to stdout -- if any row has lapsed.
`scripts/run_pip_audit.sh` is the caller, and it stops on that exit status rather than
proceeding with an empty ignore list.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VULN_ID_RE = re.compile(r"^(GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}|PYSEC-\d{4}-\d+)$")
DEFAULT_LEDGER = Path("docs/security/pip-audit-ignores.md")


@dataclass(frozen=True)
class Suppression:
    vuln_id: str
    package: str
    reason: str
    expires: dt.date


def parse_ledger(text: str) -> list[Suppression]:
    rows: list[Suppression] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4:
            continue
        vuln_id, package, reason, expires = cells
        if not VULN_ID_RE.match(vuln_id):
            continue  # header row, separator row, or prose
        if not reason:
            raise ValueError(f"{vuln_id} has no reason; a suppression without one is a habit")
        if not package:
            raise ValueError(f"{vuln_id} names no package")
        try:
            parsed = dt.date.fromisoformat(expires)
        except ValueError as exc:
            raise ValueError(
                f"{vuln_id} has an unusable expiry {expires!r}; use YYYY-MM-DD"
            ) from exc
        rows.append(Suppression(vuln_id, package, reason, parsed))
    return rows


def expired(ledger: list[Suppression], today: dt.date) -> list[Suppression]:
    return [row for row in ledger if row.expires < today]


def active_ids(ledger: list[Suppression], today: dt.date) -> list[str]:
    return [row.vuln_id for row in ledger if row.expires >= today]


def main(argv: list[str] | None = None) -> int:
    """Print the active ignore ids, one per line, for scripts/run_pip_audit.sh."""
    arguments = sys.argv[1:] if argv is None else argv[1:]
    path = Path(arguments[0]) if arguments else DEFAULT_LEDGER
    ledger = parse_ledger(path.read_text(encoding="utf-8"))
    today = dt.date.today()
    stale = expired(ledger, today)
    if stale:
        for row in stale:
            print(
                f"expired suppression {row.vuln_id} ({row.package}) lapsed {row.expires}; "
                "re-decide it or remove the row",
                file=sys.stderr,
            )
        return 1
    for vuln_id in active_ids(ledger, today):
        print(vuln_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
