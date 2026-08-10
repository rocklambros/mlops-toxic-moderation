# Post-demo control closure

Two documents in this project accept real risk, and both write their acceptance as resting
on named compensating controls:

- **`docs/tls-decision.md`** accepts cleartext HTTP on the three graded listeners. Its
  argument depends on the demo window being closable, on the reviewer secret being rotated
  after it closes, and on port 8503 never being exposed at all.
- **`MODEL_CARD.md`** accepts that a determined attacker can evade the classifier with
  white-box access, and names "the `/predict` rate limit, the input-size cap, and the demo
  API key" as what remains in force.

Until this file existed, none of those controls had an owner, a target date, or a test.
Grepping every plan for "post-demo checklist" returned exactly one hit: the sentence
promising one. **An accepted risk whose compensating controls are unverified is an
unaccepted risk with better prose.**

This is the owner. `scripts/close_demo.sh` is the procedure,
`docs/submission-manifest.yml` under `post_demo_controls` is the record, and
`tests/unit/test_post_demo_closure.py` is the tripwire.

## The controls

| Control | What it is | Satisfied by |
|---|---|---|
| `demo_cidrs_closed` | The three graded listeners answer only the operator address | Deleting `infra/terraform/demo.auto.tfvars` and re-applying |
| `reviewer_shared_secret_rotated` | The reviewer console credential is replaced after the exposure window | `aws secretsmanager put-secret-value` on `toxic-mod/reviewer-shared-secret` |
| `demo_api_key_rotated` | The `/predict` key handed out for grading is replaced | The same, on `toxic-mod/demo-api-key` |
| `rate_limit_active` | `/predict` refuses an unmetered caller **during** the window, not after | `backend/ratelimit.py`, `tests/integration/test_predict_abuse_controls.py` |

The fourth is not like the other three. It is in force *while* the window is open, which is
exactly when it matters, so it has no excuse to be deferred and the tripwire refuses to let
it be.

## How the tripwire works

It is keyed to the state of the world, not to a checkbox, because a checklist that certifies
itself is the failure mode this file exists to prevent. `infra/terraform/demo.auto.tfvars`
is the file whose presence means "the listeners are open to the internet".

**While that file exists**, the manifest must say the window is open, name what is blocking
closure, and carry a due date for each deferred control. Claiming `satisfied: true` for
`demo_cidrs_closed` while the file is present turns the suite red.

**The moment that file is deleted**, every control must be recorded closed, with a date and
evidence. Closing the listeners and forgetting to write it down also turns the suite red.

Both directions fail loudly. Neither can be satisfied by editing a YAML value, because one
of them is contradicted by Terraform and the other by the absence of Terraform.

There is a third backstop: `tests/unit/test_demo_window.py` goes red on **2026-09-15**
regardless, so an open-ended window cannot quietly become a permanent one.

## Closing it

```bash
export AWS_PROFILE=rc-mlops
export OPERATOR_CIDR="$(curl -s https://checkip.amazonaws.com)/32"
export ALERT_EMAIL=rock@rockcyber.com

bash scripts/close_demo.sh
```

The script removes the tfvars file, applies, rotates both secrets, restarts the containers
so they read the new values, and then **probes** — it checks that no `0.0.0.0/0` rule
remains on any graded listener and that `toxic-mod-reviewer` still has no ingress rule at
all. A `terraform apply` returning zero proves the API accepted a plan, not that a port
stopped answering, so the probe is the part that counts.

Then record it in `docs/submission-manifest.yml`:

```yaml
post_demo_controls:
  demo_cidrs_closed:
    satisfied: true
    verified_on: 2026-0X-XX
    evidence: "close_demo.sh: no 0.0.0.0/0 rule remains on any graded listener"
```

Until those three entries say so, `tests/unit/test_post_demo_closure.py` is red — because
the file is gone and the manifest still claims the window is open.

## What closing does not do

Rotating the demo API key does not retract anything already submitted through it. Comment
text sent to `/predict` while the window was open is in the database and is subject to the
same 30-day retention as everything else (`backend/retention.py`); closure does not shorten
that, and is not a data-deletion step.

Closing also does not destroy the stack. `make aws-down` does that, and it dumps the
database to S3 first.
