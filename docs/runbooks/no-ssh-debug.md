# Runbook: debugging three instances you cannot SSH into

`infra/aws/verify_deploy.sh` prints a pointer to this file when the deploy gate fails, and
`infra/aws/aws_up.sh` prints it when a boot marker never appears. Those are the two moments
this document exists for.

**There is no SSH, and that is not an oversight.** No security group opens port 22, no key
pair exists, and `infra/aws/scp-sandbox-guardrails.json` denies the credential-creation calls
that would let you make one. Every remote action below is Systems Manager. If SSM is broken
on an instance, that instance is unreachable by design, and the last section is how you get
out of that.

Work the ladder in order. Each rung tells you whether the next one is worth trying, and the
early rungs cost seconds.

---

## Rung 0: is this actually broken?

```bash
export AWS_PROFILE=rc-mlops AWS_REGION=us-west-2
make deploy-verify        # infra/aws/verify_live.sh
```

`verify_deploy.sh` checks all three endpoints and does **not** stop at the first failure, so
one run tells you whether you have one broken tier or three. One broken tier and two healthy
ones is a different problem from three broken tiers, and the difference is usually "the app"
versus "the account".

If all three fail at once, suspect ingress before you suspect the application — the listeners
answer only the CIDRs in `operator_cidrs` plus `demo_cidrs`, and a changed home IP presents
exactly as a dead stack:

```bash
aws ec2 describe-security-groups \
  --filters Name=group-name,Values=toxic-mod-frontend \
  --query 'SecurityGroups[0].IpPermissions[].IpRanges[].CidrIp' --output text
curl -s https://checkip.amazonaws.com    # compare
```

## Rung 1: is the instance running, and does SSM see it?

```bash
aws ec2 describe-instances \
  --filters Name=tag:Component,Values=backend Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress]' --output text

aws ssm describe-instance-information \
  --query 'InstanceInformationList[].[InstanceId,PingStatus,LastPingDateTime]' --output table
```

**`PingStatus` is the fork in the road.**

- `Online` → the agent is alive, you have a shell by proxy. Go to rung 2.
- Absent or `ConnectionLost` → you cannot run anything on the box. Skip to rung 4.

An instance that is `running` but missing from `describe-instance-information` usually means
user data failed before the agent could register, or the instance profile lost
`AmazonSSMManagedInstanceCore`.

## Rung 2: did user data finish?

The last line of user data writes `/toxic/boot/<component>` to Parameter Store. That
parameter existing is the whole claim that boot completed.

```bash
for c in backend frontend monitoring; do
  printf '%s: ' "$c"
  aws ssm get-parameter --name "/toxic/boot/$c" --query Parameter.Value --output text 2>&1 | head -1
done
```

Missing marker means user data did not reach its end. The console output carries the reason
and needs no agent at all:

```bash
aws ec2 get-console-output --instance-id i-... --output text | grep -iE \
  'TOXIC-USER-DATA-COMPLETE|error|failed|cloud-init'
```

`TOXIC-USER-DATA-COMPLETE` is the string user data prints last. Present in the console but no
SSM parameter means the write failed — look at the instance profile, not at the boot script.

## Rung 3: SSM is Online — get the application's own account

Run Command is the shell. Nothing here needs a secret as an argument; the instance reads its
own credentials.

```bash
aws ssm send-command \
  --document-name AWS-RunShellScript \
  --targets Key=tag:Component,Values=backend \
  --parameters 'commands=[
    "systemctl is-active toxic-stack.service || systemctl status toxic-stack.service --no-pager -l | tail -40",
    "docker ps -a --format \"{{.Names}}\t{{.Status}}\"",
    "docker compose -f /opt/toxic/compose.backend.yml logs --tail=80 --no-color"
  ]' --query 'Command.CommandId' --output text
```

Then read it back with the returned id:

```bash
aws ssm get-command-invocation --command-id <id> --instance-id i-... \
  --query '{status:Status,out:StandardOutputContent,err:StandardErrorContent}' --output text
```

What the common shapes mean:

| Symptom | Where to look |
|---|---|
| `toxic-stack.service` inactive | The unit is `infra/deploy/toxic-stack.service`. It brings the stack up at boot; if it never started, boot is the problem, not the app |
| Container restarting in a loop | `docker compose logs`. A digest mismatch on the model artifact is fail-closed by design and says so explicitly |
| Container up, endpoint refused | Port binding versus security group. `docker ps` shows the published port; compare with `infra/exposure.py` |
| `BaselineMissingError` on monitoring | `thresholds.json` / `baseline_flag_rates.json` were not fetched to `/var/lib/toxic/artifacts`. `roll.sh` fetches them; the drift panel is deliberately fail-closed |

CloudWatch has the same logs without a round trip, which is often faster:

```bash
aws logs tail /toxic-mod/backend --since 30m --format short
```

## Rung 4: SSM is not Online

You cannot run commands. Two things still work, in this order.

**Console output** needs nothing on the instance:

```bash
aws ec2 get-console-output --instance-id i-... --output text | tail -100
```

**The EC2 Serial Console** needs the account setting enabled and a password set on a user;
on a locked-down AL2023 image with no configured user this is frequently a dead end. Try it,
but do not spend long — rung 5 is usually faster and always works.

## Rung 5: stop debugging, replace the instance

An instance you cannot reach is not worth a long diagnosis when the fleet is cattle and the
state is all elsewhere: the database is RDS, the artifacts are S3 and ECR, and the
configuration is Parameter Store and Secrets Manager. Nothing that matters lives on the disk.

Two ways back, cheapest first.

**Re-roll the current SHA** — if the box is up but the app is wrong:

```bash
make aws-up            # brings the stack up and gates on the three health endpoints
```

**Roll back to the previous SHA** — if the current deploy is the problem. This never touches
Terraform:

```bash
aws ssm get-parameter --name /toxic/deploy/previous-sha --query Parameter.Value --output text
make rollback SHA=<that sha>
```

`infra/ROLLBACK.md` is the full procedure and has been rehearsed against the live stack;
`docs/evidence/p5-rollback-rehearsal.md` is the record.

**Replace the instance** — last resort, and an operator action from an Identity Center
session because the CI deploy role is denied `ec2:RunInstances` by
`infra/terraform/oidc.tf`:

```bash
cd infra/terraform
terraform apply -replace='aws_instance.this["backend"]' \
  -var 'operator_cidrs=["<your-ip>/32"]' -var 'alert_email=...'
```

Expect roughly six minutes to a healthy endpoint. The Elastic IP re-attaches, so the URLs in
`README.md` stay correct.

---

## What to capture before you fix it

If you are going to replace the instance, take the console output first. It is the only
artifact that does not survive termination:

```bash
aws ec2 get-console-output --instance-id i-... --output text \
  > "console-$(date -u +%Y%m%dT%H%M%SZ).txt"
```

Redact before it goes anywhere public — `scripts/redact.py` removes account ids and Elastic
IPs, both of which appear in console output as a matter of course.
