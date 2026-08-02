#!/usr/bin/env bash
# pg_dump the graded dataset to S3, and prove the uploaded bytes are restorable.
#
# RDS sits in private subnets with no internet path and there is no bastion, so the dump runs
# ON the backend instance -- the one tier with both a 5432 route and the master credential --
# through the same asserted SSM path as a deploy. A SendCommand that matched zero instances
# cannot report success here either.
#
# THE SCRIPT IT RUNS IS FETCHED, NOT ASSUMED. /opt/toxic is written by bootstrap.sh from
# s3://<bucket>/deploy/<sha>/ and by nothing else after first boot, so uploading an ops script
# to deploy/current/ and then running `bash /opt/toxic/dump.sh` dies with "No such file or
# directory" on any host that has been rolled even once -- which is every host. The remote
# script is published under deploy/ (the prefix the instance role can read, by
# data.aws_iam_policy_document.deploy_payload) and fetched to /tmp by the payload itself.
#
# usage: db_dump.sh [label]     prints the S3 key it wrote, last, on its own line
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${DB_DUMP_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PARAM_PREFIX="${TOXIC_PARAM_PREFIX:-/toxic}"

# Pinned by digest, and it is the same digest ci.yml and infra/docker-compose.yml already use.
# A floating tag would decide, at teardown time and with no review, which pg_dump writes the
# dataset the dashboard is graded on.
PG_IMAGE="${TOXIC_PG_IMAGE:-postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777}"

LABEL="${1:-}"
STAMP="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
KEY="db/${STAMP}${LABEL:+-${LABEL}}.dump"
OPS_KEY="deploy/ops/db-dump-${STAMP}.sh"

BUCKET="$(aws ssm get-parameter --region "${REGION}" --name "${PARAM_PREFIX}/deploy/bucket" \
  --query 'Parameter.Value' --output text)"

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

# The body is a QUOTED heredoc, so nothing in it is expanded here. The four values that vary
# are written above it by printf %q. The alternative -- an unquoted heredoc full of \$ -- is
# where a missed backslash silently interpolates the operator's environment into a script that
# runs as root against the production database.
{
  printf '#!/usr/bin/env bash\n'
  printf 'REGION=%q\n' "${REGION}"
  printf 'BUCKET=%q\n' "${BUCKET}"
  printf 'KEY=%q\n' "${KEY}"
  printf 'PG_IMAGE=%q\n' "${PG_IMAGE}"
  cat <<'REMOTE_EOF'
set -euo pipefail
umask 077

. /etc/toxic/backend.env

# DATABASE_URL is a SQLAlchemy URL -- `postgresql+psycopg://` -- because that is what the
# application's engine takes. libpq does not understand the dialect suffix and rejects it with
# `invalid URI scheme`, so pg_dump never connects and the failure reads like a network fault.
#
# It is split into the five libpq environment variables rather than handed over as a
# connection string, so no password and no DSN ever appears on an argv: /proc, `docker top`
# and `docker inspect` are all readable by anything else on that host. roll.sh URL-encodes the
# credentials when it builds the DSN, so they are decoded here.
eval "$(printf '%s' "${DATABASE_URL}" | python3 -c '
import shlex, sys, urllib.parse
url = urllib.parse.urlparse(sys.stdin.read().strip())
def q(value):
    return shlex.quote(urllib.parse.unquote(value or ""))
print(f"export PGHOST={q(url.hostname)}")
print(f"export PGPORT={shlex.quote(str(url.port or 5432))}")
print(f"export PGUSER={q(url.username)}")
print(f"export PGPASSWORD={q(url.password)}")
print(f"export PGDATABASE={q((url.path or chr(47)).lstrip(chr(47)))}")
')"

# --format=custom so pg_restore can be selective and so the archive carries a table of
# contents that can be verified. A direct pipe to S3 so no dump file is left sitting on an
# instance volume that a teardown is about to delete.
docker run --rm --network host \
  -e PGHOST -e PGPORT -e PGUSER -e PGPASSWORD -e PGDATABASE \
  "${PG_IMAGE}" pg_dump --no-owner --no-privileges --format=custom \
  | aws s3 cp --region "${REGION}" --sse AES256 - "s3://${BUCKET}/${KEY}"

# `aws s3 cp -` uploads whatever it received before the pipe broke, so a pg_dump that died
# halfway still leaves an object behind. That object is a truncated archive, and
# `pg_restore --clean` against it drops every table and THEN fails -- the exact data loss the
# dump exists to prevent, delivered by the restore. Read the uploaded bytes back and make
# pg_restore parse them.
aws s3 cp --region "${REGION}" "s3://${BUCKET}/${KEY}" - \
  | docker run --rm -i "${PG_IMAGE}" pg_restore --list >/dev/null

printf 'db_dump: s3://%s/%s is a readable custom-format archive\n' "${BUCKET}" "${KEY}"
REMOTE_EOF
} > "${STAGE}/db-dump.sh"

aws s3 cp --region "${REGION}" --sse AES256 "${STAGE}/db-dump.sh" "s3://${BUCKET}/${OPS_KEY}"

"${HERE}/ssm_run.sh" backend 1 \
  "aws s3 cp --region ${REGION} s3://${BUCKET}/${OPS_KEY} /tmp/toxic-db-dump.sh && bash /tmp/toxic-db-dump.sh"

printf 'db_dump: wrote s3://%s/%s\n' "${BUCKET}" "${KEY}"
# Last, on its own line, so `KEY=$(make db-dump | tail -1)` works.
printf '%s\n' "${KEY}"
