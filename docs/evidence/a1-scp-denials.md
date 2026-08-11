# Evidence: the service control policy denies what it claims to deny

**Executed 2026-08-10, against the live deployment account, by
`AWSReservedSSO_MlopsToxicAdmin/rock.lambros` — the most privileged human principal the
account has.** That last part is the point. A denial observed from a weak role proves
nothing, because the role might simply lack the permission. Every probe below was run by an
administrator whose IAM policy *does* grant the action, so the only thing that can refuse it
is the service control policy.

## When this was run, and why that matters

This is a **Phase A1 acceptance test executed on day 12, not on day 1.** The A1 plan called
for it at account-bootstrap time and it was not performed then. It is recorded here with its
real date rather than presented as a day-1 artifact, because a record that misstates when it
was taken is worse than a record that admits it was taken late.

What this costs in assurance: the policy was unverified between 2026-07-30 and 2026-08-10,
so nothing here proves the guardrails were live during the build. What it still buys: the
policy is verified now, before submission, against the account as it currently stands.

## Redaction

The account id, the organization id, the SCP policy id, and the SSO role suffix are replaced
with placeholders. AWS `RunInstances` also returns an encoded authorization failure message,
which is an opaque blob decodable only with `sts:DecodeAuthorizationMessage`; it is omitted
rather than published. Nothing else in the responses is altered — the error codes and the
phrase `with an explicit deny in a service control policy` are verbatim.

## Probe design

Every probe is constructed so that **nothing is created or destroyed even if the guardrail
were absent.** Three techniques do that work:

- `--dry-run`, which makes EC2 evaluate authorization and then stop.
- Targeting a resource that does not exist, so an un-denied call fails on `NoSuchEntity` or
  `TrailNotFound` instead of succeeding. The *distinction between those two errors and
  `AccessDenied`* is the measurement.
- Read-only calls, for the region guardrail.

One probe had to be rebuilt. The first attempt at the instance-type guardrail used
`m5.large` and came back `InvalidParameterValue: the architecture 'x86_64' … does not match
the architecture 'arm64' of the specified AMI`. EC2 validated parameters *before* it
evaluated policy, so the request never reached the SCP and the result proved nothing about
the guardrail. Re-run with `t4g.xlarge` — arm64, so the architecture check passes, and
absent from the allowlist, so the guardrail is the only thing left to refuse it.

## Results

| # | Guardrail (`Sid`) | Probe | Result |
|---|---|---|---|
| 1 | `DenyOutsideHomeRegion` | `ec2 describe-instances --region us-east-1` | **Denied** — `UnauthorizedOperation`, explicit deny in an SCP |
| 2 | `DenyNonAllowlistedInstanceLaunch` | `ec2 run-instances --dry-run --instance-type t4g.xlarge` | **Denied** — `UnauthorizedOperation`, explicit deny in an SCP |
| 2b | control for #2 | `ec2 run-instances --dry-run --instance-type t4g.small` | **Allowed** — `DryRunOperation: Request would have succeeded` |
| 3 | `DenyStaticCredentialCreation` | `iam create-access-key --user-name <nonexistent>` | **Denied** — `AccessDenied`, explicit deny in an SCP |
| 4 | `DenyDetectiveControlBlinding` | `cloudtrail stop-logging --name <nonexistent>` | **Denied** — `AccessDeniedException`, explicit deny in an SCP |
| 5 | `DenyDetectiveControlDeletionExceptOrgTeardown` | `guardduty delete-detector --detector-id <fake>` | **Denied** — `AccessDeniedException`, explicit deny in an SCP |

Row 2b is the one that makes row 2 mean something. Without it, a `RunInstances` denial is
equally consistent with "the allowlist works" and "this principal cannot launch instances at
all." The allowlisted type succeeding under the same credentials, in the same region, with
the same AMI, isolates the instance type as the variable.

## Verbatim responses

```text
# 1 — region
An error occurred (UnauthorizedOperation) when calling the DescribeInstances operation:
You are not authorized to perform this operation. User: arn:aws:sts::<ACCOUNT_ID>:assumed-role/
AWSReservedSSO_MlopsToxicAdmin_<SUFFIX>/rock.lambros is not authorized to perform:
ec2:DescribeInstances with an explicit deny in a service control policy:
arn:aws:organizations::<ACCOUNT_ID>:policy/<ORG_ID>/service_control_policy/<POLICY_ID>

# 2 — instance type not on the allowlist
An error occurred (UnauthorizedOperation) when calling the RunInstances operation:
You are not authorized to perform this operation. User: arn:aws:sts::<ACCOUNT_ID>:assumed-role/
AWSReservedSSO_MlopsToxicAdmin_<SUFFIX>/rock.lambros is not authorized to perform:
ec2:RunInstances on resource: arn:aws:ec2:us-west-2:<ACCOUNT_ID>:instance/* with an explicit
deny in a service control policy:
arn:aws:organizations::<ACCOUNT_ID>:policy/<ORG_ID>/service_control_policy/<POLICY_ID>
Encoded authorization failure message: <omitted>

# 2b — control: an allowlisted type, same credentials, same AMI
An error occurred (DryRunOperation) when calling the RunInstances operation:
Request would have succeeded, but DryRun flag is set.

# 3 — static credential creation
An error occurred (AccessDenied) when calling the CreateAccessKey operation:
User: arn:aws:sts::<ACCOUNT_ID>:assumed-role/AWSReservedSSO_MlopsToxicAdmin_<SUFFIX>/rock.lambros
is not authorized to perform: iam:CreateAccessKey on resource: user scp-probe-does-not-exist
with an explicit deny in a service control policy:
arn:aws:organizations::<ACCOUNT_ID>:policy/<ORG_ID>/service_control_policy/<POLICY_ID>

# 4 — blinding CloudTrail
An error occurred (AccessDeniedException) when calling the StopLogging operation:
User: arn:aws:sts::<ACCOUNT_ID>:assumed-role/AWSReservedSSO_MlopsToxicAdmin_<SUFFIX>/rock.lambros
is not authorized to perform: cloudtrail:StopLogging on resource:
arn:aws:cloudtrail:us-west-2:<ACCOUNT_ID>:trail/scp-probe-does-not-exist with an explicit deny
in a service control policy:
arn:aws:organizations::<ACCOUNT_ID>:policy/<ORG_ID>/service_control_policy/<POLICY_ID>

# 5 — deleting the GuardDuty detector
An error occurred (AccessDeniedException) when calling the DeleteDetector operation:
User: arn:aws:sts::<ACCOUNT_ID>:assumed-role/AWSReservedSSO_MlopsToxicAdmin_<SUFFIX>/rock.lambros
is not authorized to perform: guardduty:DeleteDetector on resource:
arn:aws:guardduty:us-west-2:<ACCOUNT_ID>:detector/<DETECTOR_ID> with an explicit deny in a
service control policy:
arn:aws:organizations::<ACCOUNT_ID>:policy/<ORG_ID>/service_control_policy/<POLICY_ID>
```

## What was deliberately not probed

Four `Sid`s carry no live probe, and the reason is the same in every case: the only way to
test them is to attempt the destructive thing, and a probe that succeeds because the
guardrail is missing would do real damage.

| `Sid` | Why not probed live |
|---|---|
| `DenyRdsCreateWithoutManagedMasterPassword`, `DenyRdsCreatePubliclyAccessible` | RDS has no dry-run. A non-denied call creates a billable database, and the publicly-accessible variant creates an internet-reachable one |
| `DenyAuroraClusters` | Same — a non-denied `CreateDBCluster` creates a billable Aurora cluster |
| `DenyOrganizationEscape` | `LeaveOrganization` and `CloseAccount` are irreversible. This is the one guardrail whose failure mode is losing the account |
| `DenyRdsModifyToPubliclyAccessible`, `DenyInstanceAttributeMutation` | Both target the *running graded stack*. A non-denied call mutates the running production system |

These rest on policy inspection instead: `tests/unit/test_scp_policy.py` parses
`infra/aws/scp-sandbox-guardrails.json` and asserts each statement's effect, actions, and
conditions. That is weaker evidence than a live denial and is labelled as such — it proves
the policy *says* the right thing, not that AWS *enforces* it. The five probes above are what
establish that this account enforces the policy at all; the file-level test then covers the
statements that cannot be safely fired.

## Reproducing

```bash
export AWS_PROFILE=rc-mlops
AMI=$(aws ssm get-parameter --region us-west-2 \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query Parameter.Value --output text)

aws ec2 describe-instances --region us-east-1 --max-items 1
aws ec2 run-instances --region us-west-2 --dry-run --image-id "$AMI" \
  --instance-type t4g.xlarge --count 1
aws ec2 run-instances --region us-west-2 --dry-run --image-id "$AMI" \
  --instance-type t4g.small --count 1
aws iam create-access-key --user-name scp-probe-does-not-exist
aws cloudtrail stop-logging --region us-west-2 --name scp-probe-does-not-exist
aws guardduty delete-detector --region us-west-2 \
  --detector-id 00000000000000000000000000000000
```

Expect a denial on every command except the third, which must return `DryRunOperation`.
