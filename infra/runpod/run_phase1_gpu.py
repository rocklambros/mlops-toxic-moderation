"""Drive one live DistilBERT fine-tune on one GPU pod, end to end, and always tear it down.

This is the thin thing on top of the lifecycle, not a second copy of it. Every destructive or
billable action goes through `deploy_runpod` and `terminate_runpod`: the atomic
registry-before-readiness write, the `PodLease` teardown on success/failure/interrupt, the
`atexit` hook, the name-guard allowlist, the orphan-safe reconcile, and the pod's own
`timeout` dead-man switch all come from there unchanged.

What this module adds is the order of operations for a real run, and three guards that only
make sense at this level:

1. **The account spending cap is read and reported before anything is created.** RunPod's cap
   is account-wide dollars-per-hour and is not settable by any customer credential -- the
   mutation exists but answers `Not authorized. Missing required scope(s): CSR_WRITE, CSR_GTM,
   CSR_ADMIN`, so it is RunPod-staff-only. What is enforceable from here is refusing to launch
   when the cap is absent or when the projected burn would breach it, plus this project's own
   ceilings, which are roughly fifty times tighter than the account cap.

2. **The held-out test rows never leave this machine.** The bundle cache is written with them;
   the copy that goes to the pod is rebuilt without them and its manifest is rewritten to say
   so, so `load_bundle_cache(with_test=True)` on the pod fails rather than succeeding quietly.
   The firewall is a property of the bytes that travel, not of a flag on the far side.

3. **A two-minute smoke train runs first, inside the same lease.** A flag typo, a missing
   wheel, a CUDA mismatch, a tokenizer that cannot reach Hugging Face -- each of these costs
   the full run if it is discovered at the end of it. The smoke run trains 2,000 rows for one
   epoch and exports ONNX from the result, exercising every step of the expensive path for
   about two cents, and it is the reason a real failure is found at minute three rather than
   at minute fifty.

The pod is terminated by the lease. Afterwards, `prove_no_pods_live` re-queries the API and
reports the count, because a teardown nobody verified is a teardown nobody can report.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infra.runpod import deploy_runpod as dep
from infra.runpod.runpod_client import (
    PASS_WANDB_KEY,
    AccountStatus,
    account_status,
    load_secret,
    pod_spend,
    scrub,
)
from infra.runpod.terminate_runpod import DEFAULT_REGISTRY, list_live_pods, reconcile

MANIFEST_NAME = "manifest.json"
TEST_FILE = "test.csv.gz"

SMOKE_OUTPUT = f"{dep.REMOTE_WORKDIR}/outputs-smoke"
SMOKE_ONNX = f"{SMOKE_OUTPUT}/onnx"
SMOKE_ROWS = 2000


class PreparationError(RuntimeError):
    """Something that must be true before a pod exists is not."""


# ---------------------------------------------------------------------------
# The leakage firewall, enforced on the bytes that travel
# ---------------------------------------------------------------------------


def build_pod_bundle(source: Path | str, dest: Path | str) -> dict[str, Any]:
    """Copy the bundle cache to `dest` without the held-out test rows, and say so in place.

    `load_bundle_cache` verifies every digest in `manifest["files"]`, so the entry for the
    file that is no longer there has to go with it, and `has_test` has to become false or the
    pod would report "the cache was built with test rows" while holding none. Both edits are
    to the *copy*: the local cache keeps its held-out rows, because Phase 1 evaluates on them
    exactly once, on this machine, under the git-tracked touch log.

    Returns the rewritten manifest, so the caller can read `split_version` out of the bytes it
    is actually shipping rather than out of the ones it meant to.
    """
    source, dest = Path(source), Path(dest)
    manifest_path = source / MANIFEST_NAME
    if not manifest_path.is_file():
        raise PreparationError(
            f"no bundle cache at {source}. Build it once on this box:\n"
            "  .venv/bin/python -m model.train_distilbert --build-cache "
            f"--csv data/raw/jigsaw-toxic-comment-train.csv --cache {source}"
        )
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(TEST_FILE, "__pycache__"))

    manifest = json.loads((dest / MANIFEST_NAME).read_text())
    manifest["files"] = {k: v for k, v in manifest.get("files", {}).items() if k != TEST_FILE}
    manifest["has_test"] = False
    manifest["held_out_withheld_from_pod"] = True
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    if (dest / TEST_FILE).exists():
        raise PreparationError(
            f"{dest / TEST_FILE} still exists: the held-out rows would reach the pod"
        )
    return manifest


# ---------------------------------------------------------------------------
# Pod-side readiness beyond "SSH answers"
# ---------------------------------------------------------------------------


def wait_for_bootstrap(
    pod: dep.Pod,
    *,
    key_path: Path,
    timeout_s: float = 1800.0,
    interval_s: float = 15.0,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> None:
    """Block until the pod's start command has finished installing wheels.

    SSH answering is not the same as the pod being usable. `dockerStartCmd` brings sshd up
    first and only then runs `pip install`, so the window between "readiness probe passes" and
    "transformers exists" is several minutes wide -- and a training command sent into that
    window dies on `ModuleNotFoundError` with the pod already billing.
    """
    deadline = monotonic() + timeout_s
    while True:
        try:
            dep.run_remote(
                pod, f"test -f {shlex.quote(dep.READY_SENTINEL)}", key_path=key_path, timeout=60.0
            )
            return
        except dep.LaunchError:
            if monotonic() >= deadline:
                raise dep.ReadinessTimeout(
                    f"pod {pod.pod_id} never finished its bootstrap within {timeout_s:.0f}s; "
                    f"the sentinel {dep.READY_SENTINEL} was never written"
                ) from None
            sleep(interval_s)


WANDB_GRAPHQL = "https://api.wandb.ai/graphql"
_VIEWER_QUERY = "{viewer {username entity teams{edges{node{name}}}}}"


def wandb_entities(*, url: str = WANDB_GRAPHQL, timeout: float = 30.0) -> tuple[str, set[str]]:
    """`(default_entity, every entity this key may write to)`, read from W&B.

    The key is used at the point of the call and never leaves this process. Only names come
    back.
    """
    import base64  # noqa: PLC0415 - only needed on this path
    import urllib.request  # noqa: PLC0415

    key = load_secret(PASS_WANDB_KEY, "WANDB_API_KEY")
    auth = base64.b64encode(f"api:{key}".encode()).decode()
    request = urllib.request.Request(  # noqa: S310 - constant https URL
        url,
        data=json.dumps({"query": _VIEWER_QUERY}).encode(),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "User-Agent": "mlops-toxic-moderation/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        viewer = (json.loads(response.read()).get("data") or {}).get("viewer") or {}
    default = str(viewer.get("entity") or viewer.get("username") or "")
    allowed = {default, str(viewer.get("username") or "")} - {""}
    for edge in ((viewer.get("teams") or {}).get("edges") or []):
        name = ((edge or {}).get("node") or {}).get("name")
        if name:
            allowed.add(str(name))
    return default, allowed


def resolve_wandb_entity(requested: str | None) -> str:
    """Refuse an entity this key cannot write to, before a pod exists.

    A wrong entity is not a wrong name in a log: `wandb.init` authenticates fine, then fails
    with `failed to upsert bucket: 404 Not Found` at the first log call. That happened live,
    on a GPU, at minute five, with `rocklambros` for an account whose entity is `rockcyber`.
    The check costs one HTTPS round trip on the box that is about to spend the money.
    """
    default, allowed = wandb_entities()
    if requested and requested not in allowed:
        raise PreparationError(
            f"W&B entity {requested!r} is not one this API key can write to ({sorted(allowed)}). "
            "wandb.init would authenticate and then 404 on the first log, after the pod is "
            "already billing."
        )
    entity = requested or default
    if not entity:
        raise PreparationError("W&B returned no entity for this API key")
    print(f"W&B entity: {entity} (key may write to {sorted(allowed)})")
    return entity


REQUIRED_POD_CREDENTIALS: tuple[str, ...] = ("WANDB_API_KEY",)


def assert_pod_credentials(
    pod: dep.Pod, *, key_path: Path, names: tuple[str, ...] = REQUIRED_POD_CREDENTIALS
) -> None:
    """Prove the pod can see the credentials the training run will need, before it needs them.

    The smoke train runs with `--no-wandb`, so it cannot catch a missing W&B key by
    construction -- and that is exactly how a run reached the real training step, loaded a
    corpus onto a GPU, and died on `load_secret` on a pod that had a perfectly valid key
    sitting in PID 1's environment the whole time.

    This asks `podenv` the same question the training command will ask, exits non-zero if the
    answer is no, and runs nothing else. Names only cross the wire; no value is ever printed.
    """
    dep.run_remote(
        pod,
        f"cd {dep.REMOTE_WORKDIR} && "
        + " ".join(dep.POD_ENV_SHIM)
        + f" --require {' '.join(shlex.quote(n) for n in names)}",
        key_path=key_path,
        timeout=120.0,
    )
    print(f"pod credentials visible to the runner: {list(names)}")


def smoke_command(spec: dep.FinetuneSpec) -> str:
    """One epoch over 2,000 rows, no W&B run, into a scratch directory.

    Deliberately built from the same `train_command` the real run uses, with three overrides
    appended, so the smoke test exercises the actual flag vocabulary. A smoke test that hand-
    writes its own command proves that the hand-written command works.
    """
    base = spec.train_command(output_dir=SMOKE_OUTPUT)
    return (
        f"{base} --max-train-rows {SMOKE_ROWS} --epochs 1 --train-probe-rows 256 --no-wandb"
    )


def assert_realized_price(
    pod: dep.Pod,
    *,
    quoted_usd_per_hr: float,
    max_hours: float,
    max_hourly_usd: float = dep.MAX_HOURLY_USD,
    max_run_usd: float = dep.MAX_RUN_USD,
) -> float:
    """Check what the pod *actually* costs, not what the quote said it would.

    Observed on the first live launch: `preflight` quoted an A40 at $0.300/hr from the GraphQL
    `lowestPrice.minimumBidPrice`, and the created pod came back at $0.440/hr. That field is
    the lowest bid across every cloud type and region on offer; the pod this project creates is
    pinned to `cloudType: SECURE`, so the two are not the same number and never were. Nothing
    was breached -- $0.44 is well inside this project's $1.50/hr ceiling -- but a quote that
    can be 47% low is a quote no ceiling should be enforced against. The ceilings are therefore
    re-checked here against the rate the account will really be charged, on a pod that exists
    and can still be torn down by the lease.
    """
    realized = pod.cost_per_hr
    if realized <= 0:
        print(
            f"WARNING: pod {pod.pod_id} reports no costPerHr; the quote of "
            f"${quoted_usd_per_hr:.3f}/hr is the only figure available",
            file=sys.stderr,
        )
        return quoted_usd_per_hr
    if abs(realized - quoted_usd_per_hr) > 0.01:
        print(
            f"NOTE: realized rate ${realized:.3f}/hr differs from the quoted "
            f"${quoted_usd_per_hr:.3f}/hr (lowestPrice is across all cloud types; this pod is "
            "SECURE)"
        )
    if realized > max_hourly_usd:
        raise dep.LaunchError(
            f"pod {pod.pod_id} bills ${realized:.3f}/hr, above this project's ceiling of "
            f"${max_hourly_usd:.2f}/hr; tearing it down rather than running it"
        )
    worst_case = realized * max_hours
    if worst_case > max_run_usd:
        raise dep.LaunchError(
            f"worst-case ${worst_case:.2f} at ${realized:.3f}/hr for {max_hours:.1f}h exceeds "
            f"the run ceiling of ${max_run_usd:.2f}"
        )
    print(f"realized rate ${realized:.3f}/hr, worst case ${worst_case:.2f} over {max_hours:.1f}h")
    return realized


# ---------------------------------------------------------------------------
# Proof
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Teardown:
    live_pods: list[dict[str, Any]]
    spend_usd: float
    records: list[dict[str, Any]]

    @property
    def count(self) -> int:
        return len(self.live_pods)


def prove_no_pods_live(*, days: int = 1) -> Teardown:
    """Re-query the API after the lease has closed and report what is still running.

    This is the only statement about teardown worth making. `PodLease` already verified the
    pods it created are gone; this asks the account, so a pod created by a crashed earlier
    attempt shows up too.
    """
    live = list_live_pods()
    total, records = pod_spend(days=days)
    return Teardown(live_pods=live, spend_usd=total, records=records)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def report_spend_cap() -> AccountStatus:
    status = account_status()
    print(
        "RunPod account:\n"
        f"  spending cap    : ${status.spend_limit}/hr (account-wide; RunPod-staff-settable "
        "only -- updateUserSpendLimit requires CSR scopes)\n"
        f"  prepaid balance : ${status.client_balance:.2f}\n"
        f"  burning now     : ${status.current_spend_per_hr:.2f}/hr\n"
        f"  project ceilings: ${dep.MAX_HOURLY_USD:.2f}/hr, ${dep.MAX_RUN_USD:.2f}/run "
        "(enforced in preflight, before anything is created)"
    )
    if not status.spend_limit or status.spend_limit <= 0:
        raise PreparationError("no account spending cap is set; refusing to launch")
    return status


def assert_nothing_is_running(registry_path: Path) -> None:
    state = reconcile(registry_path, execute=False)
    if state["live_and_ours"] or state["orphans"]:
        raise PreparationError(
            f"{len(state['live_and_ours'])} registered pod(s) and {len(state['orphans'])} "
            "orphan(s) are already live. Clear them before launching: "
            "python -m infra.runpod.terminate_runpod --execute"
        )
    print(f"live pods before launch: {len(state['live_and_ours']) + len(state['orphans'])}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infra.runpod.run_phase1_gpu",
        description=(
            "Fine-tune DistilBERT on one RunPod GPU pod, export int8 ONNX with a parity gate, "
            "register both artifacts with digests, and tear the pod down. Dry run by default."
        ),
    )
    parser.add_argument("--execute", action="store_true", help="actually launch (default: plan)")
    parser.add_argument("--name", default="distilbert")
    parser.add_argument("--cache", type=Path, default=Path("data/cache/bundle"))
    parser.add_argument("--staging", type=Path, default=Path("artifacts/pod-bundle"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/distilbert-gpu"))
    parser.add_argument("--gpu", default=None, help=f"one of {dep.GPU_CANDIDATES}")
    parser.add_argument("--max-hours", type=float, default=3.0)
    parser.add_argument("--on-demand", action="store_true", help="disable spot")
    parser.add_argument("--fold", type=int, default=0)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--wandb-project", default="mlops-toxic-moderation")
    parser.add_argument("--wandb-entity", default=None)
    parser.add_argument("--ssh-key", type=Path, default=dep.DEFAULT_SSH_KEY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--no-smoke", action="store_true", help="skip the 2-minute smoke train")
    parser.add_argument("--no-register", action="store_true", help="skip the W&B upload")
    return parser


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - a linear runbook
    args = build_arg_parser().parse_args(argv)
    name = f"{dep.POD_NAME_PREFIX}{args.name}"
    interruptible = not args.on_demand

    try:
        report_spend_cap()
        assert_nothing_is_running(args.registry)
        wandb_entity = resolve_wandb_entity(args.wandb_entity)
        manifest = build_pod_bundle(args.cache, args.staging / "bundle")
        split_version = manifest["split_version"]
        print(
            f"pod bundle staged at {args.staging / 'bundle'}: "
            f"train={manifest['n_train']} folds={manifest['n_folds']} "
            f"split_version={split_version[:12]}... held-out rows withheld"
        )
        spec = dep.FinetuneSpec(
            epochs=args.epochs,
            fold=args.fold,
            train_batch_size=args.batch_size,
            expect_split_version=split_version,
            wandb_project=args.wandb_project,
        )
        plan = dep.preflight(
            name=name,
            max_hours=args.max_hours,
            interruptible=interruptible,
            registry_path=args.registry,
            candidates=(args.gpu,) if args.gpu else dep.GPU_CANDIDATES,
        )
    except Exception as exc:  # noqa: BLE001 - one message, no traceback, no launch
        print(f"PREFLIGHT FAILED: {scrub(str(exc))}", file=sys.stderr)
        return 1

    print(f"plan:\n{plan.render()}")
    print(f"  train command   : {spec.train_command()}")
    print(f"  export command  : {spec.export_command()}")
    if not args.execute:
        print("\nDRY RUN: nothing was created. Pass --execute to launch.")
        return 0

    pub_key = Path(f"{args.ssh_key}.pub")
    if not pub_key.exists():
        print(f"ERROR: no SSH public key at {pub_key}", file=sys.stderr)
        return 1

    env = dep.build_pod_env(
        wandb_project=args.wandb_project,
        wandb_entity=wandb_entity,
        ssh_public_key=pub_key.read_text().strip(),
        extra={"WANDB_RUN_NAME": name},
    )
    started = time.time()
    failure: str | None = None
    receipt: dict[str, Any] | None = None

    try:
        with dep.PodLease(
            name=name,
            registry_path=args.registry,
            gpu_type=plan.gpu_type,
            env=env,
            interruptible=interruptible,
            max_hours=args.max_hours,
        ) as pod:
            host, port = dep._endpoint(pod)  # noqa: SLF001 - same package, one accessor
            print(f"pod {pod.pod_id} ready at {host}:{port} (${pod.cost_per_hr:.3f}/hr)")
            assert_realized_price(
                pod, quoted_usd_per_hr=plan.price_usd_per_hr, max_hours=args.max_hours
            )

            wait_for_bootstrap(pod, key_path=args.ssh_key)
            print("bootstrap complete: wheels installed")
            print(
                dep.run_remote(
                    pod,
                    "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader "
                    "&& python -c \"import torch;print('torch',torch.__version__,"
                    "'cuda',torch.cuda.is_available())\"",
                    key_path=args.ssh_key,
                    timeout=300.0,
                ).strip()
            )

            archive = Path(args.output).parent / dep.CODE_ARCHIVE_NAME
            dep.deliver_code(pod, dep.make_code_archive(archive), key_path=args.ssh_key)
            dep.deliver_dataset(pod, args.staging / "bundle", key_path=args.ssh_key)
            print("code and bundle delivered")

            # Before the expensive part, not fifty minutes into it. The smoke train runs with
            # --no-wandb, so it is structurally incapable of catching a missing W&B key: that
            # gap cost a live pod, and this line is what closes it.
            assert_pod_credentials(pod, key_path=args.ssh_key)

            if not args.no_smoke:
                print("smoke train (2,000 rows, 1 epoch) ...")
                dep.run_remote(
                    pod,
                    f"cd {dep.REMOTE_WORKDIR} && {smoke_command(spec)}",
                    key_path=args.ssh_key,
                    timeout=1800.0,
                )
                dep.run_remote(
                    pod,
                    f"cd {dep.REMOTE_WORKDIR} && "
                    + spec.export_command(
                        model_dir=f"{SMOKE_OUTPUT}/final", onnx_dir=SMOKE_ONNX
                    )
                    + " --sample-size 64",
                    key_path=args.ssh_key,
                    timeout=1800.0,
                )
                dep.run_remote(
                    pod,
                    f"rm -rf {shlex.quote(SMOKE_OUTPUT)}",
                    key_path=args.ssh_key,
                    timeout=120.0,
                )
                print("smoke train and ONNX export passed; starting the real run")

            print(dep.run_remote(
                pod,
                f"cd {dep.REMOTE_WORKDIR} && {spec.train_command()}",
                key_path=args.ssh_key,
                timeout=args.max_hours * 3600,
            ))
            print(dep.run_remote(
                pod,
                f"cd {dep.REMOTE_WORKDIR} && {spec.export_command()}",
                key_path=args.ssh_key,
                timeout=3600.0,
            ))

            if not args.no_register:
                entity = f"--entity {shlex.quote(wandb_entity)}"
                out = dep.run_remote(
                    pod,
                    f"cd {dep.REMOTE_WORKDIR} && "
                    + " ".join(dep.POD_ENV_SHIM)
                    + " --require WANDB_API_KEY -- python -m "
                    "infra.runpod.register_pod_artifacts "
                    f"--model-dir {dep.REMOTE_OUTPUT_DIR}/final "
                    f"--onnx-dir {dep.REMOTE_ONNX_DIR} "
                    f"--project {shlex.quote(args.wandb_project)} {entity} "
                    f"--run-name {shlex.quote(name)} "
                    f"--receipt {dep.REMOTE_OUTPUT_DIR}/registry_receipt.json",
                    key_path=args.ssh_key,
                    timeout=3600.0,
                )
                print(out)
                receipt = json.loads(out[out.index("{"):]) if "{" in out else None

            # Inside the lease, always: the pod's disk dies with the pod.
            dep.fetch_checkpoint(pod, args.output, key_path=args.ssh_key)
            print(f"artifacts retrieved to {args.output}")
    except BaseException as exc:  # noqa: BLE001 - the pod is already down; report and continue
        failure = scrub(f"{type(exc).__name__}: {exc}")
        print(f"RUN FAILED: {failure}", file=sys.stderr)

    elapsed_h = (time.time() - started) / 3600
    proof = prove_no_pods_live()
    print(
        f"\n=== teardown proof ===\n"
        f"  wall clock      : {elapsed_h:.2f}h\n"
        f"  pods live now   : {proof.count}\n"
        f"  billed (24h)    : ${proof.spend_usd:.4f} across {len(proof.records)} record(s)"
    )
    if receipt:
        print(f"  checkpoint sha  : {receipt['checkpoint']['sha256']}")
        print(f"  int8 onnx sha   : {receipt['onnx_int8']['sha256']}")
    if proof.count:
        print(
            "SEV-1: pods are still live. Run: "
            "python -m infra.runpod.terminate_runpod --execute",
            file=sys.stderr,
        )
        return 1
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
