#!/usr/bin/env bash
# Close the public demo window and rotate the two secrets that were exposed while it was open.
#
# This is the owner of the compensating controls that `docs/tls-decision.md` and
# `MODEL_CARD.md` both rest on. Those documents accept real risk -- cleartext HTTP, and
# white-box evasion -- on the stated basis that these controls exist. Until this script ran,
# they existed as sentences.
#
# Run it after grading, or any time the window should shut:
#
#     AWS_PROFILE=rc-mlops OPERATOR_CIDR=203.0.113.4/32 ALERT_EMAIL=you@example.com \
#       bash scripts/close_demo.sh
#
# It is idempotent. Re-running it re-closes an already-closed window, rotates the secrets
# again, and re-proves the endpoints refuse a connection.
#
# THREE PROPERTIES WORTH KEEPING when editing this file, each with a test in
# tests/unit/test_post_demo_closure.py:
#
#  1. No secret value is ever an argv element. `--secret-string "$(openssl rand ...)"` keeps
#     the value inside a command substitution the shell hands to the AWS CLI over a pipe-free
#     exec; writing it to a variable first would put it in the process table for anything
#     reading /proc on a shared box.
#  2. Closure is REMOVING infra/terraform/demo.auto.tfvars, not editing its CIDR to []. The
#     file's existence is what tests/unit/test_demo_window.py keys on, and a file whose whole
#     meaning is "the window is open" must not survive the window.
#  3. It ends by PROBING, not by asserting. A `terraform apply` that returns zero proves the
#     API accepted a plan, not that the port stopped answering.

set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
TF_DIR="infra/terraform"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

if [ -z "${OPERATOR_CIDR:-}" ]; then
  echo "close_demo: OPERATOR_CIDR is required (the address that keeps access after closing)" >&2
  echo "            e.g. OPERATOR_CIDR=\"\$(curl -s https://checkip.amazonaws.com)/32\"" >&2
  exit 2
fi
if [ -z "${ALERT_EMAIL:-}" ]; then
  echo "close_demo: ALERT_EMAIL is required; terraform has no default for it" >&2
  exit 2
fi

echo "==> 1/4  Removing the file that holds the demo window open"
if [ -f "${TF_DIR}/demo.auto.tfvars" ]; then
  rm -f "${TF_DIR}/demo.auto.tfvars"
  echo "    removed ${TF_DIR}/demo.auto.tfvars"
else
  echo "    already absent; the window was closed in code"
fi

echo "==> 2/4  Applying, so the security groups match the code"
terraform -chdir=infra/terraform apply -input=false -auto-approve \
  -var "operator_cidrs=[\"${OPERATOR_CIDR}\"]" \
  -var "alert_email=${ALERT_EMAIL}"

echo "==> 3/4  Rotating the two secrets that were reachable while the window was open"
# The reviewer shared secret never crossed a cleartext listener -- port 8503 has no ingress
# rule on any security group -- but it is rotated anyway: the demo API key did cross one, and
# rotating only the secret you are certain leaked is how you find out you were wrong.
for secret in reviewer-shared-secret demo-api-key; do
  aws secretsmanager put-secret-value \
    --region "${REGION}" \
    --secret-id "toxic-mod/${secret}" \
    --secret-string "$(openssl rand -base64 32)" \
    --query 'VersionId' --output text >/dev/null
  echo "    rotated toxic-mod/${secret}"
done

echo "    rolling the containers so they read the new values"
aws ssm send-command \
  --region "${REGION}" \
  --document-name AWS-RunShellScript \
  --targets "Key=tag:Component,Values=backend,frontend" \
  --parameters 'commands=["systemctl restart toxic-stack.service"]' \
  --query 'Command.CommandId' --output text >/dev/null

echo "==> 4/4  Proving the listeners refuse a connection from off the allowlist"
# A terraform apply is not a probe. This runs from the operator's own address, which is
# still allowed, so it cannot prove the world is shut out on its own -- what it proves is
# that the demo CIDR is gone from the group. The definitive check is the second block.
failed=0
for parameter in frontend backend monitoring; do
  url="$(aws ssm get-parameter --region "${REGION}" \
        --name "/toxic/endpoints/${parameter}" --query Parameter.Value --output text)"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${url}" || true)"
  printf '    %-11s from the operator address: HTTP %s\n' "${parameter}" "${code:-refused}"
done

open_cidrs="$(aws ec2 describe-security-groups --region "${REGION}" \
  --filters Name=group-name,Values=toxic-mod-frontend,toxic-mod-backend,toxic-mod-monitoring \
  --query "SecurityGroups[].IpPermissions[].IpRanges[?CidrIp=='0.0.0.0/0'].CidrIp" \
  --output text)"
if [ -n "${open_cidrs}" ]; then
  echo "close_demo: FAILED -- 0.0.0.0/0 is still present on a graded listener" >&2
  failed=1
else
  echo "    no 0.0.0.0/0 rule remains on any graded listener"
fi

reviewer_rules="$(aws ec2 describe-security-groups --region "${REGION}" \
  --filters Name=group-name,Values=toxic-mod-reviewer \
  --query 'SecurityGroups[].IpPermissions[]' --output text)"
if [ -n "${reviewer_rules}" ]; then
  echo "close_demo: FAILED -- the reviewer group acquired an ingress rule" >&2
  failed=1
else
  echo "    toxic-mod-reviewer still has no ingress rule at all"
fi

if [ "${failed}" -ne 0 ]; then
  exit 1
fi

cat <<'DONE'

Closed. Record it before the memory fades:

  docs/submission-manifest.yml  ->  post_demo_controls
      demo_cidrs_closed              satisfied: true, verified_on: <today>
      reviewer_shared_secret_rotated satisfied: true, verified_on: <today>
      demo_api_key_rotated           satisfied: true, verified_on: <today>

tests/unit/test_post_demo_closure.py turns red until those three say so, because
demo.auto.tfvars is gone and the manifest still claims the window is open.
DONE
