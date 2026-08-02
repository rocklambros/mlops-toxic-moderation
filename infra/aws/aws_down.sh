#!/usr/bin/env bash
# Stop the stack between sessions, and refuse to do it without a dump.
#
# `make aws-down` has `db-dump` as a hard prerequisite, so no teardown path skips it -- but a
# Make prerequisite orders two commands, it does not stop anyone from running this script
# directly, and an ordering nobody can be forced to follow is a convention rather than a
# control. So this also checks S3 for a dump newer than AWS_DOWN_MAX_DUMP_AGE and refuses
# outright without one.
#
# It changes no infrastructure. In particular it does not touch the two EventBridge schedules,
# which are deliberately switched off for the grading window: putting the graded stack back on
# a nightly timer as a side effect of a stop would be the worst kind of surprise.
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
PARAM_PREFIX="${TOXIC_PARAM_PREFIX:-/toxic}"
MAX_DUMP_AGE="${AWS_DOWN_MAX_DUMP_AGE:-3600}"
# Injectable so the deadline arithmetic can be tested against a fixed clock rather than
# against "now", which is untestable and therefore untested.
NOW="${AWS_DOWN_NOW:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

die() { printf 'aws_down: FATAL: %s\n' "$*" >&2; exit 1; }

INSTANCE_IDS="${INSTANCE_IDS:-$(cd infra/terraform && terraform output -json instance_ids | jq -r '.[]' | tr '\n' ' ')}"
DB_INSTANCE_ID="${DB_INSTANCE_ID:-$(cd infra/terraform && terraform output -raw db_instance_id)}"

BUCKET="$(aws ssm get-parameter --region "${REGION}" --name "${PARAM_PREFIX}/deploy/bucket" \
  --query 'Parameter.Value' --output text)"

# --- the dump has to exist, and be this session's -------------------------------------------
LATEST="$(aws s3api list-objects-v2 --region "${REGION}" --bucket "${BUCKET}" --prefix db/ \
  --query 'sort_by(Contents, &LastModified)[-1].LastModified' --output text 2>/dev/null || true)"
[ -n "${LATEST}" ] && [ "${LATEST}" != "None" ] \
  || die "no database dump in s3://${BUCKET}/db/ -- run 'make aws-down', which dumps first. The monitoring dashboard is graded on that data and a stopped RDS instance is seven days from an automatic restart."

NOW_EPOCH="$(date -u -d "${NOW}" +%s)"
LATEST_EPOCH="$(date -u -d "${LATEST}" +%s)"
AGE=$(( NOW_EPOCH - LATEST_EPOCH ))
[ "${AGE}" -le "${MAX_DUMP_AGE}" ] \
  || die "the newest dump in s3://${BUCKET}/db/ is ${AGE}s old (limit ${MAX_DUMP_AGE}s) -- it does not describe this session. Run 'make aws-down'."
printf 'aws_down: newest dump is %ss old, inside the %ss window\n' "${AGE}" "${MAX_DUMP_AGE}"

# --- EC2 first ------------------------------------------------------------------------------
#
# The backend holds pooled connections and a spool that fills when writes fail. Stopping the
# database underneath it produces a wall of errors and a spool full of rows, for no benefit.
# shellcheck disable=SC2086  # INSTANCE_IDS is a deliberate word-split list of instance ids
aws ec2 stop-instances --region "${REGION}" --instance-ids ${INSTANCE_IDS} >/dev/null
printf 'aws_down: stopping %s\n' "${INSTANCE_IDS}"

aws rds stop-db-instance --region "${REGION}" --db-instance-identifier "${DB_INSTANCE_ID}" >/dev/null
aws ssm put-parameter --region "${REGION}" --name "${PARAM_PREFIX}/ops/rds-stopped-at" \
  --type String --overwrite --value "${NOW}" >/dev/null

DEADLINE="$(date -u -d "${NOW} +7 days" +%Y-%m-%d)"

cat <<NOTICE
aws_down: RDS ${DB_INSTANCE_ID} stopping, recorded at ${NOW}

  A stopped RDS instance restarts by itself after 7 days. Deadline: ${DEADLINE}.
  Before then, either 'make aws-up' or run 'make aws-down' again to re-stop it.
  The dump for this session is already in S3, so 'make aws-destroy' is also safe.

  The two cost schedules are off for the grading window and this command did not
  change that. Re-enable them deliberately, with terraform, when grading is done.
NOTICE
