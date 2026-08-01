#!/usr/bin/env bash
# Runs on the instance, under the instance profile.
#
# Reads every credential from Secrets Manager at the moment of use, writes them to 0600 env
# files that only root can read, and restarts the systemd unit. No secret is ever an
# argument, an echo, or a trace. There is deliberately no `set -x`: it would print every
# expansion, including the four below.
#
# THIS SCRIPT ASSUMES NOTHING ABOUT THE HOST. The three running instances were created by a
# bootstrap that installed `toxic-mod.service` against /opt/toxic-mod/docker-compose.yml, and
# compute.tf carries `lifecycle { ignore_changes = [user_data] }` with
# `user_data_replace_on_change = false` -- so Terraform cannot see the new template and those
# boxes will never receive it. Replacing them is not an option either: the fleet is up for an
# unknown grading window and must not go dark. So this script lays down /opt/toxic,
# /etc/toxic, /var/lib/toxic, the ECR login helper and the systemd unit ITSELF, is safe to
# run on a host where all of them already exist, and stands the superseded unit down before
# starting its own. Two units managing containers on one Docker daemon is a race whose loser
# is whichever one ran `compose down` last.
#
# DESTDIR is the ordinary packaging convention and defaults to empty, so a production run
# writes to the real filesystem. tests/infra/test_roll_secrets.py sets it to a temporary
# directory, which is what makes it possible to RUN this script in a test rather than grep it.
set -euo pipefail
umask 077

SHA="${1:?usage: roll.sh <git-sha> [component]}"

DESTDIR="${DESTDIR:-}"
REGION="${AWS_REGION:-us-west-2}"
PARAM_PREFIX="${TOXIC_PARAM_PREFIX:-/toxic}"

APP_DIR="${DESTDIR}/opt/toxic"
ETC_DIR="${DESTDIR}/etc/toxic"
STATE_DIR="${DESTDIR}/var/lib/toxic"
UNIT_DIR="${DESTDIR}/etc/systemd/system"
BIN_DIR="${DESTDIR}/usr/local/bin"
# A reference no registry can serve, for the image variables belonging to components this
# host does not run. Every compose file is interpolated with the same stack.env, so the
# variables have to exist; an empty value would look like a bug and a plausible-looking
# digest would be a lie. This fails immediately and says why if anything ever uses it.
UNRESOLVED="unresolved-on-this-host"

log() { printf 'roll: %s\n' "$*"; }
die() { printf 'roll: FATAL: %s\n' "$*" >&2; exit 1; }

STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

# --- which component is this? -------------------------------------------------------------
COMPONENT="${2:-}"
if [ -z "${COMPONENT}" ] && [ -r "${ETC_DIR}/component" ]; then
  COMPONENT="$(cat "${ETC_DIR}/component")"
fi
case "${COMPONENT}" in
  backend|frontend|monitoring) ;;
  *) die "unknown component '${COMPONENT}': pass it as the second argument, one of backend, frontend, monitoring" ;;
esac
log "${COMPONENT} rolling to ${SHA}"

# --- helpers ------------------------------------------------------------------------------

param() {
  local value
  value="$(aws ssm get-parameter --region "${REGION}" --name "$1" \
    --query 'Parameter.Value' --output text)" || die "cannot read parameter $1"
  [ -n "${value}" ] && [ "${value}" != "None" ] || die "parameter $1 is empty"
  printf '%s' "${value}"
}

secret() {
  aws secretsmanager get-secret-value --region "${REGION}" \
    --secret-id "$1" --query SecretString --output text
}

# Builds the whole DSN inside python from the secret on STDIN, so the password never becomes
# a shell variable and never reaches an argument list. It is also URL-encoded there: RDS
# generates the master password and does not guarantee it is URL-safe, and one unencoded '#'
# truncates the DSN at the host -- the backend then connects to a database whose name is half
# a password, or fails in a way that looks like a network fault.
dsn() { # secret-id host database
  secret "$1" | python3 -c '
import json, sys, urllib.parse
found = json.load(sys.stdin)
user = urllib.parse.quote(found["username"], safe="")
password = urllib.parse.quote(found["password"], safe="")
print(f"postgresql+psycopg://{user}:{password}@{sys.argv[1]}/{sys.argv[2]}")
' "$2" "$3"
}

# Resolve a tag to an immutable digest ONCE, here, so the compose file never floats a tag and
# a restart six hours from now runs exactly the bytes this deploy verified.
#
# `batch-get-image` rather than `describe-images`, because ecr:BatchGetImage is already
# granted to each instance role for its own repository and ecr:DescribeImages is granted to
# nobody. And the result is checked for the shape of a digest rather than for an exit code:
# batch-get-image EXITS 0 and prints `None` when the tag does not exist.
image_digest() { # repository tag
  local digest
  digest="$(aws ecr batch-get-image --region "${REGION}" --repository-name "$1" \
    --image-ids "imageTag=$2" --query 'images[0].imageId.imageDigest' --output text 2>/dev/null)" \
    || return 1
  case "${digest}" in
    sha256:*) printf '%s' "${digest}"; return 0 ;;
  esac
  return 1
}

require_image() { # variable repository tag
  local digest
  digest="$(image_digest "$2" "$3")" \
    || die "no image digest for $2:$3 -- the build for this SHA never reached the registry"
  printf '%s=%s/%s@%s\n' "$1" "${REGISTRY}" "$2" "${digest}"
}

optional_image() { # variable repository tag
  local digest
  if digest="$(image_digest "$2" "$3")"; then
    printf '%s=%s/%s@%s\n' "$1" "${REGISTRY}" "$2" "${digest}"
  else
    log "$2:$3 is not in the registry; $1 left unresolved (the challenger is below the cut line)"
    printf '%s=%s\n' "$1" "${UNRESOLVED}"
  fi
}

unresolved_image() { printf '%s=%s\n' "$1" "${UNRESOLVED}"; }

# Writes STDIN's file into place with the mode it must have, in one step. Building the
# content in a temporary file first is not tidiness: `{ ... } > /etc/toxic/backend.env`
# truncates the destination before the first credential is read, so any failure inside the
# group leaves the running stack with a half-written env file.
install_env() { # source destination
  install -m 0600 "$1" "$2"
  log "wrote $(basename "$2")"
}

# --- the layout, created if absent ---------------------------------------------------------
install -d -m 0755 "${APP_DIR}" "${STATE_DIR}" "${STATE_DIR}/artifacts" "${STATE_DIR}/spool"
# 0700 because everything below is written into it. A 0755 directory leaves a 0600 file
# listable, and anything added later by hand inherits the default umask.
install -d -m 0700 "${ETC_DIR}"
install -d -m 0755 "${UNIT_DIR}" "${BIN_DIR}"
printf '%s\n' "${COMPONENT}" > "${ETC_DIR}/component"
chmod 0644 "${ETC_DIR}/component"

# --- what Terraform knows -------------------------------------------------------------------
REGISTRY="$(param "${PARAM_PREFIX}/deploy/registry")"
DEPLOY_BUCKET="$(param "${PARAM_PREFIX}/deploy/bucket")"
LOG_GROUP_BACKEND="$(param "${PARAM_PREFIX}/logs/backend")"
LOG_GROUP_FRONTEND="$(param "${PARAM_PREFIX}/logs/frontend")"
LOG_GROUP_MONITORING="$(param "${PARAM_PREFIX}/logs/monitoring")"
LOG_GROUP_RESCORER="$(param "${PARAM_PREFIX}/logs/rescorer")"
REPO_BACKEND="$(param "${PARAM_PREFIX}/images/backend")"
REPO_FRONTEND="$(param "${PARAM_PREFIX}/images/frontend")"
REPO_MONITORING="$(param "${PARAM_PREFIX}/images/monitoring")"
REPO_RESCORER="$(param "${PARAM_PREFIX}/images/rescorer")"
DB_ENDPOINT="$(param "${PARAM_PREFIX}/db/endpoint")"
DB_NAME="$(param "${PARAM_PREFIX}/db/name")"

# --- the ECR login helper, and the credential the unit needs on every boot --------------------
cat >"${STAGING}/toxic-ecr-login" <<TOXICECRLOGIN
#!/bin/bash
set -euo pipefail
aws ecr get-login-password --region '${REGION}' \\
  | docker login --username AWS --password-stdin '${REGISTRY}'
TOXICECRLOGIN
install -m 0755 "${STAGING}/toxic-ecr-login" "${BIN_DIR}/toxic-ecr-login"

aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null \
  || die "ECR login failed; this deploy cannot pull the images it is supposed to install"

# --- stack.env: addresses, log groups, and five image references ------------------------------
{
  printf 'AWS_REGION=%s\n' "${REGION}"
  printf 'LOG_GROUP_BACKEND=%s\n' "${LOG_GROUP_BACKEND}"
  printf 'LOG_GROUP_FRONTEND=%s\n' "${LOG_GROUP_FRONTEND}"
  printf 'LOG_GROUP_MONITORING=%s\n' "${LOG_GROUP_MONITORING}"
  printf 'LOG_GROUP_RESCORER=%s\n' "${LOG_GROUP_RESCORER}"
  # Only this host's own images are resolved. The instance roles are scoped per repository,
  # so asking for a sibling tier's digest is a guaranteed AccessDenied -- three denied calls
  # per roll, in the audit trail, to produce a value nothing on this host reads.
  case "${COMPONENT}" in
    backend)
      require_image BACKEND_IMAGE "${REPO_BACKEND}" "${SHA}"
      unresolved_image FRONTEND_IMAGE
      unresolved_image REVIEWER_IMAGE
      unresolved_image MONITORING_IMAGE
      unresolved_image RESCORER_IMAGE
      ;;
    frontend)
      unresolved_image BACKEND_IMAGE
      require_image FRONTEND_IMAGE "${REPO_FRONTEND}" "${SHA}"
      # The reviewer console is the same repository at a different tag: one Dockerfile per
      # entry point, both built from this SHA.
      require_image REVIEWER_IMAGE "${REPO_FRONTEND}" "${SHA}-reviewer"
      unresolved_image MONITORING_IMAGE
      unresolved_image RESCORER_IMAGE
      ;;
    monitoring)
      unresolved_image BACKEND_IMAGE
      unresolved_image FRONTEND_IMAGE
      unresolved_image REVIEWER_IMAGE
      require_image MONITORING_IMAGE "${REPO_MONITORING}" "${SHA}"
      # Optional, and the one image allowed to be missing: the challenger sits below the cut
      # line and its compose service is behind a profile that is not enabled.
      optional_image RESCORER_IMAGE "${REPO_RESCORER}" "${SHA}"
      ;;
  esac
} > "${STAGING}/stack.env"
install_env "${STAGING}/stack.env" "${ETC_DIR}/stack.env"

# --- per-component credentials ---------------------------------------------------------------
case "${COMPONENT}" in
  backend)
    CARD="${APP_DIR}/MODEL_CARD.md"
    [ -f "${CARD}" ] || die "the deploy payload carries no MODEL_CARD.md, so there is no digest of record"
    # The same two anchors backend/model_card.py uses, by LABEL and not by position. The bare
    # hex, not the sha256:-prefixed form: backend/model_loader.py compares it with
    # hmac.compare_digest against read_expected_digest's return value, which is bare.
    MODEL_DIGEST="$(sed -n 's/^- MODEL_DIGEST: sha256:\([0-9a-f]\{64\}\)$/\1/p' "${CARD}" | head -1)"
    [ -n "${MODEL_DIGEST}" ] || die "${CARD} declares no MODEL_DIGEST"
    MODEL_REGISTRY_VERSION="$(sed -n 's/^- MODEL_REGISTRY_VERSION: \([0-9][0-9]*\)$/\1/p' "${CARD}" | head -1)"
    [ -n "${MODEL_REGISTRY_VERSION}" ] || die "${CARD} declares no MODEL_REGISTRY_VERSION"

    {
      dsn "$(param "${PARAM_PREFIX}/db/master-secret-arn")" "${DB_ENDPOINT}" "${DB_NAME}" \
        | sed 's/^/DATABASE_URL=/'
      printf 'DEMO_API_KEY=%s\n' "$(secret "$(param "${PARAM_PREFIX}/secrets/demo-api-key")")"
      printf 'REVIEWER_SHARED_SECRET=%s\n' \
        "$(secret "$(param "${PARAM_PREFIX}/secrets/reviewer-shared-secret")")"
      printf 'REVIEWER_ID=%s\n' "$(param "${PARAM_PREFIX}/reviewer/id")"
      printf 'SUBMITTER_FP_KEY=%s\n' \
        "$(secret "$(param "${PARAM_PREFIX}/secrets/submitter-fp-key")")"
      printf 'MODEL_ARTIFACT_PATH=/artifacts/toxic-clf.skops\n'
      # Inside the image, beside the code (backend/Dockerfile COPYs it to WORKDIR /app), so
      # the provenance anchor travels with the container rather than with the host.
      printf 'MODEL_CARD_PATH=/app/MODEL_CARD.md\n'
      printf 'MODEL_DIGEST=%s\n' "${MODEL_DIGEST}"
      printf 'MODEL_REGISTRY_VERSION=%s\n' "${MODEL_REGISTRY_VERSION}"
      printf 'THRESHOLDS_PATH=/artifacts/thresholds.json\n'
      printf 'SPOOL_PATH=/var/lib/toxic/predictions.spool\n'
    } > "${STAGING}/backend.env"
    install_env "${STAGING}/backend.env" "${ETC_DIR}/backend.env"

    WANDB_ARTIFACT="$(param "${PARAM_PREFIX}/model/wandb-artifact")" \
      DEPLOY_BUCKET="${DEPLOY_BUCKET}" \
      WANDB_SECRET_ID="$(param "${PARAM_PREFIX}/secrets/wandb-api-key")" \
      MODEL_CARD_PATH="${CARD}" \
      ARTIFACT_DIR="${STATE_DIR}/artifacts" \
      bash "${APP_DIR}/fetch_artifacts.sh"
    ;;

  frontend)
    {
      # The backend's PRIVATE address, deliberately. aws_security_group.frontend permits
      # egress to 8000 only inside the public subnet CIDRs, so traffic aimed at the backend's
      # Elastic IP leaves through the internet gateway, misses that rule, and is dropped: the
      # page renders and every prediction times out.
      printf 'BACKEND_URL=%s\n' "$(param "${PARAM_PREFIX}/endpoints/backend-internal")"
      printf 'DEMO_API_KEY=%s\n' "$(secret "$(param "${PARAM_PREFIX}/secrets/demo-api-key")")"
    } > "${STAGING}/frontend.env"
    # One file, shared by both Streamlit entry points. frontend/ui.py and frontend/reviewer.py
    # read exactly these two names; the reviewer shared secret is a backend-side credential
    # and is not on this host at all.
    install_env "${STAGING}/frontend.env" "${ETC_DIR}/frontend.env"
    ;;

  monitoring)
    # EC2 #3 mounts /var/lib/toxic/artifacts read-only and reads BASELINE_PATH and
    # THRESHOLDS_PATH out of it. Nothing else on this host populates that directory, and
    # monitoring/baseline.py is deliberately fail-closed -- so without this the drift panel
    # raises BaselineMissingError on first boot and the graded dashboard is dead.
    #
    # The two sidecars only: this tier never scores anything, so the 382 MB model would be
    # 382 MB of egress and disk for a file no container here opens.
    WANDB_ARTIFACT="$(param "${PARAM_PREFIX}/model/wandb-artifact")" \
      DEPLOY_BUCKET="${DEPLOY_BUCKET}" \
      MODEL_CARD_PATH="${APP_DIR}/MODEL_CARD.md" \
      ARTIFACT_DIR="${STATE_DIR}/artifacts" \
      ARTIFACT_NAMES="thresholds.json baseline_flag_rates.json" \
      bash "${APP_DIR}/fetch_artifacts.sh"

    {
      dsn "$(param "${PARAM_PREFIX}/db/readonly-secret-arn")" "${DB_ENDPOINT}" "${DB_NAME}" \
        | sed 's/^/MONITORING_DB_DSN=/'
      printf 'BASELINE_PATH=/artifacts/baseline_flag_rates.json\n'
      printf 'THRESHOLDS_PATH=/artifacts/thresholds.json\n'
    } > "${STAGING}/monitoring.env"
    install_env "${STAGING}/monitoring.env" "${ETC_DIR}/monitoring.env"

    # compose.monitoring.yml names /etc/toxic/rescorer.env on a service that is behind the
    # `challenger` profile. The profile is not enabled in this deployment, and this file is
    # written anyway because `docker compose` resolves env_file paths while it loads the
    # project, before profiles filter anything -- a missing one fails the whole `up`,
    # including the graded dashboard beside it.
    {
      printf '# The challenger re-scorer is below the cut line and its compose profile is\n'
      printf '# not enabled. Enabling it needs a DATABASE_URL and a CHALLENGER_SHA256, and\n'
      printf '# the first of those is a write credential this tier deliberately cannot read\n'
      printf '# (premortem H16): the dashboard connects as monitor_ro and nothing else.\n'
      printf 'RESCORER_IDLE_SLEEP=5\n'
    } > "${STAGING}/rescorer.env"
    install_env "${STAGING}/rescorer.env" "${ETC_DIR}/rescorer.env"
    ;;
esac

# --- the unit, and the superseded one --------------------------------------------------------
#
# Stood down BEFORE the new unit starts. `disable --now` also runs its ExecStop, which is a
# `docker compose down` against a compose file that first boot never created -- a no-op on
# these hosts, and the correct thing on a host where it did run.
if [ -f "${UNIT_DIR}/toxic-mod.service" ]; then
  log "standing down the superseded toxic-mod.service from the Phase A2 bootstrap"
  systemctl disable --now toxic-mod.service || true
fi

ln -sfn "${APP_DIR}/compose.${COMPONENT}.yml" "${APP_DIR}/compose.yml"
install -m 0644 "${APP_DIR}/toxic-stack.service" "${UNIT_DIR}/toxic-stack.service"
systemctl daemon-reload
systemctl enable toxic-stack.service
systemctl restart toxic-stack.service

# Keeps the previous SHA's images on disk for a week, which is what makes `make rollback` a
# restart rather than a pull.
docker image prune -f --filter "until=168h" >/dev/null 2>&1 || true
log "${COMPONENT} now serving ${SHA}"
