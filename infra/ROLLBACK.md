# Rollback and recovery

Four scenarios, worst first. Everything here is a command you can paste; nothing is a
description of a command.

The rule that shapes all of it: **recovery does not touch Terraform.** An apply can
force-replace instances and destroy the artifacts baked onto them, which is a larger risk than
almost anything it would be recovering from. Exactly one scenario below runs Terraform, and it
is the one where Terraform already ran.

Every command assumes an IAM Identity Center session in the account:

```bash
aws sso login --use-device-code
export AWS_PROFILE=mlops-admin AWS_REGION=us-west-2
```

## What is deployed right now

```bash
aws ssm get-parameter --name /toxic/deploy/current-sha  --query 'Parameter.Value' --output text
aws ssm get-parameter --name /toxic/deploy/previous-sha --query 'Parameter.Value' --output text
make deploy-verify
```

`make deploy-verify` resolves the three published endpoints from Parameter Store and runs the
same gate the deploy workflow runs. It is the only statement about the system worth trusting:
an SSM invocation reporting `Success` on all three hosts is compatible with a container that
pulled, started, and died in its lifespan.

## 1. Bad deploy — the new SHA is live and wrong. Budget: 6 minutes

```bash
make rollback                       # re-rolls /toxic/deploy/previous-sha
make rollback SHA=<older-git-sha>   # or name one explicitly
```

`infra/aws/rollback.sh` checks that every ECR repository still holds that tag — including the
`<sha>-reviewer` tag the reviewer console runs from — **before** it touches anything, rolls all
three components through the asserted SSM path, gates on the live endpoints, and only then
records the target as current. It runs no Terraform and rebuilds no image.

It records the rollback with `--keep-previous`, so `/toxic/deploy/previous-sha` still names
whatever it named before. That is deliberate: without it a second `make rollback` would walk
straight back into the SHA you just escaped.

If it refuses with `this SHA is not a deployable rollback target`, that SHA's images were never
built — the ECR lifecycle policy is not the cause. Rule 2 of that policy selects
`tagPrefixList: ["sha-"]` and this pipeline tags with the bare git SHA, so no rule matches a
deployed image and rollback targets do not age out on their own. Build it:

```bash
gh workflow run deploy.yml --ref main -f sha=<git-sha>
gh run watch
```

Budget 12 minutes instead of 6. `--ref main` is not optional: the OIDC trust policy pins
`job_workflow_ref` to `deploy.yml@refs/heads/main`, so a dispatch from any other ref cannot get
credentials.

Roll forward the same way once the fix is on `main`.

## 2. Instance replaced — the box is new and empty. Budget: 10 minutes

A forced AMI change or an instance failure leaves a host with no `/opt/toxic`. User data pulls
`deploy/current/` on first boot and enables the unit, so check whether it recovered on its own
before doing anything:

```bash
aws ssm get-parameter --name /toxic/boot/backend --query 'Parameter.Value' --output text
make deploy-verify
```

If the boot marker is missing, user data did not finish. There is no SSH and no bastion, so
there are exactly three ways to see that host:

```bash
aws ec2 get-console-output --instance-id <id> --output text | grep -F TOXIC-USER-DATA-COMPLETE
aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=<id>" --query 'InstanceInformationList[0].PingStatus'
aws ec2 get-serial-console-access-status
```

`TOXIC-USER-DATA-COMPLETE` is the last line user data writes, so its absence in the console
output is the answer to "did the boot finish?".

If the marker is present but the application is down, re-roll the SHA that is supposed to be
serving. The component argument is required — `roll.sh` cannot infer it:

```bash
SHA=$(aws ssm get-parameter --name /toxic/deploy/current-sha --query 'Parameter.Value' --output text)
infra/aws/ssm_run.sh backend 1 bash /opt/toxic/bootstrap.sh "$SHA" backend
make deploy-verify
```

`make aws-up` does all of the above in one command, including waiting for the boot marker, and
is the right thing to reach for after any stop/start.

## 3. Database — the graded dataset is gone or corrupt. Budget: 20 minutes

The monitoring dashboard is scored on this data, so losing it costs rubric points that no
redeploy recovers. Two restore paths exist and the first is the one to use.

**From the pg_dump in S3.** Every teardown path produces one: `make aws-down` has `db-dump` as
a hard prerequisite, and `infra/aws/aws_down.sh` independently refuses to stop anything unless
S3 already holds a dump less than an hour old.

```bash
BUCKET=$(aws ssm get-parameter --name /toxic/deploy/bucket --query 'Parameter.Value' --output text)
aws s3 ls "s3://$BUCKET/db/"
make db-restore S3_KEY=db/2026-08-14T18-02-11Z.dump
make deploy-verify
```

The restore runs on the backend instance, because RDS is private with no bastion. It downloads
the archive, reads its table of contents with `pg_restore --list` **before** dropping anything,
and then replays it in a single transaction with `--clean --if-exists`. A truncated archive
fails at the listing step with the database untouched, and a failure mid-replay rolls back
rather than leaving the dashboard reading half a dataset.

There is no default key, on purpose. Restoring "the latest" silently is how the wrong session's
data ends up in a graded dashboard.

**From the RDS final snapshot.** `terraform destroy` leaves one, because
`skip_final_snapshot = false`. Use this only when the dump is also gone: it creates a *new*
instance with a new endpoint, so `/toxic/db/endpoint` and the Terraform state both have to be
reconciled afterwards.

```bash
aws rds describe-db-snapshots --snapshot-type manual \
  --query 'DBSnapshots[?starts_with(DBSnapshotIdentifier, `toxic-mod-final`)].[DBSnapshotIdentifier,SnapshotCreateTime]' \
  --output table
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier toxic-mod-pg-restored \
  --db-snapshot-identifier <snapshot-id> \
  --db-instance-class db.t4g.micro --no-publicly-accessible \
  --db-subnet-group-name toxic-mod-db
```

Then point the application at it and re-roll:

```bash
aws ssm put-parameter --name /toxic/db/endpoint --type String --overwrite \
  --value "$(aws rds describe-db-instances --db-instance-identifier toxic-mod-pg-restored \
    --query 'DBInstances[0].Endpoint.Address' --output text):5432"
make rollback SHA=$(aws ssm get-parameter --name /toxic/deploy/current-sha \
  --query 'Parameter.Value' --output text)
```

The new instance is outside Terraform's state. Reconcile it before the next apply, or the
apply will try to recreate `toxic-mod-pg` and point the application back at an empty database.

## 4. Total teardown — `terraform destroy` ran. Budget: 35 minutes

The dump and the deploy payloads survive in S3: the bucket is versioned, its lifecycle rule
expires `deploy/` noncurrent versions only, and nothing at all expires `db/`.

```bash
cd infra/terraform && terraform apply && cd -   # the ONE place an apply is correct
make aws-up
make db-restore S3_KEY=db/<most-recent>.dump
gh workflow run deploy.yml --ref main
make deploy-verify
```

Two things the apply does not do, and both are needed before the stack works:

* Secrets Manager holds containers, not values. Re-seed `toxic-mod/demo-api-key`,
  `toxic-mod/reviewer-shared-secret`, `toxic-mod/submitter-fp-key`, `toxic-mod/wandb-api-key`
  and `toxic-mod/db-readonly` by CLI, and re-run the `toxic-mod-db-bootstrap-readonly` SSM
  document to recreate the `monitor_ro` role.
* A destroy schedules the secrets for deletion rather than removing them, and Secrets Manager
  refuses to create a name that is still inside another secret's seven-day recovery window. If
  the apply fails on that, `aws secretsmanager delete-secret --secret-id <name>
  --force-delete-without-recovery` each name first.

## The seven-day RDS trap

A stopped RDS instance **restarts by itself after seven days**, and the obvious remedy —
destroying it instead — deletes the dataset the graded dashboard is built on. That conflict is
resolved structurally rather than by remembering: `db-dump` is a hard prerequisite of both
`aws-down` and `aws-destroy`, `aws_down.sh` refuses without a dump newer than an hour, and the
deadline is recorded where it can be read back.

```bash
aws ssm get-parameter --name /toxic/ops/rds-stopped-at --query 'Parameter.Value' --output text
```

`make aws-down` prints the exact UTC restart deadline. Before it, either bring the stack up or
run `make aws-down` again to re-stop.

## What recovery must never do

* **No `terraform apply` outside scenario 4.** It can force-replace an instance, and a replaced
  instance loses `/etc/toxic`, `/var/lib/toxic/artifacts` and the fetched model.
* **Do not re-enable the two EventBridge cost schedules while grading is open.** They are off
  by `-var nightly_stop_enabled=false`, and neither `aws_down.sh` nor `aws_up.sh` touches them.
* **Do not gate anything on SSM reporting `Success`.** It means a shell exited zero. The first
  real roll of this system reported `Success` on all three instances while the backend was
  dying in its FastAPI lifespan on an unwritable spool directory.

## Rehearsal

`docs/evidence/p5-rollback-rehearsal.md` records when each of these was last exercised against
the running system, and its `Status:` line says whether that has happened. A runbook nobody has
run is a hypothesis, and a rehearsal on the day it is needed is an incident.
