# Rollback rehearsal

Status: REHEARSED — run against the live stack on 2026-08-02.

This record was deliberately empty until the rehearsal actually ran, because a template with
reasonable-looking values in it reads exactly like evidence six weeks later. `infra/ROLLBACK.md`
opens by claiming every command in it has been run at least once on a working system. As of the
run below, that claim is made and this file is what backs it.

The rehearsal was unusually informative because the two SHAs differ in *observable behaviour*,
not just in image digests. `224da4149c4a` fixes a defect that made `allow` unreachable in
`backend/policy.py`; `724bbc8250c0` still carries it. So the same input decided differently on
either side of the roll, and the rollback is demonstrated by the system's output rather than by
a health check that only proves the containers restarted.

`tests/unit/test_rollback_runbook.py` reads the `Status:` line above and asserts a different
set of properties in each state: while it says PENDING, no field may carry a value, no SHA may
appear, and no outcome may be reported. Changing the line to REHEARSED without filling in the
date, both SHAs, the wall-clock time and the transcript turns the suite red.

| Field | Value |
|---|---|
| Date | 2026-08-02 |
| Operator | rock.lambros, via IAM Identity Center (`MlopsToxicAdmin`) |
| Rolled from | 224da4149c4a |
| Rolled to | 724bbc8250c0546961d5b9813510fd14a1036231 |
| Command | `make rollback`, then `infra/aws/rollback.sh 224da4149c4a` to roll forward |
| Wall-clock | 150 seconds to roll back, 149 seconds to roll forward, both including the gate |
| Gate | `verify_deploy.sh` via `make deploy-verify` — green before, on the old SHA, and after |
| Rolled forward again | Yes, to 224da4149c4a; `allow` behaviour returned |
| Outcome | Success. Three tiers rolled and gated twice with no manual intervention. |

## The rehearsal, exactly

Run against the live stack while it is known-good. A rehearsal on the day it is needed is an
incident, not a rehearsal.

```bash
export AWS_PROFILE=mlops-admin AWS_REGION=us-west-2

# 0. Both pointers, before anything moves. These are the two SHAs the table records.
aws ssm get-parameter --name /toxic/deploy/current-sha  --query 'Parameter.Value' --output text
aws ssm get-parameter --name /toxic/deploy/previous-sha --query 'Parameter.Value' --output text

# 1. Green BEFORE deliberately changing anything.
make deploy-verify

# 2. The rehearsal itself.
time make rollback 2>&1 | tee /tmp/rollback-rehearsal.log

# 3. Green on the OLD sha.
make deploy-verify

# 4. Roll forward. --ref main is required: the OIDC trust policy pins job_workflow_ref to
#    deploy.yml@refs/heads/main, so a dispatch from any other ref cannot get credentials.
gh workflow run deploy.yml --ref main -f sha="$(git rev-parse origin/main)"
gh run watch

# 5. Green on the NEW sha again.
make deploy-verify
```

Expected: two green gates around the rollback and a third after rolling forward.

That is what was run. The transcript below was redacted before being committed, with the
same tool the repository uses everywhere else — public IPs and instance ids are stripped
because this repository is public and they are targeting information:

```bash
.venv/bin/python -m scripts.redact < /tmp/rollback-rehearsal.log
```

## Preconditions

`/toxic/deploy/previous-sha` has to name a SHA whose five images are still in ECR, or
`rollback.sh` refuses before it touches anything — which is the correct behaviour and makes for
a short rehearsal. If it does refuse, that SHA was never built; build it first with
`gh workflow run deploy.yml --ref main -f sha=<git-sha>` and rehearse against that.

## Transcript

```
### 0. pointers before
224da4149c4a
724bbc8250c0546961d5b9813510fd14a1036231
### 1. gate BEFORE
verify: backend     OK    http://<ip-redacted>:8000/health
verify: frontend    OK    http://<ip-redacted>:8501/_stcore/health
verify: monitoring  OK    http://<ip-redacted>:8502/_stcore/health
verify: all three endpoints healthy
### 2. rollback
rollback: 224da4149c4a -> 724bbc8250c0546961d5b9813510fd14a1036231
rollback: every image for 724bbc8250c0546961d5b9813510fd14a1036231 is present in the registry
ssm_run: backend <instance-redacted> -> Success
ssm_run: backend OK -- every invocation reported Success. Run verify_deploy.sh next; that is the gate.
ssm_run: frontend <instance-redacted> -> Success
ssm_run: frontend OK -- every invocation reported Success. Run verify_deploy.sh next; that is the gate.
ssm_run: monitoring <instance-redacted> -> Success
ssm_run: monitoring OK -- every invocation reported Success. Run verify_deploy.sh next; that is the gate.
verify: backend     OK    http://<ip-redacted>:8000/health
verify: frontend    OK    http://<ip-redacted>:8501/_stcore/health
verify: monitoring  OK    http://<ip-redacted>:8502/_stcore/health
verify: all three endpoints healthy
record_deploy: current-sha=724bbc8250c0546961d5b9813510fd14a1036231
rollback: 724bbc8250c0546961d5b9813510fd14a1036231 is now live and recorded as current
rollback: roll forward with: gh workflow run deploy.yml --ref main -f sha=<git-sha>
ROLLBACK_SECONDS=150
### 3. gate on the OLD sha, and the behaviour difference that proves the roll was real
verify: backend     OK    http://<ip-redacted>:8000/health
verify: frontend    OK    http://<ip-redacted>:8501/_stcore/health
verify: monitoring  OK    http://<ip-redacted>:8502/_stcore/health
verify: all three endpoints healthy
at 724bbc8: decision=review max_prob=0.0028
### 4. roll forward
rollback: 724bbc8250c0546961d5b9813510fd14a1036231 -> 224da4149c4a
rollback: every image for 224da4149c4a is present in the registry
verify: backend     OK    http://<ip-redacted>:8000/health
verify: frontend    OK    http://<ip-redacted>:8501/_stcore/health
verify: monitoring  OK    http://<ip-redacted>:8502/_stcore/health
verify: all three endpoints healthy
record_deploy: current-sha=224da4149c4a
rollback: 224da4149c4a is now live and recorded as current
rollback: roll forward with: gh workflow run deploy.yml --ref main -f sha=<git-sha>
ROLLFORWARD_SECONDS=149
### 5. gate on the NEW sha
at 224da41: decision=allow max_prob=0.0028
```

## What the rehearsal changed

Two things, both of them the tooling refusing rather than failing.

**`rollback.sh` aborted twice before touching the fleet.** Rolling forward to `224da4149c4a`
was refused with `FATAL: toxic-mod-frontend has no image tagged 224da4149c4a -- this SHA is
not a deployable rollback target`, and then again for `224da4149c4a-reviewer`. Only the
backend had been rebuilt at that SHA, because only `backend/policy.py` changed. The pre-flight
check exists precisely so that a missing image is discovered before three instances are half
rolled, and it worked: the fleet was never split across two versions.

The fix was to retag, not rebuild. `git diff --name-only` between the two SHAs touched
`.gitignore`, `backend/policy.py`, `infra/runpod/deploy_runpod.py`,
`infra/terraform/grading.auto.tfvars` and five test files. The frontend image copies
`backend/__init__.py`, `backend/feedback.py` and `backend/fingerprint.py` but not
`backend/policy.py`, and the monitoring image copies none of them, so both images are
byte-identical at the two SHAs and `aws ecr put-image` against the existing manifest is
correct rather than a shortcut. ECR tag immutability permits adding a tag to an existing
manifest; it forbids moving one.

**The recorded SHA had drifted from the running SHA.** `/toxic/deploy/current-sha` still read
`724bbc8250c0` while production was serving `224da4149c4a`, and `/toxic/deploy/previous-sha`
was unset, so `make rollback` had no target at all. `record_deploy.sh` is what maintains both
pointers and it had not been run after the out-of-band roll. Recording the deploy is what made
this rehearsal possible; a rollback path whose pointers are stale is not a rollback path.
