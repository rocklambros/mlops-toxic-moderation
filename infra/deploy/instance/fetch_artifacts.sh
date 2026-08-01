#!/usr/bin/env bash
# Deploy-time model artifact fetch. Runs ON the instance, under the instance profile.
#
# provenance: the expected digest is read from the git-committed MODEL_CARD.md and from
# nowhere else. The environment cannot supply it, because "the thing that gave me the
# artifact also told me what it should hash to" is not provenance. The lookup is keyed on
# the artifact FILENAME rather than on position -- `grep -oE '[0-9a-f]{64}' | head -1`
# silently becomes the corpus digest or the split digest the moment a section of the card is
# reordered, and the fetcher would then be verifying a value the serving loader never checks.
#
# Primary source is the Weights & Biases registry. A registry outage at bring-up would
# otherwise turn the fail-closed loader into a demo outage, so an S3 mirror backs it. The
# mirror key IS the digest, so the mirror is not a second trust root: an object that does
# not hash to the card's value cannot be found under the name this script asks for.
#
# OPERATIONAL NOTE. The AL2023 hosts run Docker and nothing else -- there is no `wandb` CLI
# and no pip on them -- so in this deployment the registry branch below reports "command not
# found" and the digest-keyed mirror is the path that actually delivers. That is by design,
# not by accident: the mirror must be seeded before the first roll (infra/deploy/README or
# the deploy runbook carries the one-time `aws s3 cp`). The registry branch is kept because
# it costs one `if`, and because the same script is what an operator runs by hand on a box
# that does have the CLI.
set -euo pipefail

MODEL_CARD_PATH="${MODEL_CARD_PATH:-/opt/toxic/MODEL_CARD.md}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/var/lib/toxic/artifacts}"
ARTIFACT_NAME="${ARTIFACT_NAME:-toxic-clf.skops}"
WANDB_ARTIFACT="${WANDB_ARTIFACT:?WANDB_ARTIFACT must be set}"
DEPLOY_BUCKET="${DEPLOY_BUCKET:?DEPLOY_BUCKET must be set}"
REGION="${AWS_REGION:-us-west-2}"
# Secrets Manager id of the registry credential, supplied by roll.sh from SSM so that no
# secret NAME is hardcoded in a shell script where it can drift from Terraform. Empty means
# "no registry credential available", which is the normal case on these instances.
WANDB_SECRET_ID="${WANDB_SECRET_ID:-}"

log() { printf 'fetch_artifacts: %s\n' "$*"; }
die() { printf 'fetch_artifacts: FATAL: %s\n' "$*" >&2; exit 1; }

# The digest of record, looked up by filename in the committed card.
EXPECTED="$(grep -F "\`${ARTIFACT_NAME}\`" "${MODEL_CARD_PATH}" 2>/dev/null \
  | grep -oE '[0-9a-f]{64}' | head -1 || true)"
[ -n "${EXPECTED}" ] || die "no digest of record for ${ARTIFACT_NAME} in ${MODEL_CARD_PATH}"
log "digest of record for ${ARTIFACT_NAME} is ${EXPECTED} (from ${MODEL_CARD_PATH})"

STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT
TARGET="${STAGING}/${ARTIFACT_NAME}"

verify() {
  printf '%s  %s\n' "${EXPECTED}" "$1" | sha256sum -c - >/dev/null 2>&1
}

install_verified() {
  mkdir -p "${ARTIFACT_DIR}"
  # `install` writes through a fresh inode and sets the mode in one step, so a reader never
  # observes a half-written file at the final path.
  install -m 0444 "${TARGET}" "${ARTIFACT_DIR}/${ARTIFACT_NAME}"
  log "installed ${ARTIFACT_DIR}/${ARTIFACT_NAME}"
}

# --- Primary: the registry. The credential is read under the instance profile at the moment
# --- of use and is never an argument to anything that gets logged.
if [ -n "${WANDB_SECRET_ID}" ]; then
  if WANDB_API_KEY="$(aws secretsmanager get-secret-value --region "${REGION}" \
        --secret-id "${WANDB_SECRET_ID}" --query SecretString --output text 2>/dev/null)"; then
    export WANDB_API_KEY
  fi
fi
if wandb artifact get "${WANDB_ARTIFACT}" --root "${STAGING}" >/dev/null 2>&1; then
  unset WANDB_API_KEY || true
  [ -f "${TARGET}" ] || die "registry returned no ${ARTIFACT_NAME}"
  if verify "${TARGET}"; then
    log "registry copy verified"
    install_verified
    exit 0
  fi
  # Deliberately fatal. A digest mismatch is a security event, not a transport failure:
  # falling back here would hand an attacker who can publish to the registry a free retry
  # against whichever source is easier to poison, and the operator would see a green deploy.
  die "digest mismatch on the registry copy of ${ARTIFACT_NAME} -- refusing to install and refusing to fall back"
fi
unset WANDB_API_KEY || true

# --- Fallback: the digest-keyed mirror.
log "registry fetch failed; falling back to the mirror"
MIRROR_KEY="artifacts/${EXPECTED}/${ARTIFACT_NAME}"
aws s3 cp --region "${REGION}" "s3://${DEPLOY_BUCKET}/${MIRROR_KEY}" "${TARGET}" \
  || die "mirror fetch failed: s3://${DEPLOY_BUCKET}/${MIRROR_KEY}"
verify "${TARGET}" || die "digest mismatch on the mirror copy of ${ARTIFACT_NAME}"
log "mirror copy verified"
install_verified
