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
# Three artifacts, not one. `thresholds.json` IS the decision boundary -- an unverified copy
# is a silent policy change that no metric flags -- and `baseline_flag_rates.json` is the
# reference the drift panel measures production against. Both are mounted read-only into a
# container that fails closed without them (monitoring/baseline.py raises BaselineMissingError,
# and the backend lifespan's load_thresholds raises), so both are fetched and verified exactly
# the way the coefficients are. The monitoring instance overrides this with the two sidecars
# alone: it never scores anything, so it does not need the model.
ARTIFACT_NAMES="${ARTIFACT_NAMES:-toxic-clf.skops thresholds.json baseline_flag_rates.json}"
WANDB_ARTIFACT="${WANDB_ARTIFACT:?WANDB_ARTIFACT must be set}"
DEPLOY_BUCKET="${DEPLOY_BUCKET:?DEPLOY_BUCKET must be set}"
REGION="${AWS_REGION:-us-west-2}"
# Secrets Manager id of the registry credential, supplied by roll.sh from SSM so that no
# secret NAME is hardcoded in a shell script where it can drift from Terraform. Empty means
# "no registry credential available", which is the normal case on these instances.
WANDB_SECRET_ID="${WANDB_SECRET_ID:-}"

log() { printf 'fetch_artifacts: %s\n' "$*"; }
die() { printf 'fetch_artifacts: FATAL: %s\n' "$*" >&2; exit 1; }

# name -> digest of record, parsed from the git-committed card BY NAME and never by position.
# `grep -oE '[0-9a-f]{64}' | head -1` gives every artifact the model's digest, and the two
# sidecars then fail to verify against bytes that were perfectly correct.
declare -A DIGEST_OF
for name in ${ARTIFACT_NAMES}; do
  digest="$(grep -F "\`${name}\`" "${MODEL_CARD_PATH}" 2>/dev/null \
    | grep -oE '[0-9a-f]{64}' | head -1 || true)"
  [ -n "${digest}" ] || die "no digest of record for ${name} in ${MODEL_CARD_PATH}"
  DIGEST_OF["${name}"]="${digest}"
  log "digest of record for ${name} is ${digest} (from ${MODEL_CARD_PATH})"
done

STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

verify() { # path expected-digest
  printf '%s  %s\n' "$2" "$1" | sha256sum -c - >/dev/null 2>&1
}

# --- Stage 1: the registry, best effort. The credential is read under the instance profile at
# --- the moment of use and is never an argument to anything that gets logged.
#
# The registry artifact carries the model and NOTHING else: model/tracking.py's
# log_model_artifact calls `artifact.add_file(model_path)` exactly once. The two sidecars
# therefore come from the mirror on every real deploy, which is a designed path rather than a
# degraded one.
if [ -n "${WANDB_SECRET_ID}" ]; then
  if WANDB_API_KEY="$(aws secretsmanager get-secret-value --region "${REGION}" \
        --secret-id "${WANDB_SECRET_ID}" --query SecretString --output text 2>/dev/null)"; then
    export WANDB_API_KEY
  fi
fi
if wandb artifact get "${WANDB_ARTIFACT}" --root "${STAGING}" >/dev/null 2>&1; then
  log "the registry answered"
else
  log "registry fetch failed; falling back to the mirror"
fi
unset WANDB_API_KEY || true

# --- Stage 2: verify everything the registry supplied, mirror everything it did not.
#
# NOTHING is installed in this loop. A mismatch on the third artifact must not leave the
# first two in place: a half-updated /artifacts is a backend scoring with this deploy's
# coefficients at the previous deploy's thresholds, which is a policy nobody chose and no
# health check can see.
for name in ${ARTIFACT_NAMES}; do
  expected="${DIGEST_OF[${name}]}"
  if [ -f "${STAGING}/${name}" ]; then
    verify "${STAGING}/${name}" "${expected}" \
      || die "digest mismatch on the registry copy of ${name} -- refusing to install and refusing to fall back"
    log "registry copy of ${name} verified"
    continue
  fi
  log "the registry did not supply ${name}; falling back to the mirror"
  # The mirror key IS the digest, so the mirror is not a second trust root: an object that
  # does not hash to the card's value cannot be found under the name asked for here.
  key="artifacts/${expected}/${name}"
  aws s3 cp --region "${REGION}" "s3://${DEPLOY_BUCKET}/${key}" "${STAGING}/${name}" \
    || die "mirror fetch failed for ${name}: s3://${DEPLOY_BUCKET}/${key}"
  verify "${STAGING}/${name}" "${expected}" || die "digest mismatch on the mirror copy of ${name}"
  log "mirror copy of ${name} verified"
done

# --- Stage 3: install, only now that every artifact in the set has verified.
mkdir -p "${ARTIFACT_DIR}"
for name in ${ARTIFACT_NAMES}; do
  # `install` writes through a fresh inode and sets the mode in one step, so a reader never
  # observes a half-written file at the final path.
  install -m 0444 "${STAGING}/${name}" "${ARTIFACT_DIR}/${name}"
  log "installed ${ARTIFACT_DIR}/${name}"
done
