#!/usr/bin/env bash
# What SendCommand actually runs.
#
# The whole payload is `bash /opt/toxic/bootstrap.sh <sha> <component>`, so the CloudTrail
# record of a deploy -- and `aws ssm list-commands`, which returns the command text in
# plaintext to anyone who can read the account -- is one readable line with no secret in it.
# Everything that needs a credential happens on this side of the wire, under the instance
# profile, in roll.sh.
#
# What the payload at s3://<deploy bucket>/deploy/<sha>/ must contain, because roll.sh reads
# all of it and `docker compose` hard-fails on any of it being absent:
#
#   roll.sh  fetch_artifacts.sh          the deploy itself
#   compose.{backend,frontend,monitoring}.yml
#   toxic-stack.service                  installed as the systemd unit
#   MODEL_CARD.md                        the digest of record, and MODEL_REGISTRY_VERSION
#
# The component is an ARGUMENT rather than something this script infers. Nothing on the
# running instances writes /etc/toxic/component -- the applied user data predates it -- and
# reading the Component tag would need an ec2:DescribeTags grant that no role has. ssm_run.sh
# already targets one component per invocation, so it is the one thing the caller certainly
# knows.
set -euo pipefail

SHA="${1:?usage: bootstrap.sh <git-sha> [component]}"
COMPONENT="${2:-}"

DESTDIR="${DESTDIR:-}"
REGION="${AWS_REGION:-us-west-2}"
PARAM_PREFIX="${TOXIC_PARAM_PREFIX:-/toxic}"
APP_DIR="${DESTDIR}/opt/toxic"
STATE_DIR="${DESTDIR}/var/lib/toxic"
# Every image in this project declares `USER appuser`, created with --uid 10001. The number is
# the contract between the Dockerfiles and this line; it is not a preference.
APP_UID="${TOXIC_APP_UID:-10001}"

die() { printf 'bootstrap: FATAL: %s\n' "$*" >&2; exit 1; }

DEPLOY_BUCKET="$(aws ssm get-parameter --region "${REGION}" \
  --name "${PARAM_PREFIX}/deploy/bucket" --query 'Parameter.Value' --output text)"
[ -n "${DEPLOY_BUCKET}" ] && [ "${DEPLOY_BUCKET}" != "None" ] \
  || die "${PARAM_PREFIX}/deploy/bucket is empty"

install -d -m 0755 "${APP_DIR}"

# Every image runs as `appuser`, uid 10001, and compose bind-mounts the host state dirs
# into the container. user_data creates them as root, so the container cannot write its
# spool: the backend dies in its FastAPI lifespan with
# "PermissionError: [Errno 13] Permission denied: '/var/lib/toxic/predictions.spool'",
# SSM still reports Success because `docker compose up -d` exited zero, and only the health
# gate notices. Observed on the first real roll.
#
# artifacts/ stays root-owned: it is mounted read-only and the fetcher runs as root.
install -d -m 0755 "${STATE_DIR}" "${STATE_DIR}/artifacts" "${STATE_DIR}/spool"
# SendCommand runs AWS-RunShellScript as root, which is the only context in which this can
# succeed and the only context a deploy ever runs in. The guard is what lets the DESTDIR
# harness exercise the directory creation above without asking a test runner to be root, and
# it changes nothing in production: `id -u` there is 0. A failure while root is fatal, because
# a silently unowned spool is precisely the failure that reports Success and serves nothing.
if [ "$(id -u)" -eq 0 ]; then
  chown "${APP_UID}:${APP_UID}" "${STATE_DIR}/spool" \
    || die "cannot give ${STATE_DIR}/spool to uid ${APP_UID}; the backend would die in its lifespan on predictions.spool while SSM reported Success"
else
  printf 'bootstrap: not root, leaving %s owned by %s (DESTDIR harness)\n' \
    "${STATE_DIR}/spool" "$(id -un)"
fi
aws s3 cp --region "${REGION}" --recursive \
  "s3://${DEPLOY_BUCKET}/deploy/${SHA}/" "${APP_DIR}/"

# `aws s3 cp --recursive` exits 0 having copied NOTHING when the prefix does not exist, so
# the exit code above says only "the API call worked". This is the check that says the
# payload arrived. Without it a deploy of a SHA whose images were never pushed re-runs the
# PREVIOUS roll.sh, reports success, and leaves the old version serving.
[ -f "${APP_DIR}/roll.sh" ] \
  || die "no deploy payload at s3://${DEPLOY_BUCKET}/deploy/${SHA}/ -- nothing was copied"

chmod 0755 "${APP_DIR}"/*.sh

exec bash "${APP_DIR}/roll.sh" "${SHA}" ${COMPONENT:+"${COMPONENT}"}
