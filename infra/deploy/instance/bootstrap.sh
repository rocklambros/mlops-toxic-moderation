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

die() { printf 'bootstrap: FATAL: %s\n' "$*" >&2; exit 1; }

DEPLOY_BUCKET="$(aws ssm get-parameter --region "${REGION}" \
  --name "${PARAM_PREFIX}/deploy/bucket" --query 'Parameter.Value' --output text)"
[ -n "${DEPLOY_BUCKET}" ] && [ "${DEPLOY_BUCKET}" != "None" ] \
  || die "${PARAM_PREFIX}/deploy/bucket is empty"

install -d -m 0755 "${APP_DIR}"
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
