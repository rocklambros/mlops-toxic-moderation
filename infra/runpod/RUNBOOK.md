# RunPod RUNBOOK — pod-leak detection and recovery

Operational procedures for the Phase 1 build-time GPU. Read **Preflight** before the first
pod launch of any session; read **Leak Recovery** when something has already gone wrong.

Three modules, layered so the teardown path never depends on the launch path:

| Module | Job |
|---|---|
| `infra/runpod/runpod_client.py` | transport, secrets, spend guard, GPU prices, billing readback |
| `infra/runpod/terminate_runpod.py` | registry, name guard, orphan-safe reconcile, dry-run CLI |
| `infra/runpod/deploy_runpod.py` | the fine-tuning pod: image, bootstrap, atomic registry, lease |

`terminate_runpod` imports only `runpod_client`. It therefore keeps working when
`deploy_runpod` is broken, half-edited, or absent — which is exactly the state the machine
is in when a launch has crashed and somebody needs to stop the meter.

---

## Why this document exists

A forgotten GPU pod is the largest uncontrolled cost in this project. It bills at a constant
rate, produces no output, and nothing alerts you. The authorised ceiling is $1000; a mid-tier
card at roughly $0.30–$0.45 an hour turns a Friday-evening crash into a three-figure Monday.

Every in-process safety net — `atexit`, `try/finally`, signal handlers — dies with `SIGKILL`,
an OOM kill, a closed laptop lid, or a pulled power cord. **The pod registry on disk is the
only teardown mechanism that survives those**, which is why the launcher writes it, atomically
(write-temp → fsync → `os.replace`), *before* it blocks on anything.

```
create pod  ->  ATOMICALLY record it  ->  only then wait for readiness
```

If you remember one thing: **the registry is the backstop, and the reaper works from the
registry.**

---

## Vocabulary

| Term | Meaning |
|---|---|
| **registry** | `infra/runpod/runpod_pods.json` — the pods *this project* created |
| **live** | a pod the RunPod API currently reports as existing |
| **`live_and_ours`** | live **and** in the registry → the reaper terminates it |
| **`registered_gone`** | in the registry, not live → already cleaned up, pruned |
| **`orphan`** | live, **not** in the registry → reported loudly, **never** auto-terminated |
| **name guard** | only pods named `toxic-finetune-*` or `toxic-sweep-*` may be deleted |
| **dead-man switch** | `timeout N sleep infinity` inside the pod's own start command |

An orphan is never killed automatically because the registry, not the name, is proof of
ownership. A live pod nobody recorded may belong to a concurrent run or to another person,
and deleting someone else's work to save $0.30 an hour is the wrong trade.

---

## Preflight — before the first pod of a session

### 1. Set and verify the RunPod spending cap

**Do this before the first launch, not after.** `deploy_runpod` refuses to launch into an
account with no cap set — the API exposes the cap read-only, so it must be set in the console
first — but the refusal is a backstop, not a substitute for setting the number deliberately.

1. Open <https://www.runpod.io/console/user/billing>.
2. Set **Spend limit**. It is a *rate* — dollars per hour of concurrent burn — and it sits
   next to `currentSpendPerHr` in the same API object.
3. **Disable auto-top-up.** With it on, the prepaid balance is not a ceiling: RunPod charges
   the card and the pods keep running.
4. Note the credit balance. RunPod is prepaid, so with auto-top-up off that number is the
   hard total ceiling regardless of any other setting.
5. Read the same three numbers back through the API and record them below. An unrecorded
   check is a check that did not happen.

```bash
# Read-only: cap, prepaid balance, current burn, live candidate-GPU prices, recent spend.
python -m infra.runpod.runpod_client --days 7
```

| Date | Auto-top-up | Spend limit ($/hr) | Credit balance | Checked by |
|---|---|---|---|---|
| _(fill in before the first launch)_ | | | | |

Project ceilings, far below the account cap, live in `deploy_runpod.py` and are the numbers a
human actually reasons about: `MAX_HOURLY_USD = 1.50`, `MAX_RUN_USD = 25.00`,
`DEFAULT_MAX_HOURS = 4.0`. All three are enforced before anything is created.

### 2. Confirm the tooling is green

```bash
PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_terminate_runpod.py \
                                  tests/unit/test_deploy_runpod.py -q
```

143 tests, no network, no credentials, no `pass` binary. **If any of them is red, do not
launch a pod.** They are the gate, not documentation: each one describes a path on which a
live GPU survives the process that created it. See **Residual risk** at the end for what
they do not cover.

### 3. Confirm the key resolves, without printing it

```bash
pass show runpod/api-key >/dev/null && echo "runpod/api-key resolves"
```

Never `export RUNPOD_API_KEY=...` into a shell profile and never echo the value. The tools
read it from `pass` at the point of use with a 5-second timeout, so a locked GPG agent fails
fast instead of hanging with a pod already running.

### 4. Fail closed on a dirty account

```bash
python -m infra.runpod.terminate_runpod
```

Expect `live_and_ours: 0` and `orphans: 0`, exit 0. **Anything else means a previous run
leaked.** `deploy_runpod.preflight` performs this same check and refuses to launch on top of
it — launching over a leak doubles the burn and makes the two indistinguishable afterwards.

### 5. Look at the plan before spending anything

```bash
python -m infra.runpod.deploy_runpod --name distilbert --show-payload
```

With no `--execute` this prints the chosen GPU, the live price, the worst-case cost at the
dead-man-switch duration, the account guard, and the full pod payload with every environment
value redacted. It creates nothing.

---

## Detect — how to list

```bash
# What this project believes it created:
cat infra/runpod/runpod_pods.json

# What RunPod actually has, cross-referenced. DRY RUN: issues no DELETE.
python -m infra.runpod.terminate_runpod
```

The dry-run reconcile is the default. Running the reaper with no flags never deletes anything
and never edits the registry, so it is always safe to run first — including in a panic.

Sample output:

```
=== DRY-RUN reconcile: no DELETE calls will be made ===

*** ORPHAN PODS DETECTED - NOT auto-terminated ***
    ORPHAN id=abc123 name=someone-elses-pod status=RUNNING costPerHr=$0.34
*** A human decides. To kill one:
    python -m infra.runpod.terminate_runpod --pod-id <ID> --execute --force ***

  registered_gone : 0
  live_and_ours   : 1
  orphans         : 1
  would_terminate : 1  (pass --execute to act)
```

**Exit code is the signal.** The reaper exits non-zero on errors, on orphans, *or* on a
registry entry the name guard refused — three different ways of saying "a GPU may be running
and nobody has decided about it". That makes it usable as a CI gate rather than a log line.

The console is the second opinion: <https://www.runpod.io/console/pods>. If the API and the
console disagree, believe the console — it is what billing believes.

---

## Reconcile — how to clean up

```bash
# 1. Inspect the plan. No DELETE, no registry edit.
python -m infra.runpod.terminate_runpod

# 2. Act on it.
python -m infra.runpod.terminate_runpod --execute

# 3. Verify. Expect live_and_ours=0, orphans=0, exit 0.
python -m infra.runpod.terminate_runpod
```

Step 3 is not optional. `--execute` reports what it *attempted*; only a fresh reconcile reports
what is actually gone.

**Idempotent.** An already-gone pod answers HTTP 404, which the reaper treats as success.
Re-running after a partial failure is always safe and is the correct response.

**Partial failures do not stop the run.** If pod 1 fails, pods 2..n are still terminated, the
failure lands under `errors`, and the exit code is non-zero.

**Pruning is evidence-based.** Confirmed-terminated and confirmed-gone pods are removed from
the registry. A pod whose DELETE *failed* is deliberately left in it — that record is the only
thing standing between a failed teardown and an invisible leak. A dry run prunes nothing and
leaves the file byte-identical, so the plan can never damage the thing it is planning against.

---

## Force-terminate a specific pod

For when you have a pod id — from the console, from a log line, from a crash message — and
want it gone now.

```bash
# Dry run first. Prints the plan, issues nothing, needs no network.
python -m infra.runpod.terminate_runpod --pod-id <POD_ID>

# Registered with an allowed name → terminates, then de-registers.
python -m infra.runpod.terminate_runpod --pod-id <POD_ID> --execute

# Untracked, or a name outside the allowlist → REFUSED with exit 1:
#   "pod <ID> is not in infra/runpod/runpod_pods.json. An untracked pod is never
#    deleted silently; pass --force if you are sure it is yours."
python -m infra.runpod.terminate_runpod --pod-id <POD_ID> --execute --force
```

`--force` bypasses both the registry check and the name guard, and prints a warning before it
acts. It is the right tool for a pod you can see in your own console and cannot account for,
and the wrong tool for anything you have not personally looked at first.

---

## Leak recovery — the crash playbook

Symptoms: the launcher was `SIGKILL`ed, the laptop slept, SSH dropped, the process OOMed, or
`make` was `Ctrl-C`ed twice.

1. **Do not relaunch.** `preflight` will refuse anyway, and overriding it doubles the burn.

2. **Reconcile, dry run.**
   ```bash
   python -m infra.runpod.terminate_runpod
   ```

3. **Read the three categories.**
   - `live_and_ours > 0` → the registry did its job. Go to step 4.
   - `orphans > 0` → a pod exists that nothing recorded. Go to step 5.
   - both zero, exit 0 → nothing leaked. Confirm in the console and move on.

4. **Terminate what is ours.**
   ```bash
   python -m infra.runpod.terminate_runpod --execute
   python -m infra.runpod.terminate_runpod            # verify: expect 0 / 0, exit 0
   ```

5. **Adjudicate orphans by hand.** The reaper will not do this for you, by design. For each
   orphan id, open <https://www.runpod.io/console/pods>, confirm the pod is yours and that its
   creation time matches your crashed run, then:
   ```bash
   python -m infra.runpod.terminate_runpod --pod-id <ORPHAN_ID> --execute --force
   ```
   If you cannot confirm it is yours, **leave it running and ask** before deleting it.

6. **Confirm the registry is empty** once the console shows zero pods:
   ```bash
   cat infra/runpod/runpod_pods.json     # expect []
   printf '[]\n' > infra/runpod/runpod_pods.json   # only if it is not
   ```

7. **Read back what it actually cost.** Not an estimate from wall-clock — the number RunPod
   billed:
   ```bash
   python -m infra.runpod.runpod_client --days 1
   ```

### If the registry is corrupt

A truncated `runpod_pods.json` is the expected artefact of a crash mid-write, and it is
exactly when pods are most likely to be live. `read_registry` raises rather than returning
`[]` — an empty plan would read as "nothing to reap" while a GPU bills. Fall through to the
manual console fallback, then reset the file with `printf '[]\n'`.

### Manual console fallback — API down, key revoked, tooling broken

This path always works and depends on nothing in this repository.

1. Open <https://www.runpod.io/console/pods>.
2. Identify pods named `toxic-finetune-*` or `toxic-sweep-*`, and any pod whose creation time
   matches the run that crashed.
3. **Terminate**, not Stop. A stopped pod keeps its disk and keeps billing for storage.
4. Check <https://www.runpod.io/console/user/storage> for any orphaned network volume. The
   launcher attaches none, so this is normally a no-op — but a volume bills after the pod is
   gone, which is the one cost that survives terminating everything in the pod list.
5. Reset the registry: `printf '[]\n' > infra/runpod/runpod_pods.json`.
6. Confirm the pod list is empty and note the credit balance.

---

## Guardrails the tooling enforces

| Guardrail | What it prevents | Where |
|---|---|---|
| Registry written atomically before the readiness wait | A crash mid-launch leaving an unrecorded, billing pod | `launch_pod` |
| write-temp → fsync → `os.replace` | A half-written registry the reaper refuses to parse | `atomic_write_registry` |
| Dry-run default on both CLIs | A mistyped command deleting or creating | `main` in both modules |
| Name guard (`toxic-finetune-`, `toxic-sweep-`) | Deleting a pod this project did not create | `terminate_all_registered`, `reconcile` |
| Guard fails closed on a nameless entry | A corrupt registry becoming a delete-anything primitive | `_allowed` |
| Launcher refuses an unreapable name | Creating a pod the reaper is not allowed to kill | `launch_pod` |
| Orphan-safe reconcile | Killing a concurrent run's pod | `reconcile` |
| Orphans make the exit code non-zero | A leak that only shows up in a log nobody reads | `main` |
| Idempotent 404 handling | A re-run failing on already-gone pods | `terminate_pod` |
| Per-pod error isolation | Pod 1's failure leaving pods 2..n billing | `terminate_all_registered` |
| Prune only what is confirmed gone | Erasing the record of a failed teardown | `reconcile` |
| `assert_no_survivors` re-query | Trusting a 204 that did not actually delete | `PodLease.teardown` |
| Preflight fail-closed on any live pod | Launching on top of a leak | `preflight` |
| Account spend guard (4 ceilings) | An uncapped account, a breach, a run that outlives the balance | `assert_spend_guard` |
| GPU allowlist priced live | Renting a flagship card for a 66M-parameter model | `choose_gpu` |
| `interruptible=True` (spot) | Paying on-demand for a restartable fine-tune | `build_pod_payload` |
| `timeout N sleep infinity` in the pod | A dead laptop leaving the GPU running | `build_bootstrap` |
| Secrets from `pass`, 5s timeout, scrubbed | A hung GPG agent; a key in a public CI log | `load_secret`, `scrub` |

---

## Cost reference

Read from the RunPod GraphQL `gpuTypes` on 2026-07-31 (spot / on-demand, USD per hour). Prices
move; `choose_gpu` re-reads them live and picks the cheapest in-stock candidate rather than
trusting this table.

| Card | Memory | Spot / on-demand | Suitable here |
|---|---|---|---|
| NVIDIA A40 | 48 GB | 0.30 / 0.35 | Yes — usually the pick |
| RTX 4090 | 24 GB | 0.34 / 0.34 | Yes |
| NVIDIA RTX A6000 | 48 GB | 0.33 / 0.33 | Yes |
| NVIDIA L4 | 24 GB | none / 0.44 | Yes, but no spot offer, so dearest of the four |
| A100 80 GB | 80 GB | ~1.20–1.90 | **No** — oversized |
| H100 / H200 | 80+ GB | ~2.50–4.00+ | **No** — an order of magnitude of waste |

A 66M-parameter DistilBERT over 212,510 short comments is a mid-card job. An H100 would spend
ten times as much to be limited by the same dataloader, and the A40's 48 GB buys more useful
batch-size headroom than raw FLOPs do at this size.

A leaked A40 costs roughly **$7 a day, $48 a weekend**. That is the number the whole
registry-first design is buying down.

---

## After every session

1. `python -m infra.runpod.terminate_runpod` → expect `0 / 0`, exit 0.
2. Confirm the pod list is empty in the console.
3. `pod_spend(days=1)` for what the session actually cost. Report measured, not estimated.
4. **Rotate `HF_TOKEN`.** It is injected into the pod environment, so it is readable by
   anything that ran in that pod: <https://huggingface.co/settings/tokens>, then
   `pass edit huggingface/token`. The same applies to `WANDB_API_KEY` after any run where the
   pod was shared or the logs were made public.
5. Leave `infra/runpod/runpod_pods.json` as `[]`, committed. The next session's Preflight
   depends on it being clean.

---

## Residual risk — what the automation cannot cover

The contract suite is green as of 2026-07-31: 143 tests, no network, no credentials. Five
leak paths it originally found have been closed in the implementation, and each now has a
test standing over it:

| Closed leak | The test that holds it closed |
|---|---|
| `PodLease.__enter__` raising after the pod was created left it abandoned, because `__exit__` never runs and `self.pod` was never assigned | `test_the_lease_terminates_a_pod_whose_launch_never_returned` |
| A failed registry write left a pod that `reconcile` would correctly refuse to auto-terminate as an orphan, forever | `test_a_failed_registry_write_terminates_the_pod_immediately` |
| A registry entry with an empty `pod_id` passed the name guard and issued `DELETE /v1/pods/` — against the *collection* | `test_an_entry_with_an_empty_pod_id_never_produces_a_collection_delete` |
| `launch_pod` forwarded any `gpu_type` it was handed, so a caller skipping `preflight` could rent a flagship card | `test_the_launcher_refuses_a_gpu_outside_the_allowlist` |
| A failed teardown printed the pod id but not the recovery command | `test_a_failing_teardown_tells_the_operator_what_to_run` |

**What remains, and cannot be closed from this side of the API:**

1. **The create-to-record window.** Between `POST /pods` returning and the registry write
   completing there is one HTTP response parse. A `SIGKILL` inside it orphans a pod that
   nothing recorded. This is the residual the orphan report exists to catch, and it is why
   Preflight step 4 is not optional.
2. **The registry is per-machine.** A pod launched from a different machine is an orphan
   here, correctly and permanently. The console is the only cross-machine view.
3. **Storage is not pods.** Terminating every pod does not release a network volume. The
   launcher attaches none, so this is normally a no-op — but it is the one cost that
   survives an empty pod list. Check
   <https://www.runpod.io/console/user/storage> after any session that deviated from the
   defaults.
4. **Spot preemption is not a failure the tooling sees.** A preempted pod stops billing GPU
   but keeps its disk. It shows up as `live_and_ours` with a non-RUNNING status; `--execute`
   clears it.
5. **The spend cap is read-only over the API.** The tooling can refuse to launch when it is
   absent or would be breached; it cannot set it. Preflight step 1 is a human step.

---

## Related

- Contract tests: `tests/unit/test_terminate_runpod.py`, `tests/unit/test_deploy_runpod.py`
- Implementation plan: Task 18 of `docs/superpowers/plans/2026-07-31-phase-1-train-register.md`
- Canonical pattern this is ported from: `incident-rank-validation`,
  `tools/terminate_runpod.py` and the Pod-Leak Recovery section of its `docs/RUNBOOK.md`
- Scheduled backstop: **not yet built.** Task 18 of the phase-1 plan specifies
  `.github/workflows/runpod-reaper.yml` (hourly dry-run reconcile, `workflow_dispatch` with
  `execute: true` to act), and `.github/` does not exist in this worktree as of 2026-07-31.
  Until it does, **the only thing that reaps a leaked pod is a human running the command
  above.** Set a phone reminder before a long run; do not rely on a workflow that is not
  there.
