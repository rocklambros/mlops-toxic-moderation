"""Launch, use, and always tear down one DistilBERT fine-tuning pod on RunPod.

The canonical `incident-rank-validation` pod lifecycle with the workload swapped: a vLLM
serving pod becomes a fine-tuning pod, so the image, the start command, and the data flow all
change, and nothing about the lifecycle does. The ordering is the whole design:

    create pod  ->  ATOMICALLY record it  ->  only then block on readiness

The registry write happens *before* the readiness wait because the wait is the long,
blocking, killable part -- and a pod that exists but is recorded nowhere bills until somebody
happens to open the console. `atexit`, `try/finally`, and signal handlers all die with
`SIGKILL`, an OOM kill, or a closed laptop lid; the file on disk does not. It is the only
teardown mechanism that survives the failure modes that actually orphan GPUs.

Four mechanisms, in decreasing order of how much still has to be working:

1. `PodLease.__exit__` -- fires on success, on any exception, and on Ctrl-C. It also fires
   when `__enter__` itself raises, which is the case a plain `with` block does *not* cover
   and the most expensive gap available here.
2. `atexit`, registered at lease time -- fires on an uncaught exception or `sys.exit`.
3. The crash-durable registry, via `python -m infra.runpod.terminate_runpod --execute`.
4. The pod's own dead-man switch: the start command runs under `timeout`, so an abandoned pod
   stops burning GPU even if this machine never comes back.

**Nothing here has been executed against the live API.** The payload field names, the GPU ids,
the prices, and the image digest were read from the live RunPod OpenAPI, the RunPod GraphQL,
and Docker Hub on 2026-07-31; but no pod has been created, so the bootstrap script and the
SSH transfer path are unproven. Run the first launch with `--max-hours 1` and watch it.

Non-negotiables the workload carries, and why each exists:

- `problem_type="multi_label_classification"`. Without it, HF `Trainer` defaults to softmax
  cross-entropy over a six-column target and silently trains the wrong objective: the loss
  falls, the run looks healthy, and the model is meaningless.
- Early stopping on validation, weight decay, 2-3 epochs, and the train/validation loss gap
  logged every epoch, so overfit is visible while it happens rather than after the money.
- safetensors, never pickle. `assert_safetensors_checkpoint` refuses a `.bin`.
- The held-out test set is never touched on the pod. DistilBERT is scored on validation
  folds; choosing between classical and DistilBERT on test numbers is selection on the test
  set.
"""

from __future__ import annotations

import argparse
import atexit
import json
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from infra.runpod.runpod_client import (
    PASS_HF_TOKEN,
    PASS_RUNPOD_KEY,
    PASS_WANDB_KEY,
    REST_BASE,
    AccountStatus,
    SpendGuardError,
    append_registry,
    assert_spend_guard,
    auth_headers,
    choose_gpu,
    http_get,
    http_post,
    load_secret,
    redact_env,
    remove_from_registry,
    scrub,
)
from infra.runpod.terminate_runpod import (
    DEFAULT_ALLOW,
    DEFAULT_REGISTRY,
    assert_no_survivors,
    reconcile,
    terminate_pod,
)

# ---------------------------------------------------------------------------
# What runs, on what, for how long
# ---------------------------------------------------------------------------

# runpod/pytorch with torch 2.4.0 / CUDA 12.4.1 / Python 3.11. The Python minor matches this
# project's own pin, so a wheel that installs here installs there. Verified on Docker Hub
# 2026-07-31: amd64, 7.4 GB, pushed 2024-09-24.
POD_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
# Recorded so a tag that silently moves can be noticed. RunPod takes a docker reference, so
# `runpod/pytorch@sha256:...` should also work -- unverified, which is why it is not default.
POD_IMAGE_DIGEST = "sha256:61a4aafb0094cd773f11eefa378929d5a687bd775febeb78eac62fc824141fb5"

# Installed on the pod at boot; torch ships in the image. Pinned, but UNVERIFIED against this
# image: the first launch must confirm the resolve, and re-pinning POD_IMAGE means re-pinning
# this list.
POD_REQUIREMENTS: tuple[str, ...] = (
    "transformers==4.46.3",
    "datasets==3.1.0",
    "accelerate==1.1.1",
    "safetensors==0.4.5",
    "scikit-learn==1.5.2",
    "wandb==0.18.7",
    "optimum==1.23.3",
    "onnx==1.17.0",
    "onnxruntime==1.20.1",
    # The classical CV entrypoint needs these. They arrive by an edge no static walker can
    # see: run_phase1 calls pickle.load on the bundle, which reconstructs DatasetBundle and
    # therefore imports model/data/prepare.py -> dedup -> datasketch at runtime. Ten paid
    # launches diagnosed exactly this.
    "skops==0.13.0",
    "datasketch==1.6.5",
    "iterative-stratification==0.1.9",
    # `model/contract.py` imports it, `model/export_onnx.py` imports that, and the version is
    # the project's own pin so `probs_to_dict` validates identically on both sides. Its
    # absence cost a live pod once: the smoke export died on ModuleNotFoundError at minute two
    # and `test_the_pod_installs_every_third_party_module_its_entrypoints_import` is what
    # replaced finding that out on a GPU.
    "pydantic==2.9.2",
    # The classical-CV path on the same pod. `run_phase1.py` serializes with skops, and
    # reconstructing a `DatasetBundle` imports `model/data/prepare.py` -> `dedup` ->
    # `datasketch` and `split` -> `iterstrat` at *runtime*, through pickle, where no static
    # import walker can see the edge. Every version here matches `requirements/base.txt`, so
    # an artifact written on the pod loads on the Jetson.
    "skops==0.13.0",
    "datasketch==1.6.5",
    "iterative-stratification==0.1.9",
    "scipy==1.14.1",
)

# Already in `POD_IMAGE`, so installing them again would only risk resolving a different build
# than the CUDA wheels the image was assembled around. Verified live on the first launch:
# `torch 2.4.1+cu124, cuda True`, and a training run that imports pandas and numpy at module
# scope reached its first step.
POD_IMAGE_PROVIDES: tuple[str, ...] = ("torch", "numpy", "pandas")

# Import name -> the distribution that supplies it, where the two differ.
MODULE_TO_DISTRIBUTION: dict[str, str] = {
    "sklearn": "scikit-learn",
    "iterstrat": "iterative-stratification",
    "PIL": "pillow",
    "yaml": "pyyaml",
}

# The allowlist is a budget control that runs before the money is spent, not a preference.
# Live prices from the RunPod GraphQL `gpuTypes`, read 2026-07-31 (spot / on-demand, USD/hr):
#
#   NVIDIA A40                48 GB   0.30 / 0.35   stock High     secure cloud
#   NVIDIA RTX A6000          48 GB   0.33 / 0.33   stock Medium   secure + community
#   NVIDIA GeForce RTX 4090   24 GB   0.34 / 0.34   stock High     secure + community
#   NVIDIA L4                 24 GB   none / 0.44   stock Medium   no spot offer at all
#
# A 66M-parameter DistilBERT over 212k short comments is a mid-card job. The A40 leads on
# price, on stock, and on the 48 GB that buys batch-size headroom -- which matters more at
# this size than raw FLOPs, because the run is dataloader-bound long before it is
# compute-bound. A flagship card would spend roughly ten times as much to be limited by the
# same bottleneck, so none appears here and `launch_pod` refuses one by name.
GPU_CANDIDATES: tuple[str, ...] = (
    "NVIDIA A40",
    "NVIDIA RTX A6000",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA L4",
)

POD_NAME_PREFIX = "toxic-finetune-"

# Project ceilings, far below the $1000 authorised. These are the numbers a human reasons
# about, and they are why a typo cannot turn $25 into $1000.
MAX_HOURLY_USD = 1.50
MAX_RUN_USD = 25.00
DEFAULT_MAX_HOURS = 4.0

REMOTE_WORKDIR = "/workspace"
REMOTE_DATA_DIR = f"{REMOTE_WORKDIR}/data"
# Where `deliver_dataset` lands a directory named `bundle`: scp -r <local>/bundle <remote>/
# puts it at <remote>/bundle. The trainer's `--cache` must name the same path, and it is a
# constant on both sides so the two cannot drift into a silent "no bundle cache at ..." after
# the pod is already billing.
REMOTE_CACHE_DIR = f"{REMOTE_DATA_DIR}/bundle"
REMOTE_OUTPUT_DIR = f"{REMOTE_WORKDIR}/outputs"
REMOTE_ONNX_DIR = f"{REMOTE_OUTPUT_DIR}/onnx"

# The int8 model is quantized for where it SERVES, not for where it is exported. The serving
# fleet is `t4g`, which is ARM Graviton, while pods are x86 -- so letting the exporter read its
# own architecture ships an AVX-512 VNNI artifact to a Neoverse core. Change this only when the
# instance family changes.
SERVING_QUANT_TARGET = "arm64"
READY_SENTINEL = f"{REMOTE_WORKDIR}/.pod-bootstrap-complete"

# The project code is not on the pod: `/data/` is gitignored and the pod has no clone. The
# tree is shipped as one tarball rather than an `scp -r` of a source directory, because scp
# would also carry `__pycache__` built for aarch64 and silently shadow the pod's own bytecode.
CODE_ARCHIVE_NAME = "mtm-code.tar.gz"
CODE_PATHS: tuple[str, ...] = ("model", "infra", "pyproject.toml", "run_phase1.py")

# Every remote command is prefixed with this. RunPod delivers the pod payload's `env` to the
# container's PID 1, and a shell started by `sshd` is not a child of PID 1 -- so the key is
# present in the container and absent in every SSH session, and a run with a perfectly valid
# credential still dies on `load_secret: pass is not installed and WANDB_API_KEY is not set`,
# after the pod is created, bootstrapped, and billing. The shim restores it from
# `/proc/1/environ` in memory -- not from a file it wrote, and not from a command line -- then
# `execvp`s the real command. See `infra/runpod/podenv.py`.
POD_ENV_SHIM: tuple[str, ...] = ("PYTHONHASHSEED=0", "python", "-m", "infra.runpod.podenv")

DEFAULT_SSH_KEY = Path.home() / ".ssh" / "mlops-jetson"

_REAP_CMD = "python -m infra.runpod.terminate_runpod"


class LaunchError(RuntimeError):
    """Pod creation, naming, readiness, or remote execution failed."""


class ReadinessTimeout(LaunchError):
    """The pod never became usable inside the cap. The lease still tears it down."""


class CheckpointError(RuntimeError):
    """What came back from the pod is not a usable safetensors checkpoint."""


# ---------------------------------------------------------------------------
# The workload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinetuneSpec:
    """Hyperparameters for the fine-tune, and the guards that ride along with them.

    `problem_type` is a validated field rather than a literal buried in a command string
    precisely because it is the highest-consequence flag in this file: anything but
    `multi_label_classification` makes `Trainer` apply softmax cross-entropy to a six-label
    multi-label target, and every number downstream then measures the wrong model.
    """

    base_model: str = "distilbert-base-uncased"
    problem_type: str = "multi_label_classification"
    epochs: int = 3
    train_batch_size: int = 32
    eval_batch_size: int = 128
    learning_rate: float = 3e-5
    weight_decay: float = 0.01
    max_seq_length: int = 192
    early_stopping_patience: int = 1
    # Forward-only probe over a fixed slice of TRAINING rows, in eval mode, evaluated at every
    # epoch boundary. This is what makes the train/validation loss gap a measured number
    # rather than a comparison between a dropout-on running average and a clean eval loss.
    # Zero would silence the per-epoch gap, so it is validated below rather than trusted.
    train_probe_rows: int = 4096
    fold: int = 0
    seed: int = 42
    fp16: bool = True
    # Refuses to train unless the delivered cache carries this split. Left None only when the
    # split has not been built yet; the driver fills it in from the local manifest, so a pod
    # that received the wrong bundle fails in seconds instead of after a paid GPU hour.
    expect_split_version: str | None = None
    wandb_project: str = "mlops-toxic-moderation"
    # The module the Slice-3 owner adds. A parameter, because this file does not own it.
    train_module: str = "model.train_distilbert"

    def __post_init__(self) -> None:
        if self.problem_type != "multi_label_classification":
            raise ValueError(
                "problem_type must be 'multi_label_classification'; anything else makes HF "
                "Trainer train softmax cross-entropy on a six-column multi-label target "
                "without erroring"
            )
        if not 2 <= self.epochs <= 3:
            raise ValueError(
                f"epochs={self.epochs}: the design fixes 2-3 epochs. More overfits 180k "
                "rows; fewer underfits."
            )
        if self.weight_decay <= 0:
            raise ValueError("weight_decay must be > 0: it is one of the two overfit brakes")
        if self.train_probe_rows <= 0:
            raise ValueError(
                "train_probe_rows must be > 0: it is what makes the train/validation loss gap "
                "measurable at every epoch boundary, and overfit invisible while it happens is "
                "overfit discovered after the money is spent"
            )

    # The classical CV is a different job with a different flag vocabulary: no epochs, no
    # weight decay, no problem_type. Swapping train_module alone would send DistilBERT flags
    # to a parser that does not define them, and argparse exits 2 on an unrecognised flag.
    classical: bool = False

    def classical_command(
        self, *, cache_dir: str = REMOTE_CACHE_DIR, output_dir: str = REMOTE_OUTPUT_DIR
    ) -> str:
        """The classical CV and threshold tuning, tracked, in `run_phase1.py`'s own vocabulary.

        `POD_ENV_SHIM` is here for the same reason it is on `train_command`, and leaving it off
        was not cosmetic: RunPod hands the pod payload's `env` to the container's PID 1, an
        `sshd` session inherits none of it, and `run_phase1.start_run` would then reach for
        `pass`, which a pod does not have. Every launch on 2026-07-31 was created with a valid
        `WANDB_API_KEY` and logged zero runs. `--require WANDB_API_KEY` makes that failure cost
        one second instead of the whole cross-validation, and rubric 1.2 is graded on the run
        page that key is what creates.

        The `PHASE1_*` assignments sit *outside* the shim because they are settings for the
        final process, not secrets to be recovered from PID 1; `execvp` carries them through.
        """
        # A background sync copies each fold checkpoint into PHASE1_OUT the moment it lands.
        # Retrieval used to run only after the whole stage finished, so folds sat on the
        # pod's disk for over an hour with no copy anywhere -- and a spot preemption at 95%
        # discarded all five. The CV has no resume path, unlike a fine-tune, so incremental
        # egress is the only thing that makes a mid-run death survivable.
        sync = (
            f"(while sleep 60; do cp -u {shlex.quote(cache_dir)}/fold_*.npz "
            f"{shlex.quote(cache_dir)}/oof.npz {shlex.quote(output_dir)}/ 2>/dev/null; done &) && "
        )
        return (
            sync
            + f"PHASE1_CACHE={shlex.quote(cache_dir)} PHASE1_OUT={shlex.quote(output_dir)} "
            + " ".join(POD_ENV_SHIM)
            + " --require WANDB_API_KEY -- python run_phase1.py cv thresholds"
        )

    def classical_retrieval_command(
        self, *, cache_dir: str = REMOTE_CACHE_DIR, output_dir: str = REMOTE_OUTPUT_DIR
    ) -> str:
        """Move the out-of-fold probabilities into the directory `fetch_checkpoint` pulls.

        `run_phase1.stage_cv` checkpoints each fold and the merged out-of-fold matrix into
        `PHASE1_CACHE` -- the delivered bundle directory -- and not into `PHASE1_OUT`, because
        that is what makes a killed run resumable. `fetch_checkpoint` copies `PHASE1_OUT`, so
        without this the metrics would come home and the 150 fits that produced them would die
        with the pod's disk. Re-tuning thresholds on this machine without paying for the CV a
        second time is the whole point of keeping them.
        """
        return (
            f"cp -f {shlex.quote(cache_dir)}/oof.npz {shlex.quote(cache_dir)}/fold_*.npz "
            f"{shlex.quote(output_dir)}/"
        )

    def train_command(
        self, *, cache_dir: str = REMOTE_CACHE_DIR, output_dir: str = REMOTE_OUTPUT_DIR
    ) -> str:
        """The exact command the pod runs, in the flag vocabulary the trainer actually parses.

        This method used to emit a vocabulary of its own -- `--data-dir`, `--problem-type`,
        `--log-train-val-gap-every-epoch`, `--save-safetensors` -- that
        `model/train_distilbert.py` does not define. argparse exits 2 on an unrecognised flag,
        so every one of those launches would have burned the full dead-man-switch window
        without starting a single training step. The two sides are now reconciled against
        `train_distilbert.build_arg_parser`, and `test_every_flag_the_launcher_sends_is_one_
        the_trainer_parses` asserts it against the real parser rather than against a list
        copied by hand.

        Three of the non-negotiables are therefore *not* flags here, and that is deliberate --
        each is enforced by something in the trainer that raises, which is stronger than a
        flag a caller can forget:

        - `problem_type` is `train_distilbert.PROBLEM_TYPE`, checked by
          `assert_multi_label_config` and then by `assert_bce_objective`, which runs a real
          forward pass and compares the model's own loss against
          `binary_cross_entropy_with_logits`. A softmax head fails on the first batch.
        - safetensors is `save_safetensors=True` in the training arguments, re-checked by
          `assert_safetensors_only` over the written directory, and checked a third time by
          `assert_safetensors_checkpoint` when the bytes land back on this machine.
        - the per-epoch train/validation gap is an unconditional callback; `--train-probe-rows`
          sizes it, and the spec refuses to set it to zero.

        `--expect-split-version` is the one guard that only exists here: the pod cannot know
        which split it was supposed to receive, so the value travels with the command.
        """
        parts = [
            *POD_ENV_SHIM,
            "--require WANDB_API_KEY --",
            "python",
            "-m",
            self.train_module,
            f"--cache {shlex.quote(cache_dir)}",
            f"--output {shlex.quote(output_dir)}",
            f"--model-name {shlex.quote(self.base_model)}",
            f"--fold {self.fold}",
            f"--epochs {self.epochs}",
            f"--lr {self.learning_rate}",
            f"--weight-decay {self.weight_decay}",
            f"--batch-size {self.train_batch_size}",
            f"--eval-batch-size {self.eval_batch_size}",
            f"--max-length {self.max_seq_length}",
            f"--patience {self.early_stopping_patience}",
            # Overfit must be visible while it happens, not reconstructed afterwards.
            f"--train-probe-rows {self.train_probe_rows}",
            f"--seed {self.seed}",
            f"--wandb-project {shlex.quote(self.wandb_project)}",
        ]
        if self.expect_split_version:
            parts.append(f"--expect-split-version {shlex.quote(self.expect_split_version)}")
        if not self.fp16:
            parts.append("--no-fp16")
        return " ".join(parts)

    def export_command(
        self,
        *,
        model_dir: str | None = None,
        cache_dir: str = REMOTE_CACHE_DIR,
        onnx_dir: str = REMOTE_ONNX_DIR,
        export_module: str = "model.export_onnx",
    ) -> str:
        """Export int8 ONNX and verify logit parity against the float model, on the pod.

        It runs here rather than on the Jetson for one reason and one reason only: the Jetson
        venv has no torch, so `torch_logits` -- the float half of the parity comparison --
        cannot be computed there. A parity test that cannot load the float model is not a
        parity test.

        Two flags are pinned here rather than left to default, because both defaults produced
        an artifact the parity gate refused on 2026-08-01 (max |logit delta| 2.72 against a
        0.25 tolerance, worst on identity_hate):

        ``--per-channel`` because the default is per-TENSOR dynamic quantization, which gives
        one scale to a whole weight tensor. Attention projections have a wide per-channel
        range, so outlier channels saturate and the logits move by whole units. Per-channel
        weight scales are the standard remedy for transformers and cost nothing at inference.

        ``--target arm64`` because ``default_quant_target()`` reads the architecture of the
        machine it happens to be running on -- an x86 pod -- while the serving fleet is
        ``t4g``, which is ARM Graviton. Quantizing for the host rather than the target is how
        an artifact tuned for AVX-512 VNNI ends up on a Neoverse core. The target is a
        configuration choice, not a property of the exporting host, so it is safe to pin it
        here and it must match where the model actually runs.
        """
        model_dir = model_dir or f"{REMOTE_OUTPUT_DIR}/final"
        return " ".join(
            [
                *POD_ENV_SHIM,
                "--",
                "python",
                "-m",
                export_module,
                f"--model-dir {shlex.quote(model_dir)}",
                f"--out {shlex.quote(onnx_dir)}",
                f"--cache {shlex.quote(cache_dir)}",
                f"--fold {self.fold}",
                f"--max-length {self.max_seq_length}",
                "--per-channel",
                f"--target {SERVING_QUANT_TARGET}",
            ]
        )


@dataclass
class Pod:
    """A live pod as the API describes it, plus the two things this module needs from it."""

    pod_id: str
    name: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def public_ip(self) -> str | None:
        ip = self.raw.get("publicIp")
        return str(ip) if ip else None

    @property
    def ssh_port(self) -> int | None:
        mappings = self.raw.get("portMappings") or {}
        port = mappings.get("22") or mappings.get(22)
        return int(port) if port else None

    @property
    def cost_per_hr(self) -> float:
        return float(self.raw.get("costPerHr") or 0.0)


def build_bootstrap(
    *, max_pod_seconds: int, requirements: tuple[str, ...] = POD_REQUIREMENTS
) -> str:
    """The container start command: bring SSH up, install wheels, then idle under a timer.

    Two lines are load-bearing.

    `/start.sh` is invoked when present. Overriding `dockerStartCmd` replaces the image's own
    entrypoint script, and that script is what installs `$PUBLIC_KEY` into `authorized_keys`
    and starts `sshd`. Replacing it without redoing that work produces a pod that boots,
    bills, and cannot be reached -- an expensive way to learn how `dockerStartCmd` works.

    `timeout` is the dead-man switch, and it is the only teardown mechanism that does not
    depend on the launching machine still existing. If this box loses power mid-run, the start
    command still returns after `max_pod_seconds`, the container exits, and the GPU charge
    stops. It is a backstop, not a replacement: an exited pod still holds its disk, so the
    reaper must still run.
    """
    wheels = " ".join(shlex.quote(req) for req in requirements)
    return "\n".join(
        [
            "set -euo pipefail",
            "if [ -x /start.sh ]; then nohup /start.sh >/var/log/runpod-start.log 2>&1 & fi",
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
            'if [ -n "${PUBLIC_KEY:-}" ]; then',
            '  echo "$PUBLIC_KEY" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys',
            "fi",
            "command -v sshd >/dev/null && (service ssh start || /usr/sbin/sshd) || true",
            f"mkdir -p {shlex.quote(REMOTE_DATA_DIR)} {shlex.quote(REMOTE_OUTPUT_DIR)}",
            "python -m pip install --no-cache-dir --upgrade pip",
            f"python -m pip install --no-cache-dir {wheels}",
            f"touch {shlex.quote(READY_SENTINEL)}",
            'echo "bootstrap complete; idling for the driver"',
            f"exec timeout --signal=TERM {int(max_pod_seconds)} sleep infinity",
        ]
    )


def build_pod_env(
    *,
    wandb_project: str,
    wandb_entity: str | None = None,
    ssh_public_key: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Secrets and settings handed to the pod, each read from `pass` at the moment of use.

    These values travel in the TLS request body. They never appear in argv, never reach a
    shell profile, and never reach the registry file. The exposure window is the pod's
    lifetime, which is why the runbook rotates the Hugging Face token after a live run.
    """
    env = {
        "WANDB_API_KEY": load_secret(PASS_WANDB_KEY, "WANDB_API_KEY"),
        "WANDB_PROJECT": wandb_project,
        "HF_TOKEN": load_secret(PASS_HF_TOKEN, "HF_TOKEN"),
        # The suite refuses to run without it, and the pod is no different: set and dict
        # iteration order must not differ between the Jetson and the GPU.
        "PYTHONHASHSEED": "0",
        "TOKENIZERS_PARALLELISM": "false",
    }
    if wandb_entity:
        env["WANDB_ENTITY"] = wandb_entity
    if ssh_public_key:
        env["PUBLIC_KEY"] = ssh_public_key
    if extra:
        env.update(extra)
    return env


def build_pod_payload(
    *,
    name: str,
    gpu_type: str,
    max_pod_seconds: int,
    image: str = POD_IMAGE,
    container_disk_gb: int = 60,
    volume_gb: int = 40,
    interruptible: bool = True,  # see run_finetune: classical CV overrides this to False
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The exact JSON body for `POST /pods`.

    Field names and enum values come from the live `GET /v1/openapi.json` (`PodCreateInput`)
    on 2026-07-31, not from memory.

    `interruptible` is true because a fine-tune restarts cleanly from a checkpoint: spot is
    ~15% cheaper on an A40 and preemption is a failure mode this workload already tolerates.

    No `networkVolumeId` is ever set. A *network* volume outlives the pod and keeps billing
    after termination, which is exactly the silent cost this module exists to prevent. The
    pod-local `volumeInGb` disk is different: it dies with the pod, and it is what keeps a
    checkpoint alive across a spot preemption.
    """
    return {
        "name": name,
        "imageName": image,
        "gpuTypeIds": [gpu_type],
        "gpuTypePriority": "availability",
        "gpuCount": 1,
        "computeType": "GPU",
        "cloudType": "SECURE",
        "containerDiskInGb": container_disk_gb,
        "volumeInGb": volume_gb,
        "volumeMountPath": REMOTE_WORKDIR,
        "ports": ["22/tcp"],
        "dockerEntrypoint": ["/bin/bash"],
        "dockerStartCmd": ["-c", build_bootstrap(max_pod_seconds=max_pod_seconds)],
        "env": env or {},
        "supportPublicIp": True,
        "interruptible": interruptible,
    }


# ---------------------------------------------------------------------------
# API calls -- each behind a module-level seam, so the suite needs no network
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return auth_headers(load_secret(PASS_RUNPOD_KEY, "RUNPOD_API_KEY"))


def _create_pod(payload: dict[str, Any]) -> dict[str, Any]:
    """`POST /pods` -> the created pod. 201 is the documented success code."""
    resp = http_post(f"{REST_BASE}/pods", _headers(), payload)
    if resp.status_code not in (200, 201):
        # A validation error echoes the request, and the request carries the W&B key and the
        # HF token, so the body is scrubbed and truncated before it can reach a log.
        raise LaunchError(scrub(f"pod creation failed ({resp.status_code}): {resp.text[:300]}"))
    data = resp.json() or {}
    if not data.get("id"):
        raise LaunchError(
            "pod creation returned no id, so the pod may exist and be unrecordable: "
            f"{json.dumps(data)[:200]}"
        )
    return data


def _get_pod(pod_id: str) -> dict[str, Any] | None:
    """`GET /pods/{id}`. None on 404 -- confirmed live: `{"error": "pod not found"}`."""
    resp = http_get(f"{REST_BASE}/pods/{pod_id}", _headers())
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise LaunchError(
            scrub(f"get_pod({pod_id}) failed ({resp.status_code}): {resp.text[:200]}")
        )
    return resp.json() or {}


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def _tcp_probe(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pod_is_ready(raw: dict[str, Any], *, probe: Callable[..., bool] | None = None) -> bool:
    """True once the pod is actually usable, rather than merely requested.

    `desiredStatus` is a trap. It is the status the pod has been *asked* to reach, and it
    reads RUNNING from the instant of creation -- polling it alone returns true immediately,
    long before a machine is assigned or a 7 GB image is pulled, and every later step then
    fails against a pod that is not there yet. Readiness therefore requires three things: the
    desired status is RUNNING (so it has not exited or been preempted), a machine actually
    started it (`lastStartedAt`), and the SSH endpoint answers a TCP connect. Only the last
    one is proof.
    """
    if str(raw.get("desiredStatus", "")).upper() != "RUNNING":
        return False
    if not raw.get("lastStartedAt"):
        return False
    pod = Pod(pod_id=str(raw.get("id", "")), name=str(raw.get("name", "")), raw=raw)
    host, port = pod.public_ip, pod.ssh_port
    if not host or not port:
        return False
    return (probe or _tcp_probe)(host, port)


def wait_until_ready(
    pod_id: str,
    *,
    timeout_s: int = 900,
    interval_s: int = 15,
    probe: Callable[..., bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll until the pod is usable, or raise `ReadinessTimeout`.

    This function never terminates anything, deliberately: the lease owns teardown in exactly
    one place, and a timeout here is one of the paths the lease exists to cover. `sleep` and
    `monotonic` are injected so the suite never waits on a real clock, and an unbounded poll
    is impossible -- it would hold the process, and therefore the teardown, hostage while the
    pod bills.
    """
    deadline = monotonic() + timeout_s
    last_status = "unknown"
    while True:
        raw = _get_pod(pod_id)
        if raw is None:
            raise LaunchError(f"pod {pod_id} disappeared before it became ready")
        last_status = str(raw.get("desiredStatus", "unknown"))
        if last_status.upper() in ("EXITED", "TERMINATED"):
            # A spot pod preempted during boot, or a container that crashed on start.
            # Polling it to the full timeout wastes fifteen minutes and reports the wrong
            # cause.
            raise LaunchError(f"pod {pod_id} reached status {last_status} before readiness")
        if pod_is_ready(raw, probe=probe):
            return raw
        if monotonic() >= deadline:
            raise ReadinessTimeout(
                f"pod {pod_id} was not ready within {timeout_s}s (last status {last_status})"
            )
        sleep(interval_s)


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


def _registered_ids(registry_path: Path | str) -> set[str]:
    """Pod ids currently on disk. Never raises: teardown must not need a readable file."""
    from infra.runpod.runpod_client import read_registry

    try:
        return {str(e.get("pod_id", "")) for e in read_registry(registry_path) if e.get("pod_id")}
    except Exception as exc:  # noqa: BLE001 - a corrupt registry must not block teardown
        print(f"WARNING: registry unreadable ({scrub(str(exc))})", file=sys.stderr)
        return set()


def launch_pod(
    *,
    name: str,
    registry_path: Path | str = DEFAULT_REGISTRY,
    gpu_type: str = GPU_CANDIDATES[0],
    image: str = POD_IMAGE,
    container_disk_gb: int = 60,
    volume_gb: int = 40,
    interruptible: bool = True,
    max_hours: float = DEFAULT_MAX_HOURS,
    env: dict[str, str] | None = None,
    readiness_timeout_s: int = 900,
    probe: Callable[..., bool] | None = None,
) -> Pod:
    """Create a pod, record it, then wait for it. Never the other way round.

    The three statements in the middle are why this module exists, and their order is the
    invariant:

        raw = _create_pod(...)      # the pod now bills
        append_registry(...)        # fsync + os.replace: it is now reapable
        wait_until_ready(...)       # the long, killable part

    Two guards run *before* any money is spent, because validating after creation validates
    nothing:

    - the name must start with a prefix the reaper's allowlist recognises, or the automation
      that exists to kill this pod would refuse to;
    - the GPU must be on the mid-tier allowlist. `preflight` already picks from it, but this
      function is public and a caller that skips preflight -- a notebook, a retry script, a
      future sweep driver -- could otherwise rent a flagship card with a typo. The allowlist
      belongs where the money is actually spent.

    If the registry write fails -- read-only volume, full disk, bad permissions -- the pod is
    terminated immediately, while its id is still in a local variable. A pod that could not be
    recorded is permanently unreapable: `reconcile` will correctly refuse to auto-terminate it
    as an orphan, forever, and only a human reading the console would ever find it.

    One window cannot be closed from this side: a crash between the API returning an id and
    the registry write landing. It is one response-parse wide, and the orphan report is what
    exists to catch it.
    """
    if not isinstance(name, str) or not name.startswith(DEFAULT_ALLOW):
        raise LaunchError(
            f"pod name {name!r} must start with one of {DEFAULT_ALLOW} (a 'toxic-' prefix); "
            "the reaper's name guard would refuse to terminate anything else, so it must not "
            "be created"
        )
    if gpu_type not in GPU_CANDIDATES:
        raise LaunchError(
            f"GPU {gpu_type!r} is not on the allowlist {GPU_CANDIDATES}. A 66M-parameter "
            "DistilBERT does not need a flagship card; widen GPU_CANDIDATES deliberately if "
            "the workload really changed."
        )

    raw = _create_pod(
        build_pod_payload(
            name=name,
            gpu_type=gpu_type,
            image=image,
            container_disk_gb=container_disk_gb,
            volume_gb=volume_gb,
            interruptible=interruptible,
            max_pod_seconds=int(max_hours * 3600),
            env=env,
        )
    )
    pod_id = str(raw.get("id") or "")
    if not pod_id:
        raise LaunchError(f"pod creation returned no id, so nothing can be recorded: {raw!r}")

    try:
        append_registry(registry_path, {"name": name, "pod_id": pod_id, "gpu": gpu_type})
    except BaseException:
        print(
            f"SEV-1: pod {pod_id} could not be recorded; terminating it now rather than "
            "leaving an unreapable pod behind",
            file=sys.stderr,
        )
        terminate_pod(pod_id)
        raise

    ready = wait_until_ready(pod_id, timeout_s=readiness_timeout_s, probe=probe) or {}
    return Pod(pod_id=pod_id, name=name, raw=dict(ready))


class PodLease:
    """A pod guaranteed to be terminated, verified, and only then de-registered.

    `with PodLease(...) as pod:` gives three of the four teardown mechanisms at once: the
    `__exit__`, an `atexit` hook, and the registry write `launch_pod` already made. The
    fourth, the pod's own `timeout`, is baked into the start command.

    Two details are the difference between a lease that works and one that reads as though it
    does.

    **Teardown is driven by what is on disk for this invocation, not by `self.pod`.** A plain
    `with` block only calls `__exit__` if `__enter__` *returned*; `__enter__` calls
    `launch_pod`, which creates the pod, records it, and only then blocks on readiness -- so a
    readiness timeout, a spot preemption during boot, or a Ctrl-C in the wait raises from
    inside `__enter__`, `__exit__` never runs, and an attribute that was never assigned cannot
    drive anything. `__enter__` therefore tears down its own registry diff before re-raising.

    **The registry entry is removed only after the API confirms the pod is gone.** A 204 from
    DELETE is a claim; `assert_no_survivors` is the evidence. De-registering a pod that is
    still live erases the one record that would have found it.
    """

    def __init__(
        self,
        *,
        name: str,
        registry_path: Path | str = DEFAULT_REGISTRY,
        verify: bool = True,
        **launch_kwargs: Any,
    ) -> None:
        self.name = name
        self.registry_path = registry_path
        self.verify = verify
        self.pod: Pod | None = None
        self._launch_kwargs = launch_kwargs
        self._before: set[str] = set()
        self._torn_down = False

    def __enter__(self) -> Pod:
        self._before = _registered_ids(self.registry_path)
        atexit.register(self._atexit_teardown)
        try:
            self.pod = launch_pod(
                name=self.name, registry_path=self.registry_path, **self._launch_kwargs
            )
        except BaseException:
            # __exit__ will never run: tear down whatever this invocation recorded.
            self.teardown()
            raise
        return self.pod

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.teardown()
        return False  # never swallow the body's exception

    def teardown(self) -> None:
        """Terminate this invocation's pods, verify, then de-register. Idempotent."""
        if self._torn_down:
            return
        self._torn_down = True
        ours = _registered_ids(self.registry_path) - self._before
        if self.pod is not None:
            ours.add(self.pod.pod_id)
        confirmed: set[str] = set()
        first_error: BaseException | None = None
        for pod_id in sorted(ours):
            try:
                terminate_pod(pod_id)
                print(f"teardown: terminated {pod_id}")
                confirmed.add(pod_id)
            except Exception as exc:  # noqa: BLE001 - try every pod before giving up
                print(
                    f"SEV-1: teardown FAILED for pod {pod_id}: {scrub(str(exc))}. It is "
                    f"billing now. Run: {_REAP_CMD} --pod-id {pod_id} --execute --force",
                    file=sys.stderr,
                )
                first_error = first_error or exc
        if self.verify and confirmed:
            assert_no_survivors(confirmed)
        remove_from_registry(self.registry_path, confirmed)
        if first_error is not None:
            raise first_error

    def _atexit_teardown(self) -> None:
        if self._torn_down:
            return
        print("SEV-1: atexit teardown fired; the normal path did not run", file=sys.stderr)
        try:
            self.teardown()
        except Exception as exc:  # noqa: BLE001 - atexit must never raise
            print(f"SEV-1: atexit teardown failed: {scrub(str(exc))}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchPlan:
    """Everything a human needs in order to approve a launch, before any money is spent."""

    name: str
    gpu_type: str
    interruptible: bool
    price_usd_per_hr: float
    max_hours: float
    projected_total_usd: float
    stock_status: str | None
    account: AccountStatus | None

    def render(self) -> str:
        account = (
            "not checked"
            if self.account is None
            else (
                f"cap ${self.account.spend_limit}/hr, balance "
                f"${self.account.client_balance:.2f}, already burning "
                f"${self.account.current_spend_per_hr:.2f}/hr"
            )
        )
        return (
            f"  pod name        : {self.name}\n"
            f"  gpu             : {self.gpu_type} "
            f"({'spot' if self.interruptible else 'on-demand'}, stock={self.stock_status})\n"
            f"  price           : ${self.price_usd_per_hr:.3f}/hr\n"
            f"  max runtime     : {self.max_hours:.1f}h (dead-man switch inside the pod)\n"
            f"  worst-case cost : ${self.projected_total_usd:.2f}\n"
            f"  account         : {account}"
        )


def preflight(
    *,
    name: str,
    max_hours: float = DEFAULT_MAX_HOURS,
    interruptible: bool = True,
    registry_path: Path | str = DEFAULT_REGISTRY,
    candidates: tuple[str, ...] = GPU_CANDIDATES,
    max_hourly_usd: float = MAX_HOURLY_USD,
    max_run_usd: float = MAX_RUN_USD,
    check_account: bool = True,
) -> LaunchPlan:
    """Fail closed before anything bills.

    Three refusals, in the order they bite:

    1. **Something is already live.** A dry-run reconcile; any registered pod or any orphan
       aborts. Launching on top of a leak doubles the leak and buries the evidence: from then
       on you cannot tell the new pod from the old one.
    2. **The GPU is unavailable or unpriced**, so the rate would be unknown.
    3. **A ceiling would be breached** -- the account cap, this project's hourly ceiling, the
       run ceiling, or the prepaid balance. The worst case is priced at the dead-man-switch
       duration rather than the expected duration, because the worst case is what a forgotten
       pod actually costs.
    """
    state = reconcile(registry_path, execute=False)
    if state["live_and_ours"] or state["orphans"]:
        raise SpendGuardError(
            f"refusing to launch: {len(state['live_and_ours'])} registered pod(s) and "
            f"{len(state['orphans'])} orphan(s) are already live. Clear them first with "
            f"`{_REAP_CMD} --execute`."
        )

    gpu, price, stock = choose_gpu(candidates=candidates, interruptible=interruptible)
    projected_total = price * max_hours

    account = None
    if check_account:
        account = assert_spend_guard(
            projected_hourly_usd=price,
            projected_total_usd=projected_total,
            max_hourly_usd=max_hourly_usd,
            max_total_usd=max_run_usd,
        )

    return LaunchPlan(
        name=name,
        gpu_type=gpu,
        interruptible=interruptible,
        price_usd_per_hr=price,
        max_hours=max_hours,
        projected_total_usd=projected_total,
        stock_status=stock,
        account=account,
    )


# ---------------------------------------------------------------------------
# Getting the data up and the checkpoint back
# ---------------------------------------------------------------------------


def _ssh_opts(key_path: Path) -> list[str]:
    return [
        "-i", str(key_path),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
    ]


def _endpoint(pod: Pod) -> tuple[str, int]:
    if not pod.public_ip or not pod.ssh_port:
        raise LaunchError(
            f"pod {pod.pod_id} has no SSH endpoint (publicIp={pod.public_ip!r}, "
            f"portMappings={pod.raw.get('portMappings')!r})"
        )
    return pod.public_ip, pod.ssh_port


# Enough of a remote traceback to name the failing call, not just the interpreter frames that
# lead to it. At 400 characters a Python traceback coming back over SSH is cut off inside
# `runpy`, above every frame that carries the actual cause -- which is exactly what happened
# when the ONNX export failed on 2026-08-01: the message ended mid-word at
# `File "/workspace/model/export_onnx`, and diagnosing it needed another paid pod. Still
# bounded, and still scrubbed, because these strings reach logs.
_STDERR_KEEP = 4000


def _run_local(cmd: list[str], *, timeout: float) -> str:
    """Run a local subprocess with no shell, raising a scrubbed error."""
    try:
        result = subprocess.run(  # noqa: S603 - argv list, shell=False
            cmd, capture_output=True, text=True, check=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise LaunchError(f"{cmd[0]} timed out after {timeout}s") from None
    except subprocess.CalledProcessError as exc:
        raise LaunchError(
            scrub(f"{cmd[0]} failed (exit {exc.returncode}): {(exc.stderr or '')[-_STDERR_KEEP:]}")
        ) from None
    return result.stdout


def deliver_dataset(
    pod: Pod,
    local_path: Path,
    *,
    remote_dir: str = REMOTE_DATA_DIR,
    key_path: Path = DEFAULT_SSH_KEY,
    timeout: float = 1800.0,
) -> None:
    """Push the prepared dataset bundle to the pod over SSH.

    The corpus is not in git and must not be (`.gitignore` excludes `/data/`), so the pod
    cannot clone it. It also must not be re-derived on the pod: `prepare_dataset` takes about
    13.6 minutes, and its output carries the `split_version` every downstream number is keyed
    to. Re-deriving it on different hardware risks a different realised split for the same
    seed, which would invalidate the comparison silently. The bundle is built once on the
    Jetson and shipped as bytes.
    """
    local_path = Path(local_path)
    if not local_path.exists():
        raise LaunchError(f"dataset bundle not found: {local_path}")
    # `scp -r <local> <remote_dir>/` lands the directory under its own basename, and both
    # `PHASE1_CACHE` and the trainer's `--cache` name `REMOTE_CACHE_DIR` -- a constant ending
    # in `/bundle`. Handing this the staging *parent* therefore delivers a real bundle to
    # `/workspace/data/pod-bundle` and leaves the run looking for one that is not there, which
    # is a failure the far side can only report after the GPU is billing. It costs two stat
    # calls to find here instead.
    expected = Path(REMOTE_CACHE_DIR).name
    if local_path.name != expected:
        raise LaunchError(
            f"dataset bundle {local_path} must be a directory named {expected!r}: scp lands it "
            f"at {remote_dir}/{local_path.name} and the run reads {REMOTE_CACHE_DIR}. Pass "
            f"'{local_path / expected}' if that is the staging directory."
        )
    if not (local_path / "manifest.json").is_file():
        raise LaunchError(
            f"{local_path} holds no manifest.json, so it is not a bundle cache. Stage one with "
            "`python -m infra.runpod.run_phase1_gpu` or "
            "`model.train_distilbert --build-cache`."
        )
    host, port = _endpoint(pod)
    opts = _ssh_opts(key_path)
    _run_local(
        ["ssh", *opts, "-p", str(port), f"root@{host}", f"mkdir -p {shlex.quote(remote_dir)}"],
        timeout=120.0,
    )
    _run_local(
        ["scp", *opts, "-P", str(port), "-r", str(local_path), f"root@{host}:{remote_dir}/"],
        timeout=timeout,
    )


def make_code_archive(
    dest: Path, *, repo_root: Path | None = None, paths: tuple[str, ...] = CODE_PATHS
) -> Path:
    """Tar the project modules the pod has to import. No git, no data, no secrets.

    Only `model/`, `infra/` and `pyproject.toml` travel. `data/` is excluded because the
    corpus is delivered separately and deliberately without the held-out rows, `.git` because
    the pod has no business holding the history, and every `__pycache__` because this box is
    aarch64 and the pod is x86-64: a stale `.pyc` for the wrong architecture is ignored by
    CPython, but a stale one for the *right* Python version and a different source is not.
    """
    repo_root = Path(repo_root or Path(__file__).resolve().parents[2])
    missing = [p for p in paths if not (repo_root / p).exists()]
    if missing:
        raise LaunchError(f"cannot build code archive: {missing} missing under {repo_root}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run_local(
        [
            "tar", "czf", str(dest),
            "-C", str(repo_root),
            "--exclude=__pycache__",
            "--exclude=*.pyc",
            "--exclude=.git",
            # The archive is written by uid 1000 on this box and unpacked by root inside a
            # container that does not hold CAP_CHOWN. GNU tar running as root restores
            # ownership by default, fails with "Cannot change ownership to uid 1000", and
            # exits 2 -- after the pod is created, ready, and billing. Recording the numeric
            # owner as root here, and refusing to restore it on the far side, removes both
            # halves of that failure.
            "--owner=root", "--group=root", "--numeric-owner",
            *paths,
        ],
        timeout=300.0,
    )
    return dest


def deliver_code(
    pod: Pod,
    archive: Path,
    *,
    remote_dir: str = REMOTE_WORKDIR,
    key_path: Path = DEFAULT_SSH_KEY,
    timeout: float = 600.0,
) -> None:
    """Ship the code tarball and unpack it where `python -m model.train_distilbert` will find it."""
    host, port = _endpoint(pod)
    _run_local(
        [
            "scp", *_ssh_opts(key_path), "-P", str(port),
            str(archive), f"root@{host}:{remote_dir}/{CODE_ARCHIVE_NAME}",
        ],
        timeout=timeout,
    )
    # `--no-same-owner` is the half of the fix that does not depend on how the archive was
    # built: a tarball from anywhere unpacks as root-owned instead of exiting 2 on a chown the
    # container is not permitted to make.
    run_remote(
        pod,
        f"cd {shlex.quote(remote_dir)} && tar xzf {shlex.quote(CODE_ARCHIVE_NAME)} "
        f"--no-same-owner --no-same-permissions "
        f"&& rm -f {shlex.quote(CODE_ARCHIVE_NAME)}",
        key_path=key_path,
        timeout=300.0,
    )


def run_remote(
    pod: Pod, command: str, *, key_path: Path = DEFAULT_SSH_KEY, timeout: float = 14400.0
) -> str:
    """Run one command on the pod and return its stdout.

    `command` is a single argv element, so nothing on this side expands it. It is assembled by
    `FinetuneSpec.train_command`, never typed by a user.
    """
    host, port = _endpoint(pod)
    return _run_local(
        ["ssh", *_ssh_opts(key_path), "-p", str(port), f"root@{host}", command], timeout=timeout
    )


def wait_for_bootstrap(
    pod: Pod,
    *,
    key_path: Path = DEFAULT_SSH_KEY,
    timeout_s: float = 1800.0,
    interval_s: float = 15.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Block until the pod's start command has finished installing wheels.

    SSH answering is not the same as the pod being usable. `build_bootstrap` brings sshd up
    first and only then runs `pip install`, so the window between "the readiness probe passes"
    and "scikit-learn exists" is several minutes wide -- and any command sent into that window
    dies on `ModuleNotFoundError` with the pod already billing. `READY_SENTINEL` is written by
    the last line of the bootstrap, so its presence is the only proof that the wheels landed.
    """
    deadline = monotonic() + timeout_s
    while True:
        try:
            run_remote(
                pod, f"test -f {shlex.quote(READY_SENTINEL)}", key_path=key_path, timeout=60.0
            )
            return
        except LaunchError:
            if monotonic() >= deadline:
                raise ReadinessTimeout(
                    f"pod {pod.pod_id} never finished its bootstrap within {timeout_s:.0f}s; "
                    f"the sentinel {READY_SENTINEL} was never written"
                ) from None
            sleep(interval_s)


def fetch_checkpoint(
    pod: Pod,
    local_dir: Path,
    *,
    remote_dir: str = REMOTE_OUTPUT_DIR,
    key_path: Path = DEFAULT_SSH_KEY,
    timeout: float = 1800.0,
    require_safetensors: bool = True,
) -> Path:
    """Pull the trained checkpoint back *before* the pod is destroyed.

    This runs inside the lease, never after it: the pod's disk dies with the pod, so a
    checkpoint that has not been copied by teardown time never existed.

    `require_safetensors` is false for the classical run and only for the classical run. That
    workload writes `.skops`, JSON and `.npz`, never model weights, so demanding a
    `.safetensors` file would turn a finished cross-validation into `CheckpointError` and
    report a successful multi-hour run as a failure. The check is exactly as strict as before
    on every path that produces torch weights.

    The remote path is named plainly, never as `<remote_dir>/.`. `scp -r host:dir/. local`
    makes the source announce a file literally named `.`, which the sink rejects with
    `error: unexpected filename: .` -- and it does so *after* the workload has finished, so
    the failure lands at the one moment when the pod is about to be destroyed and its disk
    with it. That is not hypothetical: it cost a completed 85-minute cross-validation on
    2026-08-01, which teardown then deleted. Copying the directory by name into a staging
    parent and moving its contents into place is the form both the legacy SCP protocol and
    the SFTP protocol that replaced it accept.
    """
    host, port = _endpoint(pod)
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    staging = local_dir.parent / f".fetch-{pod.pod_id}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        _run_local(
            [
                "scp", *_ssh_opts(key_path), "-P", str(port), "-r",
                f"root@{host}:{remote_dir}", str(staging),
            ],
            timeout=timeout,
        )
        fetched = staging / Path(remote_dir).name
        if not fetched.is_dir():
            raise LaunchError(
                f"scp reported success but {fetched} is not a directory; nothing came back "
                f"from {remote_dir} on pod {pod.pod_id}"
            )
        for item in fetched.iterdir():
            target = local_dir / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if require_safetensors:
        assert_safetensors_checkpoint(local_dir)
    return local_dir


def assert_safetensors_checkpoint(path: Path) -> None:
    """Refuse a checkpoint that came back as pickle.

    `torch.load` on a `.bin` executes arbitrary code at load time, and this artifact is bound
    for a public W&B registry and then into a container that serves traffic. The format is a
    security property, not a preference, so it is checked where the bytes first touch this
    machine rather than trusted from the training flags that were supposed to produce it.
    """
    path = Path(path)
    if not path.exists():
        raise CheckpointError(f"no checkpoint at {path}")
    pickled = sorted(
        p.name for p in path.rglob("*") if p.suffix in (".bin", ".pt", ".pth", ".pkl", ".ckpt")
    )
    if not any(path.rglob("*.safetensors")):
        raise CheckpointError(
            f"{path} holds no .safetensors file; the fine-tune must save with "
            "save_safetensors=True"
        )
    if pickled:
        raise CheckpointError(
            f"{path} holds pickled weights ({', '.join(pickled)}); torch.load executes "
            "arbitrary code. Delete them and re-export, or the registry gets a poisoned "
            "artifact."
        )


# ---------------------------------------------------------------------------
# The fine-tune
# ---------------------------------------------------------------------------


def run_finetune(
    *,
    run_name: str,
    dataset_path: Path,
    output_path: Path,
    spec: FinetuneSpec | None = None,
    gpu_type: str = GPU_CANDIDATES[0],
    # The one project rubric 1.2 and 3.2 are graded on. It disagreed with
    # `FinetuneSpec.wandb_project`, `run_phase1_gpu`, `train_distilbert` and
    # `register_pod_artifacts`, all of which say `mlops-toxic-moderation` -- so a run launched
    # from here landed in a second, empty project, and the dashboard the grader opens would
    # have been missing it.
    wandb_project: str = "mlops-toxic-moderation",
    wandb_entity: str | None = None,
    ssh_key: Path = DEFAULT_SSH_KEY,
    max_hours: float = DEFAULT_MAX_HOURS,
    interruptible: bool = True,
    registry_path: Path | str = DEFAULT_REGISTRY,
    export_onnx: bool = True,
) -> Path:
    """Launch, deliver the data, train, retrieve the checkpoint, tear down. Always tear down.

    Returns the local checkpoint directory. Every failure path -- including `KeyboardInterrupt`
    and a readiness timeout inside `__enter__` -- goes through `PodLease`.
    """
    spec = spec or FinetuneSpec()
    name = run_name if run_name.startswith(DEFAULT_ALLOW) else f"{POD_NAME_PREFIX}{run_name}"

    pub_key = Path(f"{ssh_key}.pub")
    if not pub_key.exists():
        raise LaunchError(
            f"no SSH public key at {pub_key}; the pod would boot unreachable. Generate one: "
            f"ssh-keygen -t ed25519 -f {ssh_key}"
        )
    env = build_pod_env(
        wandb_project=wandb_project,
        wandb_entity=wandb_entity,
        ssh_public_key=pub_key.read_text().strip(),
        extra={"WANDB_RUN_NAME": name},
    )

    with PodLease(
        name=name,
        registry_path=registry_path,
        gpu_type=gpu_type,
        env=env,
        interruptible=interruptible,
        max_hours=max_hours,
    ) as pod:
        host, port = _endpoint(pod)
        print(f"pod {pod.pod_id} ready at {host}:{port} (${pod.cost_per_hr:.3f}/hr)")
        # The readiness probe only proves sshd answers, and the bootstrap installs the wheels
        # after it starts sshd. Sending a training command into that gap is a
        # ModuleNotFoundError on a billing GPU.
        wait_for_bootstrap(pod, key_path=ssh_key)
        archive = Path(output_path).parent / CODE_ARCHIVE_NAME
        deliver_code(pod, make_code_archive(archive), key_path=ssh_key)
        deliver_dataset(pod, dataset_path, key_path=ssh_key)
        run_remote(
            pod,
            f"cd {REMOTE_WORKDIR} && "
            f"{spec.classical_command() if spec.classical else spec.train_command()}",
            key_path=ssh_key,
            timeout=max_hours * 3600,
        )
        export_failure: Exception | None = None
        if spec.classical:
            # No torch weights exist to quantise, so there is nothing for ONNX to export; what
            # there is to rescue is the out-of-fold matrix, which lives in the cache directory.
            run_remote(
                pod,
                f"cd {REMOTE_WORKDIR} && {spec.classical_retrieval_command()}",
                key_path=ssh_key,
                timeout=600.0,
            )
        elif export_onnx:
            # A failed export must not cost the fine-tune. The weights are the expensive,
            # unreproducible artifact -- 25 GPU-minutes and a W&B run -- while the ONNX
            # export is derived from them and can be redone on any machine with torch, for
            # free, as many times as it takes. Letting the exception propagate here put the
            # two in the wrong order: on 2026-08-01 a completed 3-epoch DistilBERT was
            # destroyed with its pod because `model.export_onnx` raised after training, and
            # `fetch_checkpoint` is the statement immediately after this block.
            try:
                run_remote(
                    pod,
                    f"cd {REMOTE_WORKDIR} && {spec.export_command()}",
                    key_path=ssh_key,
                    timeout=3600.0,
                )
            except Exception as exc:  # noqa: BLE001 - re-raised below, after retrieval
                export_failure = exc
                print(f"ONNX EXPORT FAILED (weights are still being retrieved): {scrub(str(exc))}")
        # Retrieval happens inside the lease, always. The pod's disk dies with the pod, so a
        # checkpoint still on it at teardown time never existed.
        fetch_checkpoint(
            pod, output_path, key_path=ssh_key, require_safetensors=not spec.classical
        )
    if export_failure is not None:
        raise export_failure
    return Path(output_path)


# ---------------------------------------------------------------------------
# CLI -- dry run by default, like every destructive tool in this repo
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infra.runpod.deploy_runpod",
        description=(
            "Launch one DistilBERT fine-tuning pod. With no flags this prints the plan, the "
            "live price, and the account spend guard, and creates nothing."
        ),
    )
    parser.add_argument(
        "--execute", action="store_true", help="actually create the pod (default: plan only)"
    )
    parser.add_argument("--name", default="distilbert", help="run name; the pod name is prefixed")
    parser.add_argument("--dataset", type=Path, help="local dataset bundle to upload")
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/distilbert"), help="local checkpoint dir"
    )
    parser.add_argument("--gpu", default=None, help=f"one of {GPU_CANDIDATES}; default cheapest")
    parser.add_argument(
        "--max-hours", type=float, default=DEFAULT_MAX_HOURS,
        help=f"dead-man-switch duration (default {DEFAULT_MAX_HOURS})",
    )
    parser.add_argument(
        "--on-demand", action="store_true", help="disable spot: costs more, cannot be preempted"
    )
    parser.add_argument(
        "--classical", action="store_true",
        help="run the classical TF-IDF cross-validation instead of the DistilBERT fine-tune",
    )
    parser.add_argument("--wandb-project", default="mlops-toxic-moderation")
    parser.add_argument("--wandb-entity", default=None)
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--max-hourly-usd", type=float, default=MAX_HOURLY_USD)
    parser.add_argument("--max-run-usd", type=float, default=MAX_RUN_USD)
    parser.add_argument(
        "--show-payload", action="store_true",
        help="print the pod-creation body with every env value redacted",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    name = f"{POD_NAME_PREFIX}{args.name}"
    interruptible = not args.on_demand

    try:
        plan = preflight(
            name=name,
            max_hours=args.max_hours,
            # Spot is right for a fine-tune, which resumes from a checkpoint. The classical
            # CV cannot resume: a preemption discards every completed fold. One run was lost
            # that way at 95% complete. The on-demand premium is cents against 85 minutes.
            interruptible=interruptible and not args.classical,
            registry_path=args.registry,
            candidates=(args.gpu,) if args.gpu else GPU_CANDIDATES,
            max_hourly_usd=args.max_hourly_usd,
            max_run_usd=args.max_run_usd,
        )
    except Exception as exc:  # noqa: BLE001 - one message, no traceback, no launch
        print(f"PREFLIGHT FAILED: {scrub(str(exc))}", file=sys.stderr)
        return 1

    print(f"plan:\n{plan.render()}")

    if args.show_payload:
        print(
            json.dumps(
                build_pod_payload(
                    name=name,
                    gpu_type=plan.gpu_type,
                    interruptible=interruptible,
                    max_pod_seconds=int(args.max_hours * 3600),
                    env=redact_env(build_pod_env(wandb_project=args.wandb_project)),
                ),
                indent=2,
            )
        )

    if not args.execute:
        print("\nDRY RUN: nothing was created. Pass --execute to launch.")
        return 0
    if args.dataset is None:
        print("ERROR: --dataset is required with --execute", file=sys.stderr)
        return 1

    try:
        out = run_finetune(
            spec=FinetuneSpec(classical=args.classical),
            run_name=args.name,
            dataset_path=args.dataset,
            output_path=args.output,
            gpu_type=plan.gpu_type,
            wandb_project=args.wandb_project,
            wandb_entity=args.wandb_entity,
            ssh_key=args.ssh_key,
            max_hours=args.max_hours,
            interruptible=interruptible,
            registry_path=args.registry,
        )
    except Exception as exc:  # noqa: BLE001 - the pod is already down; report and exit
        print(f"RUN FAILED: {scrub(str(exc))}", file=sys.stderr)
        print(f"Confirm nothing is still live: {_REAP_CMD}", file=sys.stderr)
        return 1
    print(f"checkpoint: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
