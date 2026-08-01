# pip-audit suppressions

`scripts/run_pip_audit.sh` reads this table and passes each active row to `pip-audit` as
`--ignore-vuln`. `tests/unit/test_vuln_ledger.py` fails the build if a row has no reason, has
no parseable expiry date, or has expired, and `python -m scripts.vuln_ledger` exits non-zero on
a lapsed row so the audit stops rather than continuing with an empty ignore list.

Rules, in the order they matter:

1. A suppression is a decision about **this** project, not about the advisory. The reason must
   say why the vulnerable code path is unreachable here, not that a fix is unavailable.
2. Expiry is at most 30 days out. On day 20 of a 19-day project, an expiry beyond the due date
   is a decision nobody will ever revisit.
3. Upgrading is always preferred. Add a row only when the fixed version is not installable —
   for example when it requires a Python this project does not run.

Every advisory raised so far was answered by upgrading the pin in the relevant `.in` and
recompiling the lock -- fifty-eight of them across pip, setuptools, protobuf, pillow and
starlette, none suppressed. The three rows below are the residue: `semgrep==1.172.0`, the
newest release, pins `mcp==1.23.3` **exactly**, so no version of semgrep takes the fix and
there is nothing to upgrade to.

| Vulnerability | Package | Reason it is not exploitable here | Expires |
|---|---|---|---|
| PYSEC-2026-3481 | mcp | `mcp` is reached only through `semgrep mcp`, the Model Context Protocol server. This project invokes `semgrep scan` from `ci.yml` and `make scan`; the server transport is never started, so the vulnerable path has no caller here. It is also confined to `requirements/security.txt`, which builds a throwaway `.venv-scan` and is installed into no image that ships. | 2026-08-15 |
| PYSEC-2026-3482 | mcp | Same package and same reachability argument as PYSEC-2026-3481: the scan entrypoint never constructs an MCP server. | 2026-08-15 |
| PYSEC-2026-3483 | mcp | Same package and same reachability argument as PYSEC-2026-3481. Fixed in mcp 1.28.1, which `semgrep==1.172.0` cannot accept. | 2026-08-15 |
