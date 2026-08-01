# Cut-line log

Delivery spec §8 defines two leading indicators. Each is evaluated **on the day**, before any
work on the item below it starts, and the decision is recorded here whether or not anything is
cut. A checkpoint that is not evaluated recovers zero days (premortem C8).

`tests/unit/test_cut_log.py` enforces the schema of the table below, and Phase 4's reaper gate
reads this file as its escape hatch — so a row here is a commitment, not a note.

The ordered cut list, weakest first: AIBOM and SBOM; the W&B hyperparameter sweep on RunPod;
the DistilBERT challenger (fine-tune, ONNX export, re-scorer worker, EC2 #3's second
container); the reviewer's second-opinion column.

Never cut, at any checkpoint: the six graded components, the README, the rollback path, the
leakage firewall, the CI gate, safe model loading, and the four submission deliverables.

## Schedule anchor

Day 1 of delivery spec §7's schedule is **2026-07-30**, the day the member account was
bootstrapped (`infra/aws/bootstrap.sh`, commit "Phase A1: bootstrap the member account").
Every checkpoint date below is derived from that anchor: day 8 is 2026-08-06, day 11 is
2026-08-09.

## Checkpoints

| Checkpoint | Evaluated or due | Condition | Decision | Items cut and why | Evidence |
|---|---|---|---|---|---|
| day-8 | 2026-08-06 | PENDING | not due | - | `infra/docker-compose.yml` — Phase 3 Task 21 builds it; the condition is readable the day it exists |
| day-11 | 2026-08-09 | PENDING | not due | - | `docs/evidence/a2-smoke-deploy.md` — written by Phase A2 Task 2 |

## Status at the time this log was created (2026-08-01, day 3)

Recorded so that whoever evaluates day-8 is not reconstructing it from memory.

- Phases 0, 1 and 2 and Phase A1 are complete on their branches; Phase A2's Terraform exists
  on `feat/phase-a2-terraform`. Phase 3 is at Task 18 of 22.
- Phase 1's **training run has not happened yet** ("Phase 1 modules and the RunPod lifecycle,
  before any training run"), so there is no promoted artifact and no
  `artifacts/baseline_flag_rates.json`. The dashboard's drift panel fails closed without it,
  which is tested, but the panel is empty until that run lands.
- The slice-1 server path — submit → predict → log → enqueue → review → feedback → the
  dashboard's queries — is exercised end to end against a real Postgres by the integration
  suite, against a synthetic model artifact. The two Streamlit surfaces are not, by design:
  they import Streamlit inside their drawing functions so the unit job needs no Streamlit.
- `infra/docker-compose.yml` does not exist yet. It is Phase 3 Task 21, which is inside the
  day 7–8 window rather than after it.

**Why both rows are PENDING rather than adjudicated.** Both checkpoints are *leading*
indicators: the whole correction in delivery spec §8 was to date them before the work they
cancel, so that firing one recovers days. Day 8 is five days away. Adjudicating it today
would mean either firing a schedule-risk trigger while the schedule still has five days to
run — cutting DistilBERT on day 3 — or recording a "NOT MET" that predicts the future. The
log records what is true today, and `test_a_pending_checkpoint_is_still_in_the_future` plus
`test_a_pending_checkpoint_names_evidence_that_does_not_exist_yet` make `PENDING` expire:
from 2026-08-07, or from the moment `infra/docker-compose.yml` is committed, the suite is red
until the day-8 row carries `MET`/`NOT MET` and its pre-committed action.

**If you are the one who turned this suite red:** evaluate the condition in delivery spec §8
for that checkpoint, replace `PENDING` with `MET` or `NOT MET`, replace `not due` with `cut`
or `no cut`, name the items in column five if anything was cut, and set the date to the day
you evaluated it.
