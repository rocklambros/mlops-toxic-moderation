#!/usr/bin/env bash
# Phase A1 account bootstrap. Creates the Sandbox OU, its service control policy, the
# rockcyber-mlops-toxic member account, its alternate contacts, the Identity Center
# permission sets, and the Terraform state bucket.
#
# Idempotent throughout: every step checks for existence before writing, so a re-run
# after a partial failure performs no duplicate work. The offline suite proves that by
# running the whole script twice against stubs and asserting the second run issues zero
# mutating calls (tests/unit/test_bootstrap_script.py).
#
# Three design rules, each enforced by a test:
#   * Responses are parsed with jq. --query is never used, because the test stub
#     replays whole JSON documents and cannot emulate server-side JMESPath.
#   * The script installs nothing. This build box holds the AWS SSO refresh token, the
#     W&B key, the Kaggle token, and the RunPod key at the same time.
#   * Root credentials are never touched. Root is the break-glass path and it stays.
#
# Blast radius: the Sandbox OU. Exactly one operation writes at organization-root
# scope, step 2, and it is gated behind --ack-org-root-write.
#
# SC2015 is disabled file-wide. Every instance is the guard `[ -n "$x" ] && [ "$x" != null ]
# || die`, where ShellCheck's warning -- that C runs when A is true -- is the intent: die is
# the wanted action whenever either test fails, and die exits, so nothing runs after it.
# shellcheck disable=SC2015
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROFILE="${BOOTSTRAP_PROFILE:-rc-mgmt}"
REGION="${BOOTSTRAP_REGION:-us-west-2}"
OU_NAME="${BOOTSTRAP_OU_NAME:-Sandbox}"
SCP_NAME="${BOOTSTRAP_SCP_NAME:-sandbox-guardrails}"
ACCOUNT_NAME="${BOOTSTRAP_ACCOUNT_NAME:-rockcyber-mlops-toxic}"
ROOT_EMAIL="${BOOTSTRAP_ROOT_EMAIL:-rock+aws-mlops-toxic@rockcyber.com}"
ALT_CONTACT_EMAIL="${BOOTSTRAP_ALT_CONTACT_EMAIL:-rock@rockcyber.com}"
ALT_CONTACT_NAME="${BOOTSTRAP_ALT_CONTACT_NAME:-Rock Lambros}"
ALT_CONTACT_TITLE="${BOOTSTRAP_ALT_CONTACT_TITLE:-Owner}"
ALT_CONTACT_PHONE="${BOOTSTRAP_ALT_CONTACT_PHONE:-+10000000000}"
SSO_USER_NAME="${BOOTSTRAP_SSO_USER_NAME:-rock.lambros}"
SCP_FILE="${BOOTSTRAP_SCP_FILE:-$SCRIPT_DIR/scp-sandbox-guardrails.json}"
OUTPUTS_FILE="${BOOTSTRAP_OUTPUTS_FILE:-$SCRIPT_DIR/bootstrap-outputs.env}"
POLL_INTERVAL="${BOOTSTRAP_POLL_INTERVAL:-10}"
CREATE_ACCOUNT_TIMEOUT="${BOOTSTRAP_CREATE_ACCOUNT_TIMEOUT:-900}"

ACK_ORG_ROOT_WRITE=0
SKIP_OPERATOR_GATE=0
PREFLIGHT_ONLY=0

ORG_ROOT_ID=""
OU_ID=""
POLICY_ID=""
ACCOUNT_ID=""
UNGOVERNED_SINCE=""

mask() { if [ -n "$ACCOUNT_ID" ]; then sed "s/${ACCOUNT_ID}/<account-id>/g"; else cat; fi; }
log()  { printf '[bootstrap] %s\n' "$1" | mask; }
warn() { printf '[bootstrap] WARN: %s\n' "$1" | mask >&2; }
die()  { printf '[bootstrap] FATAL: %s\n' "$1" | mask >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: bootstrap.sh [--ack-org-root-write] [--skip-operator-gate] [--preflight-only]

  --ack-org-root-write   Acknowledge the single organization-root-wide write (step 2,
                         organizations enable-policy-type). Required only when
                         SERVICE_CONTROL_POLICY is not already enabled on the root.
  --skip-operator-gate   Do not block on the step 7 root break-glass confirmation.
                         For re-runs after the break-glass is already established.
  --preflight-only       Run the preflight checks and stop.
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --ack-org-root-write) ACK_ORG_ROOT_WRITE=1 ;;
            --skip-operator-gate) SKIP_OPERATOR_GATE=1 ;;
            --preflight-only)     PREFLIGHT_ONLY=1 ;;
            -h|--help)            usage; exit 0 ;;
            *)                    die "unknown argument: $1" ;;
        esac
        shift
    done
}

# Every management-account call goes through this wrapper so the profile and region
# are applied in one place and the test stub sees a stable argv shape.
org() { aws "$@" --profile "$PROFILE" --region "$REGION"; }

persist_output() { # key value
    local key="$1" val="$2" tmp
    mkdir -p "$(dirname "$OUTPUTS_FILE")"
    touch "$OUTPUTS_FILE"
    tmp=$(mktemp)
    grep -v "^${key}=" "$OUTPUTS_FILE" >"$tmp" || true
    printf '%s=%s\n' "$key" "$val" >>"$tmp"
    LC_ALL=C sort "$tmp" -o "$tmp"
    mv "$tmp" "$OUTPUTS_FILE"
    chmod 600 "$OUTPUTS_FILE"
}

check_credential_hygiene() {
    local errs=0 v arn caller_acct mgmt_acct

    # premortem C11: this project has no static-credential path for its own principals.
    for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
        if [ -n "${!v:-}" ]; then
            printf 'FATAL: %s is set in this shell. This project has no static AWS credential path. Unset it and run: aws sso login --profile %s\n' "$v" "$PROFILE" >&2
            errs=1
        fi
    done

    arn=$(org sts get-caller-identity 2>/dev/null | jq -r '.Arn // empty')
    case "$arn" in
        arn:aws:sts::*:assumed-role/AWSReservedSSO_*) : ;;
        "") printf 'FATAL: sts:GetCallerIdentity returned nothing for profile %s. Run: aws sso login --profile %s\n' "$PROFILE" "$PROFILE" >&2
            errs=1 ;;
        *)  printf 'FATAL: caller %s is not an IAM Identity Center session. IAM-user and static-credential paths are refused.\n' "$arn" >&2
            errs=1 ;;
    esac

    caller_acct=$(org sts get-caller-identity 2>/dev/null | jq -r '.Account // empty')
    mgmt_acct=$(org organizations describe-organization 2>/dev/null | jq -r '.Organization.MasterAccountId // empty')
    if [ -z "$mgmt_acct" ] || [ "$caller_acct" != "$mgmt_acct" ]; then
        printf 'FATAL: caller account %s is not the organization management account %s. Every step below writes at organization scope.\n' "${caller_acct:-none}" "${mgmt_acct:-unknown}" >&2
        errs=1
    fi

    # Advisory, not fatal: these are the two on-disk credentials premortem C11 found.
    if [ -f "$HOME/.aws/credentials" ] && grep -qi '^[[:space:]]*aws_access_key_id' "$HOME/.aws/credentials"; then
        printf 'WARN: ~/.aws/credentials holds a static access key. This script does not use it, but it sits on the same box as the W&B, Kaggle, and RunPod keys.\n' >&2
    fi
    if [ -f "$HOME/.netrc" ] && grep -q 'api.wandb.ai' "$HOME/.netrc"; then
        printf 'WARN: ~/.netrc holds a plaintext W&B credential, which bypasses the pass-at-point-of-use discipline.\n' >&2
    fi

    return "$errs"
}

preflight() {
    local errs=0 major tfver tfmaj tfmin

    command -v jq >/dev/null 2>&1 || { printf 'FATAL: jq not found\n' >&2; errs=1; }

    major=$(aws --version 2>&1 | sed -n 's|^aws-cli/\([0-9][0-9]*\).*|\1|p')
    if [ "${major:-0}" -lt 2 ] 2>/dev/null; then
        printf 'FATAL: AWS CLI v2 required, found: %s\n' "$(aws --version 2>&1)" >&2
        errs=1
    fi

    tfver=$(terraform version -json 2>/dev/null | jq -r '.terraform_version // empty')
    if [ -z "$tfver" ]; then
        # shellcheck disable=SC2016  # the backticks are literal: they name the subcommand
        printf 'FATAL: terraform not found, or too old to support `version -json`\n' >&2
        errs=1
    else
        tfmaj=${tfver%%.*}; tfmin=${tfver#*.}; tfmin=${tfmin%%.*}
        if [ "$tfmaj" -lt 1 ] || { [ "$tfmaj" -eq 1 ] && [ "$tfmin" -lt 11 ]; }; then
            printf 'FATAL: Terraform 1.11+ required for GA S3 native state locking, found %s\n' "$tfver" >&2
            errs=1
        fi
    fi

    gh auth status >/dev/null 2>&1 || printf 'WARN: gh is not authenticated; Phase A2 needs it\n' >&2

    if [ ! -f "$SCP_FILE" ]; then
        printf 'FATAL: SCP document not found at %s\n' "$SCP_FILE" >&2
        errs=1
    elif ! jq empty "$SCP_FILE" 2>/dev/null; then
        printf 'FATAL: %s is not valid JSON\n' "$SCP_FILE" >&2
        errs=1
    fi

    case "$ALT_CONTACT_PHONE" in
        ""|"+10000000000")
            printf 'FATAL: set BOOTSTRAP_ALT_CONTACT_PHONE to a real number. The placeholder is refused so a phone number never lands in a public repository.\n' >&2
            errs=1 ;;
    esac

    check_credential_hygiene || errs=1

    [ "$errs" -eq 0 ] || die "preflight failed"
    log "preflight passed"
}

# Step 2. THE ONE ORGANIZATION-ROOT-WIDE WRITE IN THIS SCRIPT.
#
# The AWS foundation spec section 6 states that every org-level write here is scoped to
# the Sandbox OU, and disqualifies any operation that cannot be so scoped. That rule is
# what removed centralized root access management from the design. This call breaks the
# rule: organizations:EnablePolicyType takes a root id and has no OU-level form.
#
# It is also unavoidable, because no SCP can be created or attached beneath a root that
# has not enabled the policy type. So it is performed only when genuinely absent, and
# only behind an explicit acknowledgement.
step2_enable_scp_policy_type() {
    local root_json enabled
    root_json=$(org organizations list-roots)
    ORG_ROOT_ID=$(printf '%s' "$root_json" | jq -r '.Roots[0].Id')
    [ -n "$ORG_ROOT_ID" ] && [ "$ORG_ROOT_ID" != "null" ] || die "step 2: could not resolve the organization root id"

    enabled=$(printf '%s' "$root_json" \
        | jq -r '[.Roots[0].PolicyTypes[]? | select(.Type=="SERVICE_CONTROL_POLICY" and .Status=="ENABLED")] | length')
    if [ "$enabled" -ge 1 ]; then
        log "step 2: SERVICE_CONTROL_POLICY already ENABLED on ${ORG_ROOT_ID}; no write performed"
        return 0
    fi

    cat >&2 <<EOF
[bootstrap] BLAST-RADIUS EXCEPTION, step 2.

  organizations:EnablePolicyType is the only organization root-wide write this script
  performs. It takes a root id (${ORG_ROOT_ID}) and has no OU-scoped form, so it breaks
  the invariant in the AWS foundation spec section 6 that every org-level write here is
  scoped to the new Sandbox OU. That invariant is what disqualified centralized root
  access management from the design, and it applies to this call by its own terms.

  It is performed anyway because no SCP can be created or attached beneath a root that
  has not enabled the policy type.

  What changes: SERVICE_CONTROL_POLICY becomes an available policy type organization-wide.
  What does not change: AWS attaches FullAWSAccess to the root and to every OU and
  account automatically when the type is enabled, so no principal loses a permission.
  This call attaches no policy. The management account is structurally exempt from SCPs
  regardless of what is enabled, so RCAP's posture is unchanged.

  Re-run with --ack-org-root-write to proceed.
EOF
    [ "$ACK_ORG_ROOT_WRITE" -eq 1 ] || return 2

    org organizations enable-policy-type \
        --root-id "$ORG_ROOT_ID" --policy-type SERVICE_CONTROL_POLICY >/dev/null
    log "step 2: SERVICE_CONTROL_POLICY enabled on ${ORG_ROOT_ID} (acknowledged)"
}

# Step 3. The Sandbox OU. Created before the account exists, so the account is never
# placed into an OU that has no policy attached.
step3_create_ou() {
    OU_ID=$(org organizations list-organizational-units-for-parent --parent-id "$ORG_ROOT_ID" \
            | jq -r --arg n "$OU_NAME" '.OrganizationalUnits[]? | select(.Name==$n) | .Id' | head -1)
    if [ -n "$OU_ID" ]; then
        log "step 3: OU ${OU_NAME} already exists (${OU_ID}); no write performed"
    else
        OU_ID=$(org organizations create-organizational-unit \
                    --parent-id "$ORG_ROOT_ID" --name "$OU_NAME" \
                | jq -r '.OrganizationalUnit.Id')
        [ -n "$OU_ID" ] && [ "$OU_ID" != "null" ] || die "step 3: create-organizational-unit returned no id"
        log "step 3: created OU ${OU_NAME} (${OU_ID})"
    fi
    persist_output SANDBOX_OU_ID "$OU_ID"
}

# Step 4. The SCP, attached to the OU and only to the OU. Content is synced, not merely
# checked for existence: a policy whose name matches but whose document has drifted looks
# correct in the console and denies nothing.
step4_scp() {
    local doc live attached
    doc=$(jq -c . "$SCP_FILE")
    [ "${#doc}" -lt 5120 ] || die "step 4: SCP document is ${#doc} bytes, over the 5120-byte quota"

    POLICY_ID=$(org organizations list-policies --filter SERVICE_CONTROL_POLICY \
                | jq -r --arg n "$SCP_NAME" '.Policies[]? | select(.Name==$n) | .Id' | head -1)

    if [ -z "$POLICY_ID" ]; then
        POLICY_ID=$(org organizations create-policy \
                        --name "$SCP_NAME" --type SERVICE_CONTROL_POLICY \
                        --description "Sandbox OU guardrails for ${ACCOUNT_NAME}" \
                        --content "$doc" \
                    | jq -r '.Policy.PolicySummary.Id')
        [ -n "$POLICY_ID" ] && [ "$POLICY_ID" != "null" ] || die "step 4: create-policy returned no id"
        log "step 4: created SCP ${SCP_NAME} (${POLICY_ID})"
    else
        live=$(org organizations describe-policy --policy-id "$POLICY_ID" \
               | jq -r '.Policy.Content' | jq -cS .)
        if [ "$live" != "$(printf '%s' "$doc" | jq -cS .)" ]; then
            org organizations update-policy --policy-id "$POLICY_ID" --content "$doc" >/dev/null
            log "step 4: SCP ${POLICY_ID} content had drifted; updated in place"
        else
            log "step 4: SCP ${POLICY_ID} content matches; no write performed"
        fi
    fi

    attached=$(org organizations list-policies-for-target --target-id "$OU_ID" \
                   --filter SERVICE_CONTROL_POLICY \
               | jq -r --arg p "$POLICY_ID" '[.Policies[]? | select(.Id==$p)] | length')
    if [ "$attached" -eq 0 ]; then
        org organizations attach-policy --policy-id "$POLICY_ID" --target-id "$OU_ID" >/dev/null
        log "step 4: attached ${POLICY_ID} to ${OU_ID}"
    else
        log "step 4: ${POLICY_ID} already attached to ${OU_ID}; no write performed"
    fi
    persist_output SCP_POLICY_ID "$POLICY_ID"
}

# The root email is the only property AWS enforces as unique across the organization,
# so it is the existence check for a non-idempotent create. Paginates, because
# list-accounts truncates and the account can be on a later page.
find_account_by_email() {
    local token="" page id
    while :; do
        if [ -z "$token" ]; then
            page=$(org organizations list-accounts)
        else
            page=$(org organizations list-accounts --starting-token "$token")
        fi
        id=$(printf '%s' "$page" | jq -r --arg e "$ROOT_EMAIL" \
             '.Accounts[]? | select((.Email // "" | ascii_downcase) == ($e | ascii_downcase)) | .Id' | head -1)
        if [ -n "$id" ]; then printf '%s' "$id"; return 0; fi
        token=$(printf '%s' "$page" | jq -r '.NextToken // empty')
        [ -n "$token" ] || break
    done
    return 0
}

# Step 5. organizations:CreateAccount is asynchronous AND not idempotent. Both properties
# are handled here and neither fix covers the other:
#   * not idempotent -> an existence check on the root email before creating
#   * asynchronous   -> the request id is persisted before polling starts, and the account
#                       id is persisted the instant it is known, before any later step runs
step5_create_account() {
    local car_id status st waited=0

    ACCOUNT_ID=$(find_account_by_email)
    if [ -n "$ACCOUNT_ID" ]; then
        persist_output ACCOUNT_ID "$ACCOUNT_ID"
        log "step 5: an account already exists for ${ROOT_EMAIL}; not creating"
        return 0
    fi

    car_id=$(org organizations create-account \
                 --email "$ROOT_EMAIL" --account-name "$ACCOUNT_NAME" \
                 --iam-user-access-to-billing DENY \
             | jq -r '.CreateAccountStatus.Id')
    [ -n "$car_id" ] && [ "$car_id" != "null" ] || die "step 5: create-account returned no request id"

    # Before the first poll: an account may now be materialising that nothing else records.
    persist_output CREATE_ACCOUNT_REQUEST_ID "$car_id"
    UNGOVERNED_SINCE=$(date +%s)

    while :; do
        st=$(org organizations describe-create-account-status --create-account-request-id "$car_id")
        status=$(printf '%s' "$st" | jq -r '.CreateAccountStatus.State')
        case "$status" in
            SUCCEEDED)
                ACCOUNT_ID=$(printf '%s' "$st" | jq -r '.CreateAccountStatus.AccountId')
                break ;;
            FAILED)
                die "step 5: create-account FAILED: $(printf '%s' "$st" | jq -r '.CreateAccountStatus.FailureReason // "unknown"') (request ${car_id})" ;;
        esac
        [ "$waited" -lt "$CREATE_ACCOUNT_TIMEOUT" ] \
            || die "step 5: create-account still ${status} after ${waited}s; request ${car_id} is recorded in ${OUTPUTS_FILE}"
        sleep "$POLL_INTERVAL"
        # POLL_INTERVAL is 0 under test; still advance so the timeout guard terminates.
        waited=$(( waited + (POLL_INTERVAL > 0 ? POLL_INTERVAL : 1) ))
    done

    [ -n "$ACCOUNT_ID" ] && [ "$ACCOUNT_ID" != "null" ] || die "step 5: SUCCEEDED without an account id"
    # Before step 6 runs. The account is billable and holds the root email from this moment.
    persist_output ACCOUNT_ID "$ACCOUNT_ID"
    log "step 5: created account ${ACCOUNT_ID}"
}

# Step 6. Close the ungoverned window, then set the alternate contacts.
#
# organizations:CreateAccount places the account in the ORGANIZATION ROOT, not in the OU.
# Until move-account completes, the account is billable and no SCP applies to it: no region
# lock, no instance-type allowlist, no static-credential denial, no detective-control
# protection. Three things keep that interval short and provable:
#   1. Nothing is scheduled between the status poll and the move. The offline suite
#      asserts that on the call log, so a future edit that inserts a step fails the suite.
#   2. Steps 3 and 4 already attached the SCP to the destination, so the account is governed
#      the moment it lands.
#   3. The move is verified by re-reading list-parents, and a failure is fatal, because
#      continuing would run steps 7 through 10 against an ungoverned account.
step6_govern_account() {
    local parent window t existing

    parent=$(org organizations list-parents --child-id "$ACCOUNT_ID" | jq -r '.Parents[0].Id')
    if [ "$parent" = "$OU_ID" ]; then
        log "step 6: account already in ${OU_ID}; no move performed"
    else
        org organizations move-account --account-id "$ACCOUNT_ID" \
            --source-parent-id "$parent" --destination-parent-id "$OU_ID" >/dev/null
        parent=$(org organizations list-parents --child-id "$ACCOUNT_ID" | jq -r '.Parents[0].Id')
        [ "$parent" = "$OU_ID" ] \
            || die "step 6: account is in ${parent}, not ${OU_ID}; no SCP applies to it and steps 7-10 would run ungoverned — refusing to continue"
        window=$(( $(date +%s) - ${UNGOVERNED_SINCE:-$(date +%s)} ))
        persist_output UNGOVERNED_WINDOW_SECONDS "$window"
        log "step 6: moved into ${OU_ID}; ungoverned window was ${window}s"
    fi

    # Alternate contacts point at a known-good address, so operational mail arrives even if
    # the plus-addressed root address is rejected by Mimecast recipient validation.
    #
    # The trailing `|| true` is load-bearing under `set -o pipefail`: account:GetAlternateContact
    # returns a non-zero exit when the contact is not set, which is the ordinary case on a
    # fresh account, and without it the assignment would abort the whole script here.
    for t in BILLING OPERATIONS SECURITY; do
        existing=$(org account get-alternate-contact \
                       --account-id "$ACCOUNT_ID" --alternate-contact-type "$t" 2>/dev/null \
                   | jq -r '.AlternateContact.EmailAddress // empty' || true)
        if [ "$existing" = "$ALT_CONTACT_EMAIL" ]; then
            log "step 6: ${t} alternate contact already set; no write performed"
            continue
        fi
        org account put-alternate-contact \
            --account-id "$ACCOUNT_ID" --alternate-contact-type "$t" \
            --email-address "$ALT_CONTACT_EMAIL" --name "$ALT_CONTACT_NAME" \
            --title "$ALT_CONTACT_TITLE" --phone-number "$ALT_CONTACT_PHONE" >/dev/null
        log "step 6: set ${t} alternate contact"
    done
}

# Step 7. OPERATOR STEP. The script does not touch root credentials and never calls
# iam enable-organizations-root-credentials-management. Root is the break-glass path.
step7_operator_break_glass() {
    cat <<EOF
================== OPERATOR STEP 7: root break-glass ==================

Organizations creates member accounts with no root password, so the break-glass does
not exist until you establish it. Do this now, in a browser:

  1. Sign-in page, "Forgot password", root address: ${ROOT_EMAIL}
     rockcyber.com routes inbound mail through Mimecast, whose recipient validation is
     a known cause of plus-addressed mail being rejected before it reaches the mailbox.
     If nothing arrives within a few minutes, that is the finding this phase exists to
     surface early. The management account can change a member root address without root
     credentials, and an alias such as aws-mlops@rockcyber.com sidesteps plus-addressing
     entirely at no licence cost.
  2. Set a strong unique password and store it in the password manager.
  3. Enrol MFA on the root user. A hardware key is preferred.
  4. Confirm no root access keys exist. That is the credential that actually leaks.

Alternate contacts already point at ${ALT_CONTACT_EMAIL}, so operational mail arrives
regardless of what happens to the root address.

=======================================================================
EOF
    if [ "$SKIP_OPERATOR_GATE" -eq 1 ]; then
        log "step 7: operator gate skipped (--skip-operator-gate)"
        return 0
    fi
    printf 'Type BREAKGLASS-DONE to continue: '
    local ans=""
    read -r ans || true
    [ "$ans" = "BREAKGLASS-DONE" ] || die "step 7: break-glass not acknowledged"
    persist_output BREAK_GLASS_ESTABLISHED "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    log "step 7: break-glass acknowledged"
}

ensure_permission_set() { # instance_arn name managed_policy_arn description -> arn
    local inst="$1" name="$2" policy="$3" desc="$4" arn found
    for arn in $(org sso-admin list-permission-sets --instance-arn "$inst" | jq -r '.PermissionSets[]?'); do
        found=$(org sso-admin describe-permission-set --instance-arn "$inst" --permission-set-arn "$arn" \
                | jq -r --arg n "$name" 'select(.PermissionSet.Name==$n) | .PermissionSet.PermissionSetArn')
        if [ -n "$found" ]; then printf '%s' "$found"; return 0; fi
    done
    arn=$(org sso-admin create-permission-set --instance-arn "$inst" --name "$name" \
              --description "$desc" --session-duration PT4H \
          | jq -r '.PermissionSet.PermissionSetArn')
    org sso-admin attach-managed-policy-to-permission-set --instance-arn "$inst" \
        --permission-set-arn "$arn" --managed-policy-arn "$policy" >/dev/null
    org sso-admin provision-permission-set --instance-arn "$inst" --permission-set-arn "$arn" \
        --target-id "$ACCOUNT_ID" --target-type AWS_ACCOUNT >/dev/null
    printf '%s' "$arn"
}

# Step 8. Identity Center: the directory user, both permission sets, and the assignment.
step8_identity_center() {
    local inst_arn store_id user_id ps_arn ro_arn assigned

    inst_arn=$(org sso-admin list-instances | jq -r '.Instances[0].InstanceArn')
    store_id=$(org sso-admin list-instances | jq -r '.Instances[0].IdentityStoreId')
    [ -n "$inst_arn" ] && [ "$inst_arn" != "null" ] \
        || die "step 8: no Identity Center instance; enable it in the console first"

    user_id=$(org identitystore list-users --identity-store-id "$store_id" \
              | jq -r --arg u "$SSO_USER_NAME" '.Users[]? | select(.UserName==$u) | .UserId' | head -1)
    if [ -z "$user_id" ]; then
        user_id=$(org identitystore create-user --identity-store-id "$store_id" \
                      --user-name "$SSO_USER_NAME" --display-name "$ALT_CONTACT_NAME" \
                      --name "FamilyName=Lambros,GivenName=Rock" \
                      --emails "Value=${ALT_CONTACT_EMAIL},Type=work,Primary=true" \
                  | jq -r '.UserId')
        log "step 8: created directory user ${SSO_USER_NAME}"
    else
        log "step 8: directory user ${SSO_USER_NAME} exists; no write performed"
    fi

    ps_arn=$(ensure_permission_set "$inst_arn" "MlopsToxicAdmin" \
             "arn:aws:iam::aws:policy/AdministratorAccess" "Day-to-day build and deploy")
    ro_arn=$(ensure_permission_set "$inst_arn" "MlopsToxicReadOnly" \
             "arn:aws:iam::aws:policy/ReadOnlyAccess" "Grader or reviewer access")

    assigned=$(org sso-admin list-account-assignments --instance-arn "$inst_arn" \
                   --account-id "$ACCOUNT_ID" --permission-set-arn "$ps_arn" \
               | jq -r --arg u "$user_id" '[.AccountAssignments[]? | select(.PrincipalId==$u)] | length')
    if [ "$assigned" -eq 0 ]; then
        org sso-admin create-account-assignment --instance-arn "$inst_arn" \
            --target-id "$ACCOUNT_ID" --target-type AWS_ACCOUNT \
            --permission-set-arn "$ps_arn" --principal-type USER --principal-id "$user_id" >/dev/null
        log "step 8: assigned ${SSO_USER_NAME} to MlopsToxicAdmin"
    else
        log "step 8: assignment already present; no write performed"
    fi

    persist_output ADMIN_PERMISSION_SET_ARN "$ps_arn"
    persist_output READONLY_PERMISSION_SET_ARN "$ro_arn"
}

# Step 9. Inside the member account: the alias and the Terraform state bucket. The assumed
# credentials live in a subshell and are never logged.
step9_member_account_setup() {
    local creds ak sk st bucket
    creds=$(org sts assume-role \
                --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/OrganizationAccountAccessRole" \
                --role-session-name mlops-toxic-bootstrap)
    ak=$(printf '%s' "$creds" | jq -r '.Credentials.AccessKeyId')
    sk=$(printf '%s' "$creds" | jq -r '.Credentials.SecretAccessKey')
    st=$(printf '%s' "$creds" | jq -r '.Credentials.SessionToken')
    [ -n "$ak" ] && [ "$ak" != "null" ] || die "step 9: assume-role returned no credentials"

    bucket="${ACCOUNT_NAME}-tfstate-${ACCOUNT_ID}"
    (
        export AWS_ACCESS_KEY_ID="$ak" AWS_SECRET_ACCESS_KEY="$sk" AWS_SESSION_TOKEN="$st"
        unset AWS_PROFILE
        member() { aws "$@" --region "$REGION"; }

        if member iam list-account-aliases | jq -e --arg a "$ACCOUNT_NAME" \
               '.AccountAliases | index($a)' >/dev/null 2>&1; then
            log "step 9: account alias already set; no write performed"
        else
            member iam create-account-alias --account-alias "$ACCOUNT_NAME" >/dev/null
            log "step 9: set account alias"
        fi

        if member s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
            log "step 9: Terraform state bucket already exists; no write performed"
        else
            member s3api create-bucket --bucket "$bucket" \
                --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
            member s3api put-bucket-versioning --bucket "$bucket" \
                --versioning-configuration Status=Enabled >/dev/null
            member s3api put-bucket-encryption --bucket "$bucket" \
                --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}' >/dev/null
            member s3api put-public-access-block --bucket "$bucket" \
                --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
            member s3api put-bucket-policy --bucket "$bucket" --policy "$(cat <<POLICY
{"Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::${bucket}","arn:aws:s3:::${bucket}/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
POLICY
)" >/dev/null
            log "step 9: created Terraform state bucket, versioned, encrypted, private, TLS-only"
        fi
    )
    persist_output TF_STATE_BUCKET "$bucket"
}

# Step 10. Finalise the outputs file. Every key was written as soon as it was known;
# this records the remainder and states where it goes.
step10_write_outputs() {
    persist_output AWS_REGION "$REGION"
    persist_output ACCOUNT_NAME "$ACCOUNT_NAME"
    chmod 600 "$OUTPUTS_FILE"
    log "step 10: wrote ${OUTPUTS_FILE} (gitignored, mode 600); Phase A2 sources it"
}

main() {
    parse_args "$@"
    preflight
    if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
        log "preflight only; stopping"
        return 0
    fi
    step2_enable_scp_policy_type
    step3_create_ou
    step4_scp
    step5_create_account
    step6_govern_account
    step7_operator_break_glass
    step8_identity_center
    step9_member_account_setup
    step10_write_outputs
    log "bootstrap complete"
}

[ "${BOOTSTRAP_SOURCE_ONLY:-0}" = "1" ] || main "$@"
