# Design Spec: Two Graded Panel Defects, and the Reviewer Routes' Actual Exposure

- Version: 1.0
- Owner: Rock Lambros
- Date: 2026-08-11
- Status: approved for planning
- Supersedes: `2026-08-11-reviewer-loopback-no-secret-design.md` (refuted; see its section 0)
- Scope: fix two verified defects in graded dashboard panels, then close the one live exposure
  the refuted spec correctly identified, at a fraction of its blast radius

## 1. Why this exists

An adversarial premortem aimed at a reviewer-secret redesign found two defects that outrank it,
both in panels graded under rubric 3.2, both verified in the working tree.

**The drift panel compares the seeded data to itself.** `monitoring/queries.py:132` reads
`FROM predictions WHERE ts >= :since` with no `is_seed` predicate. The reference distribution in
`baseline_flag_rates.json` is computed over the locked held-out split, and `make seed-demo`
replays that same split through `/predict` — 2000 of the window's 2048 rows. PSI is therefore
computed between a distribution and itself, is ~0 by construction, and cannot move. The panel
then prints a confident, well-denominated sentence: "No label exceeds the PSI alert threshold of
0.2, over 2000 predictions in the drift window." The `is_seed` column already exists and is
already used one function away, at `queries.py:266`.

**The live-accuracy panel has no sample floor.** `monitoring/dashboard.py:453` gates the
headline `st.metric` on `data.accuracy.point is not None` and nothing else. One reviewed row
scored correct renders **100.0%** in the largest type on the page. Panel 1 enforces
`MIN_SAMPLES_PER_BUCKET = 20`; Panel 2 enforces `MIN_DRIFT_SAMPLES = 30` plus an exact binomial
tail test. The panel a grader reads first enforces nothing. The Wilson interval in the caption
bounds the uncertainty honestly, but the caption is not what a screenshot shows.

Third, and smaller: the reviewer routes answer the internet, because they are mounted on the
same app as `/predict` (`backend/app.py:180`) and `var.demo_cidrs` opens 8000 to `0.0.0.0/0`.
This is the one argument from the refuted spec that survived. What did not survive is its
remedy — see that document's section 0. The exposure is a *guessing* surface rather than an
*interception* surface: the shared secret is posted to a private VPC address on every path this
project operates, and `backend/review_api.py:161` already meters sign-in at five attempts per
minute per peer.

## 2. Starting position, verified

| Fact | Value | How verified |
|---|---|---|
| Drift query filters seeded rows | **No.** `WHERE ts >= :since` only | `monitoring/queries.py:132` |
| `is_seed` available and used elsewhere | Yes, `queries.py:266` (`seeded_share`) | read |
| Panel 3 minimum sample size | **None** | `monitoring/dashboard.py:453` |
| Existing floors on the other panels | 20 per bucket, 30 for drift | `dashboard.py:88`, `queries.py:89` |
| Current window composition | 2048 predictions, 2000 seeded, 48 live | re-seed of 2026-08-11 |
| Reviewer sign-in rate limit | 5/minute/peer, already live | `backend/review_api.py:118,161` |
| Secret present on the frontend host | **No** | `infra/deploy/compose.frontend.yml:38-43` |
| Reverse proxy in front of 8000 | None, so `request.client.host` is the true peer | `docs/tls-decision.md:10-11` |

## 3. The design

### 3.1 Drift: separate the positive control from the measurement

Filtering seeded rows out and stopping would be wrong — it takes the window from 2048 to 48 and
mutes the graded panel. The defect is not that seeded rows are counted; it is that a comparison
with no degrees of freedom is reported in the same voice as a measurement.

`production_flag_rates` gains a `seeded` argument (`True`, `False`, or `None` for all) and the
drift report carries both series: the full-window comparison it already computes, and the live
subset with its own `n`. The caption states which is which, and withholds an inferential claim
about the full-window series when seeded rows dominate — because against replayed reference
data, "no drift" is what a correctly wired panel must say, and that is a wiring check, not a
finding.

This keeps the panel populated, keeps the existing `MIN_DRIFT_SAMPLES` and improbability gates
doing their job on the live subset, and stops the page asserting more than it knows.

### 3.2 Live accuracy: a floor that matches the two panels beside it

`MIN_REVIEWED_FOR_ESTIMATE = 30`, matching `MIN_DRIFT_SAMPLES`, applied to the estimator's
effective sample size. Below it the `st.metric` is suppressed and replaced with a statement of
how many reviews exist and how many are needed — the shape Panel 1 already uses for thin
buckets. The caption and the per-stratum table continue to render, because a reader who wants
the detail should still get it.

At the current 643 reviewed items this changes nothing on screen. It is a guard against the
degenerate case, and the degenerate case is the one that ends up in a screenshot.

### 3.3 Reviewer routes: refuse a public peer

One clause in the existing `_gate` middleware: a request whose path is under `/review/` and
whose TCP peer is a **globally routable** address is answered `404`.

`ipaddress.ip_address(host).is_global` is the whole test. It is true for any real internet
address and false for 10/8, 172.16/12, 192.168/16 and loopback — which is every legitimate
caller, since `roll.sh:259` points the console at the backend's private address. A peer that
does not parse as an IP address at all is treated as non-public, which is correct here because
the only such caller is an in-process test client.

`404` rather than `403`: the response should not confirm that the route exists.

Deliberately unchanged: `/feedback/user` stays reachable from the internet with the demo key,
because it is the anonymous mechanism rubric 3.2 grades. `/review/login` keeps its peer limiter,
which remains the binding control for anything inside the VPC.

**What this does not do.** It does not delete the shared secret, and it does not separate the
reviewer API from the public UI container: both run on the frontend instance and therefore
share its private IP, and a security group attaches to the instance's network interface, not
to one container on it, so no group boundary distinguishes them. `peer_is_public` cannot either
-- it discriminates on the peer address, and that address is the same for both. The secret
remains the only control on that path. This spec closes the internet exposure and says plainly
that it closes nothing else.

## 4. What changes

`monitoring/queries.py` — `production_flag_rates` gains the `seeded` filter; `drift_report`
carries a live-subset series and its `n`. `monitoring/dashboard.py` — `MIN_REVIEWED_FOR_ESTIMATE`
and the suppressed-metric branch; `drift_caption` distinguishes control from measurement.
`backend/app.py` — the peer clause in `_gate`. `SECURITY.md` and `docs/tls-decision.md` — record
that the reviewer routes no longer answer the internet, and that the secret is retained and why.

No Terraform. No compose changes. No container moves. No IAM changes. No Secrets Manager
deletion. Rollback remains `make rollback`, the six-minute path rehearsed in
`docs/evidence/p5-rollback-rehearsal.md`.

## 5. Testing

Unit: seeded and live rows produce different `production_flag_rates` results on the same window;
the drift caption does not make an inferential claim about a seed-dominated window; the accuracy
metric is suppressed below the floor and rendered above it; a public peer gets 404 on
`/review/pending` and `/review/submit` while a private peer does not; `/feedback/user` and
`/predict` are unaffected by the peer clause.

Live, against the deployed stack: `/review/pending` and `/review/submit` return **404** from the
open internet where they returned 401 before, and `/feedback/user` still returns 401 rather than
404 — proving the clause discriminates by path rather than blanket-hiding routes.

The existing `tests/integration/test_deployed_traversal.py` reviewer assertions are re-pointed
rather than deleted, since 401 becoming 404 is the property under test.

## 6. Risks

| Risk | Mitigation |
|---|---|
| The peer clause locks out the live console | The console calls the backend's private address (`roll.sh:259`); asserted by a live round trip through the queue before merge |
| `request.client` is `None` behind some future proxy | Treated as non-public and allowed, which fails to the current behaviour rather than to an outage; there is no proxy today (`tls-decision.md:10-11`) |
| Drift panel changes shape in the graded screenshot | Screenshot is retaken and the manifest counts updated in the same change |
| Suppressing the accuracy metric hides a real number | Floor is 30 against 643 current reviews; caption and strata table still render below it |

## 7. Out of scope, and named rather than dropped

The reviewer shared secret is retained. TLS remains unimplemented and remains an accepted risk.
The retention purge remains unscheduled and remains recorded as Partial in `SECURITY.md`. Rubric
1.3 remains PARTIAL pending an int8 re-export. The estimator's complete-case assumption — it
weights by probability of *selection* and not of *response* — is a real limitation surfaced by
the premortem and is not addressed here; it is recorded in the ledger below rather than fixed,
because fixing it means measuring response rates the system does not currently record.

Also recorded and not fixed: `frontend/reviewer.py:174` keys review checkboxes as `cb_{label}`
rather than per request id, so a failed submit can carry labels to the next item; and
`roll.sh`'s `secret()` helper fails open on a missing secret where `param()` fails closed.
