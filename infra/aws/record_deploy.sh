#!/usr/bin/env bash
# Record what is deployed, so rollback.sh can find what to go back to without reading
# Terraform state, calling the GitHub API, or relying on anyone's memory.
#
# The write order is the entire point. previous-sha is written FIRST, so a crash between the
# two writes loses the new pointer rather than the old one -- and losing the new pointer is
# survivable, while losing the rollback target is exactly the failure this exists to prevent.
#
# Nothing here is written under /toxic/boot/, which is the only prefix the three instance
# roles can write. An instance that could rewrite /toxic/deploy/current-sha could lie about
# what it is running, and the rollback would believe it.
#
# usage: record_deploy.sh <git-sha>
set -euo pipefail

NEW_SHA="${1:?usage: record_deploy.sh <git-sha>}"
# `${1:?}` fires on an unset argument but NOT on an empty one, and an empty string here is the
# realistic failure: a workflow whose IMAGE_TAG expression did not resolve calls this with "".
# Blanking the pointer after a green health gate is the worst possible moment to lose it.
[ -n "${NEW_SHA}" ] || { printf 'usage: record_deploy.sh <git-sha> (got an empty sha)\n' >&2; exit 2; }

REGION="${AWS_REGION:-us-west-2}"
PARAM_PREFIX="${TOXIC_PARAM_PREFIX:-/toxic}"

# `|| true`, because ParameterNotFound is the ordinary answer on the first deploy and must not
# abort under `set -e`. Every other failure -- AccessDenied, a throttle -- also lands here as
# an empty value, and the consequence is the same and is safe: previous-sha is not written, so
# the existing rollback target is left alone rather than overwritten with a guess.
get() {
  aws ssm get-parameter --region "${REGION}" --name "$1" \
    --query 'Parameter.Value' --output text 2>/dev/null || true
}
put() {
  aws ssm put-parameter --region "${REGION}" --name "$1" --type String --overwrite --value "$2"
}

CURRENT="$(get "${PARAM_PREFIX}/deploy/current-sha")"

# The `!=` guard is not an optimisation. Re-running a deploy of the SHA that is already
# current would otherwise set previous == current, and the rollback would re-roll the version
# it is trying to escape.
if [ -n "${CURRENT}" ] && [ "${CURRENT}" != "None" ] && [ "${CURRENT}" != "${NEW_SHA}" ]; then
  put "${PARAM_PREFIX}/deploy/previous-sha" "${CURRENT}"
  printf 'record_deploy: previous-sha=%s\n' "${CURRENT}"
fi

put "${PARAM_PREFIX}/deploy/current-sha" "${NEW_SHA}"
printf 'record_deploy: current-sha=%s\n' "${NEW_SHA}"
