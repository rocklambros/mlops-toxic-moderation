#!/usr/bin/env bash
# Audit every dependency surface this project actually ships.
#
# ECR scan-on-push is BASIC scanning: an OS-package CVE match that cannot read Python
# distributions and does not fail a build (premortem H35). This is the scan that covers the
# dependency set and blocks the merge.
#
# The list below is every HASHED lock in requirements/, which is the closure that actually
# installs anywhere: the `.in` and `.txt` inputs are human-edited requests, and auditing them
# would audit the direct dependencies while missing the transitive ones that carry most
# advisories. tests/unit/test_vuln_ledger.py discovers the locks independently and fails if
# one of them is absent from this list, so a seventh surface cannot arrive unaudited.
set -euo pipefail

LOCKS=(
  requirements/pip-tools.txt
  requirements/dev.lock
  requirements/serve.txt
  requirements/ui.txt
  requirements/monitor.txt
  requirements/rescorer.txt
  requirements/security.txt
)

# NOT `mapfile -t IGNORED < <(python -m scripts.vuln_ledger)`. A process substitution's exit
# status is invisible to both `set -e` and `pipefail`, so an expired suppression would print
# its complaint to stderr, return 1, and this script would carry on with an empty ignore list
# -- an expiry rule that fails nothing. Command substitution inside a tested condition is what
# makes the ledger's exit status reach the build.
if ! LEDGER_OUTPUT="$(python -m scripts.vuln_ledger)"; then
  echo "the suppression ledger is unusable or holds an expired row; refusing to audit" >&2
  exit 1
fi

IGNORE_ARGS=()
while IFS= read -r vuln; do
  [ -n "${vuln}" ] || continue
  echo "suppressed by the reviewed ledger: ${vuln}"
  IGNORE_ARGS+=(--ignore-vuln "${vuln}")
done <<< "${LEDGER_OUTPUT}"

status=0
for lock in "${LOCKS[@]}"; do
  if [ ! -f "${lock}" ]; then
    echo "::error::${lock} is listed for audit and does not exist" >&2
    status=1
    continue
  fi
  echo "::group::pip-audit ${lock}"
  # --no-deps: the lock IS the full resolved closure, so no network resolution is needed and
  # nothing outside the audited set can be pulled in.
  # --strict: fail if any listed distribution could not be audited, rather than skipping it.
  if ! pip-audit --strict --no-deps --progress-spinner=off -r "${lock}" "${IGNORE_ARGS[@]}"; then
    status=1
  fi
  echo "::endgroup::"
done

exit "${status}"
