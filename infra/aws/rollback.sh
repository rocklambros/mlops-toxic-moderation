#!/usr/bin/env bash
# Re-roll a previously deployed git SHA. No infrastructure change, no image rebuild, no
# GitHub run.
#
# On the day this is needed, an apply is a larger risk than whatever it would be recovering
# from -- it can force-replace instances and destroy the artifacts baked onto them -- so this
# path touches images and containers only. Nothing below shells out to a provisioning tool,
# and tests/infra/test_rollback.py asserts that by reading the code with its comments removed.
#
# usage: rollback.sh [target-sha]      (defaults to /toxic/deploy/previous-sha)
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${ROLLBACK_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PARAM_PREFIX="${TOXIC_PARAM_PREFIX:-/toxic}"
COMPONENTS="backend frontend monitoring"

die() { printf 'rollback: FATAL: %s\n' "$*" >&2; exit 1; }

# Tolerant on purpose: ParameterNotFound is a legitimate answer for previous-sha before the
# second deploy, and the caller decides what an empty value means.
param() {
  local value
  value="$(aws ssm get-parameter --region "${REGION}" --name "$1" \
    --query 'Parameter.Value' --output text 2>/dev/null)" || return 0
  [ "${value}" = "None" ] && return 0
  printf '%s' "${value}"
}

TARGET="${1:-}"
[ -n "${TARGET}" ] || TARGET="$(param "${PARAM_PREFIX}/deploy/previous-sha")"
[ -n "${TARGET}" ] \
  || die "no rollback target: ${PARAM_PREFIX}/deploy/previous-sha is unset and no SHA was given"
CURRENT="$(param "${PARAM_PREFIX}/deploy/current-sha")"
printf 'rollback: %s -> %s\n' "${CURRENT:-unknown}" "${TARGET}"

# --- the whole target has to exist BEFORE anything moves ------------------------------------
#
# Discovering a missing image halfway through leaves the fleet split across two versions,
# which is worse than either of them. `docker compose` on the instance hard-fails on an
# unresolvable image reference and takes the rest of that host's stack down with it.
#
# Repository names come from Parameter Store, never from a literal: they are
# `<project>-<component>` and `project` is a Terraform variable. The plan for this phase
# assumed `toxic-<component>` while the applied account has `toxic-mod-<component>`, and a
# literal would make every rollback refuse for a reason that has nothing to do with the images.
image_present() { # repository tag
  local digest
  digest="$(aws ecr describe-images --region "${REGION}" --repository-name "$1" \
    --image-ids "imageTag=$2" --query 'imageDetails[0].imageDigest' --output text 2>/dev/null)" \
    || return 1
  # `--output text` prints `None` rather than failing when the field is absent, and `None` is
  # a perfectly good shell string that an exit-code check would accept.
  case "${digest}" in
    sha256:*) return 0 ;;
  esac
  return 1
}

repository_of() { # component
  local name
  name="$(param "${PARAM_PREFIX}/images/$1")"
  [ -n "${name}" ] || die "${PARAM_PREFIX}/images/$1 is unset; cannot tell which repository to check"
  printf '%s' "${name}"
}

for component in ${COMPONENTS}; do
  repository="$(repository_of "${component}")"
  image_present "${repository}" "${TARGET}" \
    || die "${repository} has no image tagged ${TARGET} -- this SHA is not a deployable rollback target"
done

# The reviewer console is the frontend repository at a different tag, built from the same
# commit by a second Dockerfile. It can be absent on its own, and compose.frontend.yml names
# it unconditionally, so a missing one takes the graded user interface down with it.
FRONTEND_REPOSITORY="$(repository_of frontend)"
image_present "${FRONTEND_REPOSITORY}" "${TARGET}-reviewer" \
  || die "${FRONTEND_REPOSITORY} has no image tagged ${TARGET}-reviewer (the reviewer console)"
printf 'rollback: every image for %s is present in the registry\n' "${TARGET}"

# --- the roll -------------------------------------------------------------------------------
#
# One component per invocation, each asserted to a terminal Success by ssm_run.sh, and each
# naming its component: roll.sh cannot infer it (nothing on the running instances writes
# /etc/toxic/component, and reading the Component tag would need an ec2:DescribeTags grant no
# role has), so a payload without it dies on every host with `unknown component ''`.
for component in ${COMPONENTS}; do
  "${HERE}/ssm_run.sh" "${component}" 1 \
    bash /opt/toxic/bootstrap.sh "${TARGET}" "${component}"
done

# The gate. Every invocation reporting Success is not a rollback: a container that pulled,
# started and died in its lifespan reports Success too.
"${HERE}/verify_live.sh"

# --keep-previous, and this is the difference between a recovery command and a toggle.
# The ordinary deploy path moves current to previous, which is right when the new SHA is
# newer. Here the SHA being replaced is the one this command exists to escape, and recording
# it as the rollback target means a second `make rollback` walks straight back into it.
"${HERE}/record_deploy.sh" --keep-previous "${TARGET}"
printf 'rollback: %s is now live and recorded as current\n' "${TARGET}"
printf 'rollback: roll forward with: gh workflow run deploy.yml --ref main -f sha=<git-sha>\n'
