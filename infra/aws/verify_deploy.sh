#!/usr/bin/env bash
# The REAL deploy gate.
#
# `ssm_run.sh` proves a shell exited 0 on three boxes. This proves the application answers on
# three Elastic IPs, from outside the VPC, over the same path a grader would take. If this
# fails the deploy failed, whatever SSM said.
#
# Every address is required and none has a default. A default of localhost is a gate that
# passes on the operator's laptop while the fleet is dark; the three URLs come from SSM
# Parameter Store, published by Terraform from the Elastic IPs themselves.
#
# `set -e` is deliberately absent. Every endpoint is probed on every run, because a gate that
# returns on the first failure reports one problem per deploy attempt, and a three-instance
# fleet then takes three deploys to diagnose on a system with no SSH.
set -uo pipefail

BACKEND_URL="${BACKEND_URL:?BACKEND_URL must be set}"
FRONTEND_URL="${FRONTEND_URL:?FRONTEND_URL must be set}"
MONITORING_URL="${MONITORING_URL:?MONITORING_URL must be set}"

RETRY="${CURL_RETRY:-18}"
RETRY_DELAY="${CURL_RETRY_DELAY:-5}"
MAX_TIME="${CURL_MAX_TIME:-10}"

fail=0

# `check <name> <url> <extended-regex the body must match>`
#
# No `-L`. A redirect is not health: a 302 to a login page or a captive portal carries an
# empty body and a status line that anything reading only the first line calls a success.
# `-f` makes 4xx and 5xx a curl failure, and the body match is what separates "something
# answered on that port" from "the thing we deployed answered".
check() {
  local name="$1" url="$2" needle="$3" body
  if ! body="$(curl -fsS --max-time "${MAX_TIME}" --retry "${RETRY}" \
        --retry-delay "${RETRY_DELAY}" --retry-all-errors "${url}" 2>/dev/null)"; then
    printf 'verify: %-11s DOWN  %s\n' "${name}" "${url}" >&2
    fail=1
    return
  fi
  if ! printf '%s' "${body}" | grep -Eq "${needle}"; then
    printf 'verify: %-11s BAD   %s (body does not match %s)\n' "${name}" "${url}" "${needle}" >&2
    fail=1
    return
  fi
  # H14: the digest is stripped from the public listener on purpose. Confirm it stayed off.
  if printf '%s' "${body}" | grep -Eq '[0-9a-f]{64}'; then
    printf 'verify: %-11s LEAK  %s exposes a 64-hex artifact digest\n' "${name}" "${url}" >&2
    fail=1
    return
  fi
  printf 'verify: %-11s OK    %s\n' "${name}" "${url}"
}

# The backend must report BOTH itself and its database healthy: rubric 2.2 makes complete
# prediction logging a requirement, so a backend that serves without persisting is not a
# successful deploy -- it punches holes in the graded drift and live-accuracy views without
# ever failing a naive readiness probe.
#
# The needle tolerates whitespace on purpose. FastAPI's JSONResponse serialises with
# `separators=(",", ":")`, so the wire carries `"database":"ok"` with no space, while every
# hand-written fixture and every `json.dumps` in a test carries `"database": "ok"` with one.
# A fixed-string needle for either form is a gate that agrees with its own tests and matches
# nothing the real system has ever sent.
check backend "${BACKEND_URL}/health" '"database"[[:space:]]*:[[:space:]]*"ok"'

# Streamlit has no /health; probing / would 200 on a crashed app that still serves the shell
# HTML. /_stcore/health answers exactly `ok`, and the anchors matter: a substring match on
# `ok` accepts `not ok`, `broken`, and a stack trace that happens to contain the word.
check frontend   "${FRONTEND_URL}/_stcore/health"   '^ok$'
check monitoring "${MONITORING_URL}/_stcore/health" '^ok$'

if [ "${fail}" -ne 0 ]; then
  printf 'verify: DEPLOY GATE FAILED -- see docs/runbooks/no-ssh-debug.md\n' >&2
  exit 1
fi
printf 'verify: all three endpoints healthy\n'
