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

The table is empty, and that is the intended steady state: every advisory raised so far was
answered by upgrading the pin in the relevant `.in` and recompiling the lock.

| Vulnerability | Package | Reason it is not exploitable here | Expires |
|---|---|---|---|
