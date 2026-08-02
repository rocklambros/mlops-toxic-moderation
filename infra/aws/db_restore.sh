#!/usr/bin/env bash
# Put the graded dashboard dataset back, from a dump in S3.
#
# This is the other half of the H29 resolution and the only reason `make aws-destroy` is safe
# to run at all. It runs ON the backend instance, because RDS is private with no bastion, and
# through ssm_run.sh, so a SendCommand that matched zero instances cannot report success.
#
# The remote script is fetched rather than assumed, for the same reason db_dump.sh fetches
# its own: /opt/toxic holds whatever bootstrap.sh copied from deploy/<sha>/ and nothing else.
#
# usage: db_restore.sh <s3-key>     (or: make db-restore S3_KEY=db/...dump)
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${DB_RESTORE_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PARAM_PREFIX="${TOXIC_PARAM_PREFIX:-/toxic}"
PG_IMAGE="${TOXIC_PG_IMAGE:-postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777}"

die() { printf 'db_restore: FATAL: %s\n' "$*" >&2; exit 1; }

BUCKET="$(aws ssm get-parameter --region "${REGION}" --name "${PARAM_PREFIX}/deploy/bucket" \
  --query 'Parameter.Value' --output text)"
S3_KEY="${1:-}"

# No default, and no "latest". Restoring whatever happens to be newest is how the wrong
# session's dataset ends up in a graded dashboard, silently and with a green health check.
if [ -z "${S3_KEY}" ]; then
  printf 'usage: db_restore.sh <s3-key>   (or: make db-restore S3_KEY=db/...dump)\n' >&2
  printf 'available dumps in s3://%s/db/\n' "${BUCKET}" >&2
  aws s3 ls --region "${REGION}" "s3://${BUCKET}/db/" || true
  exit 2
fi

# The restore DROPs every table before it loads. A key that is not a dump -- `deploy/current/
# roll.sh`, say, from a mistyped variable -- would do the dropping first and discover the
# problem second.
case "${S3_KEY}" in
  db/*) ;;
  *) die "'${S3_KEY}' is not under the db/ prefix; only a database dump can be restored" ;;
esac

STAMP="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
OPS_KEY="deploy/ops/db-restore-${STAMP}.sh"

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

{
  printf '#!/usr/bin/env bash\n'
  printf 'REGION=%q\n' "${REGION}"
  printf 'BUCKET=%q\n' "${BUCKET}"
  printf 'KEY=%q\n' "${S3_KEY}"
  printf 'PG_IMAGE=%q\n' "${PG_IMAGE}"
  cat <<'REMOTE_EOF'
set -euo pipefail
umask 077

. /etc/toxic/backend.env

# DATABASE_URL is a SQLAlchemy URL (postgresql+psycopg://), which libpq refuses. Split into
# the five libpq variables so nothing lands on an argv either: /proc and `docker inspect` are
# readable by anything else on this host. roll.sh URL-encodes the credentials.
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

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
ARCHIVE="${WORK}/restore.dump"

# Materialised, not streamed. Piping S3 straight into `pg_restore --clean` means the DROPs are
# already committed by the time a short read is discovered -- and a short read is exactly what
# `aws s3 cp -` leaves behind when a dump's pipe broke.
aws s3 cp --region "${REGION}" "s3://${BUCKET}/${KEY}" "${ARCHIVE}"

# Read the table of contents BEFORE anything is dropped. A truncated or non-archive object
# fails here, with the database untouched.
docker run --rm -v "${WORK}:/work:ro" "${PG_IMAGE}" \
  pg_restore --list /work/restore.dump >/dev/null

# --clean --if-exists so a re-run replaces rather than duplicates; this is the command most
# likely to be run twice under stress. --single-transaction so a failure halfway leaves the
# database as it was rather than leaving the dashboard reading half a dataset and reporting it
# as fact; it implies --exit-on-error, which is also what is wanted here.
docker run --rm --network host -v "${WORK}:/work:ro" \
  -e PGHOST -e PGPORT -e PGUSER -e PGPASSWORD -e PGDATABASE \
  "${PG_IMAGE}" pg_restore --clean --if-exists --no-owner --no-privileges \
  --single-transaction --dbname "${PGDATABASE}" /work/restore.dump

rm -f "${ARCHIVE}"
printf 'db_restore: %s restored into %s\n' "${KEY}" "${PGDATABASE}"
REMOTE_EOF
} > "${STAGE}/db-restore.sh"

aws s3 cp --region "${REGION}" --sse AES256 "${STAGE}/db-restore.sh" "s3://${BUCKET}/${OPS_KEY}"

"${HERE}/ssm_run.sh" backend 1 \
  "aws s3 cp --region ${REGION} s3://${BUCKET}/${OPS_KEY} /tmp/toxic-db-restore.sh && bash /tmp/toxic-db-restore.sh"

printf 'db_restore: restored s3://%s/%s\n' "${BUCKET}" "${S3_KEY}"
printf 'db_restore: confirm the dashboard repopulated: make deploy-verify\n'
