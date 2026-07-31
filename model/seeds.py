"""Seed hygiene and reproducibility metadata.

PYTHONHASHSEED must be set BEFORE the interpreter starts, so setting it from Python is a
no-op for the running process. `conftest.py` enforces it for the suite by reading
`sys.flags.hash_randomization`, which no plugin can spoof; the Makefile sets it for
`make test` and `make data`; CI must set it as a job-level env var because CI runs bare
`pytest`, not `make test`. set_all_seeds deliberately does not touch it.
"""

import datetime as dt
import random
import subprocess
import sys

import numpy as np


def set_all_seeds(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    try:
        import torch  # guarded: torch is a build-time dependency only

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except ImportError:
        pass


def assert_hash_seed_pinned() -> None:
    if sys.flags.hash_randomization:
        raise RuntimeError("PYTHONHASHSEED=0 is required for reproducible runs")


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown"


def run_metadata(
    seed: int,
    raw_sha256: str | None = None,
    split_version: str | None = None,
    env_version: str | None = None,
) -> dict:
    return {
        "git_sha": _git_sha(),
        "seed": seed,
        "raw_sha256": raw_sha256,
        "split_version": split_version,
        "env_version": env_version,
        "hash_randomization": bool(sys.flags.hash_randomization),
        "timestamp_utc": dt.datetime.now(dt.UTC).isoformat(),
    }
