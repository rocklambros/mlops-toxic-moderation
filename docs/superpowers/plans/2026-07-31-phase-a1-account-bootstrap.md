# Phase A1: AWS Account Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dedicated AWS Organizations member account `rockcyber-mlops-toxic` living inside a `Sandbox` OU that carries a tested service control policy, created by an idempotent ten-step bootstrap script, with the Terraform state bucket, the Identity Center permission sets, and the root break-glass all established — and with every guardrail proven by a test that fails if the guardrail is absent.

**Architecture:** Two artifacts and one test harness. `infra/aws/scp-sandbox-guardrails.json` is a pure data artifact: eleven Deny statements, no code, testable entirely offline with `jq`. `infra/aws/bootstrap.sh` is the imperative half: ten ordered steps against the AWS Organizations, Identity Center, STS, and S3 APIs, every one of them guarded by an existence check so a re-run after a partial failure performs no duplicate write. The tests intercept the `aws` binary with a PATH-shim stub that records argv and replays canned JSON, so the whole control flow — including the parts that create an account — is exercised without touching AWS. A separate live acceptance suite runs once, on day 1, against the real account, and produces the evidence that the guardrails actually deny.

This runs **first, on day 1**, because account creation is the only irreducible-latency task in the project. `organizations:CreateAccount` polls asynchronously, and the root break-glass depends on password-recovery mail reaching a plus-addressed `rockcyber.com` address that routes through Mimecast, whose recipient validation is a known cause of plus-addressed mail being rejected. More effort on day 10 does not make that finish faster. An empty account holds no billable resources, so starting early costs nothing.

**Tech Stack:** bash 5 (`#!/usr/bin/env bash`), AWS CLI v2.36.3, jq 1.6, shellcheck, Terraform 1.15.8 (version-checked only, not invoked), `gh` 2.96.0 (auth-checked only). No test framework, no package manager, no interpreter beyond bash and jq. That is a deliberate choice, justified under "Testing approach" below.

## Global Constraints

Every task inherits these. They are copied from the delivery spec (`docs/superpowers/specs/2026-07-30-delivery-plan-design.md`, which governs on conflict), the AWS foundation spec (`docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`), and the binding owner decisions.

- **Region is `us-west-2`.** It is the only region any workload resource may exist in. The SCP does **not** allowlist `us-east-1`; it exempts named global services by `NotAction` instead.
- **Three EC2 instances**, each separate: backend, frontend, monitoring. The SCP instance-type allowlist must therefore carry `t4g.small`, `t4g.medium`, `t4g.large`, and `c7g.xlarge`.
- **The AWS Academy Learner Lab is dead.** No `LabRole`, no pasted STS credentials, no `vockey`, no x86 `t3`, no `us-east-1` as a workload region. Nothing in this phase may reference them.
- **No static AWS credential path exists for this project's own principals.** Humans authenticate through IAM Identity Center; CI through GitHub OIDC; EC2 through instance profiles. The bootstrap script **verifies** this at runtime rather than asserting it in prose. Premortem C11 established that the unqualified claim is false at the organization level, and this phase corrects it.
- **The script installs nothing.** The build box simultaneously holds the AWS SSO refresh token, the W&B key, the Kaggle token, and the RunPod key. A single malicious post-install hook harvests all four (premortem C11). Neither the bootstrap nor its tests may run `pip install`, `npm install`, `apt-get install`, `brew install`, or `curl | sh`.
- **Blast radius is the `Sandbox` OU.** Exactly one operation in this script writes at organization-root scope — step 2, `enable-policy-type` — and it is gated behind an explicit acknowledgement flag that names the invariant it violates. Nothing else may write outside the OU. RCAP runs in the management account and is structurally immune to SCPs, so no policy this phase creates can reach it.
- **Root is break-glass and stays.** The script never calls `iam enable-organizations-root-credentials-management` and never deletes a root credential.
- **Solo developer, 19 days from 2026-07-30, public repository, human author.** No AI attribution in any commit, code comment, or document.
- **Every premortem finding assigned to this phase carries a task whose test fails if the finding is unfixed**, with the finding id in the task heading. A normative item without a failing test is a memo, and memos disappear under schedule pressure.

**Branch:** `feat/phase-a1-account-bootstrap` off `main`.

## Testing approach: why a pure-bash harness, not bats

This is shell, not Python, so `pytest` does not apply. The two candidates were bats-core and a shellcheck-plus-stub harness. The harness wins on three grounds, and the reasoning is recorded here so it is not re-litigated.

1. **C11 forbids the install.** `bats` is not present on the build box. `apt install bats` is version 1.2.1, five years stale, and needs `sudo`. `npm install bats` runs an unhashed third-party install on the machine that holds four live credentials — precisely the supply-chain path premortem C11 identifies as the highest-blast-radius risk in the project. Vendoring bats-core at a pinned commit is defensible but costs a submodule and buys nothing the harness below does not already provide.
2. **The hard part is not assertions, it is intercepting `aws`.** A test framework does not help with that. The real work is a PATH-shim fake `aws` that records argv, replays sequenced fixtures, and can be told to fail on the *n*th call. That stub is framework-agnostic, so choosing bats would leave the difficult half unchanged and add a dependency.
3. **Zero setup means the suite runs anywhere.** `make -C infra/aws test` works on a bare GitHub runner in Phase 4 with no install step, which keeps the CI gate cheap.

The harness is ~50 lines, emits TAP 13, and has a self-test that proves it can report a failure — because a test harness that cannot fail is the same defect class as premortem C2's tautological firewall gate.

Three layers, all real:

| Layer | What it proves | Where |
|---|---|---|
| `shellcheck -S style` | The shell is not subtly wrong | `make -C infra/aws lint` |
| Offline unit suite, stubbed `aws` | Control flow, idempotency, ordering, fail-closed paths, and the SCP document's exact content | `make -C infra/aws test` |
| Live acceptance suite, real account | The SCP actually denies, and actually permits the four classes the topology needs | `make -C infra/aws accept`, once, on day 1 |

## File Structure

- `infra/aws/bootstrap.sh` — the ten-step idempotent bootstrap. **Deliverable.**
- `infra/aws/scp-sandbox-guardrails.json` — the SCP attached to the `Sandbox` OU. **Deliverable.**
- `infra/aws/a2-constraints.json` — machine-readable constraints this phase's SCP imposes on Phase A2's Terraform, one row per premortem finding.
- `infra/aws/Makefile` — `lint`, `test`, `accept`, `bootstrap`.
- `infra/aws/tests/lib/harness.sh` — TAP assertion harness.
- `infra/aws/tests/lib/stubctx.sh` — per-test stub environment setup and teardown.
- `infra/aws/tests/stubs/{aws,terraform,gh}` — PATH-shim fakes.
- `infra/aws/tests/fixtures/<scenario>/*.json` — canned API responses per scenario.
- `infra/aws/tests/test_selftest.sh` — proves the harness can fail.
- `infra/aws/tests/test_stub.sh` — proves the stub records, sequences, and fails loudly.
- `infra/aws/tests/test_scp.sh` — the SCP document's content, offline.
- `infra/aws/tests/test_constraints.sh` — `a2-constraints.json` and its cross-file consistency with the SCP.
- `infra/aws/tests/test_preflight.sh` — credential hygiene and the installs-nothing guard.
- `infra/aws/tests/test_bootstrap_steps.sh` — per-step idempotency and ordering.
- `infra/aws/tests/test_bootstrap_full.sh` — whole-script runs, including the double-run zero-write proof.
- `infra/aws/tests/test_docs_claims.sh` — the C11 documentation corrections.
- `infra/aws/tests/acceptance/run_acceptance.sh` — the live day-1 suite.
- `infra/aws/bootstrap-outputs.env` — generated, mode 600, already gitignored.
- `infra/aws/acceptance-evidence.json` — generated, gitignored, carries the raw account id.

## Interfaces Produced (consumed by Phase A2 and Phase 5)

```
infra/aws/bootstrap-outputs.env      # sourced by A2; KEY=VALUE, LC_ALL=C sorted, mode 600
  ACCOUNT_ID=<12 digits>             # the member account
  AWS_REGION=us-west-2
  SANDBOX_OU_ID=ou-....
  SCP_POLICY_ID=p-....
  TF_STATE_BUCKET=rockcyber-mlops-toxic-tfstate-<account-id>
  ADMIN_PERMISSION_SET_ARN=arn:aws:sso:::permissionSet/ssoins-..../ps-....
  READONLY_PERMISSION_SET_ARN=arn:aws:sso:::permissionSet/ssoins-..../ps-....
  CREATE_ACCOUNT_REQUEST_ID=car-....
  UNGOVERNED_WINDOW_SECONDS=<int>    # seconds the account sat in the org root
  BREAK_GLASS_ESTABLISHED=<iso8601>

infra/aws/a2-constraints.json        # A2's Terraform must satisfy every row; A2's plan
                                     # carries one test per row
  .trail_bucket_prefix               -> the S3 ARN prefix the SCP protects
  .instance_type_allowlist[]         -> must equal the SCP allowlist exactly
  .constraints[] {id, finding, constraint, why}
```

`bootstrap.sh` exposes its step functions for testing. Sourcing it with `BOOTSTRAP_SOURCE_ONLY=1` defines every function and runs nothing.

---

### Task 1: Test harness scaffold, and prove it can fail (C11)

The harness exists before anything it tests. It installs nothing, which is this task's C11 obligation: every later test in this phase runs on a box holding four live credentials, and none of them may pull a package to do it.

**Files:**
- Create: `infra/aws/Makefile`, `infra/aws/tests/lib/harness.sh`
- Test: `infra/aws/tests/test_selftest.sh`

- [ ] **Step 1: Write the failing test**

`infra/aws/tests/test_selftest.sh`:
```bash
#!/usr/bin/env bash
# Proves the harness reports failures. A test harness that cannot fail is the same
# defect class as premortem C2's tautological firewall gate: it ships green while
# checking nothing.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib/harness.sh"

test_passing_assertions_pass() {
    assert_eq "a" "a"
    assert_contains "hello world" "lo wo"
    assert_not_contains "hello" "zzz"
    assert_rc 0 true
    assert_rc 3 bash -c 'exit 3'
}

test_harness_reports_a_failure() {
    local out
    out=$(bash -c '
        set -uo pipefail
        . '"$HERE"'/lib/harness.sh
        test_deliberately_fails() { assert_eq "got" "want"; }
        run_suite' 2>&1) && fail "nested suite exited 0 but its only test failed"
    assert_contains "$out" "not ok 1 - test_deliberately_fails"
    assert_contains "$out" "expected [want] got [got]"
}

test_harness_reports_a_crash() {
    local out
    out=$(bash -c '
        set -uo pipefail
        . '"$HERE"'/lib/harness.sh
        test_crashes() { exit 127; }
        run_suite' 2>&1) && fail "nested suite exited 0 but its only test crashed"
    assert_contains "$out" "test crashed with exit code 127"
}

run_suite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_selftest.sh`
Expected: FAIL with `infra/aws/tests/test_selftest.sh: line 7: /home/rock/github_projects/mlops-toxic-moderation/infra/aws/tests/lib/harness.sh: No such file or directory`

- [ ] **Step 3: Write minimal implementation**

`infra/aws/tests/lib/harness.sh`:
```bash
# shellcheck shell=bash
# Minimal TAP-13 assertion harness with zero third-party dependencies.
#
# Why not bats: this repository's build box simultaneously holds the AWS SSO refresh
# token, the W&B key, the Kaggle token, and the RunPod key (premortem C11). Installing
# a test framework there — apt, npm, or otherwise — is the exact supply-chain path that
# finding identifies. Fifty lines of bash costs less than the dependency.
#
# Usage: source this file, define test_* functions, call run_suite last.
# Assertions are the only failure channel; each returns 1 and appends to a per-test
# failure file that run_suite inspects.

_H_FAILFILE="${TMPDIR:-/tmp}/harness-unset"

_h_record() { printf '%s\n' "$1" >>"$_H_FAILFILE"; }

assert_eq() { # actual expected [message]
    if [ "$1" = "$2" ]; then return 0; fi
    _h_record "assert_eq: expected [$2] got [$1] ${3:-}"
    return 1
}

assert_contains() { # haystack needle [message]
    case "$1" in *"$2"*) return 0 ;; esac
    _h_record "assert_contains: [$2] not present ${3:-}"
    return 1
}

assert_not_contains() { # haystack needle [message]
    case "$1" in *"$2"*) ;; *) return 0 ;; esac
    _h_record "assert_not_contains: [$2] unexpectedly present ${3:-}"
    return 1
}

assert_rc() { # expected_rc command...
    local want="$1"; shift
    local got=0
    "$@" >/dev/null 2>&1 || got=$?
    if [ "$got" = "$want" ]; then return 0; fi
    _h_record "assert_rc: expected rc $want got $got from: $*"
    return 1
}

fail() { _h_record "$1"; return 1; }

run_suite() {
    local fns total=0 failed=0 fn rc errfile
    fns=$(declare -F | awk '{print $3}' | grep '^test_' | sort)
    printf 'TAP version 13\n'
    printf '1..%s\n' "$(printf '%s' "$fns" | grep -c . || true)"
    for fn in $fns; do
        total=$((total + 1))
        _H_FAILFILE=$(mktemp); errfile=$(mktemp)
        rc=0
        ( set +e; "$fn" ) >/dev/null 2>"$errfile" || rc=$?
        [ "$rc" -ge 126 ] && _h_record "test crashed with exit code $rc"
        if [ -s "$_H_FAILFILE" ]; then
            failed=$((failed + 1))
            printf 'not ok %d - %s\n' "$total" "$fn"
            sed 's/^/  # /' "$_H_FAILFILE"
            [ -s "$errfile" ] && sed 's/^/  # stderr: /' "$errfile"
        else
            printf 'ok %d - %s\n' "$total" "$fn"
        fi
        rm -f "$_H_FAILFILE" "$errfile"
    done
    printf '# %d tests, %d failures\n' "$total" "$failed"
    [ "$failed" -eq 0 ]
}
```

`infra/aws/Makefile` (tabs, not spaces, for recipe lines):
```makefile
.PHONY: lint test accept bootstrap
HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

lint:
	shellcheck -S style $(HERE)bootstrap.sh $(HERE)tests/stubs/* $(HERE)tests/acceptance/*.sh
	shellcheck -S style -e SC2148 $(HERE)tests/lib/*.sh
	shellcheck -S style -x $(HERE)tests/test_*.sh
	jq empty $(HERE)scp-sandbox-guardrails.json $(HERE)a2-constraints.json

test:
	@rc=0; for t in $(HERE)tests/test_*.sh; do \
	  echo "# $$t"; bash "$$t" || rc=1; \
	done; exit $$rc

accept:
	bash $(HERE)tests/acceptance/run_acceptance.sh

bootstrap:
	bash $(HERE)bootstrap.sh $(BOOTSTRAP_ARGS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_selftest.sh`
Expected:
```
TAP version 13
1..3
ok 1 - test_harness_reports_a_crash
ok 2 - test_harness_reports_a_failure
ok 3 - test_passing_assertions_pass
# 3 tests, 0 failures
```

- [ ] **Step 5: Commit**

```bash
git add infra/aws/Makefile infra/aws/tests/lib/harness.sh infra/aws/tests/test_selftest.sh
git commit -m "Add dependency-free TAP harness for the AWS bootstrap shell tests"
```

---

### Task 2: AWS CLI stub that records, sequences, and fails loudly

The stub is the reason the whole bootstrap is testable. It sits ahead of the real `aws` on `PATH`, writes every invocation to a call log, and replays a canned response keyed on `<service>_<operation>`. Sequenced fixtures (`.1.json`, `.2.json`) model asynchronous polling. A call with no fixture exits 90 rather than 0, so a forgotten fixture fails the test instead of silently passing it.

Two design rules fall out of the stub and both are enforced by tests later: `bootstrap.sh` parses every response with `jq` and never uses `--query` (the stub cannot emulate server-side JMESPath), and service and operation always precede any flag.

**Files:**
- Create: `infra/aws/tests/stubs/aws`, `infra/aws/tests/stubs/terraform`, `infra/aws/tests/stubs/gh`, `infra/aws/tests/lib/stubctx.sh`
- Test: `infra/aws/tests/test_stub.sh`

- [ ] **Step 1: Write the failing test**

`infra/aws/tests/test_stub.sh`:
```bash
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib/harness.sh"
. "$HERE/lib/stubctx.sh"

test_stub_reports_its_version_without_a_fixture() {
    stub_up
    assert_eq "$(aws --version)" "aws-cli/2.36.3 Python/3.14.6 Linux/6.0 exe/aarch64"
    stub_down
}

test_stub_records_service_and_operation_regardless_of_flags() {
    stub_up
    stub_fixture organizations_list-roots '{"Roots":[{"Id":"r-abcd"}]}'
    aws organizations list-roots --profile rc-mgmt --region us-west-2 >/dev/null
    assert_eq "$(stub_calls)" "organizations_list-roots"
    stub_down
}

test_stub_replays_json_parseable_by_jq() {
    stub_up
    stub_fixture organizations_list-roots '{"Roots":[{"Id":"r-abcd"}]}'
    assert_eq "$(aws organizations list-roots --profile rc-mgmt | jq -r '.Roots[0].Id')" "r-abcd"
    stub_down
}

test_stub_sequences_fixtures_for_asynchronous_polling() {
    stub_up
    stub_fixture organizations_describe-create-account-status.1 '{"CreateAccountStatus":{"State":"IN_PROGRESS"}}'
    stub_fixture organizations_describe-create-account-status.2 '{"CreateAccountStatus":{"State":"SUCCEEDED","AccountId":"123456789012"}}'
    local a b
    a=$(aws organizations describe-create-account-status --create-account-request-id car-1 | jq -r '.CreateAccountStatus.State')
    b=$(aws organizations describe-create-account-status --create-account-request-id car-1 | jq -r '.CreateAccountStatus.State')
    assert_eq "$a" "IN_PROGRESS"
    assert_eq "$b" "SUCCEEDED"
    stub_down
}

test_stub_honours_a_canned_exit_code() {
    stub_up
    stub_rc organizations_move-account 254
    assert_rc 254 aws organizations move-account --account-id 123456789012
    stub_down
}

test_stub_fails_loudly_on_a_missing_fixture() {
    stub_up
    assert_rc 90 aws ec2 describe-instances
    stub_down
}

test_terraform_and_gh_stubs_answer_the_preflight_probes() {
    stub_up
    assert_eq "$(terraform version -json | jq -r '.terraform_version')" "1.15.8"
    assert_rc 0 gh auth status
    stub_down
}

run_suite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_stub.sh`
Expected: FAIL with `infra/aws/tests/test_stub.sh: line 6: .../tests/lib/stubctx.sh: No such file or directory`

- [ ] **Step 3: Write minimal implementation**

`infra/aws/tests/stubs/aws`:
```bash
#!/usr/bin/env bash
# Fake `aws` CLI. Tests place this directory ahead of the real aws on PATH. It records
# every invocation to $AWS_STUB_CALLLOG and replays a canned response from
# $AWS_STUB_DIR/<service>_<operation>[.<n>].json, exiting with the code in a matching
# .rc file when one exists. A call with no fixture exits 90 loudly, so a missing fixture
# fails the test instead of silently passing it.
#
# Convention this stub imposes on bootstrap.sh: service and operation come before any
# flag, and responses are parsed with jq rather than --query.
set -u
: "${AWS_STUB_DIR:?AWS_STUB_DIR must be set}"
: "${AWS_STUB_CALLLOG:?AWS_STUB_CALLLOG must be set}"

if [ "${1:-}" = "--version" ]; then
    printf '%s\n' "${AWS_STUB_VERSION:-aws-cli/2.36.3 Python/3.14.6 Linux/6.0 exe/aarch64}"
    exit 0
fi

service=""; operation=""; skip=0
for a in "$@"; do
    if [ "$skip" -eq 1 ]; then skip=0; continue; fi
    case "$a" in
        --profile|--region|--output|--query|--access-token) skip=1; continue ;;
        --*) continue ;;
    esac
    if [ -z "$service" ]; then service="$a"
    elif [ -z "$operation" ]; then operation="$a"; fi
done

key="${service}_${operation}"
printf '%s %s\n' "$key" "$*" >>"$AWS_STUB_CALLLOG"
n=$(grep -c "^${key} " "$AWS_STUB_CALLLOG")

resp="$AWS_STUB_DIR/${key}.${n}.json"; [ -f "$resp" ] || resp="$AWS_STUB_DIR/${key}.json"
rcf="$AWS_STUB_DIR/${key}.${n}.rc";   [ -f "$rcf" ]  || rcf="$AWS_STUB_DIR/${key}.rc"

if [ -f "$resp" ]; then
    cat "$resp"
elif [ ! -f "$rcf" ]; then
    printf 'aws-stub: no fixture for %s (call #%s)\n' "$key" "$n" >&2
    exit 90
fi
[ -f "$rcf" ] && exit "$(cat "$rcf")"
exit 0
```

`infra/aws/tests/stubs/terraform`:
```bash
#!/usr/bin/env bash
# Fake `terraform`, present only so the bootstrap preflight's version probe is hermetic.
set -u
if [ "${1:-}" = "version" ]; then
    printf '{"terraform_version":"%s"}\n' "${TERRAFORM_STUB_VERSION:-1.15.8}"
    exit 0
fi
exit 0
```

`infra/aws/tests/stubs/gh`:
```bash
#!/usr/bin/env bash
# Fake `gh`, present only so the bootstrap preflight's auth probe is hermetic.
set -u
exit "${GH_STUB_RC:-0}"
```

`infra/aws/tests/lib/stubctx.sh`:
```bash
# shellcheck shell=bash
# Per-test stub environment. stub_up creates a scratch fixture directory and call log
# and puts the stubs first on PATH; stub_down restores PATH and removes the scratch.

STUBCTX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

stub_up() {
    _STUB_OLD_PATH="$PATH"
    _STUB_ROOT=$(mktemp -d)
    export AWS_STUB_DIR="$_STUB_ROOT/fixtures"
    export AWS_STUB_CALLLOG="$_STUB_ROOT/calls.log"
    mkdir -p "$AWS_STUB_DIR"
    : >"$AWS_STUB_CALLLOG"
    export PATH="$STUBCTX_DIR/stubs:$PATH"
    export BOOTSTRAP_OUTPUTS_FILE="$_STUB_ROOT/bootstrap-outputs.env"
    export BOOTSTRAP_POLL_INTERVAL=0
    export BOOTSTRAP_CREATE_ACCOUNT_TIMEOUT=3
    export BOOTSTRAP_ALT_CONTACT_PHONE="+13035550100"
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
}

stub_down() {
    PATH="$_STUB_OLD_PATH"
    export PATH
    rm -rf "$_STUB_ROOT"
}

# stub_fixture <key> <json>   e.g. stub_fixture organizations_list-roots '{"Roots":[]}'
# A key ending in .N is the Nth response for that operation.
stub_fixture() { printf '%s' "$2" >"$AWS_STUB_DIR/$1.json"; }

# stub_rc <key> <exit-code>
stub_rc() { printf '%s' "$2" >"$AWS_STUB_DIR/$1.rc"; }

# stub_calls -> newline-separated <service>_<operation> in call order
stub_calls() { awk '{print $1}' "$AWS_STUB_CALLLOG"; }

# stub_call_args <key> -> the full argv of the first call to that operation
stub_call_args() { grep -m1 "^$1 " "$AWS_STUB_CALLLOG" || true; }

# stub_scenario <name> -> copy a whole fixture directory in
stub_scenario() { cp "$STUBCTX_DIR/fixtures/$1/"* "$AWS_STUB_DIR/"; }

# stub_index <key> -> 1-based call index of the first call, or empty
stub_index() { stub_calls | grep -n "^$1$" | head -1 | cut -d: -f1; }

# stub_last_index <key> -> 1-based call index of the last call, or empty
stub_last_index() { stub_calls | grep -n "^$1$" | tail -1 | cut -d: -f1; }

# stub_call_at <n> -> the operation at 1-based call index n
stub_call_at() { stub_calls | sed -n "$1p"; }

# stub_writes -> every mutating call made, in order
stub_writes() {
    stub_calls | grep -E '_(create|update|delete|attach|detach|move|put|enable|provision|tag|register)' || true
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `chmod +x infra/aws/tests/stubs/* && bash infra/aws/tests/test_stub.sh`
Expected: `1..7` and `# 7 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/tests/stubs infra/aws/tests/lib/stubctx.sh infra/aws/tests/test_stub.sh
git commit -m "Add PATH-shim AWS CLI stub with sequenced fixtures and a call log"
```

---

### Task 3 (H3): Enumerate the SCP instance-type allowlist, resource-scoped

H3: the allowlist was never written down in any artifact, so the Terraform author would have derived it from a sizing table that omits `t4g.small` and `c7g.xlarge`. Authored day 1, it fails on day 9–11 as an opaque `UnauthorizedOperation`. This task writes it down, in the one place that is machine-checkable, and pins the resource-scoping trap alongside it: `ec2:InstanceType` is a **resource-level** key on the `instance` resource, so scoping the statement to `Resource: "*"` denies every launch including the intended ones, because the volume and network interface the same call creates carry no `ec2:InstanceType` and a `StringNotEquals` test on an absent key evaluates true.

**Files:**
- Create: `infra/aws/scp-sandbox-guardrails.json`
- Test: `infra/aws/tests/test_scp.sh`

- [ ] **Step 1: Write the failing test**

`infra/aws/tests/test_scp.sh`:
```bash
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib/harness.sh"
SCP="$HERE/../scp-sandbox-guardrails.json"

sid() { jq -c --arg s "$1" '.Statement[] | select(.Sid==$s)' "$SCP"; }

test_document_is_valid_policy_json() {
    assert_rc 0 jq empty "$SCP"
    assert_eq "$(jq -r '.Version' "$SCP")" "2012-10-17"
    assert_eq "$(jq -r '.Statement | type' "$SCP")" "array"
    assert_eq "$(jq -r '[.Statement[] | select(.Effect != "Deny")] | length' "$SCP")" "0" \
        "an SCP that Allows anything is a filter that grants nothing and confuses review"
}

test_every_statement_has_a_unique_sid() {
    local n u
    n=$(jq -r '.Statement | length' "$SCP")
    u=$(jq -r '[.Statement[].Sid] | unique | length' "$SCP")
    assert_eq "$u" "$n"
}

# H3: the allowlist is exactly the four classes the three-instance topology needs.
test_instance_type_allowlist_is_exactly_the_four_required_classes() {
    local got
    got=$(sid DenyNonAllowlistedInstanceLaunch \
          | jq -c '.Condition.StringNotEquals["ec2:InstanceType"] | sort')
    assert_eq "$got" '["c7g.xlarge","t4g.large","t4g.medium","t4g.small"]' \
        "t4g.small runs the frontend; c7g.xlarge is the sanctioned upsize target"
}

# H3: the resource-scoping trap. Scoping to "*" denies every launch, including ours.
test_instance_type_statement_is_scoped_to_the_instance_resource() {
    assert_eq "$(sid DenyNonAllowlistedInstanceLaunch | jq -r '.Resource')" \
        "arn:aws:ec2:*:*:instance/*"
}

test_allowlist_uses_stringnotequals_not_stringnotlike() {
    local keys
    keys=$(sid DenyNonAllowlistedInstanceLaunch | jq -r '.Condition | keys | join(",")')
    assert_eq "$keys" "StringNotEquals" \
        "wildcards would readmit t4g.2xlarge and every other size in the family"
}

run_suite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_scp.sh`
Expected: FAIL — `1..5` with all five `not ok`, each recording `assert_rc: expected rc 0 got 2 from: jq empty .../scp-sandbox-guardrails.json` or `assert_eq: expected [...] got []`, because the document does not exist.

- [ ] **Step 3: Write minimal implementation**

`infra/aws/scp-sandbox-guardrails.json`:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyNonAllowlistedInstanceLaunch",
      "Effect": "Deny",
      "Action": [
        "ec2:RunInstances"
      ],
      "Resource": "arn:aws:ec2:*:*:instance/*",
      "Condition": {
        "StringNotEquals": {
          "ec2:InstanceType": [
            "t4g.small",
            "t4g.medium",
            "t4g.large",
            "c7g.xlarge"
          ]
        }
      }
    }
  ]
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_scp.sh`
Expected: `1..5` and `# 5 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/scp-sandbox-guardrails.json infra/aws/tests/test_scp.sh
git commit -m "Add Sandbox OU SCP with the enumerated Graviton instance-type allowlist"
```

---

### Task 4 (H19): Deny every launch path, and deny instance-type mutation outright

H19: the SCP does not deny what the foundation spec claims. `ec2:RunInstances` is one launch path among several. `CreateFleet`, `RequestSpotInstances`, and `RequestSpotFleet` each reach any instance type without calling `RunInstances`, and `ModifyInstanceAttribute` — **the plan's own documented resize workflow** — reaches any type on an existing instance.

`ModifyInstanceAttribute` needs a different treatment from the other three, and getting this wrong is the whole finding. On `RunInstances`, `CreateFleet`, `RequestSpotInstances`, and `RequestSpotFleet` the instance is being created, so `ec2:InstanceType` in the request context is the **requested** type and a condition constrains it. On `ModifyInstanceAttribute` the instance already exists, so `ec2:InstanceType` resolves to the instance's **current** type. Conditioning there is inert: a `t4g.medium` being modified to a GPU class presents `t4g.medium` to the condition, passes the allowlist, and the deny never fires. The only effective SCP control is an unconditional deny, so the resize path becomes a Terraform-driven replacement through `RunInstances`, which the allowlist already guards. That is a real constraint on Phase A2's Terraform and Task 8 records it.

**Files:**
- Modify: `infra/aws/scp-sandbox-guardrails.json`
- Modify: `infra/aws/tests/test_scp.sh`

- [ ] **Step 1: Write the failing test**

Append to `infra/aws/tests/test_scp.sh`, before the final `run_suite` line:
```bash
# H19: RunInstances is one launch path among four.
test_all_four_launch_paths_carry_the_allowlist() {
    local got
    got=$(sid DenyNonAllowlistedInstanceLaunch | jq -c '.Action | sort')
    assert_eq "$got" '["ec2:CreateFleet","ec2:RequestSpotFleet","ec2:RequestSpotInstances","ec2:RunInstances"]'
}

# H19: ec2:InstanceType on ModifyInstanceAttribute is the CURRENT type, not the
# requested one, so any condition there is inert. The deny must be unconditional.
test_modify_instance_attribute_is_denied_without_a_condition() {
    local st
    st=$(sid DenyInstanceAttributeMutation)
    assert_eq "$(printf '%s' "$st" | jq -c '.Action')" '["ec2:ModifyInstanceAttribute"]'
    assert_eq "$(printf '%s' "$st" | jq -r '.Resource')" "arn:aws:ec2:*:*:instance/*"
    assert_eq "$(printf '%s' "$st" | jq -r 'has("Condition")')" "false" \
        "a condition on ec2:InstanceType here reads the current type and never fires"
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_scp.sh`
Expected: FAIL —
```
not ok 1 - test_all_four_launch_paths_carry_the_allowlist
  # assert_eq: expected [["ec2:CreateFleet","ec2:RequestSpotFleet","ec2:RequestSpotInstances","ec2:RunInstances"]] got [["ec2:RunInstances"]]
not ok 5 - test_modify_instance_attribute_is_denied_without_a_condition
  # assert_eq: expected [["ec2:ModifyInstanceAttribute"]] got []
```

- [ ] **Step 3: Write minimal implementation**

In `infra/aws/scp-sandbox-guardrails.json`, replace the `Action` array of `DenyNonAllowlistedInstanceLaunch` and append one statement:
```json
      "Action": [
        "ec2:RunInstances",
        "ec2:CreateFleet",
        "ec2:RequestSpotInstances",
        "ec2:RequestSpotFleet"
      ],
```
```json
    {
      "Sid": "DenyInstanceAttributeMutation",
      "Effect": "Deny",
      "Action": [
        "ec2:ModifyInstanceAttribute"
      ],
      "Resource": "arn:aws:ec2:*:*:instance/*"
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_scp.sh`
Expected: `1..7` and `# 7 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/scp-sandbox-guardrails.json infra/aws/tests/test_scp.sh
git commit -m "Deny fleet, spot, and attribute-mutation launch paths in the sandbox SCP"
```

---

### Task 5 (H18): Get Bool versus BoolIfExists right on the two RDS require statements, and test it

H18: the two RDS "require" statements repeat the exact key-absence trap the foundation spec congratulates itself for catching. The semantics are unforgiving and the correct answer differs per action, which is why prose alone never settles it.

| Operator | Key present, matching value | Key present, other value | Key absent |
|---|---|---|---|
| `Bool` | matches → Deny fires | no match | **no match → fails open** |
| `BoolIfExists` | matches → Deny fires | no match | **matches → fails closed** |

Applying that to the three RDS statements:

- **`CreateDBInstance` without a Secrets-Manager-managed password.** Deny on `ManageMasterUserPassword` false. `Bool` fails open: a caller that simply omits the parameter — which is exactly what Terraform does when a `random_password` is supplied instead — sails through. `BoolIfExists` fails closed. The spec worried that `BoolIfExists` would deny *every* `CreateDBInstance`; it does not, because `manage_master_user_password = true` makes the provider send `ManageMasterUserPassword: true`, so the key is present and unequal to `false`. **`BoolIfExists` is correct.**
- **`CreateDBInstance` / `RestoreDBInstanceFromDBSnapshot` with a public endpoint.** Absence is dangerous here too, because RDS's own default when the key is absent is not universally `false`. Terraform always sends `PubliclyAccessible` on create. **`BoolIfExists` is correct.**
- **`ModifyDBInstance` to a public endpoint.** Absence means "do not change this attribute", which is safe, and Terraform sends only changed attributes — so `BoolIfExists` here would deny every unrelated modification, such as bumping `backup_retention_period`. **`Bool` is correct.**

The same operator in three places would be wrong in at least one of them. The offline test pins the operator per statement; the live test in Task 16 proves the semantics with `iam simulate-custom-policy` across the full present-true / present-false / absent matrix.

**Files:**
- Modify: `infra/aws/scp-sandbox-guardrails.json`
- Modify: `infra/aws/tests/test_scp.sh`

- [ ] **Step 1: Write the failing test**

Append to `infra/aws/tests/test_scp.sh`, before `run_suite`:
```bash
# H18: absence must fail CLOSED on create. Bool fails open and lets a Terraform
# random_password through, which is the outcome the guardrail exists to prevent.
test_rds_managed_password_requirement_fails_closed_on_key_absence() {
    local st
    st=$(sid DenyRdsCreateWithoutManagedMasterPassword)
    assert_eq "$(printf '%s' "$st" | jq -c '.Action')" '["rds:CreateDBInstance"]'
    assert_eq "$(printf '%s' "$st" | jq -c '.Condition')" \
        '{"BoolIfExists":{"rds:ManageMasterUserPassword":"false"}}'
    assert_eq "$(printf '%s' "$st" | jq -r '.Condition | has("Bool")')" "false" \
        "Bool would not fire when the caller omits the parameter"
}

# H18: absence must fail CLOSED on create, because RDS's own default when the key
# is absent is not universally false.
test_rds_public_endpoint_denial_fails_closed_on_create() {
    local st
    st=$(sid DenyRdsCreatePubliclyAccessible)
    assert_eq "$(printf '%s' "$st" | jq -c '.Action | sort')" \
        '["rds:CreateDBInstance","rds:RestoreDBInstanceFromDBSnapshot"]'
    assert_eq "$(printf '%s' "$st" | jq -c '.Condition')" \
        '{"BoolIfExists":{"rds:PubliclyAccessible":"true"}}'
}

# H18: absence on MODIFY means "leave this attribute alone", which is safe.
# BoolIfExists here would deny every unrelated ModifyDBInstance and break A2.
test_rds_modify_uses_bool_because_absence_means_no_change() {
    local st
    st=$(sid DenyRdsModifyToPubliclyAccessible)
    assert_eq "$(printf '%s' "$st" | jq -c '.Action')" '["rds:ModifyDBInstance"]'
    assert_eq "$(printf '%s' "$st" | jq -c '.Condition')" \
        '{"Bool":{"rds:PubliclyAccessible":"true"}}'
    assert_eq "$(printf '%s' "$st" | jq -r '.Condition | has("BoolIfExists")')" "false" \
        "BoolIfExists would deny bumping backup_retention_period"
}

# The foundation spec's verified refutation: rds:DatabaseClass is unsupported on
# CreateDBInstance and would deny every RDS creation.
test_no_statement_conditions_on_rds_databaseclass() {
    assert_not_contains "$(jq -c . "$SCP")" "rds:DatabaseClass" \
        "unsupported on CreateDBInstance; the key would be absent and deny everything"
}

test_aurora_clusters_are_denied_outright() {
    assert_eq "$(sid DenyAuroraClusters | jq -c '.Action | sort')" \
        '["rds:CreateDBCluster","rds:RestoreDBClusterFromSnapshot","rds:RestoreDBClusterToPointInTime"]'
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_scp.sh`
Expected: FAIL — four new `not ok` lines, the first recording
`assert_eq: expected [["rds:CreateDBInstance"]] got []` because `DenyRdsCreateWithoutManagedMasterPassword` does not exist. `test_no_statement_conditions_on_rds_databaseclass` passes already; that is intended — it is a regression guard, not a driver.

- [ ] **Step 3: Write minimal implementation**

Append four statements to `infra/aws/scp-sandbox-guardrails.json`:
```json
    {
      "Sid": "DenyRdsCreateWithoutManagedMasterPassword",
      "Effect": "Deny",
      "Action": [
        "rds:CreateDBInstance"
      ],
      "Resource": "*",
      "Condition": {
        "BoolIfExists": {
          "rds:ManageMasterUserPassword": "false"
        }
      }
    },
    {
      "Sid": "DenyRdsCreatePubliclyAccessible",
      "Effect": "Deny",
      "Action": [
        "rds:CreateDBInstance",
        "rds:RestoreDBInstanceFromDBSnapshot"
      ],
      "Resource": "*",
      "Condition": {
        "BoolIfExists": {
          "rds:PubliclyAccessible": "true"
        }
      }
    },
    {
      "Sid": "DenyRdsModifyToPubliclyAccessible",
      "Effect": "Deny",
      "Action": [
        "rds:ModifyDBInstance"
      ],
      "Resource": "*",
      "Condition": {
        "Bool": {
          "rds:PubliclyAccessible": "true"
        }
      }
    },
    {
      "Sid": "DenyAuroraClusters",
      "Effect": "Deny",
      "Action": [
        "rds:CreateDBCluster",
        "rds:RestoreDBClusterFromSnapshot",
        "rds:RestoreDBClusterToPointInTime"
      ],
      "Resource": "*"
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_scp.sh`
Expected: `1..12` and `# 12 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/scp-sandbox-guardrails.json infra/aws/tests/test_scp.sh
git commit -m "Fix RDS SCP key-absence semantics: BoolIfExists on create, Bool on modify"
```

---

### Task 6 (H17): Complete the detective controls and protect the trail's evidence

H17: denying `cloudtrail:StopLogging`, `cloudtrail:DeleteTrail`, `guardduty:Delete*`, and `guardduty:Disassociate*` leaves the objective unmet. `guardduty:UpdateDetector` with `enable=false` disables GuardDuty without deleting it. `cloudtrail:UpdateTrail` redirects the trail to another bucket. `cloudtrail:PutEventSelectors` narrows it to nothing. And the trail's own S3 bucket is in-account, so `s3:DeleteObject` destroys the evidence without touching CloudTrail at all.

Two deliberate limits, both recorded in Task 8's constraints file rather than left as surprises:

- `s3:PutBucketVersioning` and `s3:PutBucketPolicy` stay **allowed**, because Terraform needs both to create the trail bucket in the first place. Suspending versioning does not destroy existing objects, and CloudTrail log file validation — an A2 obligation — detects modification.
- The delete denial means `terraform destroy` cannot empty the trail bucket. That is correct behaviour for an evidence store and a genuine conflict with cost control #2, so A2 must exclude the trail bucket from destroy (`force_destroy = false`) and removal, if ever needed, happens from the management account after detaching the SCP.

The bucket ARN prefix is hardcoded in the SCP, which couples this file to a Terraform name that does not exist yet. Task 8 makes that coupling a tested invariant instead of a latent break.

**Files:**
- Modify: `infra/aws/scp-sandbox-guardrails.json`
- Modify: `infra/aws/tests/test_scp.sh`

- [ ] **Step 1: Write the failing test**

Append to `infra/aws/tests/test_scp.sh`, before `run_suite`:
```bash
# H17: disable-without-delete and redirect-the-trail are the real evasions.
test_detective_control_denies_cover_disable_and_redirect() {
    local acts
    acts=$(sid DenyDetectiveControlTampering | jq -r '.Action[]')
    for a in cloudtrail:StopLogging cloudtrail:DeleteTrail cloudtrail:UpdateTrail \
             cloudtrail:PutEventSelectors cloudtrail:PutInsightSelectors \
             guardduty:DeleteDetector guardduty:UpdateDetector \
             guardduty:DisassociateFromAdministratorAccount guardduty:DisassociateMembers \
             guardduty:DeleteMembers guardduty:StopMonitoringMembers \
             guardduty:DeletePublishingDestination guardduty:UpdatePublishingDestination; do
        assert_contains "$acts" "$a" "undenied detective-control action"
    done
}

# H17: the trail bucket is in-account, so evidence dies without touching CloudTrail.
test_trail_bucket_evidence_deletion_is_denied() {
    local st acts res
    st=$(sid DenyTrailEvidenceDestruction)
    acts=$(printf '%s' "$st" | jq -r '.Action[]')
    for a in s3:DeleteBucket s3:DeleteObject s3:DeleteObjectVersion \
             s3:DeleteBucketPolicy s3:PutLifecycleConfiguration; do
        assert_contains "$acts" "$a"
    done
    res=$(printf '%s' "$st" | jq -c '.Resource | sort')
    assert_eq "$res" '["arn:aws:s3:::rockcyber-mlops-toxic-cloudtrail-*","arn:aws:s3:::rockcyber-mlops-toxic-cloudtrail-*/*"]' \
        "both the bucket and its objects, or half the evidence is unprotected"
}

# Terraform must still be able to CREATE that bucket under this SCP.
test_trail_bucket_creation_verbs_stay_permitted() {
    local acts
    acts=$(sid DenyTrailEvidenceDestruction | jq -r '.Action[]')
    assert_not_contains "$acts" "s3:PutBucketVersioning" "A2 needs it at create time"
    assert_not_contains "$acts" "s3:PutBucketPolicy" "A2 needs it for the CloudTrail write policy"
    assert_not_contains "$acts" "s3:CreateBucket"
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_scp.sh`
Expected: FAIL —
```
not ok 2 - test_detective_control_denies_cover_disable_and_redirect
  # assert_contains: [cloudtrail:StopLogging] not present undenied detective-control action
  ... (13 records)
not ok 13 - test_trail_bucket_evidence_deletion_is_denied
  # assert_contains: [s3:DeleteBucket] not present
```
`test_trail_bucket_creation_verbs_stay_permitted` passes vacuously against the missing statement; it is a regression guard.

- [ ] **Step 3: Write minimal implementation**

Append two statements to `infra/aws/scp-sandbox-guardrails.json`:
```json
    {
      "Sid": "DenyDetectiveControlTampering",
      "Effect": "Deny",
      "Action": [
        "cloudtrail:StopLogging",
        "cloudtrail:DeleteTrail",
        "cloudtrail:UpdateTrail",
        "cloudtrail:PutEventSelectors",
        "cloudtrail:PutInsightSelectors",
        "guardduty:DeleteDetector",
        "guardduty:UpdateDetector",
        "guardduty:DeleteMembers",
        "guardduty:DisassociateFromAdministratorAccount",
        "guardduty:DisassociateMembers",
        "guardduty:DeletePublishingDestination",
        "guardduty:UpdatePublishingDestination",
        "guardduty:StopMonitoringMembers"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DenyTrailEvidenceDestruction",
      "Effect": "Deny",
      "Action": [
        "s3:DeleteBucket",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:DeleteBucketPolicy",
        "s3:PutLifecycleConfiguration"
      ],
      "Resource": [
        "arn:aws:s3:::rockcyber-mlops-toxic-cloudtrail-*",
        "arn:aws:s3:::rockcyber-mlops-toxic-cloudtrail-*/*"
      ]
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_scp.sh`
Expected: `1..15` and `# 15 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/scp-sandbox-guardrails.json infra/aws/tests/test_scp.sh
git commit -m "Close detective-control gaps and protect the CloudTrail evidence bucket"
```

---

### Task 7 (H19, C11): Region lock without a wholesale us-east-1, static-credential denials, and the quota gate

Two findings close here.

H19's last clause: `us-east-1` was allowed wholesale with only EC2 and RDS constrained, which leaves every other service free to sprawl into a second region. It was allowed because IAM, Organizations, billing, Route 53, and CloudFront present `us-east-1` as their `aws:RequestedRegion`, and denying it breaks `terraform apply` on `iam.tf`. The correct construction is the AWS-documented region lock: one Deny on `StringNotEquals aws:RequestedRegion us-west-2`, with a `NotAction` list naming exactly the global services that must be exempt. `us-east-1` is then not a permitted region for anything regional — which also satisfies the binding owner decision that `us-east-1` is dead.

C11's enforcement half: "no static credentials" becomes an enforced property here rather than a convention. `iam:CreateUser`, `iam:CreateAccessKey`, `iam:UpdateAccessKey`, `iam:CreateLoginProfile`, `iam:UpdateLoginProfile`, and `iam:CreateServiceSpecificCredential` are all denied, so the member account cannot mint a long-lived credential even by mistake.

The task also lands the two structural gates the SCP itself is subject to: the 5,120-byte quota on a policy document, and an escape denial on `organizations:LeaveOrganization`.

**Files:**
- Modify: `infra/aws/scp-sandbox-guardrails.json`
- Modify: `infra/aws/tests/test_scp.sh`

- [ ] **Step 1: Write the failing test**

Append to `infra/aws/tests/test_scp.sh`, before `run_suite`:
```bash
# H19: us-east-1 must not be a permitted region. Global services are exempted by
# NotAction instead, which is the AWS-documented region-lock construction.
test_region_lock_permits_only_us_west_2() {
    local st
    st=$(sid DenyOutsideHomeRegion)
    assert_eq "$(printf '%s' "$st" | jq -r '.Condition.StringNotEquals["aws:RequestedRegion"] | if type=="array" then join(",") else . end')" \
        "us-west-2"
    assert_eq "$(printf '%s' "$st" | jq -r 'has("Action")')" "false" \
        "an Action list here would leave every unlisted service unconstrained"
    assert_not_contains "$(printf '%s' "$st" | jq -c '.')" "us-east-1"
}

test_region_lock_exempts_the_global_services_terraform_needs() {
    local na
    na=$(sid DenyOutsideHomeRegion | jq -r '.NotAction[]')
    for a in "iam:*" "sts:*" "organizations:*" "account:*" "budgets:*" "ce:*" \
             "support:*" "health:*" "cloudfront:*" "route53:*" \
             "s3:ListAllMyBuckets" "s3:GetBucketLocation"; do
        assert_contains "$na" "$a" "denying this breaks terraform apply on iam.tf or the budget"
    done
}

test_region_lock_does_not_exempt_regional_workload_services() {
    local na
    na=$(sid DenyOutsideHomeRegion | jq -r '.NotAction | join(",")')
    for a in "ec2:" "rds:" "ecr:" "ssm:" "secretsmanager:" "logs:" "cloudtrail:" "guardduty:"; do
        assert_not_contains "$na" "$a" "exempting a regional service reopens region sprawl"
    done
}

# C11: make "no static credentials" an enforced property of the account.
test_static_credential_creation_is_denied() {
    local acts
    acts=$(sid DenyStaticCredentialCreation | jq -r '.Action[]')
    for a in iam:CreateUser iam:CreateAccessKey iam:UpdateAccessKey \
             iam:CreateLoginProfile iam:UpdateLoginProfile \
             iam:CreateServiceSpecificCredential; do
        assert_contains "$acts" "$a"
    done
}

test_organization_escape_is_denied() {
    assert_eq "$(sid DenyOrganizationEscape | jq -c '.Action | sort')" \
        '["account:CloseAccount","organizations:LeaveOrganization"]'
}

# AWS caps an SCP document at 5120 bytes with whitespace excluded.
test_policy_fits_the_scp_size_quota() {
    local bytes
    bytes=$(jq -c . "$SCP" | tr -d '\n' | wc -c)
    assert_eq "$(( bytes < 5120 ))" "1" "compacted document is ${bytes} bytes, quota is 5120"
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_scp.sh`
Expected: FAIL —
```
not ok 9 - test_region_lock_permits_only_us_west_2
  # assert_eq: expected [us-west-2] got []
not ok 14 - test_static_credential_creation_is_denied
  # assert_contains: [iam:CreateUser] not present
not ok 8 - test_organization_escape_is_denied
  # assert_eq: expected [["account:CloseAccount","organizations:LeaveOrganization"]] got []
```
`test_policy_fits_the_scp_size_quota` passes throughout; it is the regression guard that catches a future statement pushing the document over quota.

- [ ] **Step 3: Write minimal implementation**

Insert `DenyOutsideHomeRegion` as the **first** statement of `infra/aws/scp-sandbox-guardrails.json` and append the other two:
```json
    {
      "Sid": "DenyOutsideHomeRegion",
      "Effect": "Deny",
      "NotAction": [
        "iam:*",
        "sts:*",
        "organizations:*",
        "account:*",
        "budgets:*",
        "ce:*",
        "support:*",
        "health:*",
        "cloudfront:*",
        "route53:*",
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation"
      ],
      "Resource": "*",
      "Condition": {
        "StringNotEquals": {
          "aws:RequestedRegion": "us-west-2"
        }
      }
    },
```
```json
    {
      "Sid": "DenyStaticCredentialCreation",
      "Effect": "Deny",
      "Action": [
        "iam:CreateUser",
        "iam:CreateAccessKey",
        "iam:UpdateAccessKey",
        "iam:CreateLoginProfile",
        "iam:UpdateLoginProfile",
        "iam:CreateServiceSpecificCredential"
      ],
      "Resource": "*"
    },
    {
      "Sid": "DenyOrganizationEscape",
      "Effect": "Deny",
      "Action": [
        "organizations:LeaveOrganization",
        "account:CloseAccount"
      ],
      "Resource": "*"
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_scp.sh && jq -c . infra/aws/scp-sandbox-guardrails.json | wc -c`
Expected: `1..21`, `# 21 tests, 0 failures`, then `2660` — the compacted document is 2660 bytes against the 5120-byte quota, leaving room for later additions.

- [ ] **Step 5: Commit**

```bash
git add infra/aws/scp-sandbox-guardrails.json infra/aws/tests/test_scp.sh
git commit -m "Lock the sandbox to us-west-2 and deny static-credential creation"
```

---

### Task 8 (H17, H18, H19, H3): Hand Phase A2 a machine-checkable constraints file

Three of this phase's SCP statements only work if Phase A2's Terraform is written a particular way, and every one of those couplings is currently invisible to A2. The trail-bucket ARN prefix is hardcoded in the SCP against a bucket name that does not exist yet; the unconditional `ModifyInstanceAttribute` deny breaks any post-create attribute change; the `BoolIfExists` RDS statements require Terraform to send both keys explicitly. Left as prose, each of these becomes an opaque `AccessDenied` during the most compressed days of the project.

`a2-constraints.json` turns them into data, and two of its tests compare that data against the SCP itself, so drift between the two files fails the suite rather than the deploy. Phase A2's plan carries one test per row.

CloudTrail log file validation is on the list even though no SCP can enforce it: `cloudtrail:CreateTrail` has no condition key for it, so the only place it can be made auditable from this phase is here.

**Files:**
- Create: `infra/aws/a2-constraints.json`
- Test: `infra/aws/tests/test_constraints.sh`

- [ ] **Step 1: Write the failing test**

`infra/aws/tests/test_constraints.sh`:
```bash
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib/harness.sh"
CON="$HERE/../a2-constraints.json"
SCP="$HERE/../scp-sandbox-guardrails.json"

test_constraints_file_is_well_formed() {
    assert_rc 0 jq empty "$CON"
    assert_eq "$(jq -r '.constraints | type' "$CON")" "array"
    assert_eq "$(jq -r '[.constraints[] | select((.id|length)==0 or (.finding|length)==0 or (.constraint|length)==0 or (.why|length)==0)] | length' "$CON")" "0" \
        "every row needs an id, an owning finding, the constraint, and why it exists"
    assert_eq "$(jq -r '[.constraints[].id] | unique | length' "$CON")" \
              "$(jq -r '.constraints | length' "$CON")"
}

test_every_finding_this_phase_owns_has_at_least_one_constraint_or_is_scp_only() {
    local findings
    findings=$(jq -r '[.constraints[].finding] | unique | join(",")' "$CON")
    for f in H3 H17 H18 H19 C6; do
        assert_contains "$findings" "$f" "no A2 constraint carries finding $f"
    done
}

# The SCP hardcodes an S3 ARN prefix for a bucket A2 has not created yet. If the two
# files disagree, the trail bucket is silently unprotected.
test_trail_bucket_prefix_matches_the_scp_arn_pattern() {
    local prefix scp_res
    prefix=$(jq -r '.trail_bucket_prefix' "$CON")
    scp_res=$(jq -r '.Statement[] | select(.Sid=="DenyTrailEvidenceDestruction") | .Resource[]' "$SCP" | sort | head -1)
    assert_eq "$scp_res" "arn:aws:s3:::${prefix}*"
}

# The SCP allowlist and the list A2 sizes instances from must be the same list.
test_instance_type_allowlist_matches_the_scp() {
    local con_list scp_list
    con_list=$(jq -c '.instance_type_allowlist | sort' "$CON")
    scp_list=$(jq -c '.Statement[] | select(.Sid=="DenyNonAllowlistedInstanceLaunch")
                      | .Condition.StringNotEquals["ec2:InstanceType"] | sort' "$SCP")
    assert_eq "$con_list" "$scp_list"
}

test_the_three_instances_are_named_with_their_classes() {
    local roles
    roles=$(jq -r '.instances[] | "\(.role)=\(.instance_type)"' "$CON" | sort | tr '\n' ' ')
    assert_eq "$roles" "backend=t4g.medium frontend=t4g.small monitoring=t4g.medium "
}

run_suite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_constraints.sh`
Expected: FAIL — `1..5` with all five `not ok`, the first recording
`assert_rc: expected rc 0 got 2 from: jq empty .../a2-constraints.json`

- [ ] **Step 3: Write minimal implementation**

`infra/aws/a2-constraints.json`:
```json
{
  "produced_by": "Phase A1, infra/aws/scp-sandbox-guardrails.json",
  "consumed_by": "Phase A2, infra/terraform/",
  "purpose": "Constraints the Sandbox OU SCP places on A2's Terraform. A2's plan carries one test per row. Violating any row surfaces as an opaque AccessDenied during terraform apply.",
  "trail_bucket_prefix": "rockcyber-mlops-toxic-cloudtrail-",
  "instance_type_allowlist": ["t4g.small", "t4g.medium", "t4g.large", "c7g.xlarge"],
  "instances": [
    {"role": "backend", "instance_type": "t4g.medium", "runs": "FastAPI /predict /health"},
    {"role": "frontend", "instance_type": "t4g.small", "runs": "Streamlit user and reviewer UI"},
    {"role": "monitoring", "instance_type": "t4g.medium", "runs": "monitoring dashboard, and the re-scorer if it survives the cut-line"}
  ],
  "constraints": [
    {
      "id": "A2-C01",
      "finding": "H3",
      "constraint": "every aws_instance.instance_type is one of instance_type_allowlist",
      "why": "the SCP DenyNonAllowlistedInstanceLaunch statement denies any other class on RunInstances, CreateFleet, RequestSpotInstances, and RequestSpotFleet"
    },
    {
      "id": "A2-C02",
      "finding": "H19",
      "constraint": "aws_instance sets user_data_replace_on_change = true, leaves source_dest_check at its default, and never changes an instance attribute after create",
      "why": "ec2:ModifyInstanceAttribute is denied unconditionally, because ec2:InstanceType on that action resolves to the instance's current type and any condition would be inert. Resizing is a Terraform-driven replacement through RunInstances"
    },
    {
      "id": "A2-C03",
      "finding": "H19",
      "constraint": "the aws provider is configured for us-west-2 only, with no aliased provider in another region",
      "why": "the SCP denies every regional action outside us-west-2; only named global services are exempted by NotAction"
    },
    {
      "id": "A2-C04",
      "finding": "H18",
      "constraint": "aws_db_instance sets manage_master_user_password = true and publicly_accessible = false, both explicitly",
      "why": "DenyRdsCreateWithoutManagedMasterPassword and DenyRdsCreatePubliclyAccessible use BoolIfExists, so an omitted key is denied. A random_password in state is denied by construction"
    },
    {
      "id": "A2-C05",
      "finding": "H17",
      "constraint": "the CloudTrail S3 bucket name begins with trail_bucket_prefix",
      "why": "the SCP DenyTrailEvidenceDestruction statement matches that ARN prefix; any other name leaves the evidence unprotected and the SCP silently inert"
    },
    {
      "id": "A2-C06",
      "finding": "H17",
      "constraint": "aws_cloudtrail sets enable_log_file_validation = true",
      "why": "no condition key exists on cloudtrail:CreateTrail for this, so the SCP cannot enforce it. Validation is what detects modification of logs the SCP still permits to be overwritten"
    },
    {
      "id": "A2-C07",
      "finding": "H17",
      "constraint": "the trail bucket has force_destroy = false and no aws_s3_bucket_lifecycle_configuration, and is excluded from routine terraform destroy",
      "why": "s3:DeleteObject and s3:PutLifecycleConfiguration are denied on that bucket, so terraform destroy cannot empty it. Removal requires detaching the SCP from the management account"
    },
    {
      "id": "A2-C08",
      "finding": "H17",
      "constraint": "aws_guardduty_detector is create-only; no post-create change to the detector",
      "why": "guardduty:UpdateDetector is denied, because enable=false disables GuardDuty without deleting it"
    },
    {
      "id": "A2-C09",
      "finding": "C6",
      "constraint": "every aws_security_group declares an explicit egress block permitting 443",
      "why": "Terraform removes the default 0.0.0.0/0 egress when a security group is declared without one. An instance in that state never registers with Systems Manager, and this design has no SSH, no bastion, and no NAT, so the only remaining channel is the broken one"
    }
  ]
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_constraints.sh`
Expected: `1..5` and `# 5 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/a2-constraints.json infra/aws/tests/test_constraints.sh
git commit -m "Record the SCP constraints Phase A2 Terraform must satisfy, with drift tests"
```

---

### Task 9 (C11): Bootstrap skeleton and a preflight that verifies credential hygiene

C11 established that "no static AWS access key exists anywhere in this project" is false three ways, and that the build box runs installs while holding four live credentials. This phase cannot fix the management account's legacy IAM users, but it can stop this script from ever being the path that uses one, and it can refuse to run under a static credential at all.

The preflight therefore **verifies** rather than asserts: it refuses if `AWS_ACCESS_KEY_ID` is in the environment, refuses if the caller is anything other than an `AWSReservedSSO_*` assumed role, refuses if that caller is not the organization management account, and warns about the two credential files C11 found on disk. Two more tests pin the C11 property of the script itself — it installs nothing — and the stub-compatibility rule that makes everything else testable: `jq` for parsing, never `--query`.

`ALT_CONTACT_PHONE` deliberately defaults to an obviously fake number that preflight refuses, so the real value must be supplied at run time rather than committed to a public repository.

**Files:**
- Create: `infra/aws/bootstrap.sh`
- Test: `infra/aws/tests/test_preflight.sh`

- [ ] **Step 1: Write the failing test**

`infra/aws/tests/test_preflight.sh`:
```bash
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib/harness.sh"
. "$HERE/lib/stubctx.sh"
BOOT="$HERE/../bootstrap.sh"

good_identity() {
    stub_fixture sts_get-caller-identity \
      '{"Arn":"arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_AdministratorAccess_abc/rock.lambros","Account":"111111111111"}'
    stub_fixture organizations_describe-organization \
      '{"Organization":{"Id":"o-abc","MasterAccountId":"111111111111"}}'
}

run_preflight() { bash "$BOOT" --preflight-only 2>&1; }

test_preflight_passes_under_an_identity_center_session() {
    stub_up; good_identity
    local out
    out=$(run_preflight) || fail "preflight exited non-zero: $out"
    assert_contains "$out" "preflight passed"
    stub_down
}

# C11: a static key in the environment is refused, not tolerated.
test_preflight_refuses_a_static_access_key_in_the_environment() {
    stub_up; good_identity
    local out
    out=$(AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE run_preflight) && fail "preflight accepted a static key"
    assert_contains "$out" "AWS_ACCESS_KEY_ID is set in this shell"
    stub_down
}

# C11: an IAM user is a static-credential principal even without an env var.
test_preflight_refuses_an_iam_user_caller() {
    stub_up
    stub_fixture sts_get-caller-identity \
      '{"Arn":"arn:aws:iam::111111111111:user/rc-script-user","Account":"111111111111"}'
    stub_fixture organizations_describe-organization \
      '{"Organization":{"MasterAccountId":"111111111111"}}'
    local out
    out=$(run_preflight) && fail "preflight accepted an IAM user"
    assert_contains "$out" "is not an IAM Identity Center session"
    stub_down
}

test_preflight_refuses_a_caller_outside_the_management_account() {
    stub_up
    stub_fixture sts_get-caller-identity \
      '{"Arn":"arn:aws:sts::222222222222:assumed-role/AWSReservedSSO_AdministratorAccess_abc/rock.lambros","Account":"222222222222"}'
    stub_fixture organizations_describe-organization \
      '{"Organization":{"MasterAccountId":"111111111111"}}'
    local out
    out=$(run_preflight) && fail "preflight accepted a non-management caller"
    assert_contains "$out" "is not the organization management account"
    stub_down
}

test_preflight_refuses_aws_cli_v1() {
    stub_up; good_identity
    local out
    out=$(AWS_STUB_VERSION="aws-cli/1.35.0 Python/3.12" run_preflight) && fail "preflight accepted CLI v1"
    assert_contains "$out" "AWS CLI v2 required"
    stub_down
}

test_preflight_refuses_terraform_below_1_11() {
    stub_up; good_identity
    local out
    out=$(TERRAFORM_STUB_VERSION="1.9.8" run_preflight) && fail "preflight accepted Terraform 1.9.8"
    assert_contains "$out" "Terraform 1.11+ required"
    stub_down
}

test_preflight_refuses_the_placeholder_alternate_contact_phone() {
    stub_up; good_identity
    local out
    out=$(BOOTSTRAP_ALT_CONTACT_PHONE="+10000000000" run_preflight) && fail "preflight accepted the placeholder phone"
    assert_contains "$out" "BOOTSTRAP_ALT_CONTACT_PHONE"
    stub_down
}

# C11: nothing may be installed on a box holding four live credentials.
test_bootstrap_installs_nothing() {
    local hits
    hits=$(grep -nE 'pip[0-9.]* +install|npm +(i|install|ci)|apt(-get)? +install|brew +install|curl[^|]*\| *(ba)?sh|wget[^|]*\| *(ba)?sh' \
           "$BOOT" "$HERE"/lib/*.sh "$HERE"/stubs/* "$HERE"/test_*.sh || true)
    assert_eq "$hits" "" "premortem C11: no install on the credential-bearing build box"
}

# The stub replays whole documents and cannot emulate server-side JMESPath.
test_bootstrap_parses_with_jq_and_never_with_query() {
    local hits
    hits=$(grep -n -- '--query' "$BOOT" || true)
    assert_eq "$hits" "" "use jq so every response stays stub-replayable"
}

test_bootstrap_never_touches_root_credentials() {
    local hits
    hits=$(grep -nE 'enable-organizations-root-credentials-management|delete-login-profile|assume-root' "$BOOT" || true)
    assert_eq "$hits" "" "root is break-glass and is never touched by this script"
}

run_suite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_preflight.sh`
Expected: FAIL — `1..10`, with `not ok` on all ten. The first records
`assert_contains: [preflight passed] not present` and the stderr line
`# stderr: bash: .../infra/aws/bootstrap.sh: No such file or directory`.

- [ ] **Step 3: Write minimal implementation**

`infra/aws/bootstrap.sh`:
```bash
#!/usr/bin/env bash
# Phase A1 account bootstrap. Creates the Sandbox OU, its service control policy, the
# rockcyber-mlops-toxic member account, its alternate contacts, the Identity Center
# permission sets, and the Terraform state bucket.
#
# Idempotent throughout: every step checks for existence before writing, so a re-run
# after a partial failure performs no duplicate work. `make -C infra/aws test` proves
# that by running the whole script twice against stubs and asserting the second run
# issues zero mutating calls.
#
# Three design rules, each enforced by a test in tests/test_preflight.sh:
#   * Responses are parsed with jq. --query is never used, because the test stub
#     replays whole JSON documents and cannot emulate server-side JMESPath.
#   * The script installs nothing. This build box holds the AWS SSO refresh token, the
#     W&B key, the Kaggle token, and the RunPod key at the same time.
#   * Root credentials are never touched. Root is the break-glass path and it stays.
#
# Blast radius: the Sandbox OU. Exactly one operation writes at organization-root
# scope, step 2, and it is gated behind --ack-org-root-write.
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

main() {
    parse_args "$@"
    preflight
    if [ "$PREFLIGHT_ONLY" -eq 1 ]; then
        log "preflight only; stopping"
        return 0
    fi
    log "bootstrap complete"
}

[ "${BOOTSTRAP_SOURCE_ONLY:-0}" = "1" ] || main "$@"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_preflight.sh && shellcheck -S style infra/aws/bootstrap.sh`
Expected: `1..10`, `# 10 tests, 0 failures`, and shellcheck silent.

- [ ] **Step 5: Commit**

```bash
git add infra/aws/bootstrap.sh infra/aws/tests/test_preflight.sh
git commit -m "Add bootstrap preflight that verifies Identity Center credential hygiene"
```

---

### Task 10 (bootstrap idempotency defect 1): Step 2 is an org-root-wide write that must be checked and acknowledged

The foundation spec states its own invariant plainly: "Every org-level write it performs is scoped to the new `Sandbox` OU. Any operation without OU-level scoping is disqualified by that rule." Step 2 violates it. `organizations enable-policy-type --root-id <root> --policy-type SERVICE_CONTROL_POLICY` writes at organization-root scope and cannot be scoped to an OU. The rule that removed centralized root access management from the design applies to this call by its own terms, and the script has been silently exempting it.

It is also unavoidable: no SCP can be created or attached anywhere beneath a root that has not enabled the policy type. So the defect is not the call, it is the silence. Two things fix it, and both are testable. First, the call must not be made at all when the policy type is already enabled, which is the ordinary case after the first run. Second, when it does have to be made, the script prints the exception, names the invariant it breaks, states what does and does not change, and refuses to proceed without `--ack-org-root-write`.

What genuinely does not change: AWS attaches `FullAWSAccess` to the root and to every OU and account automatically when the policy type is enabled, so no principal loses a permission. The management account is structurally exempt from SCPs regardless. RCAP's posture is unchanged. Saying so in the acknowledgement is what makes the exception reviewable rather than merely loud.

**Files:**
- Modify: `infra/aws/bootstrap.sh`
- Create: `infra/aws/tests/test_bootstrap_steps.sh`

- [ ] **Step 1: Write the failing test**

`infra/aws/tests/test_bootstrap_steps.sh`:
```bash
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib/harness.sh"
. "$HERE/lib/stubctx.sh"
BOOT="$HERE/../bootstrap.sh"

# Load every step function without running main.
load_bootstrap() {
    # shellcheck disable=SC1090
    BOOTSTRAP_SOURCE_ONLY=1 . "$BOOT"
}

root_scp_enabled() {
    stub_fixture organizations_list-roots \
      '{"Roots":[{"Id":"r-abcd","Name":"Root","PolicyTypes":[{"Type":"SERVICE_CONTROL_POLICY","Status":"ENABLED"}]}]}'
}
root_scp_disabled() {
    stub_fixture organizations_list-roots \
      '{"Roots":[{"Id":"r-abcd","Name":"Root","PolicyTypes":[]}]}'
}

# Idempotency: the one org-root-wide write must not happen when it is already done.
test_step2_makes_no_write_when_the_policy_type_is_already_enabled() {
    stub_up; load_bootstrap; root_scp_enabled
    local out
    out=$(step2_enable_scp_policy_type 2>&1) || fail "step 2 failed: $out"
    assert_not_contains "$(stub_calls)" "organizations_enable-policy-type"
    assert_contains "$out" "already ENABLED"
    assert_eq "$ORG_ROOT_ID" "r-abcd"
    stub_down
}

# Blast radius: the exception must be stated and acknowledged, not performed silently.
test_step2_refuses_the_org_root_write_without_an_explicit_acknowledgement() {
    stub_up; load_bootstrap; root_scp_disabled
    ACK_ORG_ROOT_WRITE=0
    local out rc=0
    out=$(step2_enable_scp_policy_type 2>&1) || rc=$?
    assert_eq "$rc" "2"
    assert_not_contains "$(stub_calls)" "organizations_enable-policy-type"
    assert_contains "$out" "BLAST-RADIUS EXCEPTION"
    assert_contains "$out" "organization root"
    assert_contains "$out" "--ack-org-root-write"
    stub_down
}

test_step2_writes_exactly_once_when_acknowledged() {
    stub_up; load_bootstrap; root_scp_disabled
    stub_fixture organizations_enable-policy-type '{"Root":{"Id":"r-abcd"}}'
    ACK_ORG_ROOT_WRITE=1
    local out
    out=$(step2_enable_scp_policy_type 2>&1) || fail "step 2 failed: $out"
    assert_eq "$(stub_calls | grep -c '^organizations_enable-policy-type$')" "1"
    assert_contains "$(stub_call_args organizations_enable-policy-type)" "--root-id r-abcd"
    assert_contains "$(stub_call_args organizations_enable-policy-type)" "--policy-type SERVICE_CONTROL_POLICY"
    stub_down
}

test_step2_acknowledgement_states_what_does_not_change() {
    stub_up; load_bootstrap; root_scp_disabled
    ACK_ORG_ROOT_WRITE=0
    local out
    out=$(step2_enable_scp_policy_type 2>&1) || true
    assert_contains "$out" "FullAWSAccess"
    assert_contains "$out" "management account"
    stub_down
}

run_suite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_bootstrap_steps.sh`
Expected: FAIL — `1..4`, all four `not ok`, each recording
`# stderr: .../test_bootstrap_steps.sh: line NN: step2_enable_scp_policy_type: command not found`

- [ ] **Step 3: Write minimal implementation**

Add to `infra/aws/bootstrap.sh`, after `preflight`:
```bash
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
```

And in `main`, between `preflight` and the completion log:
```bash
    step2_enable_scp_policy_type
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_bootstrap_steps.sh`
Expected: `1..4` and `# 4 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/bootstrap.sh infra/aws/tests/test_bootstrap_steps.sh
git commit -m "Gate the one organization-root-wide write behind an explicit acknowledgement"
```

---

### Task 11: Steps 3 and 4 — the OU and its SCP, created before the account exists

Ordering carries a security property here, so it is worth stating before the code. The OU is created and the SCP is attached to it **before** `create-account` runs, so the account is never inside an unprotected OU. That leaves exactly one gap — between the account's creation in the org root and its move into the OU — which Task 13 narrows and measures.

Step 4 also syncs content rather than merely checking existence. A policy whose name matches but whose document has drifted is worse than a missing one, because it looks correct in the console.

**Files:**
- Modify: `infra/aws/bootstrap.sh`
- Modify: `infra/aws/tests/test_bootstrap_steps.sh`

- [ ] **Step 1: Write the failing test**

Append to `infra/aws/tests/test_bootstrap_steps.sh`, before `run_suite`:
```bash
test_step3_reuses_an_existing_ou() {
    stub_up; load_bootstrap
    ORG_ROOT_ID="r-abcd"
    stub_fixture organizations_list-organizational-units-for-parent \
      '{"OrganizationalUnits":[{"Id":"ou-abcd-1111","Name":"Sandbox"}]}'
    step3_create_ou >/dev/null
    assert_eq "$OU_ID" "ou-abcd-1111"
    assert_not_contains "$(stub_calls)" "organizations_create-organizational-unit"
    assert_contains "$(cat "$BOOTSTRAP_OUTPUTS_FILE")" "SANDBOX_OU_ID=ou-abcd-1111"
    stub_down
}

test_step3_creates_the_ou_when_absent() {
    stub_up; load_bootstrap
    ORG_ROOT_ID="r-abcd"
    stub_fixture organizations_list-organizational-units-for-parent '{"OrganizationalUnits":[]}'
    stub_fixture organizations_create-organizational-unit \
      '{"OrganizationalUnit":{"Id":"ou-abcd-2222","Name":"Sandbox"}}'
    step3_create_ou >/dev/null
    assert_eq "$OU_ID" "ou-abcd-2222"
    assert_contains "$(stub_call_args organizations_create-organizational-unit)" "--name Sandbox"
    stub_down
}

test_step4_creates_and_attaches_the_policy_when_absent() {
    stub_up; load_bootstrap
    OU_ID="ou-abcd-1111"
    stub_fixture organizations_list-policies '{"Policies":[]}'
    stub_fixture organizations_create-policy '{"Policy":{"PolicySummary":{"Id":"p-1111"}}}'
    stub_fixture organizations_list-policies-for-target '{"Policies":[]}'
    stub_fixture organizations_attach-policy '{}'
    step4_scp >/dev/null
    assert_eq "$POLICY_ID" "p-1111"
    assert_contains "$(stub_call_args organizations_attach-policy)" "--target-id ou-abcd-1111"
    stub_down
}

test_step4_makes_no_write_when_content_and_attachment_already_match() {
    stub_up; load_bootstrap
    OU_ID="ou-abcd-1111"
    stub_fixture organizations_list-policies '{"Policies":[{"Id":"p-1111","Name":"sandbox-guardrails"}]}'
    stub_fixture organizations_describe-policy \
      "$(jq -c --argjson c "$(jq -c . "$HERE/../scp-sandbox-guardrails.json")" \
              -n '{Policy:{Content:($c|tostring)}}')"
    stub_fixture organizations_list-policies-for-target '{"Policies":[{"Id":"p-1111"}]}'
    step4_scp >/dev/null
    assert_eq "$(stub_writes)" "" "an unchanged, attached policy must produce no write"
    stub_down
}

test_step4_updates_the_policy_when_the_document_has_drifted() {
    stub_up; load_bootstrap
    OU_ID="ou-abcd-1111"
    stub_fixture organizations_list-policies '{"Policies":[{"Id":"p-1111","Name":"sandbox-guardrails"}]}'
    stub_fixture organizations_describe-policy \
      '{"Policy":{"Content":"{\"Version\":\"2012-10-17\",\"Statement\":[]}"}}'
    stub_fixture organizations_update-policy '{"Policy":{"PolicySummary":{"Id":"p-1111"}}}'
    stub_fixture organizations_list-policies-for-target '{"Policies":[{"Id":"p-1111"}]}'
    local out
    out=$(step4_scp 2>&1)
    assert_contains "$(stub_calls)" "organizations_update-policy"
    assert_contains "$out" "drifted"
    stub_down
}

test_step4_refuses_a_document_over_the_scp_size_quota() {
    stub_up; load_bootstrap
    OU_ID="ou-abcd-1111"
    local big; big=$(mktemp)
    jq -n --arg pad "$(head -c 6000 /dev/zero | tr '\0' 'x')" \
       '{Version:"2012-10-17",Statement:[{Sid:"Pad",Effect:"Deny",Action:[$pad],Resource:"*"}]}' >"$big"
    local rc=0 out
    out=$(BOOTSTRAP_SCP_FILE="$big" bash -c ". $BOOT; SCP_FILE=$big OU_ID=ou-abcd-1111 step4_scp" 2>&1) || rc=$?
    assert_eq "$rc" "1"
    assert_contains "$out" "5120-byte quota"
    rm -f "$big"
    stub_down
}
```

Note the last test runs `bootstrap.sh` in a child shell because `die` calls `exit`, which would terminate the suite if invoked in-process.

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_bootstrap_steps.sh`
Expected: FAIL — six new `not ok`, each recording
`# stderr: ...: step3_create_ou: command not found` or `step4_scp: command not found`

- [ ] **Step 3: Write minimal implementation**

Add to `infra/aws/bootstrap.sh`, after `step2_enable_scp_policy_type`:
```bash
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
```

And in `main`, after `step2_enable_scp_policy_type`:
```bash
    step3_create_ou
    step4_scp
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_bootstrap_steps.sh`
Expected: `1..10` and `# 10 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/bootstrap.sh infra/aws/tests/test_bootstrap_steps.sh
git commit -m "Create the Sandbox OU and sync its SCP before any account exists"
```

---

### Task 12 (bootstrap idempotency defect 2): `create-account` is asynchronous and not idempotent

Two distinct defects live in step 5 and the fix for one does not cover the other.

**It is not idempotent.** `organizations:CreateAccount` has no natural idempotency key that this script supplies. A second run against an organization that already holds the account either creates a duplicate or fails on the duplicate root email, and both outcomes end the same way: a human has to work out what state the organization is in. The check that resolves it is the root email, because that is the one property AWS enforces as unique across the organization. The comparison is case-insensitive, and it must paginate, because `list-accounts` truncates and the account this script is looking for can be on page two.

**It is asynchronous, so a crash loses the account id.** `create-account` returns a request id and the account materialises later. The id only becomes knowable through `describe-create-account-status`. If the script dies between the poll succeeding and whatever writes state next, the account exists, is billable, occupies the root email, and nothing on disk records it. So the request id is persisted before polling starts, and the account id is persisted the instant it is known — before step 6 or anything else runs. The test proves that by making step 6 fail and asserting the id survives.

**Files:**
- Modify: `infra/aws/bootstrap.sh`
- Modify: `infra/aws/tests/test_bootstrap_steps.sh`

- [ ] **Step 1: Write the failing test**

Append to `infra/aws/tests/test_bootstrap_steps.sh`, before `run_suite`:
```bash
# Idempotency: the root email is the organization-unique key, so it is the existence check.
test_step5_adopts_an_existing_account_matched_on_root_email() {
    stub_up; load_bootstrap
    stub_fixture organizations_list-accounts \
      '{"Accounts":[{"Id":"999999999999","Email":"someone@example.com"},
                    {"Id":"123456789012","Email":"rock+aws-mlops-toxic@rockcyber.com"}]}'
    local out
    out=$(step5_create_account 2>&1) || fail "step 5 failed: $out"
    assert_eq "$ACCOUNT_ID" "123456789012"
    assert_not_contains "$(stub_calls)" "organizations_create-account"
    assert_contains "$(cat "$BOOTSTRAP_OUTPUTS_FILE")" "ACCOUNT_ID=123456789012"
    stub_down
}

test_step5_matches_the_root_email_case_insensitively() {
    stub_up; load_bootstrap
    stub_fixture organizations_list-accounts \
      '{"Accounts":[{"Id":"123456789012","Email":"Rock+AWS-MLOPS-Toxic@RockCyber.com"}]}'
    step5_create_account >/dev/null
    assert_eq "$ACCOUNT_ID" "123456789012"
    assert_not_contains "$(stub_calls)" "organizations_create-account"
    stub_down
}

test_step5_paginates_before_concluding_the_account_is_absent() {
    stub_up; load_bootstrap
    stub_fixture organizations_list-accounts.1 \
      '{"Accounts":[{"Id":"999999999999","Email":"someone@example.com"}],"NextToken":"tok2"}'
    stub_fixture organizations_list-accounts.2 \
      '{"Accounts":[{"Id":"123456789012","Email":"rock+aws-mlops-toxic@rockcyber.com"}]}'
    step5_create_account >/dev/null
    assert_eq "$ACCOUNT_ID" "123456789012"
    assert_eq "$(stub_calls | grep -c '^organizations_list-accounts$')" "2"
    assert_not_contains "$(stub_calls)" "organizations_create-account"
    stub_down
}

test_step5_polls_the_asynchronous_status_to_a_terminal_state() {
    stub_up; load_bootstrap
    stub_fixture organizations_list-accounts '{"Accounts":[]}'
    stub_fixture organizations_create-account '{"CreateAccountStatus":{"Id":"car-1","State":"IN_PROGRESS"}}'
    stub_fixture organizations_describe-create-account-status.1 '{"CreateAccountStatus":{"Id":"car-1","State":"IN_PROGRESS"}}'
    stub_fixture organizations_describe-create-account-status.2 '{"CreateAccountStatus":{"Id":"car-1","State":"IN_PROGRESS"}}'
    stub_fixture organizations_describe-create-account-status.3 '{"CreateAccountStatus":{"Id":"car-1","State":"SUCCEEDED","AccountId":"123456789012"}}'
    step5_create_account >/dev/null
    assert_eq "$ACCOUNT_ID" "123456789012"
    assert_eq "$(stub_calls | grep -c '^organizations_describe-create-account-status$')" "3"
    stub_down
}

test_step5_persists_the_request_id_before_it_starts_polling() {
    stub_up; load_bootstrap
    stub_fixture organizations_list-accounts '{"Accounts":[]}'
    stub_fixture organizations_create-account '{"CreateAccountStatus":{"Id":"car-77","State":"IN_PROGRESS"}}'
    stub_rc organizations_describe-create-account-status 254
    local rc=0
    ( step5_create_account >/dev/null 2>&1 ) || rc=$?
    assert_contains "$(cat "$BOOTSTRAP_OUTPUTS_FILE")" "CREATE_ACCOUNT_REQUEST_ID=car-77" \
        "a crash during polling must still leave a trail to the in-flight account"
    stub_down
}

# The load-bearing test: the account id must be on disk before step 6 can lose it.
test_account_id_is_persisted_before_any_later_step_runs() {
    stub_up; load_bootstrap
    stub_fixture organizations_list-accounts '{"Accounts":[]}'
    stub_fixture organizations_create-account '{"CreateAccountStatus":{"Id":"car-1","State":"IN_PROGRESS"}}'
    stub_fixture organizations_describe-create-account-status \
      '{"CreateAccountStatus":{"Id":"car-1","State":"SUCCEEDED","AccountId":"123456789012"}}'
    step5_create_account >/dev/null
    # Simulate step 6 exploding immediately afterwards.
    stub_rc organizations_list-parents 254
    ( step6_govern_account >/dev/null 2>&1 ) || true
    assert_contains "$(cat "$BOOTSTRAP_OUTPUTS_FILE")" "ACCOUNT_ID=123456789012" \
        "the account exists, is billable, and occupies the root email; losing its id is unrecoverable state"
    stub_down
}

test_step5_dies_on_a_failed_create_rather_than_continuing() {
    stub_up
    stub_fixture organizations_list-accounts '{"Accounts":[]}'
    stub_fixture organizations_create-account '{"CreateAccountStatus":{"Id":"car-9","State":"IN_PROGRESS"}}'
    stub_fixture organizations_describe-create-account-status \
      '{"CreateAccountStatus":{"Id":"car-9","State":"FAILED","FailureReason":"EMAIL_ALREADY_EXISTS"}}'
    # A child shell, because die calls exit and would otherwise end the suite.
    local rc=0 out
    out=$(bash -c "BOOTSTRAP_SOURCE_ONLY=1 . $BOOT; step5_create_account" 2>&1) || rc=$?
    assert_eq "$rc" "1"
    assert_contains "$out" "EMAIL_ALREADY_EXISTS"
    stub_down
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_bootstrap_steps.sh`
Expected: FAIL — seven new `not ok`, each recording
`# stderr: ...: step5_create_account: command not found`

- [ ] **Step 3: Write minimal implementation**

Add to `infra/aws/bootstrap.sh`, after `step4_scp`:
```bash
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
```

And in `main`, after `step4_scp`:
```bash
    step5_create_account
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_bootstrap_steps.sh`
Expected: `1..17` and `# 17 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/bootstrap.sh infra/aws/tests/test_bootstrap_steps.sh
git commit -m "Make account creation idempotent on root email and persist the id before use"
```

---

### Task 13 (bootstrap idempotency defect 3): Narrow and measure the ungoverned window

`organizations:CreateAccount` places the new account in the **organization root**, not in the OU that was named when it was requested. Between the poll returning `SUCCEEDED` and `move-account` completing, the account exists, is billable, has an `OrganizationAccountAccessRole` that the management account can assume, and is governed by no SCP at all. Every guardrail this phase builds — the region lock, the instance-type allowlist, the static-credential denial, the detective-control protection — is inert for the duration.

Three things narrow it, and each is a separate test because each fails differently.

**Nothing may run between creation and governance.** The window is as long as the work scheduled inside it. Alternate contacts, Identity Center, and the state bucket all happen after the move, not before. The test asserts on the call log directly: the operation immediately following the last status poll is `list-parents`, and the one after that is `move-account`. Any step inserted between them fails the assertion, which is the point — this is a constraint on future edits, not just on today's code.

**The OU and its policy must already exist.** They do, because steps 3 and 4 run before step 5, so the destination is protected the moment the account lands. The test asserts `attach-policy` precedes `create-account` in the call log.

**The move must be verified, and failure must be fatal.** `move-account` returning success is not evidence that the account is in the OU. The script re-reads `list-parents` afterwards and refuses to continue if the parent is anything other than the Sandbox OU, because continuing would run steps 7 through 10 against an ungoverned account and leave it that way.

The measured window is written to the outputs file, so the number is auditable rather than assumed.

**Files:**
- Modify: `infra/aws/bootstrap.sh`
- Modify: `infra/aws/tests/test_bootstrap_steps.sh`

- [ ] **Step 1: Write the failing test**

Append to `infra/aws/tests/test_bootstrap_steps.sh`, before `run_suite`:
```bash
# Fixtures for a complete first run, used by the ordering tests.
full_first_run_fixtures() {
    stub_fixture sts_get-caller-identity \
      '{"Arn":"arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_AdministratorAccess_abc/rock.lambros","Account":"111111111111"}'
    stub_fixture organizations_describe-organization '{"Organization":{"MasterAccountId":"111111111111"}}'
    stub_fixture organizations_list-roots \
      '{"Roots":[{"Id":"r-abcd","PolicyTypes":[{"Type":"SERVICE_CONTROL_POLICY","Status":"ENABLED"}]}]}'
    stub_fixture organizations_list-organizational-units-for-parent '{"OrganizationalUnits":[]}'
    stub_fixture organizations_create-organizational-unit '{"OrganizationalUnit":{"Id":"ou-abcd-1111"}}'
    stub_fixture organizations_list-policies '{"Policies":[]}'
    stub_fixture organizations_create-policy '{"Policy":{"PolicySummary":{"Id":"p-1111"}}}'
    stub_fixture organizations_list-policies-for-target '{"Policies":[]}'
    stub_fixture organizations_attach-policy '{}'
    stub_fixture organizations_list-accounts '{"Accounts":[]}'
    stub_fixture organizations_create-account '{"CreateAccountStatus":{"Id":"car-1","State":"IN_PROGRESS"}}'
    stub_fixture organizations_describe-create-account-status.1 '{"CreateAccountStatus":{"State":"IN_PROGRESS"}}'
    stub_fixture organizations_describe-create-account-status.2 \
      '{"CreateAccountStatus":{"State":"SUCCEEDED","AccountId":"123456789012"}}'
    stub_fixture organizations_list-parents.1 '{"Parents":[{"Id":"r-abcd","Type":"ROOT"}]}'
    stub_fixture organizations_move-account '{}'
    stub_fixture organizations_list-parents.2 '{"Parents":[{"Id":"ou-abcd-1111","Type":"ORGANIZATIONAL_UNIT"}]}'
    stub_fixture account_get-alternate-contact '{}'
    stub_rc account_get-alternate-contact 255
    stub_fixture account_put-alternate-contact '{}'
}

# The window is exactly as long as the work scheduled inside it.
test_nothing_runs_between_account_creation_and_governance() {
    stub_up; load_bootstrap; full_first_run_fixtures
    step2_enable_scp_policy_type >/dev/null
    step3_create_ou >/dev/null
    step4_scp >/dev/null
    step5_create_account >/dev/null
    step6_govern_account >/dev/null
    local last
    last=$(stub_last_index organizations_describe-create-account-status)
    assert_eq "$(stub_call_at $((last + 1)))" "organizations_list-parents"
    assert_eq "$(stub_call_at $((last + 2)))" "organizations_move-account" \
        "any step inserted here extends the interval in which no SCP applies"
    stub_down
}

# The destination must already be protected when the account lands in it.
test_the_scp_is_attached_before_the_account_is_created() {
    stub_up; load_bootstrap; full_first_run_fixtures
    step2_enable_scp_policy_type >/dev/null
    step3_create_ou >/dev/null
    step4_scp >/dev/null
    step5_create_account >/dev/null
    local a c
    a=$(stub_index organizations_attach-policy)
    c=$(stub_index organizations_create-account)
    assert_eq "$(( a < c ))" "1" "attach-policy at $a must precede create-account at $c"
    stub_down
}

test_step6_verifies_the_move_and_records_the_window() {
    stub_up; load_bootstrap; full_first_run_fixtures
    step5_create_account >/dev/null
    OU_ID="ou-abcd-1111"
    step6_govern_account >/dev/null
    assert_contains "$(stub_call_args organizations_move-account)" "--destination-parent-id ou-abcd-1111"
    assert_contains "$(cat "$BOOTSTRAP_OUTPUTS_FILE")" "UNGOVERNED_WINDOW_SECONDS="
    stub_down
}

# Fail closed: a move that did not take must not be followed by steps 7 through 10.
test_step6_refuses_to_continue_when_the_account_is_not_in_the_ou() {
    stub_up
    stub_fixture organizations_list-parents '{"Parents":[{"Id":"r-abcd","Type":"ROOT"}]}'
    stub_fixture organizations_move-account '{}'
    local rc=0 out
    out=$(bash -c "BOOTSTRAP_SOURCE_ONLY=1 . $BOOT
                   ACCOUNT_ID=123456789012 OU_ID=ou-abcd-1111 step6_govern_account" 2>&1) || rc=$?
    assert_eq "$rc" "1"
    assert_contains "$out" "refusing to continue"
    assert_not_contains "$(stub_calls)" "account_put-alternate-contact"
    stub_down
}

test_step6_skips_the_move_when_the_account_is_already_in_the_ou() {
    stub_up; load_bootstrap
    ACCOUNT_ID="123456789012"; OU_ID="ou-abcd-1111"
    stub_fixture organizations_list-parents '{"Parents":[{"Id":"ou-abcd-1111","Type":"ORGANIZATIONAL_UNIT"}]}'
    stub_fixture account_get-alternate-contact \
      '{"AlternateContact":{"EmailAddress":"rock@rockcyber.com","Name":"Rock Lambros","PhoneNumber":"+13035550100","Title":"Owner"}}'
    step6_govern_account >/dev/null
    assert_not_contains "$(stub_calls)" "organizations_move-account"
    assert_not_contains "$(stub_calls)" "account_put-alternate-contact"
    stub_down
}

test_step6_sets_all_three_alternate_contacts_when_absent() {
    stub_up; load_bootstrap
    ACCOUNT_ID="123456789012"; OU_ID="ou-abcd-1111"
    stub_fixture organizations_list-parents '{"Parents":[{"Id":"ou-abcd-1111"}]}'
    stub_rc account_get-alternate-contact 255
    stub_fixture account_put-alternate-contact '{}'
    step6_govern_account >/dev/null
    assert_eq "$(stub_calls | grep -c '^account_put-alternate-contact$')" "3"
    local args
    args=$(grep '^account_put-alternate-contact ' "$AWS_STUB_CALLLOG" | tr '\n' ' ')
    assert_contains "$args" "BILLING"
    assert_contains "$args" "OPERATIONS"
    assert_contains "$args" "SECURITY"
    stub_down
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_bootstrap_steps.sh`
Expected: FAIL — six new `not ok`, each recording
`# stderr: ...: step6_govern_account: command not found`

- [ ] **Step 3: Write minimal implementation**

Add to `infra/aws/bootstrap.sh`, after `step5_create_account`:
```bash
# Step 6. Close the ungoverned window, then set the alternate contacts.
#
# organizations:CreateAccount places the account in the ORGANIZATION ROOT, not in the OU.
# Until move-account completes, the account is billable and no SCP applies to it: no region
# lock, no instance-type allowlist, no static-credential denial, no detective-control
# protection. Three things keep that interval short and provable:
#   1. Nothing is scheduled between the status poll and the move. tests/test_bootstrap_steps.sh
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
    for t in BILLING OPERATIONS SECURITY; do
        existing=$(org account get-alternate-contact \
                       --account-id "$ACCOUNT_ID" --alternate-contact-type "$t" 2>/dev/null \
                   | jq -r '.AlternateContact.EmailAddress // empty')
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
```

And in `main`, after `step5_create_account`:
```bash
    step6_govern_account
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_bootstrap_steps.sh`
Expected: `1..23` and `# 23 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add infra/aws/bootstrap.sh infra/aws/tests/test_bootstrap_steps.sh
git commit -m "Close the ungoverned window between account creation and OU placement"
```

---

### Task 14: Steps 7 through 10, and prove the whole script is idempotent

Step 7 is the operator gate that establishes the break-glass, and it is the reason this phase runs on day 1: it depends on password-recovery mail reaching a plus-addressed address behind Mimecast, and if that fails it must be discovered with eighteen days of runway. Steps 8 through 10 are ordinary API work, and each carries its own existence check.

Two properties get tested here that no single step can prove on its own.

**A second run performs no writes.** The whole point of idempotency is that a re-run after a partial failure is safe. The test runs `main` twice against fixtures describing a fully-provisioned organization and asserts the second run's call log contains zero mutating verbs. That assertion is what forces every step to have a real existence check rather than an approximate one — including `put-alternate-contact`, which is idempotent at the API but is still a write.

**Nothing leaks.** The script handles short-lived STS credentials in step 9 and an account id throughout. The account id is masked in every log line, because screenshots of this run are part of the submission deliverable and the deliverable checklist says no account id may be visible. The STS secret must never appear on either stream.

**Files:**
- Modify: `infra/aws/bootstrap.sh`
- Create: `infra/aws/tests/test_bootstrap_full.sh`

- [ ] **Step 1: Write the failing test**

`infra/aws/tests/test_bootstrap_full.sh`:
```bash
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib/harness.sh"
. "$HERE/lib/stubctx.sh"
BOOT="$HERE/../bootstrap.sh"

# An organization in which everything this script creates already exists.
steady_state_fixtures() {
    stub_fixture sts_get-caller-identity \
      '{"Arn":"arn:aws:sts::111111111111:assumed-role/AWSReservedSSO_AdministratorAccess_abc/rock.lambros","Account":"111111111111"}'
    stub_fixture organizations_describe-organization '{"Organization":{"MasterAccountId":"111111111111"}}'
    stub_fixture organizations_list-roots \
      '{"Roots":[{"Id":"r-abcd","PolicyTypes":[{"Type":"SERVICE_CONTROL_POLICY","Status":"ENABLED"}]}]}'
    stub_fixture organizations_list-organizational-units-for-parent \
      '{"OrganizationalUnits":[{"Id":"ou-abcd-1111","Name":"Sandbox"}]}'
    stub_fixture organizations_list-policies '{"Policies":[{"Id":"p-1111","Name":"sandbox-guardrails"}]}'
    stub_fixture organizations_describe-policy \
      "$(jq -c --argjson c "$(jq -c . "$HERE/../scp-sandbox-guardrails.json")" -n '{Policy:{Content:($c|tostring)}}')"
    stub_fixture organizations_list-policies-for-target '{"Policies":[{"Id":"p-1111"}]}'
    stub_fixture organizations_list-accounts \
      '{"Accounts":[{"Id":"123456789012","Email":"rock+aws-mlops-toxic@rockcyber.com"}]}'
    stub_fixture organizations_list-parents '{"Parents":[{"Id":"ou-abcd-1111"}]}'
    stub_fixture account_get-alternate-contact \
      '{"AlternateContact":{"EmailAddress":"rock@rockcyber.com"}}'
    stub_fixture sso-admin_list-instances \
      '{"Instances":[{"InstanceArn":"arn:aws:sso:::instance/ssoins-abc","IdentityStoreId":"d-123"}]}'
    stub_fixture identitystore_list-users '{"Users":[{"UserId":"u-1","UserName":"rock.lambros"}]}'
    stub_fixture sso-admin_list-permission-sets \
      '{"PermissionSets":["arn:aws:sso:::permissionSet/ssoins-abc/ps-admin","arn:aws:sso:::permissionSet/ssoins-abc/ps-ro"]}'
    stub_fixture sso-admin_describe-permission-set.1 '{"PermissionSet":{"Name":"MlopsToxicAdmin","PermissionSetArn":"arn:aws:sso:::permissionSet/ssoins-abc/ps-admin"}}'
    stub_fixture sso-admin_describe-permission-set.2 '{"PermissionSet":{"Name":"MlopsToxicReadOnly","PermissionSetArn":"arn:aws:sso:::permissionSet/ssoins-abc/ps-ro"}}'
    stub_fixture sso-admin_list-account-assignments '{"AccountAssignments":[{"PrincipalId":"u-1","PermissionSetArn":"arn:aws:sso:::permissionSet/ssoins-abc/ps-admin"}]}'
    stub_fixture sts_assume-role \
      '{"Credentials":{"AccessKeyId":"ASIAFIXTURE","SecretAccessKey":"SECRETKEYFIXTUREVALUE","SessionToken":"TOKENFIXTURE"}}'
    stub_fixture iam_list-account-aliases '{"AccountAliases":["rockcyber-mlops-toxic"]}'
    stub_fixture s3api_head-bucket '{}'
}

run_main() { bash "$BOOT" --skip-operator-gate 2>&1; }

test_a_fully_provisioned_organization_runs_clean() {
    stub_up; steady_state_fixtures
    local out
    out=$(run_main) || fail "bootstrap failed: $out"
    assert_contains "$out" "bootstrap complete"
    stub_down
}

# The load-bearing idempotency proof.
test_a_second_run_performs_no_writes() {
    stub_up; steady_state_fixtures
    run_main >/dev/null
    : >"$AWS_STUB_CALLLOG"
    run_main >/dev/null
    assert_eq "$(stub_writes)" "" "a re-run after a partial failure must be a no-op"
    stub_down
}

test_the_outputs_file_carries_every_interface_key_and_is_mode_600() {
    stub_up; steady_state_fixtures
    run_main >/dev/null
    local body
    body=$(cat "$BOOTSTRAP_OUTPUTS_FILE")
    for k in ACCOUNT_ID AWS_REGION SANDBOX_OU_ID SCP_POLICY_ID TF_STATE_BUCKET \
             ADMIN_PERMISSION_SET_ARN READONLY_PERMISSION_SET_ARN; do
        assert_contains "$body" "${k}="
    done
    assert_eq "$(stat -c '%a' "$BOOTSTRAP_OUTPUTS_FILE")" "600"
    stub_down
}

test_the_outputs_file_is_gitignored() {
    assert_rc 0 git -C "$HERE/../../.." check-ignore -q infra/aws/bootstrap-outputs.env
}

# The submission deliverable requires screenshots with no account id visible.
test_the_account_id_never_reaches_stdout_or_stderr() {
    stub_up; steady_state_fixtures
    local out
    out=$(run_main)
    assert_not_contains "$out" "123456789012"
    assert_contains "$out" "<account-id>"
    stub_down
}

test_the_assumed_role_secret_never_reaches_stdout_or_stderr() {
    stub_up; steady_state_fixtures
    local out
    out=$(run_main)
    assert_not_contains "$out" "SECRETKEYFIXTUREVALUE"
    assert_not_contains "$out" "TOKENFIXTURE"
    stub_down
}

test_the_operator_gate_blocks_when_not_skipped() {
    stub_up; steady_state_fixtures
    local rc=0 out
    out=$(bash "$BOOT" </dev/null 2>&1) || rc=$?
    assert_eq "$rc" "1"
    assert_contains "$out" "OPERATOR STEP 7"
    assert_contains "$out" "not acknowledged"
    stub_down
}

test_the_operator_gate_explains_the_mimecast_dependency() {
    stub_up; steady_state_fixtures
    local out
    out=$(bash "$BOOT" </dev/null 2>&1) || true
    assert_contains "$out" "rock+aws-mlops-toxic@rockcyber.com"
    assert_contains "$out" "Mimecast"
    assert_contains "$out" "MFA"
    stub_down
}

test_the_state_bucket_is_created_private_versioned_and_encrypted_when_absent() {
    stub_up; steady_state_fixtures
    stub_rc s3api_head-bucket 255
    stub_fixture s3api_create-bucket '{}'
    stub_fixture s3api_put-bucket-versioning '{}'
    stub_fixture s3api_put-bucket-encryption '{}'
    stub_fixture s3api_put-public-access-block '{}'
    stub_fixture s3api_put-bucket-policy '{}'
    run_main >/dev/null
    local calls; calls=$(stub_calls)
    for c in s3api_create-bucket s3api_put-bucket-versioning s3api_put-bucket-encryption \
             s3api_put-public-access-block s3api_put-bucket-policy; do
        assert_contains "$calls" "$c"
    done
    assert_contains "$(stub_call_args s3api_create-bucket)" "LocationConstraint=us-west-2"
    stub_down
}

run_suite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_bootstrap_full.sh`
Expected: FAIL — `1..9` with eight `not ok` (`test_the_outputs_file_is_gitignored` already passes). The first records
`assert_contains: [bootstrap complete] not present` with `# stderr: aws-stub: no fixture for sso-admin_list-instances (call #1)`, because steps 7 through 10 do not exist.

- [ ] **Step 3: Write minimal implementation**

Add to `infra/aws/bootstrap.sh`, after `step6_govern_account`:
```bash
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
```

Replace `main`'s body between `preflight` and the completion log with:
```bash
    step2_enable_scp_policy_type
    step3_create_ou
    step4_scp
    step5_create_account
    step6_govern_account
    step7_operator_break_glass
    step8_identity_center
    step9_member_account_setup
    step10_write_outputs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_bootstrap_full.sh && make -C infra/aws lint`
Expected: `1..9`, `# 9 tests, 0 failures`, and shellcheck silent.

- [ ] **Step 5: Commit**

```bash
git add infra/aws/bootstrap.sh infra/aws/tests/test_bootstrap_full.sh
git commit -m "Complete the bootstrap with break-glass gate, Identity Center, and state bucket"
```

---

### Task 15 (C11): Correct the false static-credential claims, in all five places

C11 is a documentation defect with a security consequence, and the code in Task 9 only closes half of it. The other half is that four documents assert, in the present tense, a property that is false. On a public repository that is a claim a reader can check and disbelieve, and internally it is worse: it is the reason nobody scoped the permission set below `AdministratorAccess` or questioned running an unhashed `pip install` on the build box.

The claim is refuted three ways, all verified:

1. `OrganizationAccountAccessRole` in the member account trusts `arn:aws:iam::<mgmt>:root`, and two legacy IAM users with static keys live in that management account. SCPs cannot constrain them, by exactly the property the foundation spec celebrates for protecting RCAP.
2. `~/.aws/sso/cache/*.json` on the build box holds an `accessToken` **and a `refreshToken`** under a client registration valid until 2026-10-29 — a portable, copyable, roughly 90-day credential to `AdministratorAccess` on the management account. "Short-lived" describes the session, not the refresh token that regenerates it.
3. `~/.netrc` contains `machine api.wandb.ai` in plaintext, and the `wandb` SDK prefers it silently, so the `pass`-at-point-of-use discipline is bypassed by default rather than by mistake.

The correction is not a retraction. The narrower claim is true, valuable, and now enforced by the SCP written in Task 7: **no static AWS access key is created in, or issued to, the member account, and the SCP denies the API calls that would create one.** Each site gets that sentence plus a pointer to the refutations. Delivery-spec section 2 already carries the correction and is the reference text.

Five sites, located by grep:

| File | Line | Current text |
|---|---|---|
| `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md` | 94 | "No static AWS access key exists anywhere in this project." |
| `docs/2026-07-01-toxic-moderation-mlops-design.md` | 64 | "No static AWS credentials exist." |
| `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md` | 25 | "No static AWS credentials exist anywhere" |
| `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md` | 226 | "no static AWS access key exists in the account" — already correctly scoped, left alone |
| `SECURITY.md` | 76 | "No static AWS credentials exist." |

The same pass folds in two other corrections this phase touches. The master plan's Phase A is a single block that predates the A1/A2 split, and its task 2 describes the SCP as allowing `us-east-1` with `Bool` RDS conditions — both now wrong. Correcting them here is the delivery spec's "edited at source" rule: a supersession table is not a merge, and a subagent reading a narrow slice will read the stale text.

**Files:**
- Modify: `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`, `docs/2026-07-01-toxic-moderation-mlops-design.md`, `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`, `SECURITY.md`
- Test: `infra/aws/tests/test_docs_claims.sh`

- [ ] **Step 1: Write the failing test**

`infra/aws/tests/test_docs_claims.sh`:
```bash
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib/harness.sh"
ROOT="$(cd "$HERE/../../.." && pwd)"

FOUNDATION="$ROOT/docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md"
DESIGN="$ROOT/docs/2026-07-01-toxic-moderation-mlops-design.md"
MASTER="$ROOT/docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md"
SECURITY="$ROOT/SECURITY.md"

# C11: the unqualified organization-wide claim is false and must not survive anywhere
# outside the premortem that refutes it.
test_no_unqualified_static_credential_claim_remains() {
    local hits
    hits=$(grep -rniE 'no static aws (access key|credential)s? (exists?|are) ?(anywhere)?\.?$' \
           "$FOUNDATION" "$DESIGN" "$MASTER" "$SECURITY" || true)
    assert_eq "$hits" "" "the claim is refuted three ways in the premortem C11 evidence"
}

test_the_narrower_enforced_claim_is_present_in_all_four_documents() {
    local f
    for f in "$FOUNDATION" "$DESIGN" "$MASTER" "$SECURITY"; do
        assert_contains "$(cat "$f")" \
            "No static AWS access key is created in, or issued to, the member account" \
            "missing from $(basename "$f")"
    done
}

test_the_foundation_spec_names_all_three_refutations() {
    local body
    body=$(cat "$FOUNDATION")
    assert_contains "$body" "refreshToken"
    assert_contains "$body" "OrganizationAccountAccessRole"
    assert_contains "$body" ".netrc"
}

test_the_correction_points_at_the_enforcing_scp_statement() {
    assert_contains "$(cat "$FOUNDATION")" "DenyStaticCredentialCreation"
}

# The master plan's Phase A predates the A1/A2 split and still describes the SCP wrongly.
test_the_master_plan_reflects_the_a1_a2_split() {
    local body
    body=$(cat "$MASTER")
    assert_contains "$body" "Phase A1"
    assert_contains "$body" "Phase A2"
    assert_contains "$body" "2026-07-31-phase-a1-account-bootstrap.md"
}

test_the_master_plan_scp_description_matches_the_shipped_policy() {
    local body
    body=$(cat "$MASTER")
    assert_contains "$body" "BoolIfExists"
    assert_contains "$body" "ec2:ModifyInstanceAttribute"
    assert_not_contains "$body" "except \`us-west-2\` and \`us-east-1\`"
}

# The Learner Lab is dead; no planning document may still reference its artefacts.
test_no_learner_lab_artefacts_survive_in_the_planning_documents() {
    local hits
    hits=$(grep -rnE 'LabRole|vockey|Learner Lab account|x86 t3\b' \
           "$FOUNDATION" "$DESIGN" "$MASTER" "$SECURITY" || true)
    assert_eq "$hits" ""
}

run_suite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_docs_claims.sh`
Expected: FAIL — `1..7` with six `not ok`. The first two record
```
not ok 3 - test_no_unqualified_static_credential_claim_remains
  # assert_eq: expected [] got [.../2026-07-30-aws-account-foundation-design.md:94:**No static AWS access key exists anywhere in this project.** ...]
not ok 6 - test_the_narrower_enforced_claim_is_present_in_all_four_documents
  # assert_contains: [No static AWS access key is created in, or issued to, the member account] not present missing from 2026-07-30-aws-account-foundation-design.md
```

- [ ] **Step 3: Write minimal implementation**

In `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`, replace the section 4.2 closing line:

> **No static AWS access key is created in, or issued to, the member account.** The SCP's `DenyStaticCredentialCreation` statement denies `iam:CreateUser`, `iam:CreateAccessKey`, `iam:UpdateAccessKey`, `iam:CreateLoginProfile`, `iam:UpdateLoginProfile`, and `iam:CreateServiceSpecificCredential`, so this is an enforced property of the account rather than a convention, and `bootstrap.sh` refuses to run under a static credential or an IAM-user principal.
>
> **Corrected 2026-07-31.** An earlier version of this line claimed no static AWS access key existed *anywhere in this project*. That was false, verified three ways, and the wider claim is what stopped anyone scoping the permission set below `AdministratorAccess` or questioning an unhashed install on the build box. (a) `OrganizationAccountAccessRole` in the member account trusts `arn:aws:iam::<mgmt>:root`, and two legacy IAM users with static keys live in that management account, unconstrainable by any SCP — by exactly the property section 3 celebrates for protecting RCAP. (b) `~/.aws/sso/cache/*.json` on the build box holds an `accessToken` **and a `refreshToken`** under a client registration valid to 2026-10-29: a portable, copyable, roughly 90-day credential to `AdministratorAccess` on the management account. "Short-lived" describes the session, not the refresh token that regenerates it. (c) `~/.netrc` holds `machine api.wandb.ai` in plaintext, and the `wandb` SDK prefers it silently. Consequences that bind: dependencies install from a hashed lock from day 1 rather than from Phase 1, and `aws sso logout` runs when the box is idle.

In `docs/2026-07-01-toxic-moderation-mlops-design.md` section 3.1, replace the "**No static AWS credentials exist.**" sentence:

> **No static AWS access key is created in, or issued to, the member account.** Humans authenticate through IAM Identity Center, GitHub Actions through OIDC, EC2 through instance profiles. The SCP's `DenyStaticCredentialCreation` statement makes it an enforced property rather than a convention. The wider claim — that no static AWS credential exists anywhere in this project — was corrected on 2026-07-31 and is false: see the AWS foundation spec section 4.2 for the three refutations.

In `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`, in the "AWS foundation" global constraint, replace "No static AWS credentials exist anywhere:" with:

> No static AWS access key is created in, or issued to, the member account, and the SCP's `DenyStaticCredentialCreation` statement enforces it:

In the same file, replace the Phase A heading block:

> ## Phase A: AWS account foundation (splits into A1 and A2)
>
> **A1 — account provisioning.** `infra/aws/bootstrap.sh`, `infra/aws/scp-sandbox-guardrails.json`, `infra/aws/a2-constraints.json`. **Runs first, on day 1**, because account creation is the only irreducible-latency task in the project. **Detailed plan:** `docs/superpowers/plans/2026-07-31-phase-a1-account-bootstrap.md`. **Branch:** `feat/phase-a1-account-bootstrap`.
>
> **A2 — infrastructure.** `infra/terraform/`, `.github/workflows/deploy.yml`, `docs/rcap-iam-audit.md`. Runs from day 9, when the slice is ready to deploy, so nothing bills while the application is still local. **Branch:** `feat/phase-a2-terraform`. A2 must satisfy every row of `infra/aws/a2-constraints.json`.

and replace Phase A tasks 2 and 3:

> 2. `infra/aws/scp-sandbox-guardrails.json`: eleven Deny statements. Region lock to `us-west-2` **only**, expressed as a `NotAction` exemption for named global services rather than by allowlisting a second region, which would leave every non-EC2, non-RDS service free to sprawl. The Graviton instance-type allowlist is `t4g.small`, `t4g.medium`, `t4g.large`, `c7g.xlarge`, applied to `ec2:RunInstances`, `ec2:CreateFleet`, `ec2:RequestSpotInstances`, and `ec2:RequestSpotFleet`, **scoped to `Resource: "arn:aws:ec2:*:*:instance/*"`**. `ec2:ModifyInstanceAttribute` is denied **unconditionally**, because `ec2:InstanceType` on that action resolves to the instance's current type and any condition there is inert. The two RDS require statements use **`BoolIfExists`** on create so an absent key fails closed, and **`Bool`** on `rds:ModifyDBInstance` where an absent key means "no change". **Do not attempt an RDS class cap with `rds:DatabaseClass`.** Detective-control denies cover `cloudtrail:UpdateTrail` and `guardduty:UpdateDetector`, not just delete and stop, and the trail bucket's deletes are restricted by ARN prefix. See spec section 5.1 and the A1 plan.
> 3. `infra/aws/bootstrap.sh`: idempotent, ten ordered steps per spec section 6. Step 2 is the single organization-root-wide write and is gated behind `--ack-org-root-write`. `create-account` is checked against the root email before creating, and its account id is persisted before any later step runs. The move into the OU immediately follows the status poll and is verified by `list-parents`, fail-closed. **The script never calls `iam enable-organizations-root-credentials-management` and never deletes a root credential.**

In `SECURITY.md`, replace the "No static AWS credentials exist." bullet:

> - No static AWS access key is created in, or issued to, the member account this project runs in, and the service control policy on its OU denies the API calls that would create one. Human access uses IAM Identity Center, CI uses GitHub OIDC with separate read and deploy roles, and compute uses instance profiles. This is a statement about the member account, not about every machine an operator uses; see `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md` section 4.2.

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_docs_claims.sh`
Expected: `1..7` and `# 7 tests, 0 failures`

- [ ] **Step 5: Commit**

```bash
git add docs SECURITY.md infra/aws/tests/test_docs_claims.sh
git commit -m "Scope the static-credential claim to the member account and split Phase A"
```

---

### Task 15a (C11): Scope the day-to-day permission set, and make the idle logout a target rather than a sentence

Task 15 above closes C11's **documentation** half honestly, and Task 9 closes the **install-hygiene** half properly — hashed locks, `test_bootstrap_installs_nothing`, and a live `C11-iam-createaccesskey-deny` probe. The **credential-scope** half is still a memo.

Task 15's own corrected text concedes that "the wider claim is what stopped anyone scoping the permission set below `AdministratorAccess`" and ends "Consequences that bind: … and `aws sso logout` runs when the box is idle." But Step 8 of the bootstrap still calls:

```bash
ensure_permission_set "$inst_arn" "MlopsToxicAdmin" \
    "arn:aws:iam::aws:policy/AdministratorAccess" "Day-to-day build and deploy"
```

and no test anywhere asserts that the day-to-day permission set is anything less than full admin, or that an idle-session logout exists. The refutation in Task 15 is specific about why that matters: `~/.aws/sso/cache/*.json` on the build box holds a **refresh token** valid for roughly ninety days. It is a portable, copyable, file-system credential to whatever this permission set grants. Granting it `AdministratorAccess` on a management account that can `AssumeRole` into the member account makes that file the single most valuable object on the Jetson. A correction that names a consequence and does not implement it is the same class of artifact C11 diagnosed.

What the project actually needs day to day is Terraform on one member account plus SSM, ECR, Secrets Manager, CloudWatch and Budgets — not organisations-wide admin. `MlopsToxicAdmin` is renamed and scoped; a separate, deliberately awkward break-glass path stays available for the two operations that genuinely need more.

**Files:**
- Modify: `infra/aws/bootstrap_account.sh`, `Makefile`
- Test: `infra/aws/tests/test_bootstrap.sh` (append)

- [ ] **Step 1: Write the failing test**

Append to `infra/aws/tests/test_bootstrap.sh`:
```bash
test_the_day_to_day_permission_set_is_not_administrator_access() {
    assert_not_contains "$(cat "$ROOT/infra/aws/bootstrap_account.sh")" \
        'MlopsToxicAdmin" \\\n             "arn:aws:iam::aws:policy/AdministratorAccess' \
        "premortem C11: the SSO refresh token on the build box is a ~90-day portable credential to whatever this permission set grants"
    assert_contains "$(cat "$ROOT/infra/aws/bootstrap_account.sh")" \
        "MlopsToxicDeploy" \
        "the day-to-day permission set is scoped to what Terraform and SSM need"
}

test_the_break_glass_set_exists_and_is_not_the_default_assignment() {
    local body assigned
    body=$(cat "$ROOT/infra/aws/bootstrap_account.sh")
    assert_contains "$body" "MlopsToxicBreakGlass" "no documented path for the two operations that need more"
    # The user is assigned to the DEPLOY set. Break-glass is created and provisioned but
    # left unassigned, so using it is a deliberate console action that CloudTrail records.
    assigned=$(printf '%s' "$body" | grep -c 'create-account-assignment' || true)
    assert_eq "$assigned" "1" "exactly one permission set is assigned by the bootstrap"
}

test_the_permission_set_session_duration_is_at_most_four_hours() {
    assert_contains "$(cat "$ROOT/infra/aws/bootstrap_account.sh")" "--session-duration PT4H" \
        "a long session is a long-lived credential on a laptop"
}

test_an_idle_logout_target_exists() {
    assert_contains "$(cat "$ROOT/Makefile")" "sso-logout:" \
        "Task 15 promises 'aws sso logout runs when the box is idle'; make it one command"
    assert_contains "$(cat "$ROOT/Makefile")" "aws sso logout"
}

test_the_deploy_permission_set_denies_creating_an_access_key() {
    # Defence in depth with the SCP: the permission set itself must not grant it either,
    # so the same denial holds in the MANAGEMENT account where no SCP applies.
    assert_contains "$(cat "$ROOT/infra/aws/deploy_permission_set.json")" '"iam:CreateAccessKey"'
    assert_contains "$(cat "$ROOT/infra/aws/deploy_permission_set.json")" '"Deny"'
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash infra/aws/tests/test_bootstrap.sh`
Expected: FAIL — `assert_contains: [MlopsToxicDeploy] absent`, `assert_contains: [MlopsToxicBreakGlass] absent`, `assert_contains: [sso-logout:] absent`, and a missing `infra/aws/deploy_permission_set.json`.

- [ ] **Step 3: Write minimal implementation**

1. Create `infra/aws/deploy_permission_set.json` — an inline policy for the day-to-day set. Start from the actions this project's Terraform and scripts actually call, and keep the two explicit denies at the end:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TerraformAndOperations",
      "Effect": "Allow",
      "Action": [
        "ec2:*", "rds:*", "iam:Get*", "iam:List*", "iam:CreateRole", "iam:DeleteRole",
        "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:PutRolePolicy",
        "iam:DeleteRolePolicy", "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile",
        "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
        "iam:PassRole", "iam:CreateOpenIDConnectProvider", "iam:TagRole",
        "ecr:*", "ssm:*", "secretsmanager:*", "logs:*", "cloudwatch:*", "sns:*",
        "budgets:*", "ce:Get*", "s3:*", "cloudtrail:*", "guardduty:*", "kms:*",
        "events:*", "scheduler:*", "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "C11NoStaticCredentialsEvenInTheManagementAccount",
      "Effect": "Deny",
      "Action": ["iam:CreateAccessKey", "iam:CreateUser", "iam:CreateLoginProfile"],
      "Resource": "*"
    },
    {
      "Sid": "NoOrganizationsWrites",
      "Effect": "Deny",
      "Action": ["organizations:*", "account:*"],
      "Resource": "*"
    }
  ]
}
```
   `iam:PassRole` is unscoped here only because the instance-profile names are not known until A2's first apply. Narrow it to `arn:aws:iam::<member>:role/${var.project}-*` in the A2 follow-up and record the date in the file's header comment.

2. In `bootstrap_account.sh` step 8, replace the admin set with two sets, and assign only the first:
```bash
    ps_arn=$(ensure_permission_set_inline "$inst_arn" "MlopsToxicDeploy" \
             "${SCRIPT_DIR}/deploy_permission_set.json" \
             "Day-to-day build and deploy. Scoped below AdministratorAccess: premortem C11.")
    ro_arn=$(ensure_permission_set "$inst_arn" "MlopsToxicReadOnly" \
             "arn:aws:iam::aws:policy/ReadOnlyAccess" "Grader or reviewer access")
    # Break-glass: created and provisioned so it EXISTS when it is needed at 2 a.m., and
    # deliberately NOT assigned, so reaching for it is a console action CloudTrail records.
    bg_arn=$(ensure_permission_set "$inst_arn" "MlopsToxicBreakGlass" \
             "arn:aws:iam::aws:policy/AdministratorAccess" \
             "Break glass only. Assign by hand, use, then remove the assignment.")
    persist_output BREAKGLASS_PERMISSION_SET_ARN "$bg_arn"
```
   `ensure_permission_set_inline` is `ensure_permission_set` with `put-inline-policy-to-permission-set --inline-policy file://$3` in place of `attach-managed-policy-to-permission-set`, and the same idempotent describe-first loop. Both keep `--session-duration PT4H`.

3. Add to the `Makefile`:
```makefile
.PHONY: sso-logout
sso-logout:  ## Drop the SSO access AND refresh token from ~/.aws/sso/cache (premortem C11)
	aws sso logout || true
	rm -f $(HOME)/.aws/sso/cache/*.json
	@test -z "$$(ls -A $(HOME)/.aws/sso/cache 2>/dev/null)" \
	  && echo "sso cache empty" || { echo "sso cache NOT empty"; exit 1; }
```
   `aws sso logout` alone does not always remove the client registration file, which is the object holding the refresh token, so the `rm` is the part that matters and the check is what proves it happened.

4. In Task 15's corrected text, change "and `aws sso logout` runs when the box is idle" to "and `make sso-logout` runs when the box is idle, which clears the refresh token as well as the session; the day-to-day permission set is `MlopsToxicDeploy`, scoped below `AdministratorAccess`, with break-glass admin created but unassigned."

- [ ] **Step 4: Run test to verify it passes**

Run: `bash infra/aws/tests/test_bootstrap.sh && bash infra/aws/tests/test_docs_claims.sh`
Expected: all green, including the existing idempotency cases — re-running the bootstrap must still perform no writes.

- [ ] **Step 5: Commit**

```bash
git add infra/aws/bootstrap_account.sh infra/aws/deploy_permission_set.json \
        infra/aws/tests/test_bootstrap.sh Makefile
git commit -m "Scope the day-to-day permission set below AdministratorAccess and add make sso-logout"
```

**Amendment to Task 16.** Add two live probes to the day-1 acceptance suite, run under the `MlopsToxicDeploy` session rather than `OrganizationAccountAccessRole`:
- `aws iam create-access-key --user-name whatever` must be denied by the **permission set** — this is the management-account case the SCP cannot reach.
- `aws organizations list-accounts` must be denied, proving the day-to-day session cannot reshape the organisation.

---

### Task 16 (H3, H17, H18, H19): The day-1 live acceptance suite

Everything up to here proves the SCP document says what it should. This proves AWS agrees. The two are different claims, and the gap between them is where `UnauthorizedOperation` lives on day 9.

The suite runs through `OrganizationAccountAccessRole`, which is a **member-account** role and is therefore subject to the SCP — unlike the management-account session that created everything. Running it under `rc-mgmt` directly would prove nothing, because SCPs never apply to the management account.

Every probe has a positive control where one is possible, because a suite that only ever observes denials cannot distinguish a working guardrail from a broken credential.

- **H3, allowed classes.** Launch one real instance of each of `t4g.small`, `t4g.medium`, `t4g.large`, `c7g.xlarge` in the default VPC and terminate every one from a `trap` that fires on success, failure, and interrupt. Real launches rather than dry runs, because this is the evidence that the topology can actually be built. Four instances alive for well under a minute costs cents.
- **H3, denied class.** `t4g.xlarge` — same family, outside the allowlist — so the probe tests the allowlist rather than the family. Plus `g5.xlarge` for the GPU case. A denied launch creates nothing, so no cleanup is needed.
- **H19, other launch paths.** `ec2:CreateFleet` with `t4g.xlarge`, expecting denial. This is the path that bypassed the guardrail before Task 4.
- **H19, region lock.** `describe-instances` in a non-home region must be denied, and the same call in `us-west-2` must succeed. That pair is the whole test; either alone proves nothing.
- **H18, key-absence semantics.** `iam simulate-custom-policy` over the six-cell matrix, then two real `create-db-instance` attempts. The real attempts are the conclusive ones: the compliant call must fail with `DBSubnetGroupNotFound`, which proves authorization passed before parameter validation, and the non-compliant call must fail with an SCP denial. Both fail, so neither creates a billable database.
- **H17, detective controls.** `cloudtrail:UpdateTrail`, `cloudtrail:PutEventSelectors`, and `guardduty:UpdateDetector` against non-existent resources. A denial arriving *before* a not-found error is the proof that the SCP evaluates first.
- **C11.** `iam:CreateAccessKey` and `iam:CreateUser` must be denied.

Evidence goes to `infra/aws/acceptance-evidence.json`, which holds the raw account id and is therefore gitignored; a masked summary prints to the terminal for the submission screenshots.

**Files:**
- Create: `infra/aws/tests/acceptance/run_acceptance.sh`
- Modify: `.gitignore`

- [ ] **Step 1: Write the failing test**

The acceptance script is itself the test: it exits non-zero if any probe returns the wrong verdict.

`infra/aws/tests/acceptance/run_acceptance.sh`, part 1 of 2 — setup, helpers, and the H3 and H19 probes:
```bash
#!/usr/bin/env bash
# Day-1 live acceptance for the Sandbox OU guardrails. Runs against the real member
# account through OrganizationAccountAccessRole, which is a member-account role and is
# therefore subject to the SCP, unlike the management-account session that created it.
#
# Every probe that could create a resource either creates one it immediately terminates,
# or is constructed so authorization is the only thing under test and the call fails on a
# later validation step. Nothing is left running.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
AWS_DIR="$(cd "$HERE/../.." && pwd)"
# shellcheck disable=SC1091
. "$AWS_DIR/bootstrap-outputs.env"

PROFILE="${BOOTSTRAP_PROFILE:-rc-mgmt}"
REGION="${AWS_REGION:-us-west-2}"
OFF_REGION="${ACCEPT_OFF_REGION:-eu-west-1}"
EVIDENCE="$AWS_DIR/acceptance-evidence.json"
ALLOWED=(t4g.small t4g.medium t4g.large c7g.xlarge)
DENIED=(t4g.xlarge g5.xlarge)
LAUNCHED=()
PASS=0
FAIL=0
RESULTS="[]"

member() { aws "$@" --region "$REGION"; }

cleanup() {
    if [ "${#LAUNCHED[@]}" -gt 0 ]; then
        printf '[accept] terminating %d probe instances\n' "${#LAUNCHED[@]}" >&2
        member ec2 terminate-instances --instance-ids "${LAUNCHED[@]}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

creds=$(aws sts assume-role --profile "$PROFILE" --region "$REGION" \
        --role-arn "arn:aws:iam::${ACCOUNT_ID}:role/OrganizationAccountAccessRole" \
        --role-session-name mlops-toxic-acceptance)
AWS_ACCESS_KEY_ID=$(printf '%s' "$creds" | jq -r '.Credentials.AccessKeyId')
AWS_SECRET_ACCESS_KEY=$(printf '%s' "$creds" | jq -r '.Credentials.SecretAccessKey')
AWS_SESSION_TOKEN=$(printf '%s' "$creds" | jq -r '.Credentials.SessionToken')
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset AWS_PROFILE

record() { # name verdict detail
    RESULTS=$(printf '%s' "$RESULTS" | jq -c --arg n "$1" --arg v "$2" --arg d "$3" \
              '. + [{probe:$n, verdict:$v, detail:$d}]')
    if [ "$2" = "PASS" ]; then
        PASS=$((PASS + 1)); printf '[accept] PASS %s\n' "$1"
    else
        FAIL=$((FAIL + 1)); printf '[accept] FAIL %s - %s\n' "$1" "$3"
    fi
}

is_denied() { # error-text
    case "$1" in
        *UnauthorizedOperation*|*AccessDenied*|*"explicit deny in a service control policy"*) return 0 ;;
    esac
    return 1
}

brief() { printf '%s' "$1" | head -2 | tr '\n' ' '; }

AMI=$(member ssm get-parameter \
        --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
      | jq -r '.Parameter.Value')
AMI_X86=$(member ssm get-parameter \
        --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
      | jq -r '.Parameter.Value')

# --- H3: every allowlisted class must actually launch --------------------------------
for t in "${ALLOWED[@]}"; do
    out=$(member ec2 run-instances --image-id "$AMI" --instance-type "$t" --count 1 \
              --instance-initiated-shutdown-behavior terminate \
              --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=scp-probe}]" 2>&1)
    id=$(printf '%s' "$out" | jq -r '.Instances[0].InstanceId' 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "null" ]; then
        LAUNCHED+=("$id")
        record "H3-allow-$t" PASS "launched $id"
    else
        record "H3-allow-$t" FAIL "$(brief "$out")"
    fi
done

# --- H3: a class outside the allowlist, and a GPU class, must be denied ---------------
for t in "${DENIED[@]}"; do
    ami="$AMI"
    case "$t" in g5.*) ami="$AMI_X86" ;; esac
    out=$(member ec2 run-instances --image-id "$ami" --instance-type "$t" --count 1 2>&1)
    if is_denied "$out"; then
        record "H3-deny-$t" PASS "denied"
    else
        record "H3-deny-$t" FAIL "not denied: $(brief "$out")"
    fi
done

# --- H19: CreateFleet is a separate launch path and must carry the same allowlist -----
out=$(member ec2 create-fleet --type instant \
          --target-capacity-specification 'TotalTargetCapacity=1,DefaultTargetCapacityType=on-demand' \
          --launch-template-configs '[{"Overrides":[{"InstanceType":"t4g.xlarge"}]}]' 2>&1)
if is_denied "$out"; then
    record "H19-createfleet-deny" PASS "denied"
else
    record "H19-createfleet-deny" FAIL "not denied: $(brief "$out")"
fi

# --- H19: region lock, with its positive control -------------------------------------
out=$(aws ec2 describe-instances --region "$OFF_REGION" --max-items 1 2>&1)
if is_denied "$out"; then
    record "H19-region-deny-$OFF_REGION" PASS "denied"
else
    record "H19-region-deny-$OFF_REGION" FAIL "$OFF_REGION is reachable"
fi

if member ec2 describe-instances --max-items 1 >/dev/null 2>&1; then
    record "H19-region-allow-$REGION" PASS "reachable"
else
    record "H19-region-allow-$REGION" FAIL "the home region is denied; the SCP is over-broad"
fi
```

`infra/aws/tests/acceptance/run_acceptance.sh`, part 2 of 2 — the H18, H17, and C11 probes and the evidence file. Append directly to part 1:
```bash
# --- H18: key-absence semantics, simulated then observed ------------------------------
SCP_STMTS=$(jq -c '{Version, Statement: [.Statement[] | select(.Sid | startswith("DenyRds"))]}' \
            "$AWS_DIR/scp-sandbox-guardrails.json")
ALLOW_ALL='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'

simulate() { # name expected_decision context-entry...
    local name="$1" want="$2"
    shift 2
    local got
    got=$(aws iam simulate-custom-policy --profile "$PROFILE" --region "$REGION" \
              --policy-input-list "$ALLOW_ALL" "$SCP_STMTS" \
              --action-names rds:CreateDBInstance \
              ${1:+--context-entries "$@"} \
          | jq -r '.EvaluationResults[0].EvalDecision')
    if [ "$got" = "$want" ]; then
        record "$name" PASS "$got"
    else
        record "$name" FAIL "expected $want got $got"
    fi
}

MMUP_T="ContextKeyName=rds:ManageMasterUserPassword,ContextKeyValues=true,ContextKeyType=boolean"
MMUP_F="ContextKeyName=rds:ManageMasterUserPassword,ContextKeyValues=false,ContextKeyType=boolean"
PUB_T="ContextKeyName=rds:PubliclyAccessible,ContextKeyValues=true,ContextKeyType=boolean"
PUB_F="ContextKeyName=rds:PubliclyAccessible,ContextKeyValues=false,ContextKeyType=boolean"

simulate "H18-managed-password-true"   allowed      "$MMUP_T" "$PUB_F"
simulate "H18-managed-password-false"  explicitDeny "$MMUP_F" "$PUB_F"
simulate "H18-managed-password-absent" explicitDeny "$PUB_F"
simulate "H18-public-false"            allowed      "$MMUP_T" "$PUB_F"
simulate "H18-public-true"             explicitDeny "$MMUP_T" "$PUB_T"
simulate "H18-public-absent"           explicitDeny "$MMUP_T"

# Observed rather than simulated. Both calls fail; only the REASON differs, and that is
# the test. Neither creates a database, so neither costs anything.
out=$(member rds create-db-instance --db-instance-identifier scp-probe-compliant \
          --db-instance-class db.t4g.micro --engine postgres --allocated-storage 20 \
          --master-username probe --manage-master-user-password --no-publicly-accessible \
          --db-subnet-group-name scp-probe-nonexistent-subnet-group 2>&1)
if is_denied "$out"; then
    record "H18-observed-compliant-create" FAIL "the compliant path is denied; BoolIfExists is over-broad"
elif printf '%s' "$out" | grep -q 'DBSubnetGroupNotFound'; then
    record "H18-observed-compliant-create" PASS "authorized; failed on the subnet group, as designed"
else
    record "H18-observed-compliant-create" FAIL "unexpected: $(brief "$out")"
fi

out=$(member rds create-db-instance --db-instance-identifier scp-probe-noncompliant \
          --db-instance-class db.t4g.micro --engine postgres --allocated-storage 20 \
          --master-username probe --master-user-password 'Probe-Pass-1234' \
          --db-subnet-group-name scp-probe-nonexistent-subnet-group 2>&1)
if is_denied "$out"; then
    record "H18-observed-omitted-key-create" PASS "denied; fails closed"
else
    record "H18-observed-omitted-key-create" FAIL "an omitted key was permitted: $(brief "$out")"
fi

# --- H17: detective controls. Denial must arrive BEFORE not-found ---------------------
out=$(member cloudtrail update-trail --name scp-probe-nonexistent-trail \
          --s3-bucket-name scp-probe-elsewhere 2>&1)
if is_denied "$out"; then
    record "H17-cloudtrail-updatetrail-deny" PASS "denied"
else
    record "H17-cloudtrail-updatetrail-deny" FAIL "reached the API: $(brief "$out")"
fi

out=$(member cloudtrail put-event-selectors --trail-name scp-probe-nonexistent-trail \
          --event-selectors '[]' 2>&1)
if is_denied "$out"; then
    record "H17-cloudtrail-puteventselectors-deny" PASS "denied"
else
    record "H17-cloudtrail-puteventselectors-deny" FAIL "reached the API: $(brief "$out")"
fi

out=$(member guardduty update-detector \
          --detector-id 00000000000000000000000000000000 --no-enable 2>&1)
if is_denied "$out"; then
    record "H17-guardduty-updatedetector-deny" PASS "denied"
else
    record "H17-guardduty-updatedetector-deny" FAIL "reached the API: $(brief "$out")"
fi

# --- C11: the account cannot mint a long-lived credential -----------------------------
out=$(member iam create-access-key --user-name scp-probe-nonexistent-user 2>&1)
if is_denied "$out"; then
    record "C11-iam-createaccesskey-deny" PASS "denied"
else
    record "C11-iam-createaccesskey-deny" FAIL "reached the API: $(brief "$out")"
fi

out=$(member iam create-user --user-name scp-probe-user 2>&1)
if is_denied "$out"; then
    record "C11-iam-createuser-deny" PASS "denied"
else
    record "C11-iam-createuser-deny" FAIL "reached the API: $(brief "$out")"
fi

jq -n --argjson r "$RESULTS" --arg a "$ACCOUNT_ID" \
      --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson p "$PASS" --argjson f "$FAIL" \
   '{account_id: $a, timestamp_utc: $ts, passed: $p, failed: $f, probes: $r}' >"$EVIDENCE"
chmod 600 "$EVIDENCE"

printf '\n[accept] %d passed, %d failed. Evidence: %s\n' "$PASS" "$FAIL" "$EVIDENCE"
jq -r '.probes[] | "  \(.verdict)  \(.probe)"' "$EVIDENCE"
[ "$FAIL" -eq 0 ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `make -C infra/aws accept`
Expected, before any account exists: FAIL immediately with
`.../run_acceptance.sh: line 15: .../bootstrap-outputs.env: No such file or directory`.
The suite cannot pass until the bootstrap has run, which is Task 17.

- [ ] **Step 3: Write minimal implementation**

Add to `.gitignore`, under the existing account-identifier block:
```
# Live acceptance evidence. Carries the raw member account id.
infra/aws/acceptance-evidence.json
```

- [ ] **Step 4: Verify the suite is syntactically sound and lints clean**

Run: `bash -n infra/aws/tests/acceptance/run_acceptance.sh && make -C infra/aws lint && git check-ignore -q infra/aws/acceptance-evidence.json && echo IGNORED`
Expected: no syntax error, shellcheck silent, then `IGNORED`.

- [ ] **Step 5: Commit**

```bash
git add infra/aws/tests/acceptance/run_acceptance.sh .gitignore
git commit -m "Add day-1 live acceptance suite for the Sandbox OU guardrails"
```

---

### Task 17: Run it for real, capture the evidence, open the PR

Everything before this is offline. This is the day-1 execution: the account gets created, the break-glass gets established or fails to, and the guardrails get proven against AWS. It is deliberately the last task, so a mail-delivery failure on the root address is discovered with the whole toolchain already tested rather than half-written.

- [ ] **Step 1: Full offline suite and lint green**

Run: `make -C infra/aws lint && make -C infra/aws test`
Expected: shellcheck silent, `jq empty` silent, and every suite reporting `0 failures` — 3 + 7 + 21 + 5 + 10 + 23 + 9 + 7 = 85 tests across eight files.

- [ ] **Step 2: Preflight against the real management account**

```bash
aws sso login --profile rc-mgmt
BOOTSTRAP_ALT_CONTACT_PHONE='+1XXXXXXXXXX' bash infra/aws/bootstrap.sh --preflight-only
```
Expected: `[bootstrap] preflight passed` then `[bootstrap] preflight only; stopping`.

If it prints `caller ... is not an IAM Identity Center session`, the SSO session has expired; re-run `aws sso login`. If it prints a `~/.netrc` or `~/.aws/credentials` warning, that is C11 evidence rather than a failure — record it and continue.

- [ ] **Step 3: Run the bootstrap**

```bash
BOOTSTRAP_ALT_CONTACT_PHONE='+1XXXXXXXXXX' bash infra/aws/bootstrap.sh --ack-org-root-write
```

Expected: the step 2 blast-radius exception prints and, being acknowledged, the policy type is enabled once. Steps 3 through 6 create the OU, the SCP, the account, and the governance. Step 7 blocks on the operator gate.

At the gate, run root password recovery for `rock+aws-mlops-toxic@rockcyber.com`. **This is the moment this phase exists to reach on day 1.** If nothing arrives within a few minutes, Mimecast recipient validation has rejected the plus-addressed mail. That is recoverable, and it must be recovered now rather than on day 15: the management account can change a member account's root email without any root credential, so point it at an alias such as `aws-mlops@rockcyber.com` — an alias is not a mailbox and costs no licence seat — and retry recovery.

Then set a strong password, store it in `pass`, enrol MFA, confirm no root access keys exist, and type `BREAKGLASS-DONE`.

Expected tail: steps 8 through 10 complete and the script prints `[bootstrap] bootstrap complete`, with every account id rendered as `<account-id>`.

- [ ] **Step 4: Re-run for idempotency, then run the acceptance suite**

Run: `BOOTSTRAP_ALT_CONTACT_PHONE='+1XXXXXXXXXX' bash infra/aws/bootstrap.sh --skip-operator-gate`
Expected: every step reports `no write performed`, including `step 2: SERVICE_CONTROL_POLICY already ENABLED on r-...`, and the run needs no `--ack-org-root-write`.

Run: `make -C infra/aws accept`
Expected: `[accept] 22 passed, 0 failed`, every probe `PASS`, and the four probe instances terminated by the trap.

Confirm nothing survived:
```bash
aws ec2 describe-instances --profile rc-mgmt --region us-west-2 \
  --filters Name=tag:Name,Values=scp-probe Name=instance-state-name,Values=running,pending \
  | jq '.Reservations | length'
```
Expected: `0`.

If `H3-allow-c7g.xlarge` fails on capacity rather than authorization, that is an AWS-side condition and not a guardrail defect. Re-run it, and record the distinction in the evidence file rather than relaxing the allowlist.

- [ ] **Step 5: Update the handoff and open the PR**

Update `docs/HANDOFF.md`: Stage C is complete for A1; the member account exists in the `Sandbox` OU with the SCP attached and observed to deny; the break-glass is established; the Terraform state bucket exists; the next artefact is the Phase A2 plan. Record the measured `UNGOVERNED_WINDOW_SECONDS` and the acceptance pass count. Do not paste the account id.

```bash
git add docs/HANDOFF.md
git commit -m "Record Phase A1 completion and the measured guardrail acceptance results"
git push -u origin feat/phase-a1-account-bootstrap
gh pr create --base main --title "Phase A1: AWS account provisioning and Sandbox OU guardrails" \
  --body "Idempotent ten-step bootstrap, an eleven-statement SCP, and an A2 constraints file, with 85 offline tests and a 22-probe live acceptance suite run against the real account. Closes premortem H3, H17, H18, H19, C11, and the three bootstrap idempotency defects. The account exists in the Sandbox OU with the SCP attached and observed to deny; break-glass established; Terraform state bucket created."
```

---

## Self-Review

**Premortem coverage.** Every finding assigned to this phase has an owning task whose test fails if the finding is unfixed.

| Finding | Owning task | The test that fails without the fix |
|---|---|---|
| H3 — allowlist never enumerated | 3, 16 | `test_instance_type_allowlist_is_exactly_the_four_required_classes`, `test_instance_type_statement_is_scoped_to_the_instance_resource`, live `H3-allow-*` and `H3-deny-*` |
| H18 — Bool vs BoolIfExists key absence | 5, 16 | `test_rds_managed_password_requirement_fails_closed_on_key_absence`, `test_rds_public_endpoint_denial_fails_closed_on_create`, `test_rds_modify_uses_bool_because_absence_means_no_change`, six live `simulate` cells plus two observed `create-db-instance` probes |
| H19 — non-RunInstances launch paths, and a wholesale second region | 4, 7, 16 | `test_all_four_launch_paths_carry_the_allowlist`, `test_modify_instance_attribute_is_denied_without_a_condition`, `test_region_lock_permits_only_us_west_2`, live `H19-createfleet-deny` and the region-lock pair |
| H17 — incomplete detective controls | 6, 8, 16 | `test_detective_control_denies_cover_disable_and_redirect`, `test_trail_bucket_evidence_deletion_is_denied`, `test_trail_bucket_prefix_matches_the_scp_arn_pattern`, live `H17-*-deny` |
| C11 — false no-static-credentials claim, unhashed installs | 1, 7, 9, 15, 16 | `test_bootstrap_installs_nothing`, `test_preflight_refuses_a_static_access_key_in_the_environment`, `test_preflight_refuses_an_iam_user_caller`, `test_static_credential_creation_is_denied`, `test_no_unqualified_static_credential_claim_remains`, live `C11-iam-*-deny` |
| Idempotency defect 1 — step 2 is an org-root-wide write | 10 | `test_step2_makes_no_write_when_the_policy_type_is_already_enabled`, `test_step2_refuses_the_org_root_write_without_an_explicit_acknowledgement`, `test_step2_acknowledgement_states_what_does_not_change` |
| Idempotency defect 2 — create-account is async and not idempotent | 12 | `test_step5_adopts_an_existing_account_matched_on_root_email`, `test_step5_paginates_before_concluding_the_account_is_absent`, `test_step5_persists_the_request_id_before_it_starts_polling`, `test_account_id_is_persisted_before_any_later_step_runs` |
| Idempotency defect 3 — ungoverned window in the org root | 13 | `test_nothing_runs_between_account_creation_and_governance`, `test_the_scp_is_attached_before_the_account_is_created`, `test_step6_refuses_to_continue_when_the_account_is_not_in_the_ou` |
| C6 — handed forward, not owned here | 8 | `test_every_finding_this_phase_owns_has_at_least_one_constraint_or_is_scp_only` requires an A2 egress row |
| Whole-script idempotency | 14 | `test_a_second_run_performs_no_writes` |

**Spec coverage.** Foundation spec section 6's ten ordered steps map to `preflight` plus `step2` through `step10`, in the stated order, with the two additions the premortem required: an existence check at every step, and an acknowledgement gate on the one step that writes outside the OU. Section 5.1's four implementation traps each close under a named test — the resource-scoping trap on `ec2:InstanceType` (Task 3), the `rds:DatabaseClass` trap (Task 5's regression guard), trap 3's key-absence semantics (Task 5), trap 4's detective-control gaps (Task 6). Section 5.2's root posture holds: `test_bootstrap_never_touches_root_credentials` fails if the script ever gains `enable-organizations-root-credentials-management`, `delete-login-profile`, or `assume-root`. Section 5.3's decision to decline an automated budget stop action is what makes the instance-type allowlist load-bearing, which is why Task 16 launches real instances rather than dry-running them. Delivery spec section 4's three-instance topology drives the allowlist and is cross-checked against `a2-constraints.json`. Delivery spec section 3.1's rationale for A1 running first is why Task 17 is last: the mail-delivery gate is reached with the toolchain already proven rather than half-written.

**Rubric coverage.** This phase earns no rubric points directly, which delivery spec section 13 already records as accepted opportunity cost. It carries two indirect obligations and both are met. Rubric 5.2 requires three separate EC2 instances, and the SCP allowlist is what permits them — `test_the_three_instances_are_named_with_their_classes` fails if the constraints file and the rubric-driven topology diverge. The Deliverables list requires screenshots with no account id visible, and `test_the_account_id_never_reaches_stdout_or_stderr` enforces that at the source rather than at the screenshot.

**Placeholder scan.** Every step carries real code and an exact command. No TODO, no "handle edge cases", no "similar to", no elided block. The three values that cannot be committed to a public repository are handled explicitly rather than left blank: `BOOTSTRAP_ALT_CONTACT_PHONE` defaults to a placeholder that preflight **refuses**, so a real number must be supplied at run time and never lands in the repository; `ACCOUNT_ID` is discovered at run time and masked in every log line; the assumed-role secret lives only inside a subshell and has a test asserting it reaches neither stream. The SCP's size was measured rather than estimated: 2660 bytes compacted, against the 5120-byte quota.

**Code verified before writing.** The harness, the stub, the sequenced-fixture mechanism, the call-ordering assertion, `persist_output`, indirect expansion under `set -u`, the `set -e` behaviour of a `&&` list, the Terraform and AWS CLI version parsing, the account-id masking, and the SCP byte count were each executed on this machine before being written into this plan. Premortem C1 and C3 exist precisely because the previous plan's code had never been run, and two of that plan's own tests failed on its own fixture.

**Type consistency.** `bootstrap-outputs.env` is `KEY=VALUE`, `LC_ALL=C` sorted, mode 600, and every key listed under Interfaces Produced is asserted present by `test_the_outputs_file_carries_every_interface_key_and_is_mode_600`. The `instance_type_allowlist` in `a2-constraints.json` is asserted equal to the SCP's `ec2:InstanceType` array, and its `trail_bucket_prefix` is asserted to reconstruct the SCP's S3 ARN, so those two committed files cannot drift apart silently. The `org()` wrapper is the single place `--profile` and `--region` are applied; member-account calls use a separate `member()` wrapper with no profile, so the two credential contexts cannot be confused. The master plan's Interface Contracts block defines Python seams only and is untouched by this phase; the master plan's **Phase A section** is edited at source in Task 15, per the delivery spec's rule that a supersession table is not a merge.

**Known residual risk, accepted and named.**

- `ec2:InstanceType` behaviour is proven live for `RunInstances` and `CreateFleet` and inferred for `RequestSpotInstances` and `RequestSpotFleet`. If a probe ever shows a spot request slipping through, the fix is an unconditional deny on those two actions, since this project never uses spot.
- The trail-bucket ARN prefix couples the SCP to a Terraform name A2 has not written yet. Task 8's cross-file test catches drift between the two committed files, but it cannot catch A2 choosing a different bucket name — that is A2's test, listed as constraint `A2-C05`.
- The `s3:DeleteObject` denial on the trail bucket means `terraform destroy` cannot empty it. Recorded as `A2-C07` now, rather than discovered on day 19.
- The management account's two legacy IAM users with static keys are unchanged and unconstrainable by any SCP. Out of scope by decision, named honestly in the corrected section 4.2, and the subject of the separate read-only RCAP audit.
- Denying `ec2:ModifyInstanceAttribute` outright trades a documented resize workflow for a guardrail that actually holds. The replacement path — change the variable, `terraform apply`, RunInstances against the allowlist — has strictly better provenance, and `c7g.xlarge` is in the allowlist precisely so that path stays open.

## Execution Handoff

Two options:

1. **Subagent-Driven (recommended):** a fresh subagent per task with review between tasks. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`. Tasks 3 through 8 all touch the SCP and its test file, and tasks 9 through 14 all touch `bootstrap.sh`, so each group runs strictly in order.
2. **Inline Execution:** in-session with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`. Task 17 must be inline regardless, because the step 7 operator gate needs a human at a browser and the root-mail outcome cannot be predicted.
