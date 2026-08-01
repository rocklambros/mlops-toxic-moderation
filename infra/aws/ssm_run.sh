#!/usr/bin/env bash
# Run a command on every instance carrying a tag, and PROVE it ran.
#
# `aws ssm send-command` is fire-and-forget. A --targets expression matching ZERO instances
# still returns a CommandId and exits 0, so a deploy job built on send-command alone goes
# green while nothing was deployed -- and the demo URL keeps serving last week's SHA.
# Everything below exists to make that impossible:
#
#   1. the number of invocations must equal the number of instances we expected
#   2. every invocation must reach a terminal state before this returns
#   3. every terminal state must be Success; anything else prints its output and fails
#
# It still does not prove the application works. A shell exiting 0 on three boxes is not a
# container that started, an artifact that verified, a database that answered, or a security
# group that lets the grader in. `infra/aws/verify_deploy.sh` is the gate that says that, and
# conflating the two is how a green job ships a container that never came up.
#
# usage: ssm_run.sh <component-tag-value> <expected-count> <remote command...>
set -euo pipefail

usage() { printf 'usage: ssm_run.sh <component> <expected_count> <command...>\n' >&2; exit 2; }
die() { printf 'ssm_run: FATAL: %s\n' "$*" >&2; exit 1; }

COMPONENT="${1:-}"
EXPECTED="${2:-}"
[ -n "${COMPONENT}" ] && [ -n "${EXPECTED}" ] || usage
# Validated as a positive integer BEFORE it reaches `[ "$observed" -ge "$EXPECTED" ]`. A
# non-numeric value there is a shell error inside the loop that proves the fleet matched --
# a proof that never ran -- and zero is trivially satisfiable, which is what someone writes
# when they do not know how many instances there are.
case "${EXPECTED}" in
  ''|*[!0-9]*) printf 'ssm_run: expected_count must be a positive integer\n' >&2; usage ;;
esac
[ "${EXPECTED}" -gt 0 ] || { printf 'ssm_run: expected_count must be greater than zero\n' >&2; usage; }
shift 2
[ "$#" -gt 0 ] || usage
REMOTE_COMMAND="$*"

# --parameters takes a JSON document that is assembled below by string interpolation. A
# double quote or a backslash in the command silently changes that document's shape: SSM then
# runs something other than what the caller wrote, or rejects the call in a way that reads
# like an IAM problem. The payload this repository sends is one `bash /opt/toxic/...` line
# with neither character in it, so refusing is free.
case "${REMOTE_COMMAND}" in
  *'"'*|*'\'*)
    printf 'ssm_run: unsupported character in the remote command: a double quote or a backslash would corrupt the --parameters JSON\n' >&2
    usage
    ;;
esac

REGION="${AWS_REGION:?AWS_REGION must be set}"
TAG_KEY="${SSM_TARGET_TAG:-Component}"
REGISTER_TIMEOUT="${SSM_REGISTER_TIMEOUT:-120}"
RUN_TIMEOUT="${SSM_RUN_TIMEOUT:-900}"
POLL="${SSM_POLL_SECONDS:-5}"
# The instance's own stdout and stderr end up in a GitHub Actions log on a PUBLIC repository.
# Nothing on the instance is supposed to print anything sensitive; this is the second line of
# defence for the day something does. It is NOT optional: a missing redactor fails the print
# rather than falling back to raw output, because a control that silently degrades on the one
# runner nobody is watching is not a control.
REDACTOR="${SSM_REDACTOR:-scripts/redact.py}"

redacted() {
  if [ ! -f "${REDACTOR}" ]; then
    printf '<output withheld: the redactor %s is not readable from %s>\n' "${REDACTOR}" "${PWD}"
    cat >/dev/null
    return 0
  fi
  python3 "${REDACTOR}"
}

command_id="$(aws ssm send-command \
  --region "${REGION}" \
  --document-name AWS-RunShellScript \
  --targets "Key=tag:${TAG_KEY},Values=${COMPONENT}" \
  --parameters "commands=[\"${REMOTE_COMMAND}\"]" \
  --timeout-seconds 600 \
  --comment "toxic roll ${COMPONENT}" \
  --query 'Command.CommandId' --output text)"
# `--output text` prints the string `None` for an absent field rather than failing, and
# `None` is a perfectly good shell string that every later call would happily accept.
[ -n "${command_id}" ] && [ "${command_id}" != "None" ] || die "send-command returned no CommandId"
printf 'ssm_run: %s CommandId=%s\n' "${COMPONENT}" "${command_id}"

# --- Assertion 1: the target expression matched exactly EXPECTED instances. ---
deadline=$(( $(date +%s) + REGISTER_TIMEOUT ))
observed=0
while :; do
  observed="$(aws ssm list-command-invocations --region "${REGION}" \
      --command-id "${command_id}" --query 'length(CommandInvocations)' --output text)"
  [ "${observed}" = "None" ] && observed=0
  [ "${observed}" -ge "${EXPECTED}" ] && break
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    die "expected ${EXPECTED} invocations for tag ${TAG_KEY}=${COMPONENT}, saw ${observed} after ${REGISTER_TIMEOUT}s -- nothing was deployed"
  fi
  sleep "${POLL}"
done
[ "${observed}" -eq "${EXPECTED}" ] || \
  die "expected ${EXPECTED} invocations for tag ${TAG_KEY}=${COMPONENT}, saw ${observed} -- the running fleet does not match the plan"
printf 'ssm_run: %s matched %s/%s instances\n' "${COMPONENT}" "${observed}" "${EXPECTED}"

instance_ids="$(aws ssm list-command-invocations --region "${REGION}" \
    --command-id "${command_id}" --query 'CommandInvocations[].InstanceId' --output text)"

# --- Assertions 2 and 3: poll each invocation to a terminal state; only Success passes. ---
failed=0
for instance in ${instance_ids}; do
  run_deadline=$(( $(date +%s) + RUN_TIMEOUT ))
  status="Pending"
  while :; do
    status="$(aws ssm get-command-invocation --region "${REGION}" \
        --command-id "${command_id}" --instance-id "${instance}" \
        --query 'Status' --output text)"
    case "${status}" in
      Success|Failed|Cancelled|TimedOut) break ;;
    esac
    if [ "$(date +%s)" -ge "${run_deadline}" ]; then
      status="PollTimeout"
      break
    fi
    sleep "${POLL}"
  done
  printf 'ssm_run: %s %s -> %s\n' "${COMPONENT}" "${instance}" "${status}"
  if [ "${status}" != "Success" ]; then
    failed=1
    printf -- '--- %s StandardErrorContent ---\n' "${instance}" >&2
    aws ssm get-command-invocation --region "${REGION}" --command-id "${command_id}" \
      --instance-id "${instance}" --query 'StandardErrorContent' --output text \
      | redacted >&2 || true
    printf -- '--- %s StandardOutputContent ---\n' "${instance}" >&2
    aws ssm get-command-invocation --region "${REGION}" --command-id "${command_id}" \
      --instance-id "${instance}" --query 'StandardOutputContent' --output text \
      | redacted >&2 || true
  fi
done
[ "${failed}" -eq 0 ] || die "${COMPONENT}: at least one invocation did not reach Success"
printf 'ssm_run: %s OK -- every invocation reported Success. Run verify_deploy.sh next; that is the gate.\n' "${COMPONENT}"
