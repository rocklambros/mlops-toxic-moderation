"""`infra/aws/bootstrap.sh`, exercised end to end with a fake AWS CLI on PATH.

The stub records every invocation to a call log and replays canned JSON keyed on
`<service>_<operation>`, with `.1.json`/`.2.json` sequencing for asynchronous polling and
a loud exit 90 when a fixture is missing, so a forgotten fixture fails a test instead of
silently passing it. That is what makes the account-creation path testable without
creating an account: nothing here touches AWS.

Two conventions the stub imposes on the script, each pinned by a test below: service and
operation precede every flag, and responses are parsed with `jq` rather than `--query`,
because the stub replays whole documents and cannot emulate server-side JMESPath.

The three defects these tests exist to hold closed:

1. `organizations:EnablePolicyType` is the one organization-root-wide write in a script
   whose stated blast radius is the Sandbox OU. It must not run when the policy type is
   already enabled, and when it must run it has to name the invariant it breaks.
2. `organizations:CreateAccount` is asynchronous *and* not idempotent, and neither fix
   covers the other: an existence check on the organization-unique root email before
   creating, and the account id persisted the instant it is known.
3. A created account lands in the organization root, where no SCP applies to it. The
   window between creation and the move into the governed OU is asserted on the call log,
   so a future edit that schedules work inside it fails the suite.
"""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOT = REPO_ROOT / "infra" / "aws" / "bootstrap.sh"
SCP = REPO_ROOT / "infra" / "aws" / "scp-sandbox-guardrails.json"

ROOT_EMAIL = "rock+aws-mlops-toxic@rockcyber.com"
ACCOUNT_ID = "123456789012"

# Every mutating verb the AWS CLI uses in this script's surface area.
WRITE_VERB = re.compile(
    r"_(create|update|delete|attach|detach|move|put|enable|provision|tag|register)"
)

AWS_STUB = r"""#!/usr/bin/env bash
# Fake `aws` CLI. Records every invocation to $AWS_STUB_CALLLOG and replays a canned
# response from $AWS_STUB_DIR/<service>_<operation>[.<n>].json, exiting with the code in
# a matching .rc file when one exists. A call with no fixture exits 90 loudly.
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
"""

TERRAFORM_STUB = r"""#!/usr/bin/env bash
# Fake `terraform`, present only so the preflight's version probe is hermetic.
set -u
if [ "${1:-}" = "version" ]; then
    printf '{"terraform_version":"%s"}\n' "${TERRAFORM_STUB_VERSION:-1.15.8}"
    exit 0
fi
exit 0
"""

GH_STUB = r"""#!/usr/bin/env bash
# Fake `gh`, present only so the preflight's auth probe is hermetic.
set -u
exit "${GH_STUB_RC:-0}"
"""


class Stub:
    """A scratch fixture directory, a call log, and the three stubs first on PATH."""

    def __init__(self, tmp_path):
        self.fixtures = tmp_path / "fixtures"
        self.fixtures.mkdir()
        self.calllog = tmp_path / "calls.log"
        self.calllog.write_text("")
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        for name, body in (("aws", AWS_STUB), ("terraform", TERRAFORM_STUB), ("gh", GH_STUB)):
            path = self.bindir / name
            path.write_text(body)
            path.chmod(0o755)
        self.outputs = tmp_path / "bootstrap-outputs.env"
        self.home = tmp_path / "home"
        self.home.mkdir()
        # HOME is redirected so the advisory on-disk credential warnings are deterministic
        # and no test reads the operator's real ~/.aws or ~/.netrc.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        self.env.update(
            {
                "PATH": f"{self.bindir}:{os.environ['PATH']}",
                "HOME": str(self.home),
                "AWS_STUB_DIR": str(self.fixtures),
                "AWS_STUB_CALLLOG": str(self.calllog),
                "BOOTSTRAP_OUTPUTS_FILE": str(self.outputs),
                "BOOTSTRAP_POLL_INTERVAL": "0",
                "BOOTSTRAP_CREATE_ACCOUNT_TIMEOUT": "3",
                "BOOTSTRAP_ALT_CONTACT_PHONE": "+13035550100",
            }
        )

    # --- fixture authoring ---
    def fixture(self, key, body):
        (self.fixtures / f"{key}.json").write_text(body)

    def rc(self, key, code):
        (self.fixtures / f"{key}.rc").write_text(str(code))

    # --- call log inspection ---
    def _lines(self):
        return [ln for ln in self.calllog.read_text().splitlines() if ln]

    def calls(self):
        return [ln.split(" ", 1)[0] for ln in self._lines()]

    def call_args(self, key):
        for line in self._lines():
            if line.split(" ", 1)[0] == key:
                return line
        return ""

    def all_call_args(self, key):
        return [ln for ln in self._lines() if ln.split(" ", 1)[0] == key]

    def writes(self):
        return [c for c in self.calls() if WRITE_VERB.search(c)]

    def index(self, key):
        calls = self.calls()
        return calls.index(key) + 1 if key in calls else 0

    def last_index(self, key):
        calls = self.calls()
        return len(calls) - calls[::-1].index(key) if key in calls else 0

    def call_at(self, position):
        calls = self.calls()
        return calls[position - 1] if 0 < position <= len(calls) else ""

    def clear_calls(self):
        self.calllog.write_text("")

    # --- running the script ---
    def bash(self, script, env=None, stdin=""):
        environment = dict(self.env)
        if env:
            environment.update(env)
        return subprocess.run(
            ["bash", "-c", script],
            cwd=REPO_ROOT,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
        )

    def source(self, snippet, **kwargs):
        """Load every step function without running main, then run the snippet."""
        return self.bash(f'BOOTSTRAP_SOURCE_ONLY=1 . "{BOOT}"\n{snippet}\n', **kwargs)

    def run_main(self, *args, **kwargs):
        return self.bash(f'bash "{BOOT}" {" ".join(args)}', **kwargs)

    def outputs_text(self):
        return self.outputs.read_text() if self.outputs.exists() else ""


@pytest.fixture
def stub(tmp_path):
    return Stub(tmp_path)


def identity_fixtures(stub):
    stub.fixture(
        "sts_get-caller-identity",
        json.dumps(
            {
                "Arn": "arn:aws:sts::111111111111:assumed-role/"
                "AWSReservedSSO_AdministratorAccess_abc/rock.lambros",
                "Account": "111111111111",
            }
        ),
    )
    stub.fixture(
        "organizations_describe-organization",
        '{"Organization":{"Id":"o-abc","MasterAccountId":"111111111111"}}',
    )


def scp_content_fixture(stub):
    """describe-policy replaying the document this repository actually ships."""
    compact = json.dumps(json.loads(SCP.read_text()), separators=(",", ":"))
    stub.fixture("organizations_describe-policy", json.dumps({"Policy": {"Content": compact}}))


def full_first_run_fixtures(stub):
    """An organization holding nothing this script creates."""
    identity_fixtures(stub)
    stub.fixture(
        "organizations_list-roots",
        '{"Roots":[{"Id":"r-abcd","PolicyTypes":'
        '[{"Type":"SERVICE_CONTROL_POLICY","Status":"ENABLED"}]}]}',
    )
    stub.fixture("organizations_list-organizational-units-for-parent", '{"OrganizationalUnits":[]}')
    stub.fixture(
        "organizations_create-organizational-unit",
        '{"OrganizationalUnit":{"Id":"ou-abcd-1111"}}',
    )
    stub.fixture("organizations_list-policies", '{"Policies":[]}')
    stub.fixture("organizations_create-policy", '{"Policy":{"PolicySummary":{"Id":"p-1111"}}}')
    stub.fixture("organizations_list-policies-for-target", '{"Policies":[]}')
    stub.fixture("organizations_attach-policy", "{}")
    stub.fixture("organizations_list-accounts", '{"Accounts":[]}')
    stub.fixture(
        "organizations_create-account",
        '{"CreateAccountStatus":{"Id":"car-1","State":"IN_PROGRESS"}}',
    )
    stub.fixture(
        "organizations_describe-create-account-status.1",
        '{"CreateAccountStatus":{"State":"IN_PROGRESS"}}',
    )
    stub.fixture(
        "organizations_describe-create-account-status.2",
        '{"CreateAccountStatus":{"State":"SUCCEEDED","AccountId":"' + ACCOUNT_ID + '"}}',
    )
    stub.fixture("organizations_list-parents.1", '{"Parents":[{"Id":"r-abcd","Type":"ROOT"}]}')
    stub.fixture("organizations_move-account", "{}")
    stub.fixture(
        "organizations_list-parents.2",
        '{"Parents":[{"Id":"ou-abcd-1111","Type":"ORGANIZATIONAL_UNIT"}]}',
    )
    stub.fixture("account_get-alternate-contact", "{}")
    stub.rc("account_get-alternate-contact", 255)
    stub.fixture("account_put-alternate-contact", "{}")


def steady_state_fixtures(stub):
    """An organization in which everything this script creates already exists."""
    identity_fixtures(stub)
    stub.fixture(
        "organizations_list-roots",
        '{"Roots":[{"Id":"r-abcd","PolicyTypes":'
        '[{"Type":"SERVICE_CONTROL_POLICY","Status":"ENABLED"}]}]}',
    )
    stub.fixture(
        "organizations_list-organizational-units-for-parent",
        '{"OrganizationalUnits":[{"Id":"ou-abcd-1111","Name":"Sandbox"}]}',
    )
    stub.fixture(
        "organizations_list-policies",
        '{"Policies":[{"Id":"p-1111","Name":"sandbox-guardrails"}]}',
    )
    scp_content_fixture(stub)
    stub.fixture("organizations_list-policies-for-target", '{"Policies":[{"Id":"p-1111"}]}')
    stub.fixture(
        "organizations_list-accounts",
        '{"Accounts":[{"Id":"' + ACCOUNT_ID + '","Email":"' + ROOT_EMAIL + '"}]}',
    )
    stub.fixture("organizations_list-parents", '{"Parents":[{"Id":"ou-abcd-1111"}]}')
    stub.fixture(
        "account_get-alternate-contact",
        '{"AlternateContact":{"EmailAddress":"rock@rockcyber.com"}}',
    )
    stub.fixture(
        "sso-admin_list-instances",
        '{"Instances":[{"InstanceArn":"arn:aws:sso:::instance/ssoins-abc",'
        '"IdentityStoreId":"d-123"}]}',
    )
    stub.fixture(
        "identitystore_list-users", '{"Users":[{"UserId":"u-1","UserName":"rock.lambros"}]}'
    )
    stub.fixture(
        "sso-admin_list-permission-sets",
        '{"PermissionSets":["arn:aws:sso:::permissionSet/ssoins-abc/ps-admin",'
        '"arn:aws:sso:::permissionSet/ssoins-abc/ps-ro"]}',
    )
    stub.fixture(
        "sso-admin_describe-permission-set.1",
        '{"PermissionSet":{"Name":"MlopsToxicAdmin",'
        '"PermissionSetArn":"arn:aws:sso:::permissionSet/ssoins-abc/ps-admin"}}',
    )
    stub.fixture(
        "sso-admin_describe-permission-set.2",
        '{"PermissionSet":{"Name":"MlopsToxicReadOnly",'
        '"PermissionSetArn":"arn:aws:sso:::permissionSet/ssoins-abc/ps-ro"}}',
    )
    stub.fixture(
        "sso-admin_list-account-assignments",
        '{"AccountAssignments":[{"PrincipalId":"u-1",'
        '"PermissionSetArn":"arn:aws:sso:::permissionSet/ssoins-abc/ps-admin"}]}',
    )
    stub.fixture(
        "sts_assume-role",
        '{"Credentials":{"AccessKeyId":"ASIAFIXTURE",'
        '"SecretAccessKey":"SECRETKEYFIXTUREVALUE","SessionToken":"TOKENFIXTURE"}}',
    )
    stub.fixture("iam_list-account-aliases", '{"AccountAliases":["rockcyber-mlops-toxic"]}')
    stub.fixture("s3api_head-bucket", "{}")


# --- the script itself ----------------------------------------------------------------


def test_the_script_exists_and_fails_fast():
    assert BOOT.is_file()
    assert "set -euo pipefail" in BOOT.read_text()


def test_the_script_parses_under_bash_n():
    result = subprocess.run(["bash", "-n", str(BOOT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_the_script_is_shellcheck_clean_at_style_severity():
    result = subprocess.run(
        ["shellcheck", "-S", "style", str(BOOT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout


def code_lines():
    """The script with whole-line comments removed.

    The three scans below are about what the script *calls*, and the script's header
    documents the very strings they forbid: it says in prose that `--query` is never used
    and that `iam enable-organizations-root-credentials-management` is never called.
    Scanning raw text would fail on the documentation of the property being asserted.
    """
    return "\n".join(
        line for line in BOOT.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_the_script_installs_nothing():
    # premortem C11: this build box holds the AWS SSO refresh token, the W&B key, the
    # Kaggle token, and the RunPod key at the same time. One malicious post-install hook
    # harvests all four.
    forbidden = re.compile(
        r"pip[0-9.]* +install|npm +(i|install|ci)|apt(-get)? +install"
        r"|brew +install|curl[^|]*\| *(ba)?sh|wget[^|]*\| *(ba)?sh"
    )
    assert forbidden.search(code_lines()) is None


def test_the_script_parses_with_jq_and_never_with_query():
    # The stub replays whole documents and cannot emulate server-side JMESPath, so
    # --query would make the control flow untestable offline.
    assert "--query" not in code_lines()


def test_the_script_never_touches_root_credentials():
    # Root is the break-glass path and it stays.
    forbidden = re.compile(
        r"enable-organizations-root-credentials-management|delete-login-profile|assume-root"
    )
    assert forbidden.search(code_lines()) is None


# --- preflight: credential hygiene ----------------------------------------------------


def test_preflight_passes_under_an_identity_center_session(stub):
    identity_fixtures(stub)
    result = stub.run_main("--preflight-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preflight passed" in result.stdout


def test_preflight_refuses_a_static_access_key_in_the_environment(stub):
    identity_fixtures(stub)
    result = stub.run_main("--preflight-only", env={"AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE"})
    assert result.returncode != 0
    assert "AWS_ACCESS_KEY_ID is set in this shell" in result.stderr


def test_preflight_refuses_an_iam_user_caller(stub):
    # An IAM user is a static-credential principal even with no env var set.
    stub.fixture(
        "sts_get-caller-identity",
        '{"Arn":"arn:aws:iam::111111111111:user/rc-script-user","Account":"111111111111"}',
    )
    stub.fixture(
        "organizations_describe-organization",
        '{"Organization":{"MasterAccountId":"111111111111"}}',
    )
    result = stub.run_main("--preflight-only")
    assert result.returncode != 0
    assert "is not an IAM Identity Center session" in result.stderr


def test_preflight_refuses_a_caller_outside_the_management_account(stub):
    stub.fixture(
        "sts_get-caller-identity",
        '{"Arn":"arn:aws:sts::222222222222:assumed-role/'
        'AWSReservedSSO_AdministratorAccess_abc/rock.lambros","Account":"222222222222"}',
    )
    stub.fixture(
        "organizations_describe-organization",
        '{"Organization":{"MasterAccountId":"111111111111"}}',
    )
    result = stub.run_main("--preflight-only")
    assert result.returncode != 0
    assert "is not the organization management account" in result.stderr


def test_preflight_refuses_aws_cli_v1(stub):
    identity_fixtures(stub)
    result = stub.run_main(
        "--preflight-only", env={"AWS_STUB_VERSION": "aws-cli/1.35.0 Python/3.12"}
    )
    assert result.returncode != 0
    assert "AWS CLI v2 required" in result.stderr


def test_preflight_refuses_terraform_below_1_11(stub):
    identity_fixtures(stub)
    result = stub.run_main("--preflight-only", env={"TERRAFORM_STUB_VERSION": "1.9.8"})
    assert result.returncode != 0
    assert "Terraform 1.11+ required" in result.stderr


def test_preflight_refuses_the_placeholder_alternate_contact_phone(stub):
    # The placeholder is refused so a real phone number is supplied at run time and
    # never lands in a public repository.
    identity_fixtures(stub)
    result = stub.run_main("--preflight-only", env={"BOOTSTRAP_ALT_CONTACT_PHONE": "+10000000000"})
    assert result.returncode != 0
    assert "BOOTSTRAP_ALT_CONTACT_PHONE" in result.stderr


def test_preflight_warns_about_an_on_disk_static_key_without_refusing(stub):
    identity_fixtures(stub)
    aws_dir = stub.home / ".aws"
    aws_dir.mkdir()
    (aws_dir / "credentials").write_text("[default]\naws_access_key_id = AKIAIOSFODNN7EXAMPLE\n")
    result = stub.run_main("--preflight-only")
    assert result.returncode == 0, result.stderr
    assert "static access key" in result.stderr


# --- defect 1: the one organization-root-wide write -----------------------------------


def root_scp_enabled(stub):
    stub.fixture(
        "organizations_list-roots",
        '{"Roots":[{"Id":"r-abcd","Name":"Root","PolicyTypes":'
        '[{"Type":"SERVICE_CONTROL_POLICY","Status":"ENABLED"}]}]}',
    )


def root_scp_disabled(stub):
    stub.fixture(
        "organizations_list-roots",
        '{"Roots":[{"Id":"r-abcd","Name":"Root","PolicyTypes":[]}]}',
    )


def test_step2_makes_no_write_when_the_policy_type_is_already_enabled(stub):
    root_scp_enabled(stub)
    result = stub.source('step2_enable_scp_policy_type\nprintf "ORG_ROOT_ID=%s\\n" "$ORG_ROOT_ID"')
    assert result.returncode == 0, result.stderr
    assert "organizations_enable-policy-type" not in stub.calls()
    assert "already ENABLED" in result.stdout
    assert "ORG_ROOT_ID=r-abcd" in result.stdout


def test_step2_refuses_the_org_root_write_without_an_explicit_acknowledgement(stub):
    root_scp_disabled(stub)
    result = stub.source("ACK_ORG_ROOT_WRITE=0\nstep2_enable_scp_policy_type")
    assert result.returncode == 2
    assert "organizations_enable-policy-type" not in stub.calls()
    assert "BLAST-RADIUS EXCEPTION" in result.stderr
    assert "organization root" in result.stderr
    assert "--ack-org-root-write" in result.stderr


def test_step2_acknowledgement_states_what_does_not_change(stub):
    # An exception that is merely loud is not reviewable. It has to say what is and is
    # not affected: AWS attaches FullAWSAccess automatically, so no principal loses a
    # permission, and the management account is structurally exempt from SCPs anyway.
    root_scp_disabled(stub)
    result = stub.source("ACK_ORG_ROOT_WRITE=0\nstep2_enable_scp_policy_type")
    assert "FullAWSAccess" in result.stderr
    assert "management account" in result.stderr
    assert "Sandbox OU" in result.stderr


def test_step2_writes_exactly_once_when_acknowledged(stub):
    root_scp_disabled(stub)
    stub.fixture("organizations_enable-policy-type", '{"Root":{"Id":"r-abcd"}}')
    result = stub.source("ACK_ORG_ROOT_WRITE=1\nstep2_enable_scp_policy_type")
    assert result.returncode == 0, result.stderr
    assert stub.calls().count("organizations_enable-policy-type") == 1
    args = stub.call_args("organizations_enable-policy-type")
    assert "--root-id r-abcd" in args
    assert "--policy-type SERVICE_CONTROL_POLICY" in args


# --- steps 3 and 4: the OU and its policy, created before the account -----------------


def test_step3_reuses_an_existing_ou(stub):
    stub.fixture(
        "organizations_list-organizational-units-for-parent",
        '{"OrganizationalUnits":[{"Id":"ou-abcd-1111","Name":"Sandbox"}]}',
    )
    result = stub.source(
        'ORG_ROOT_ID=r-abcd\nstep3_create_ou >/dev/null\nprintf "OU_ID=%s\\n" "$OU_ID"'
    )
    assert result.returncode == 0, result.stderr
    assert "OU_ID=ou-abcd-1111" in result.stdout
    assert "organizations_create-organizational-unit" not in stub.calls()
    assert "SANDBOX_OU_ID=ou-abcd-1111" in stub.outputs_text()


def test_step3_creates_the_ou_when_absent(stub):
    stub.fixture("organizations_list-organizational-units-for-parent", '{"OrganizationalUnits":[]}')
    stub.fixture(
        "organizations_create-organizational-unit",
        '{"OrganizationalUnit":{"Id":"ou-abcd-2222","Name":"Sandbox"}}',
    )
    result = stub.source(
        'ORG_ROOT_ID=r-abcd\nstep3_create_ou >/dev/null\nprintf "OU_ID=%s\\n" "$OU_ID"'
    )
    assert result.returncode == 0, result.stderr
    assert "OU_ID=ou-abcd-2222" in result.stdout
    assert "--name Sandbox" in stub.call_args("organizations_create-organizational-unit")


def test_step4_creates_and_attaches_the_policy_when_absent(stub):
    stub.fixture("organizations_list-policies", '{"Policies":[]}')
    stub.fixture("organizations_create-policy", '{"Policy":{"PolicySummary":{"Id":"p-1111"}}}')
    stub.fixture("organizations_list-policies-for-target", '{"Policies":[]}')
    stub.fixture("organizations_attach-policy", "{}")
    result = stub.source(
        'OU_ID=ou-abcd-1111\nstep4_scp >/dev/null\nprintf "POLICY_ID=%s\\n" "$POLICY_ID"'
    )
    assert result.returncode == 0, result.stderr
    assert "POLICY_ID=p-1111" in result.stdout
    assert "--target-id ou-abcd-1111" in stub.call_args("organizations_attach-policy")


def test_step4_makes_no_write_when_content_and_attachment_already_match(stub):
    stub.fixture(
        "organizations_list-policies",
        '{"Policies":[{"Id":"p-1111","Name":"sandbox-guardrails"}]}',
    )
    scp_content_fixture(stub)
    stub.fixture("organizations_list-policies-for-target", '{"Policies":[{"Id":"p-1111"}]}')
    result = stub.source("OU_ID=ou-abcd-1111\nstep4_scp >/dev/null")
    assert result.returncode == 0, result.stderr
    assert stub.writes() == []


def test_step4_updates_the_policy_when_the_document_has_drifted(stub):
    # A policy whose name matches but whose document has drifted is worse than a missing
    # one: it looks correct in the console and denies nothing.
    stub.fixture(
        "organizations_list-policies",
        '{"Policies":[{"Id":"p-1111","Name":"sandbox-guardrails"}]}',
    )
    stub.fixture(
        "organizations_describe-policy",
        '{"Policy":{"Content":"{\\"Version\\":\\"2012-10-17\\",\\"Statement\\":[]}"}}',
    )
    stub.fixture("organizations_update-policy", '{"Policy":{"PolicySummary":{"Id":"p-1111"}}}')
    stub.fixture("organizations_list-policies-for-target", '{"Policies":[{"Id":"p-1111"}]}')
    result = stub.source("OU_ID=ou-abcd-1111\nstep4_scp")
    assert result.returncode == 0, result.stderr
    assert "organizations_update-policy" in stub.calls()
    assert "drifted" in result.stdout


def test_step4_refuses_a_document_over_the_scp_size_quota(stub, tmp_path):
    oversized = tmp_path / "oversized.json"
    oversized.write_text(
        json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {"Sid": "Pad", "Effect": "Deny", "Action": ["x" * 6000], "Resource": "*"}
                ],
            }
        )
    )
    result = stub.source(
        "OU_ID=ou-abcd-1111\nstep4_scp", env={"BOOTSTRAP_SCP_FILE": str(oversized)}
    )
    assert result.returncode == 1
    assert "5120-byte quota" in result.stderr
    assert stub.writes() == []


# --- defect 2: create-account is asynchronous and not idempotent -----------------------


def test_step5_adopts_an_existing_account_matched_on_root_email(stub):
    # The root email is the one property AWS enforces as unique across the organization,
    # so it is the existence check for a create that has no idempotency key.
    stub.fixture(
        "organizations_list-accounts",
        '{"Accounts":[{"Id":"999999999999","Email":"someone@example.com"},'
        '{"Id":"' + ACCOUNT_ID + '","Email":"' + ROOT_EMAIL + '"}]}',
    )
    result = stub.source(
        'step5_create_account >/dev/null\nprintf "ACCOUNT_ID=%s\\n" "$ACCOUNT_ID"'
    )
    assert result.returncode == 0, result.stderr
    assert f"ACCOUNT_ID={ACCOUNT_ID}" in result.stdout
    assert "organizations_create-account" not in stub.calls()
    assert f"ACCOUNT_ID={ACCOUNT_ID}" in stub.outputs_text()


def test_step5_matches_the_root_email_case_insensitively(stub):
    stub.fixture(
        "organizations_list-accounts",
        '{"Accounts":[{"Id":"' + ACCOUNT_ID + '","Email":"Rock+AWS-MLOPS-Toxic@RockCyber.com"}]}',
    )
    result = stub.source(
        'step5_create_account >/dev/null\nprintf "ACCOUNT_ID=%s\\n" "$ACCOUNT_ID"'
    )
    assert result.returncode == 0, result.stderr
    assert f"ACCOUNT_ID={ACCOUNT_ID}" in result.stdout
    assert "organizations_create-account" not in stub.calls()


def test_step5_paginates_before_concluding_the_account_is_absent(stub):
    # list-accounts truncates, and the account this script looks for can be on page two.
    stub.fixture(
        "organizations_list-accounts.1",
        '{"Accounts":[{"Id":"999999999999","Email":"someone@example.com"}],"NextToken":"tok2"}',
    )
    stub.fixture(
        "organizations_list-accounts.2",
        '{"Accounts":[{"Id":"' + ACCOUNT_ID + '","Email":"' + ROOT_EMAIL + '"}]}',
    )
    result = stub.source(
        'step5_create_account >/dev/null\nprintf "ACCOUNT_ID=%s\\n" "$ACCOUNT_ID"'
    )
    assert result.returncode == 0, result.stderr
    assert f"ACCOUNT_ID={ACCOUNT_ID}" in result.stdout
    assert stub.calls().count("organizations_list-accounts") == 2
    assert "organizations_create-account" not in stub.calls()


def test_step5_polls_the_asynchronous_status_to_a_terminal_state(stub):
    stub.fixture("organizations_list-accounts", '{"Accounts":[]}')
    stub.fixture(
        "organizations_create-account",
        '{"CreateAccountStatus":{"Id":"car-1","State":"IN_PROGRESS"}}',
    )
    for n in (1, 2):
        stub.fixture(
            f"organizations_describe-create-account-status.{n}",
            '{"CreateAccountStatus":{"Id":"car-1","State":"IN_PROGRESS"}}',
        )
    stub.fixture(
        "organizations_describe-create-account-status.3",
        '{"CreateAccountStatus":{"Id":"car-1","State":"SUCCEEDED","AccountId":"'
        + ACCOUNT_ID
        + '"}}',
    )
    result = stub.source(
        'step5_create_account >/dev/null\nprintf "ACCOUNT_ID=%s\\n" "$ACCOUNT_ID"'
    )
    assert result.returncode == 0, result.stderr
    assert f"ACCOUNT_ID={ACCOUNT_ID}" in result.stdout
    assert stub.calls().count("organizations_describe-create-account-status") == 3


def test_step5_persists_the_request_id_before_it_starts_polling(stub):
    # A crash during polling must still leave a trail to the in-flight account.
    stub.fixture("organizations_list-accounts", '{"Accounts":[]}')
    stub.fixture(
        "organizations_create-account",
        '{"CreateAccountStatus":{"Id":"car-77","State":"IN_PROGRESS"}}',
    )
    stub.rc("organizations_describe-create-account-status", 254)
    result = stub.source("( step5_create_account >/dev/null 2>&1 ) || true")
    assert result.returncode == 0, result.stderr
    assert "CREATE_ACCOUNT_REQUEST_ID=car-77" in stub.outputs_text()


def test_the_account_id_is_persisted_before_any_later_step_runs(stub):
    # The load-bearing one. If the script dies after the poll and before anything writes
    # state, the account exists, is billable, occupies the root email, and nothing on
    # disk records it.
    stub.fixture("organizations_list-accounts", '{"Accounts":[]}')
    stub.fixture(
        "organizations_create-account",
        '{"CreateAccountStatus":{"Id":"car-1","State":"IN_PROGRESS"}}',
    )
    stub.fixture(
        "organizations_describe-create-account-status",
        '{"CreateAccountStatus":{"Id":"car-1","State":"SUCCEEDED","AccountId":"'
        + ACCOUNT_ID
        + '"}}',
    )
    result = stub.source(
        "step5_create_account >/dev/null\n"
        # Simulate step 6 exploding the instant it starts.
        'printf "254" >"$AWS_STUB_DIR/organizations_list-parents.rc"\n'
        "( step6_govern_account >/dev/null 2>&1 ) || true\n"
    )
    assert result.returncode == 0, result.stderr
    assert f"ACCOUNT_ID={ACCOUNT_ID}" in stub.outputs_text()


def test_step5_dies_on_a_failed_create_rather_than_continuing(stub):
    stub.fixture("organizations_list-accounts", '{"Accounts":[]}')
    stub.fixture(
        "organizations_create-account",
        '{"CreateAccountStatus":{"Id":"car-9","State":"IN_PROGRESS"}}',
    )
    stub.fixture(
        "organizations_describe-create-account-status",
        '{"CreateAccountStatus":{"Id":"car-9","State":"FAILED",'
        '"FailureReason":"EMAIL_ALREADY_EXISTS"}}',
    )
    result = stub.source("step5_create_account")
    assert result.returncode == 1
    assert "EMAIL_ALREADY_EXISTS" in result.stderr


# --- defect 3: the ungoverned window --------------------------------------------------


def test_nothing_runs_between_account_creation_and_governance(stub):
    # The window is exactly as long as the work scheduled inside it. Asserting on the
    # call log makes this a constraint on future edits, not just on today's code.
    full_first_run_fixtures(stub)
    result = stub.source(
        "step2_enable_scp_policy_type >/dev/null\n"
        "step3_create_ou >/dev/null\n"
        "step4_scp >/dev/null\n"
        "step5_create_account >/dev/null\n"
        "step6_govern_account >/dev/null\n"
    )
    assert result.returncode == 0, result.stderr
    last_poll = stub.last_index("organizations_describe-create-account-status")
    assert stub.call_at(last_poll + 1) == "organizations_list-parents"
    assert stub.call_at(last_poll + 2) == "organizations_move-account"


def test_the_scp_is_attached_before_the_account_is_created(stub):
    # The destination must already be protected when the account lands in it.
    full_first_run_fixtures(stub)
    result = stub.source(
        "step2_enable_scp_policy_type >/dev/null\n"
        "step3_create_ou >/dev/null\n"
        "step4_scp >/dev/null\n"
        "step5_create_account >/dev/null\n"
    )
    assert result.returncode == 0, result.stderr
    attach = stub.index("organizations_attach-policy")
    create = stub.index("organizations_create-account")
    assert 0 < attach < create


def test_step6_verifies_the_move_and_records_the_window(stub):
    full_first_run_fixtures(stub)
    result = stub.source(
        "step5_create_account >/dev/null\nOU_ID=ou-abcd-1111\nstep6_govern_account >/dev/null"
    )
    assert result.returncode == 0, result.stderr
    assert "--destination-parent-id ou-abcd-1111" in stub.call_args("organizations_move-account")
    assert "UNGOVERNED_WINDOW_SECONDS=" in stub.outputs_text()
    # The move is re-read rather than trusted: two list-parents, one either side.
    assert stub.calls().count("organizations_list-parents") == 2


def test_step6_refuses_to_continue_when_the_account_is_not_in_the_ou(stub):
    # Fail closed. Continuing would run steps 7 through 10 against an ungoverned account
    # and leave it that way.
    stub.fixture("organizations_list-parents", '{"Parents":[{"Id":"r-abcd","Type":"ROOT"}]}')
    stub.fixture("organizations_move-account", "{}")
    result = stub.source(
        f"ACCOUNT_ID={ACCOUNT_ID}\nOU_ID=ou-abcd-1111\nstep6_govern_account"
    )
    assert result.returncode == 1
    assert "refusing to continue" in result.stderr
    assert "account_put-alternate-contact" not in stub.calls()


def test_step6_skips_the_move_when_the_account_is_already_in_the_ou(stub):
    stub.fixture(
        "organizations_list-parents",
        '{"Parents":[{"Id":"ou-abcd-1111","Type":"ORGANIZATIONAL_UNIT"}]}',
    )
    stub.fixture(
        "account_get-alternate-contact",
        '{"AlternateContact":{"EmailAddress":"rock@rockcyber.com","Name":"Rock Lambros",'
        '"PhoneNumber":"+13035550100","Title":"Owner"}}',
    )
    result = stub.source(
        f"ACCOUNT_ID={ACCOUNT_ID}\nOU_ID=ou-abcd-1111\nstep6_govern_account >/dev/null"
    )
    assert result.returncode == 0, result.stderr
    assert "organizations_move-account" not in stub.calls()
    assert "account_put-alternate-contact" not in stub.calls()


def test_step6_sets_all_three_alternate_contacts_when_absent(stub):
    # get-alternate-contact fails when the contact is unset, which is the ordinary case
    # on a fresh account. The loop has to survive that.
    stub.fixture("organizations_list-parents", '{"Parents":[{"Id":"ou-abcd-1111"}]}')
    stub.rc("account_get-alternate-contact", 255)
    stub.fixture("account_put-alternate-contact", "{}")
    result = stub.source(
        f"ACCOUNT_ID={ACCOUNT_ID}\nOU_ID=ou-abcd-1111\nstep6_govern_account >/dev/null"
    )
    assert result.returncode == 0, result.stderr
    assert stub.calls().count("account_put-alternate-contact") == 3
    args = " ".join(stub.all_call_args("account_put-alternate-contact"))
    for contact_type in ("BILLING", "OPERATIONS", "SECURITY"):
        assert contact_type in args


# --- the whole script -----------------------------------------------------------------


def test_a_fully_provisioned_organization_runs_clean(stub):
    steady_state_fixtures(stub)
    result = stub.run_main("--skip-operator-gate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bootstrap complete" in result.stdout


def test_a_second_run_performs_no_writes(stub):
    # The whole point of idempotency: a re-run after a partial failure is a no-op.
    steady_state_fixtures(stub)
    first = stub.run_main("--skip-operator-gate")
    assert first.returncode == 0, first.stdout + first.stderr
    stub.clear_calls()
    second = stub.run_main("--skip-operator-gate")
    assert second.returncode == 0, second.stdout + second.stderr
    assert stub.writes() == []


def test_the_outputs_file_carries_every_interface_key_and_is_mode_600(stub):
    steady_state_fixtures(stub)
    result = stub.run_main("--skip-operator-gate")
    assert result.returncode == 0, result.stdout + result.stderr
    body = stub.outputs_text()
    for key in (
        "ACCOUNT_ID",
        "AWS_REGION",
        "SANDBOX_OU_ID",
        "SCP_POLICY_ID",
        "TF_STATE_BUCKET",
        "ADMIN_PERMISSION_SET_ARN",
        "READONLY_PERMISSION_SET_ARN",
    ):
        assert f"{key}=" in body, f"Phase A2 sources this file and needs {key}"
    assert stat.S_IMODE(stub.outputs.stat().st_mode) == 0o600
    # LC_ALL=C sorted, so a diff between runs is a real change rather than reordering.
    lines = [ln for ln in body.splitlines() if ln]
    assert lines == sorted(lines)


def test_the_outputs_file_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", "-q", "infra/aws/bootstrap-outputs.env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "the outputs file carries the raw account id"


def test_the_account_id_never_reaches_stdout_or_stderr(stub):
    # The submission deliverable requires screenshots with no account id visible, so it
    # is masked at the source rather than at the screenshot.
    steady_state_fixtures(stub)
    result = stub.run_main("--skip-operator-gate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ACCOUNT_ID not in result.stdout
    assert ACCOUNT_ID not in result.stderr


def test_the_account_id_is_masked_in_the_one_line_that_carries_it(stub):
    # Absence alone is a weak claim: no line on the steady-state path interpolates the
    # id at all, so that test would pass against a broken mask. The create path does
    # interpolate it, which is where the mask has to be observed working.
    full_first_run_fixtures(stub)
    result = stub.source(
        "step2_enable_scp_policy_type >/dev/null\n"
        "step3_create_ou >/dev/null\n"
        "step4_scp >/dev/null\n"
        "step5_create_account\n"
    )
    assert result.returncode == 0, result.stderr
    assert "created account <account-id>" in result.stdout
    assert ACCOUNT_ID not in result.stdout + result.stderr


def test_the_assumed_role_secret_never_reaches_stdout_or_stderr(stub):
    steady_state_fixtures(stub)
    result = stub.run_main("--skip-operator-gate")
    assert result.returncode == 0, result.stdout + result.stderr
    for secret in ("SECRETKEYFIXTUREVALUE", "TOKENFIXTURE"):
        assert secret not in result.stdout
        assert secret not in result.stderr


def test_the_operator_gate_blocks_when_not_skipped(stub):
    steady_state_fixtures(stub)
    result = stub.run_main()
    assert result.returncode == 1
    assert "OPERATOR STEP 7" in result.stdout
    assert "not acknowledged" in result.stderr


def test_the_operator_gate_explains_the_mimecast_dependency(stub):
    # This is the finding the phase runs on day 1 to surface: plus-addressed mail to
    # rockcyber.com routes through Mimecast, whose recipient validation rejects it.
    steady_state_fixtures(stub)
    result = stub.run_main()
    combined = result.stdout + result.stderr
    assert ROOT_EMAIL in combined
    assert "Mimecast" in combined
    assert "MFA" in combined


def test_the_state_bucket_is_created_private_versioned_and_encrypted_when_absent(stub):
    steady_state_fixtures(stub)
    stub.rc("s3api_head-bucket", 255)
    for operation in (
        "create-bucket",
        "put-bucket-versioning",
        "put-bucket-encryption",
        "put-public-access-block",
        "put-bucket-policy",
    ):
        stub.fixture(f"s3api_{operation}", "{}")
    result = stub.run_main("--skip-operator-gate")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = stub.calls()
    for operation in (
        "create-bucket",
        "put-bucket-versioning",
        "put-bucket-encryption",
        "put-public-access-block",
        "put-bucket-policy",
    ):
        assert f"s3api_{operation}" in calls
    assert "LocationConstraint=us-west-2" in stub.call_args("s3api_create-bucket")
    assert "aws:SecureTransport" in stub.call_args("s3api_put-bucket-policy")
