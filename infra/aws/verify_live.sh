#!/usr/bin/env bash
# Resolve the three published endpoints from Parameter Store, then run the gate against them.
#
# `verify_deploy.sh` deliberately has no defaults: every address is required and a default of
# localhost is a gate that passes on the operator's laptop while the fleet is dark. That makes
# it awkward to call by hand, and the awkward version was being retyped in four places -- the
# deploy workflow, the rollback, `aws-up`, and `make deploy-verify` -- each of which is a
# chance to get one parameter name wrong and probe two live endpoints and one typo.
#
# So this is the ONE place that knows the three parameter names. It resolves them, refuses if
# any is missing or empty, and hands off. It adds no policy of its own: the gate is still
# verify_deploy.sh, and this script cannot make a red one green.
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${VERIFY_LIVE_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PARAM_PREFIX="${TOXIC_PARAM_PREFIX:-/toxic}"

die() { printf 'verify_live: FATAL: %s\n' "$*" >&2; exit 1; }

# `local value` on its own line, not `local value="$(...)"`: the second form is a `local`
# builtin that always succeeds, so the command substitution's exit status is discarded and a
# failed lookup becomes an empty string that flows on.
param() { # parameter-name
  local value
  value="$(aws ssm get-parameter --region "${REGION}" --name "$1" \
    --query 'Parameter.Value' --output text 2>/dev/null)" \
    || die "cannot read $1 -- Terraform publishes it; has this stack been applied?"
  # `--output text` prints the string `None` for an absent field rather than failing, and an
  # empty URL reaches curl as no argument at all, which curl reports as a usage error that
  # reads nothing like "the endpoint is not published".
  [ -n "${value}" ] && [ "${value}" != "None" ] || die "$1 is empty; there is nothing to probe"
  printf '%s' "${value}"
}

BACKEND_URL="$(param "${PARAM_PREFIX}/endpoints/backend")"
FRONTEND_URL="$(param "${PARAM_PREFIX}/endpoints/frontend")"
MONITORING_URL="$(param "${PARAM_PREFIX}/endpoints/monitoring")"
export BACKEND_URL FRONTEND_URL MONITORING_URL

exec bash "${HERE}/verify_deploy.sh"
