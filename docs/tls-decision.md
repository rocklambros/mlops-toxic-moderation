# Decision record: no TLS terminator on the public listeners

- Originally decided: 2026-07-31 (Phase A2, premortem finding H15)
- **Re-opened and re-accepted: 2026-08-10**
- Record written: 2026-08-10
- Status: accepted, with a materially larger residual risk than the original acceptance
- Owner: Rock Lambros

**Decision: the three public listeners serve cleartext HTTP.** There is no load balancer, no
certificate, and no reverse proxy. The specific harm H15 names — the reviewer shared secret
crossing the internet in cleartext — is removed by taking the reviewer interface off the
internet entirely, not by accepting it.

## A note on when this was written

The decision was taken in Phase A2 and is visible in the built system: no Terraform file
anywhere creates a listener on 443, an ACM certificate, or a load balancer. **The record was
not written at the time.** It is written here on 2026-08-10, and the honest reading is that
between 2026-07-31 and today the project had the decision without the reasoning, which is
the state this kind of record exists to prevent.

It is written now rather than backdated, and its first substantive act is to re-open itself,
because the posture it was meant to describe has since changed.

## What changed on 2026-08-10, and why that re-opens it

The original acceptance rested on a specific, bounded exposure claim: `demo_cidrs` defaults
to `[]`, is opened "only while a grader is looking", and "the exposure window is measured in
hours". Its own *re-open this decision if* list named `demo_cidrs` being "left open
overnight" as a trigger.

On 2026-08-10 `infra/terraform/demo.auto.tfvars` set `demo_cidrs = ["0.0.0.0/0"]` **with no
scheduled close**, because a grading date was never published and a stack a grader cannot
reach is not a deliverable. That is an **open-ended** window, not a supervised one. It fires
the trigger the original decision wrote for itself.

So the decision is re-opened here and re-accepted, on narrower grounds and with the residual
risk restated. What did *not* change is the structural control the whole argument rests on:
port 8503 is still unreachable from the internet, and the demo window does not touch it.

## What is exposed, and on what

| Listener | Port | Ingress as of 2026-08-10 | Carries |
|---|---|---|---|
| FastAPI `/predict`, `/health` | 8000 | **`0.0.0.0/0`** plus the operator address | Comment text submitted by anyone holding the demo API key |
| Streamlit user UI | 8501 | **`0.0.0.0/0`** plus the operator address | The same text and the returned probabilities |
| Monitoring dashboard | 8502 | **`0.0.0.0/0`** plus the operator address | Aggregated counts, latencies and rates. No raw comment text |
| **Reviewer UI** | **8503** | **none, on any security group** | **The reviewer shared secret and raw comment text** |

Verified live on 2026-08-10 with `aws ec2 describe-security-groups`: the security group
`toxic-mod-reviewer` exists and has **no ingress rules at all**, and `toxic-mod-db` accepts
only security-group references on 5432, never a CIDR. Evidence and probe commands are in
`docs/evidence/a1-scp-denials.md` for the account-level guardrails and in
`docs/evidence/p5-deploy-traversal.md` for the serving path.

The reviewer UI is reached only through Systems Manager, which is TLS-encrypted end to end
by the service and needs no ingress rule at all:

```bash
aws ssm start-session --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8503"],"localPortNumber":["8503"]}'
```

No credential of any kind is sent to a cleartext listener.

## Alternatives considered and why each was rejected

**Application Load Balancer plus AWS Certificate Manager.** The correct production answer,
and the one this would take with more runway. Rejected on three grounds: an ALB is roughly
$16.20 per month in base charges before LCUs, which is 16 percent of the $100 ceiling
`docs/cost-model.md` is written against; a public ACM certificate requires a validated
domain, so it adds a Route 53 hosted zone, DNS records, and a validation wait to the
critical path; and it introduces target groups, health checks and listener rules as new
first-time-ever integrations on exactly the days the schedule has no slack.

**A per-instance reverse proxy with a self-signed certificate.** Rejected because it does not
close the harm. A self-signed certificate provides no authentication, so it does not defend
against an active attacker, and it puts a browser interstitial in the middle of the graded
screenshot of the working prototype.

**Caddy with Let's Encrypt on a `rockcyber.com` subdomain.** Genuinely closes it and is free.
Rejected on schedule: it needs DNS records for three hosts, ingress on 80 and 443 opened to
the world for the ACME HTTP challenge, and a renewal path — four to six hours on the critical
path.

**This rejection is the weakest of the three, and it got weaker on 2026-08-10.** The
schedule argument was written when the exposure was hours. Against an open-ended window it
buys much less, and the honest position is that Caddy is now the right answer if this window
outlives the assignment. That is why it appears in the re-open list below rather than being
dismissed.

## Residual risk, restated for an open-ended window

Comment text submitted to `/predict` and rendered by the user UI crosses the internet in
cleartext, and so does the aggregate content of the monitoring dashboard. A network observer
between a visitor and the instance can read submitted comments and predicted probabilities,
and can tamper with them in flight. **As of 2026-08-10 that observer no longer has to be on
the operator's path — the listeners answer the whole internet, continuously, until someone
deletes `demo.auto.tfvars`.**

This is accepted here for reasons that are specific and would not transfer to a real
moderation service:

- **No credential transits these listeners.** No session cookie, no password, no personal
  identifier. The demo API key is a request header the operator or grader supplies; it gates
  `/predict` against unmetered use and is rotated after grading, and its disclosure exposes
  nothing beyond the ability to call a public demo endpoint that is already open.
- **The data is public-dataset-derived text.** Jigsaw comments and whatever a grader types.
  There is no third-party user population whose speech is being exposed by this choice.
- **The high-value surface is structurally out of reach**, not merely restricted. Port 8503
  has no ingress rule on any security group, so the reviewer shared secret and the raw
  moderation queue never touch a cleartext listener regardless of what the demo window does.
- **The whole stack is destroyed after grading**, and the exposure ends with it.

What is genuinely worse than the original acceptance, stated rather than smoothed over: the
window is unbounded in time, unsupervised, and reachable by automated scanners rather than
only by a grader. Expect background scanning traffic in the request log. The rate limit and
the input-size cap are what stand between that and an unmetered endpoint, and they are now
load-bearing in a way they were not when the window was hours long.

## Compensating controls in force

1. **Reviewer UI on 8503 has no ingress rule on any security group.** Enforced in code by
   `tests/unit/test_exposure_contract.py::test_no_terraform_rule_of_any_kind_reaches_an_operator_only_port`,
   against the deployed system by
   `tests/integration/test_deployed_traversal.py::test_the_reviewer_ui_is_not_reachable_from_the_internet`,
   and against the demo window specifically by
   `tests/unit/test_demo_window.py::test_the_reviewer_port_is_not_opened_by_the_demo_window`.
2. **`demo_cidrs` defaults to `[]`, and `operator_cidrs` is validated to reject any prefix
   wider than a /24.** Opening the listeners cannot happen by widening the allowlist; it
   requires the separate, committed, reviewable `demo.auto.tfvars`.
3. **`tests/unit/test_demo_window.py` fails once `RESTORE_AFTER` passes.** The window has no
   scheduled close by decision, so the backstop is a red test rather than a timer. It does
   not close anything; it makes the exception impossible to forget.
4. **`/predict` carries an input-size cap and a per-key rate limit** from Phase 2, so a
   cleartext endpoint is not also an unmetered one. `backend/ratelimit.py`,
   `backend/config.py`.
5. **IMDSv2 required on every instance**, so an SSRF through a cleartext listener cannot
   become credential theft.
6. **Rotate the reviewer shared secret and the demo API key after the window closes**, and
   again before submission:
   ```bash
   aws secretsmanager put-secret-value --secret-id toxic-mod/reviewer-shared-secret \
     --secret-string "$(openssl rand -base64 32)"
   ```
   Owner and verification for this and for closing `demo_cidrs` are in
   `docs/post-demo-closure.md`.

## Re-open this decision if

- the demo window outlives the assignment, or the stack is not destroyed after grading;
- any authenticated action, session cookie, or API key moves onto a public listener;
- real user traffic reaches `/predict` from anyone other than the operator or a grader;
- port 8503 acquires an ingress rule on any security group, in which case the structural
  control is gone and cleartext is no longer defensible at all.

The first of those is now a live possibility rather than a hypothetical, because the window
has no close date. If it fires, Caddy with Let's Encrypt is the answer, not an ALB — the
cost argument against the ALB does not improve with time, and the schedule argument against
Caddy disappears once the deadline has passed.

## Disclosure

Stated in `SECURITY.md` as a claim with status **Not true**, in `MODEL_CARD.md` under
limitations, and in `README.md` beside the live URLs and their availability window.
