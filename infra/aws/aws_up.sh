#!/usr/bin/env bash
# Bring the stack up and PROVE it is up.
#
# Starting three instances and returning is how a bookmarked URL turns out to be dead five
# minutes before a demo. Nothing in the earlier design started containers on a stop/start
# cycle at all, and delivery spec section 12 requires the live URL to be reachable after one.
#
# It changes no infrastructure. In particular it does not re-enable the two EventBridge cost
# schedules, which are deliberately switched off for the grading window: a bring-up that
# quietly put the graded stack back on a nightly timer would fail the following morning, in
# the dark, with nobody connecting the two events.
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${AWS_UP_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PARAM_PREFIX="${TOXIC_PARAM_PREFIX:-/toxic}"
POLL="${AWS_UP_POLL_SECONDS:-15}"
TIMEOUT="${AWS_UP_TIMEOUT:-600}"
COMPONENTS="backend frontend monitoring"

INSTANCE_IDS="${INSTANCE_IDS:-$(cd infra/terraform && terraform output -json instance_ids | jq -r '.[]' | tr '\n' ' ')}"
DB_INSTANCE_ID="${DB_INSTANCE_ID:-$(cd infra/terraform && terraform output -raw db_instance_id)}"

die() { printf 'aws_up: FATAL: %s\n' "$*" >&2; exit 1; }
param() {
  aws ssm get-parameter --region "${REGION}" --name "$1" \
    --query 'Parameter.Value' --output text 2>/dev/null || true
}

# 1. The database first. The backend's FastAPI lifespan fails closed on an unreachable
#    Postgres, so a host that comes up ahead of it simply restart-loops -- and the unit's
#    TimeoutStartSec is 900 seconds, so that loop is slow and looks like a hang.
#
#    `|| true` on the start itself: an instance that is already `available` answers
#    InvalidDBInstanceState, which is the state this command wants. The wait below is what
#    decides, not the exit code of the request.
aws rds start-db-instance --region "${REGION}" \
  --db-instance-identifier "${DB_INSTANCE_ID}" >/dev/null 2>&1 || true
printf 'aws_up: waiting for RDS %s\n' "${DB_INSTANCE_ID}"
deadline=$(( $(date +%s) + TIMEOUT ))
until [ "$(aws rds describe-db-instances --region "${REGION}" \
        --db-instance-identifier "${DB_INSTANCE_ID}" \
        --query 'DBInstances[0].DBInstanceStatus' --output text)" = "available" ]; do
  [ "$(date +%s)" -lt "${deadline}" ] || die "RDS did not reach available within ${TIMEOUT}s"
  sleep "${POLL}"
done
printf 'aws_up: RDS %s is available\n' "${DB_INSTANCE_ID}"

# 2. The instances. The Elastic IPs survive a stop, so the three published URLs do not move.
# shellcheck disable=SC2086  # INSTANCE_IDS is a deliberate word-split list of instance ids
aws ec2 start-instances --region "${REGION}" --instance-ids ${INSTANCE_IDS} >/dev/null 2>&1 || true
printf 'aws_up: waiting for %s\n' "${INSTANCE_IDS}"
# shellcheck disable=SC2086
aws ec2 wait instance-status-ok --region "${REGION}" --instance-ids ${INSTANCE_IDS} 2>/dev/null || true

# 3. The boot marker, which is the last line of user data and therefore the whole answer to
#    "did this host's bootstrap reach the end?" on a box with no SSH. Rolling into a host that
#    never finished fails for the wrong reason and wastes the first ten minutes of every
#    debugging session.
#
#    On an ordinary stop/start this is already present from the first boot and the loop exits
#    immediately; it earns its place when an instance has been REPLACED.
for component in ${COMPONENTS}; do
  deadline=$(( $(date +%s) + TIMEOUT ))
  until [ -n "$(param "${PARAM_PREFIX}/boot/${component}")" ]; do
    [ "$(date +%s)" -lt "${deadline}" ] \
      || die "${component}: no boot marker at ${PARAM_PREFIX}/boot/${component} -- see docs/runbooks/no-ssh-debug.md, and grep the console output for TOXIC-USER-DATA-COMPLETE"
    sleep "${POLL}"
  done
  printf 'aws_up: %s boot marker present\n' "${component}"
done

# 4. The application. `restart: unless-stopped` covers a Docker daemon restart; it does not
#    cover a replaced instance, and it does not cover a host whose stack was taken down by a
#    rollback's `compose down`. `systemctl start` is idempotent and covers both.
for component in ${COMPONENTS}; do
  "${HERE}/ssm_run.sh" "${component}" 1 systemctl start toxic-stack.service
done

# 5. The gate -- the same one the deploy and the rollback use. A second implementation of
#    "is it up?" is a second thing that can disagree with the deploy, and the one that
#    disagrees quietly is the one an operator believes.
"${HERE}/verify_live.sh"

BACKEND_URL="$(param "${PARAM_PREFIX}/endpoints/backend")"
FRONTEND_URL="$(param "${PARAM_PREFIX}/endpoints/frontend")"
MONITORING_URL="$(param "${PARAM_PREFIX}/endpoints/monitoring")"

cat <<URLS
aws_up: the stack is live
  user interface      ${FRONTEND_URL}
  moderation API      ${BACKEND_URL}
  monitoring          ${MONITORING_URL}
  reviewer queue      aws ssm start-session --target <frontend-instance-id> \\
                        --document-name AWS-StartPortForwardingSession \\
                        --parameters 'portNumber=8503,localPortNumber=8503'
URLS
