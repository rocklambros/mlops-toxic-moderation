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
| day-8 | 2026-08-01 | NOT MET | no cut | - | Evaluated five days early because the artifact it was waiting on landed early. Slice 1 **is** serving end to end on local compose: `infra/docker-compose.yml`, five graded containers healthy, traversal below |
| day-11 | 2026-08-09 | PENDING | not due | - | `docs/evidence/a2-smoke-deploy.md` — written by Phase A2 Task 2 |

## Status at the time this log was created (2026-08-01, day 3, before Phase 3 Task 21)

Recorded so that whoever evaluates day-8 is not reconstructing it from memory. Kept verbatim
rather than edited: the last two bullets were overtaken later the same day by Task 21, and
the next section is the adjudication that followed. Rewriting a leading indicator's inputs
after the fact is how a checkpoint stops being evidence.

- Phases 0, 1 and 2 and Phase A1 are complete on their branches; Phase A2's Terraform exists
  on `feat/phase-a2-terraform`. Phase 3 is at Task 18 of 22.
- Phase 1's **classical training run has not happened yet** ("Phase 1 modules and the RunPod
  lifecycle, before any training run"), so there is no promoted artifact and no
  `artifacts/baseline_flag_rates.json`. The dashboard's drift panel fails closed without it,
  which is tested, but the panel is empty until that run lands. (The DistilBERT fine-tune
  *has* since run; see the adjudication below.)
- The slice-1 server path — submit → predict → log → enqueue → review → feedback → the
  dashboard's queries — is exercised end to end against a real Postgres by the integration
  suite, against a synthetic model artifact. The two Streamlit surfaces are not, by design:
  they import Streamlit inside their drawing functions so the unit job needs no Streamlit.
- `infra/docker-compose.yml` does not exist yet. It is Phase 3 Task 21, which is inside the
  day 7–8 window rather than after it.

## day-8 adjudication (2026-08-01, day 3)

The row above was `PENDING` for one reason: the condition — "Slice 1 not serving end-to-end on
local compose" — could not be read until there was a local compose stack to read it against.
`test_a_pending_checkpoint_names_evidence_that_does_not_exist_yet` named
`infra/docker-compose.yml` as that artifact and turned the suite red the moment Phase 3
Task 21 committed it. The sentinel was the right one and it fired on the day it was supposed
to; what it forced was an evaluation, and this is that evaluation, done against a running
stack rather than against a promise.

**Condition NOT MET → no cut. The DistilBERT branch survives.**

Measured on 2026-08-01 with `docker compose -f infra/docker-compose.yml up -d`:

- Five graded containers up and reporting healthy: `postgres`, `backend` (8000),
  `frontend` (8501), `monitoring` (8502), `reviewer` (127.0.0.1:8503 only).
- The whole traversal, against those containers: `POST /predict` returned a decision and six
  probabilities → the row was logged with a measured `latency_ms` → it was enqueued to
  `review_queue` with `source='flagged'` and `sample_rate=1.0` and a verbatim
  `input_text_snapshot` → `POST /review/login` issued a session → `GET /review/pending`
  returned the snapshot byte-identical → `POST /review/submit` derived a `feedback` row with
  a per-label agreement vector → `POST /feedback/user` recorded an anonymous verdict → the
  dashboard rendered all three graded panels from those rows.
- The dashboard read them as `monitoring_ro`. That role's `DELETE` and `UPDATE` were both
  refused with `permission denied` (premortem H16, `infra/postgres-init/02-monitoring-role.sql`).
- The re-scorer, behind the `challenger` profile, drained the queue with the **real** Phase 1
  DistilBERT export and was idempotent on a second pass.

**What is genuinely not finished, recorded so the day-11 evaluator is not misled.** The
classical Production model has not been promoted: there is no `artifacts/toxic-clf.skops` of
record and no `artifacts/baseline_flag_rates.json` of record, so the traversal above ran the
*serving path* against the committed synthetic fixture artifact. That is what the checkpoint
asks about — the condition is "slice 1 serving", not "the final model is trained" — but the
distinction belongs in the log rather than in somebody's memory.

**The DistilBERT branch is further along than the checkpoint assumed.** The fine-tune has
run (3 epochs, `eval_macro_pr_auc` 0.7268 against the classical model's OOF 0.6656) and the
float32 ONNX export is valid and passes the load-time parity gate. Its int8 sibling does not:
`model_quantized.onnx` is refused at max |logit delta| 0.5728 against the 0.05 ceiling, and
Phase 1's own export gate measured 2.7206 on a larger sample. The cause is diagnosed — the
quantizer ran per-tensor and targeted the exporting host's x86 architecture rather than the
arm64 serving fleet — and fixed in Phase 1's `deploy_runpod.py`, but **the fix has not been
proven by a passing re-export**. Until it is, the challenger runs float32. That is a
configuration choice (`CHALLENGER_MODEL_FILE`, `CHALLENGER_SHA256`), not a code change, and
it costs memory and latency on EC2 #3 rather than correctness.

Cutting item 3 today would therefore discard work that is already paid for and passing, to
recover schedule that is not under pressure. That is precisely the trailing-trigger mistake
delivery spec §8 was corrected to avoid, in the other direction.

**Why the day-11 row is still PENDING.** It is a *leading*
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
