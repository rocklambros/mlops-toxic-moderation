# Cost model

The document of record for what this project costs to run. Written against premortem
finding H7, whose complaint was not that the old number was wrong but that it quoted one
half of a two-half cost and read as if it were the whole.

- Account: the member account in the Sandbox OU. Region `us-west-2`, Graviton `arm64`.
- Rates are approximate list prices for `us-west-2`. They are to be confirmed against the
  first full bill in Cost Explorer, not trusted from here.
- Budget ceiling: **$100 per month**, alerting at 50, 80 and 100 percent of both actual and
  forecast spend.
- Every quantity below is read from the applied Terraform in `infra/terraform/`, not
  estimated: three `aws_eip`, root volumes of 30 + 20 + 30 GB in `compute.tf`,
  `allocated_storage = 20` in `data.tf`, four ECR repositories from `local.components`,
  `var.log_retention_days = 14`, and three `aws_secretsmanager_secret` resources plus the
  RDS-managed master password.

## Why this document replaces the previous figure

The superseded estimate was **`$0.101/hr` with everything running**. It counted four
on-demand compute rates and nothing else. Everything below the compute rows in the fixed
table was missing, including roughly **$27 per month that accrues whether or not a single
instance is running**. At the planned duty cycle that omission is larger than the number it
was attached to, which is why an hourly rate is the wrong unit for the decision this
document supports. The question is "does the graded fortnight fit inside $100", and that is
a monthly scenario, not an hour.

## Fixed monthly cost — accrues even with everything stopped

| Line item | Basis | Rate | Quantity | Monthly |
|---|---|---|---|---|
| Elastic IP addresses | Every public IPv4 address, in use or idle, since Feb 2024 | $0.005 / hr | 3 × 730 hr | **$10.95** |
| EBS root volumes | gp3 | $0.08 / GB-month | 30 + 20 + 30 = 80 GB | **$6.40** |
| GuardDuty | Scales with CloudTrail, VPC flow and DNS log volume | estimate | 1 detector | **$4.00** |
| RDS storage | gp3, allocated not used (`max_allocated_storage = 0`) | $0.115 / GB-month | 20 GB | **$2.30** |
| Secrets Manager | Per secret. 3 created by Terraform plus 1 RDS-managed master = four | $0.40 / secret-month | 4 secrets | **$1.60** |
| ECR storage | Four repositories, retained tags per the lifecycle policy | $0.10 / GB-month | ~6 GB | **$0.60** |
| CloudWatch Logs | 14-day retention; ingestion $0.50/GB plus storage $0.03/GB-month | mixed | ~1 GB/month | **$0.53** |
| CloudTrail | First copy of management events is free; this is the S3 storage | $0.023 / GB-month | ~10 GB | **$0.25** |
| Terraform state | S3 standard, versioned, tiny | $0.023 / GB-month | < 1 MB | **$0.02** |
| RDS backup storage | Free up to 100% of allocated storage; the retention window on 20 GB stays inside it | $0.095 / GB-month above allocation | 0 GB billable | **$0.00** |
| SNS | Email notifications; first 1,000 per month are free | $0 | ~50 | **$0.00** |
| CloudWatch alarms | First 10 standard alarms per account are free | $0 | 2 | **$0.00** |
| EventBridge Scheduler | First 14 million invocations per month are free | $0 | ~60 | **$0.00** |
| Data transfer out | First 100 GB per month is free across the account | $0 | < 1 GB | **$0.00** |
| **Fixed monthly subtotal** | | | | **$26.65** |

## Variable, per running hour — only while EC2 and RDS are up

| Line item | Class | Hourly |
|---|---|---|
| EC2 #1 backend | `t4g.medium` | $0.0336 |
| EC2 #2 frontend | `t4g.small` | $0.0168 |
| EC2 #3 monitoring | `t4g.medium` | $0.0336 |
| RDS | `db.t4g.micro` | $0.0160 |
| **Variable subtotal** | | **$0.100 / hr** |

The Elastic IP charge is deliberately *not* in this table. It bills 24/7 whether the
instance is running, stopped, or the address is unattached, so it belongs in the fixed
block. That placement is the single largest correction to the old figure.

## Scenarios against the $100 ceiling

The project runs from 2026-07-30 to 2026-08-18, which is 19 days spanning one billing
month. Fixed cost is prorated at 19/30 in the two bounded scenarios.

| | Running hours | Variable | Fixed | Total |
|---|---|---|---|---|
| **Scenario A — planned.** 6 hours per work session, nightly stop enforced | 114 | $11.40 | $16.88 | **$28.28** |
| **Scenario B — nightly stop disabled and forgotten for the whole project** | 456 | $45.60 | $16.88 | **$62.48** |
| **Scenario C — everything left running for a full billing month** | 730 | $73.00 | $26.65 | **$99.65** |

Scenario C is the number that matters. It sits **at** the ceiling, and it is reachable
without a single service control policy violation, because the SCP instance-type allowlist
caps the hourly *rate* and says nothing about *duration*. That is precisely the gap the
nightly stop schedule closes.

## Controls, strongest first

1. **`terraform destroy`.** Full teardown. It works because the ECR repositories set
   `force_delete`, RDS has `deletion_protection = false` and a unique
   `final_snapshot_identifier`, and the CloudTrail bucket sets `force_destroy`. The final
   snapshot preserves the graded dashboard dataset across a teardown.
2. **The nightly stop schedule** (`nightly_stop_enabled`, default `true`). A hard,
   scheduled stop of all three instances and the database. Disable it deliberately for the
   project is running and re-enable it afterwards:
   `terraform apply -var nightly_stop_enabled=false`.
3. **The SCP instance-type allowlist** from Phase A1. A hard denial on the rate:
   `t4g.small`, `t4g.medium`, `t4g.large`, `c7g.xlarge` only, GPU and metal denied.
4. **Budget alerts** at 50, 80 and 100 percent of both actual and forecast, to SNS and to
   email. Detective, not preventive — which is why they are fourth and not first.

## Costs that survive a `terraform destroy`

Worth knowing before assuming teardown means zero.

| Item | Why it persists | How long |
|---|---|---|
| The RDS final snapshot | Deliberate: it is the graded dataset | Until deleted by hand; $0.095/GB-month beyond free tier |
| Deleted Secrets Manager secrets | `recovery_window_in_days` is non-zero | The recovery window, at $0.40 per secret-month prorated |
| CloudTrail S3 objects | Lifecycle expiry, not immediate deletion | Up to the lifecycle window, cents |

## What to check against the real bill

GuardDuty is the only line here that is an estimate rather than a published rate for a known
quantity. Check it against the first full month in Cost Explorer, and if it is materially
above $4, decide whether it earns its place on a class project.
