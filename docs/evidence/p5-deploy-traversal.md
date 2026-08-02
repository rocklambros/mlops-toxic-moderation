# The deployed end-to-end traversal

Run 2026-08-02 against the live stack at `224da4149c4a`, from the operator workstation, over
the public internet. `tests/integration/test_deployed_traversal.py`, `-m integration`.

Delivery spec §3.3: no phase is complete until every route and integration it introduces is
proven working against a real dependency. Phase 3 proved this traversal on local compose.
This is the same traversal across three EC2 instances, through two security groups for the
instance-to-instance hop, into an RDS that is not publicly accessible.

## Result

**11 tests, 11 passed.**

| Assertion | What it proves |
|---|---|
| `/health` reports `database: ok` | the backend instance reaches private RDS |
| `/health` carries no 64-hex string | the artifact digest is not exposed to callers |
| `/predict` returns all six labels, a valid decision, `0 <= max_prob <= 1` | the contract holds over the network |
| a clean comment decides `allow` | the review band floor is positive (see below) |
| `/predict` without the key returns 401 | the demo key is enforced, not decorative |
| the frontend serves `200` and `_stcore/health` is `ok` | Streamlit is up and reached the backend |
| three distinct hosts | rubric 3.2 — the dashboard is a different EC2 server |
| port 8503 refuses from the internet | H12 — the reviewer console is not public |
| a new prediction increments the count | the row reached the database |
| the count is read by `monitor_ro` | and the dashboard's own credentials can see it |
| `CREATE TABLE` as `monitor_ro` is refused | H16 — read-only means read-only |

Observed at the time of the run:

```
predictions visible to the dashboard role : 2029
feedback by source                        : {'reviewer': 650, 'user': 147}
monitor_ro CREATE TABLE                   : (psycopg.errors.InsufficientPrivilege)
                                            permission denied for schema public
```

## Two assertions that are not contract checks

**`allow` is asserted specifically, not merely as a valid decision.** The contract test —
"`decision` is one of allow, review, block" — passed for the entire period a defect made
`allow` unreachable, because `review` is a perfectly valid decision. `REVIEW_MARGIN` is 0.10
and three labels are tuned to 0.05, so the review band floor was −0.05, no probability is
negative, and every input matched the review branch. Fixed in `224da4149c4a`; the traversal
now asserts a clean comment with no flag set decides `allow`, which is a behaviour check and
would have caught it.

**H16 is observed, not inferred.** The read-only grant is asserted by attempting a write and
requiring `permission denied`, rather than by reading the `GRANT` statements in
`monitoring_readonly.sql`. A grant that was intended and a grant that was applied are
different facts, and only one of them is visible from outside the database.

## First-time-ever integrations, and where each was first exercised

The plan for this task expected a day-9 smoke deploy (`docs/evidence/a2-smoke-deploy.md`) to
have exercised all of this five days before it mattered. **That smoke deploy never happened.**
The schedule compressed, `docs/cut-log.md` still records the day-11 checkpoint as `PENDING`,
and the first real deploy of this stack was 2026-08-01. Recording that plainly rather than
citing a document that does not exist:

| Integration | First exercised | Since |
|---|---|---|
| ECR auth from the instance profile | 2026-08-01, first roll | every roll; token re-fetched per start, 12 h expiry |
| arm64 boot on `t4g` Graviton | 2026-08-01, first boot | 3 instances, continuously up |
| digest-verified artifact fetch | 2026-08-01, first roll | 2026-08-02 roll fell back to the S3 mirror when the registry was unreachable, and the digest still matched |
| instance-to-instance HTTP | 2026-08-01, frontend → backend | asserted again in this traversal |
| private RDS from the backend | 2026-08-01 | `database: ok` in this traversal |
| Elastic IP association | 2026-08-01 | all three addresses stable across today's three rolls |
| SSM roll | 2026-08-01 | 6 further rolls on 2026-08-02, including the rollback rehearsal |

So none of these were first-time-ever *during this traversal* — but the margin was one day,
not five, and that is the honest version. The rollback rehearsal recorded in
`p5-rollback-rehearsal.md` is what took the SSM roll from "worked once" to "worked seven
times, including in both directions".

## How it was run

RDS is not publicly accessible, so the database assertions reach it through an SSM
port-forward that uses the backend instance as the relay. Nothing about the deployed system
changes to make the gate runnable: no security group is opened, no instance is made public,
and the database keeps `PubliclyAccessible=false`.

```bash
aws ssm start-session --target <backend-instance> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<rds-endpoint>"],"portNumber":["5432"],"localPortNumber":["15432"]}' &

export MONITORING_DB_DSN="postgresql+psycopg://monitor_ro:<secret>@127.0.0.1:15432/toxicmod"
export BACKEND_URL=$(aws ssm get-parameter --name /toxic/endpoints/backend --query Parameter.Value --output text)
export FRONTEND_URL=$(aws ssm get-parameter --name /toxic/endpoints/frontend --query Parameter.Value --output text)
export MONITORING_URL=$(aws ssm get-parameter --name /toxic/endpoints/monitoring --query Parameter.Value --output text)
export DEMO_API_KEY=$(aws secretsmanager get-secret-value --secret-id toxic-mod/demo-api-key --query SecretString --output text)

PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_deployed_traversal.py -v -m integration
```

The credential used is `monitor_ro`, the dashboard's own read-only role — not the RDS master.
Counting rows as the master would prove the row landed and *assume* the dashboard can see it,
and the assumed half is the one H16 is about.

## Known limitation

The port-forward is subject to SSM's idle timeout and drops after a period of inactivity;
the first attempt at this run failed with `connection refused` because a previous session had
already been terminated for inactivity. The check that matters is whether port 15432 is
listening, not whether a `session-manager-plugin` process exists — a terminated session
leaves the process behind for a moment and the process is therefore not evidence of a usable
tunnel.
