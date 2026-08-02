# Rollback rehearsal

Status: PENDING — written, not yet run.

This record is deliberately empty rather than plausible. A template with reasonable-looking
values in it reads exactly like evidence six weeks later, and `infra/ROLLBACK.md` opens by
claiming every command in it has been run at least once on a working system. Until the table
below is filled in from a real run, that claim is not made.

`tests/unit/test_rollback_runbook.py` reads the `Status:` line above and asserts a different
set of properties in each state: while it says PENDING, no field may carry a value, no SHA may
appear, and no outcome may be reported. Changing the line to REHEARSED without filling in the
date, both SHAs, the wall-clock time and the transcript turns the suite red.

| Field | Value |
|---|---|
| Date | (pending) |
| Operator | (pending) |
| Rolled from | (pending) |
| Rolled to | (pending) |
| Command | (pending) |
| Wall-clock | (pending) |
| Gate | (pending) |
| Rolled forward again | (pending) |
| Outcome | (pending) |

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

Then redact and paste the transcript below, set every field in the table, and change the
`Status:` line to `REHEARSED`:

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
```

## What the rehearsal changed

<!-- Record anything that had to be fixed. If nothing did, say so explicitly -- "no changes
required" is a finding, and an empty section is not. -->
