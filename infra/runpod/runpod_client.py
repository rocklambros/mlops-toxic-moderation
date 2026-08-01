"""Transport, secrets, the crash-durable registry, the spend guard, and billing readback.

This is the bottom of `infra/runpod`. It knows how to speak to RunPod and how to write the
one file that makes a leaked pod recoverable; everything above it works against the small
surface defined here, and every unit test monkeypatches at that surface rather than at a
socket.

Four design notes, each checked against the live API on 2026-07-31 rather than recalled:

**Two APIs, not one.** Pod lifecycle is REST v1 (`https://rest.runpod.io/v1`). The account
spending cap, the prepaid balance, and GPU prices are *not* there -- `GET /v1/openapi.json`
lists exactly 23 paths and none of them expose those -- so they come from the legacy GraphQL
endpoint. Both are read here; neither is guessed.

**The cap is readable but not settable, and GraphQL introspection is disabled server-side**,
so there is no mutation to discover. The cap has to be set once in the console
(Billing -> Spend limit). What code *can* do, and does, is refuse to launch when it is absent
or when the projected burn would breach it. That is the enforceable half of "set a spending
cap before the first pod", and it runs on every launch instead of once.

**The default urllib User-Agent is blocked.** `api.runpod.io` sits behind Cloudflare, which
answers `HTTP 403, error 1010` to `Python-urllib/3.11`. Any non-default UA is accepted, so a
project-branded one is sent rather than a spoofed browser.

**stdlib only.** The project venv installs from a hashed lock that contains no HTTP client,
and the reaper has to run from a bare CI container with no install step. `Response` mirrors
the part of the `httpx.Response` surface the callers use, so a later swap is a drop-in and a
test fake stays three lines long.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REST_BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"

USER_AGENT = "mlops-toxic-moderation/0.1 (runpod-lifecycle)"
DEFAULT_TIMEOUT_S = 30.0

# `pass` is the only credential store this project uses; nothing is exported into a profile.
PASS_RUNPOD_KEY = "runpod/api-key"
PASS_WANDB_KEY = "wandb/api-key"
PASS_HF_TOKEN = "huggingface/token"

# Three credentials reach a pod -- the RunPod key, the W&B key, the HF token -- and any of
# them landing in a public GitHub Actions log is a rotation event, so all three shapes are
# matched rather than just the bearer header they arrive in.
_SECRET_PATTERNS = (
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"\brpa_[A-Za-z0-9]+"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
)


class RunPodError(RuntimeError):
    """A RunPod API call failed in a way the caller cannot recover from."""


class SpendGuardError(RuntimeError):
    """A spending guard would be breached. Nothing was launched."""


class RegistryError(ValueError):
    """The pod registry is unreadable. Never silently treated as "there are no pods"."""


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def load_secret(pass_name: str, env_var: str) -> str:
    """Return a secret from `env_var` if set, else from `pass show <pass_name>`.

    Four properties are load-bearing, and each one is a way this has gone wrong before:

    - **The secret is never an argument.** `ps aux` is readable by every process on this
      box, so the value comes back on stdout and the argv is a fixed three-element list
      with `shell=False`.
    - **Five-second timeout.** A locked GPG agent waiting on a pinentry that will never
      appear must not wedge a launcher that already has a pod running.
    - **`raise ... from None`.** `CalledProcessError` and `TimeoutExpired` both render the
      child's captured output in the traceback, and that output is the secret. Suppressing
      the chain is what keeps it out of the log.
    - **An empty value is an error, not a secret.** It would produce `Authorization: Bearer`
      and a 401, which reads exactly like "the reaper ran and found nothing".
    """
    value = os.environ.get(env_var, "")
    if value:
        return value.strip()
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, shell=False, no user input
            ["pass", "show", pass_name],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"load_secret: `pass` is not installed and {env_var} is not set"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"load_secret: `pass show {pass_name}` timed out after 5 seconds "
            "(is the GPG agent waiting for a passphrase?)"
        ) from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"load_secret: `pass show {pass_name}` failed (exit {exc.returncode}); "
            "secret not loaded"
        ) from None
    secret = (result.stdout or "").strip()
    if not secret:
        raise RuntimeError(f"load_secret: `pass show {pass_name}` returned an empty value")
    return secret


def scrub(text: str) -> str:
    """Redact anything token-shaped before it reaches a log line, a traceback, or a summary."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def redact_env(env: dict[str, str]) -> dict[str, str]:
    """Every value replaced, so a pod payload can be printed for review without a rotation."""
    return dict.fromkeys(env, "[REDACTED]")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    """The subset of `httpx.Response` the callers use, so test fakes stay tiny."""

    status_code: int
    text: str

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None


def auth_headers(api_key: str | None = None) -> dict[str, str]:
    """Bearer headers. The key is read lazily, so importing this module needs no secret."""
    key = api_key or load_secret(PASS_RUNPOD_KEY, "RUNPOD_API_KEY")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def request(
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Response:
    """One HTTP round trip.

    An HTTP error *status* comes back as a `Response` rather than an exception, because the
    callers make status decisions -- 404 on DELETE is idempotent success -- and cannot make
    them from a traceback. Transport failures do raise, scrubbed.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return Response(status_code=resp.status, text=resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return Response(status_code=exc.code, text=exc.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        raise RunPodError(scrub(f"{method} {url} failed: {exc.reason}")) from None


def http_get(url: str, headers: dict[str, str]) -> Response:
    return request("GET", url, headers)


def http_post(url: str, headers: dict[str, str], body: Any) -> Response:
    return request("POST", url, headers, body=body)


def http_delete(url: str, headers: dict[str, str]) -> Response:
    return request("DELETE", url, headers)


def graphql(query: str, *, api_key: str | None = None) -> dict[str, Any]:
    """Run one read-only GraphQL query.

    Partial errors are normal and not fatal: a scoped key resolves `spendLimit` while 401-ing
    `clientLifetimeSpend` in the same response. Only a wholly absent `data` raises.
    """
    resp = http_post(GRAPHQL_URL, auth_headers(api_key), {"query": query})
    if resp.status_code != 200:
        raise RunPodError(scrub(f"graphql failed ({resp.status_code}): {resp.text[:300]}"))
    payload = resp.json() or {}
    data = payload.get("data")
    if data is None:
        raise RunPodError(scrub(f"graphql returned no data: {json.dumps(payload)[:300]}"))
    return data


# ---------------------------------------------------------------------------
# The registry -- the only teardown mechanism that survives SIGKILL
# ---------------------------------------------------------------------------


def read_registry(path: Path | str) -> list[dict[str, Any]]:
    """Registry entries in file order.

    A missing, empty, or whitespace-only file is `[]`: the reaper has to run on a machine
    where the launcher never got far enough to write one. **Truncated JSON is not.** That is
    the expected artefact of a crash mid-write, which is exactly when a pod is most likely to
    be live, and answering `[]` there would report "nothing to reap" while a GPU bills. It
    raises instead, naming the file, so the operator goes and looks in the console.
    """
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text().strip()
    if not text:
        return []
    try:
        entries = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(
            f"{p} is not valid JSON ({exc.msg} at line {exc.lineno}). A truncated registry is "
            "the signature of a crash mid-write: check the RunPod console for live pods "
            "before repairing or deleting this file."
        ) from None
    if not isinstance(entries, list):
        raise RegistryError(f"{p} must hold a JSON list of pod entries, got {type(entries)}")
    return entries


def atomic_write_registry(path: Path | str, entries: list[dict[str, Any]]) -> None:
    """Write the whole registry with write-temp, fsync, `os.replace`.

    `os.replace` is atomic within a filesystem, so a concurrent reader -- including the
    reaper running as a separate process while the launcher is being killed -- sees either
    the old file or the new one, never a truncated one. The fsync *before* the rename is what
    makes that survive a power loss rather than merely a process death; an fsync afterwards
    would not make the rename durable. The temp file is unlinked on any failure so a crash
    does not leave litter that looks like a registry.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(p.parent), delete=False, suffix=".tmp"
        ) as handle:
            json.dump(entries, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_name = handle.name
        os.replace(tmp_name, str(p))
    except BaseException:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def append_registry(path: Path | str, entry: dict[str, Any]) -> None:
    """Append one `{"name", "pod_id", ...}` entry, de-duplicating by pod id.

    Read-modify-write, not truncate-and-write: a sweep is several pods, and an append that
    forgets the earlier ones turns them into orphans the reaper will refuse to touch.
    """
    entries = read_registry(path)
    if not any(e.get("pod_id") == entry.get("pod_id") for e in entries):
        entries.append(entry)
    atomic_write_registry(path, entries)


def remove_from_registry(path: Path | str, pod_ids: set[str]) -> None:
    """Drop the named pods, preserving every entry written by another process.

    Called only after a termination is *confirmed*. A pod whose DELETE failed deliberately
    stays on the books: that record is the only thing standing between a failed teardown and
    an invisible leak.
    """
    if not pod_ids:
        return
    remaining = [e for e in read_registry(path) if str(e.get("pod_id", "")) not in pod_ids]
    atomic_write_registry(path, remaining)


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountStatus:
    """What the account will allow, read from `myself`.

    `spend_limit` sits beside `current_spend_per_hr` in the same object, so it is a rate --
    dollars per hour of concurrent burn -- rather than a lifetime total. The unit is not
    stated in the response, so nothing here divides by it: it is compared against the
    *hourly* cost of what is about to be launched, which is the right comparison under either
    reading. The total ceiling is enforced separately against `client_balance`, because
    RunPod is prepaid and the balance is itself a hard stop.
    """

    user_id: str
    spend_limit: float | None
    client_balance: float
    current_spend_per_hr: float


_ACCOUNT_QUERY = "query { myself { id spendLimit clientBalance currentSpendPerHr } }"


def account_status(*, api_key: str | None = None) -> AccountStatus:
    """Read the cap, the prepaid balance, and what is burning right now."""
    myself = (graphql(_ACCOUNT_QUERY, api_key=api_key) or {}).get("myself") or {}
    limit = myself.get("spendLimit")
    return AccountStatus(
        user_id=str(myself.get("id", "")),
        spend_limit=float(limit) if limit is not None else None,
        client_balance=float(myself.get("clientBalance") or 0.0),
        current_spend_per_hr=float(myself.get("currentSpendPerHr") or 0.0),
    )


def assert_spend_guard(
    *,
    projected_hourly_usd: float,
    projected_total_usd: float,
    max_hourly_usd: float,
    max_total_usd: float,
    status: AccountStatus | None = None,
) -> AccountStatus:
    """Refuse the launch unless every ceiling holds. Raises; it never merely warns.

    Four ceilings, because each one fails differently:

    1. **A cap is set at all.** An uncapped account is a configuration error, not a default.
    2. **What is about to run, plus what is already running, stays under it.** The cap is a
       rate, so the second pod launched beside the first is what actually breaches it, and
       only the combined figure catches that.
    3. **This project's own hourly ceiling**, far tighter than the account cap, so a typo
       cannot turn a $0.30/hr A40 into a flagship card.
    4. **The worst-case total fits in the run ceiling and in the prepaid balance.** A run
       priced above the balance stops mid-training, wasting the whole spend rather than part
       of it, and a forgotten pod cannot outlive money that is already gone.
    """
    st = status or account_status()

    if st.spend_limit is None or st.spend_limit <= 0:
        raise SpendGuardError(
            "no RunPod account spending cap is set. Set one in the console "
            "(Billing -> Spend limit) before launching; the API exposes the cap read-only."
        )

    combined = st.current_spend_per_hr + projected_hourly_usd
    if combined > st.spend_limit:
        raise SpendGuardError(
            f"projected burn ${combined:.2f}/hr (${st.current_spend_per_hr:.2f}/hr already "
            f"running + ${projected_hourly_usd:.2f}/hr requested) exceeds the account cap of "
            f"${st.spend_limit:.2f}"
        )

    if projected_hourly_usd > max_hourly_usd:
        raise SpendGuardError(
            f"projected ${projected_hourly_usd:.2f}/hr exceeds this project's ceiling of "
            f"${max_hourly_usd:.2f}/hr. A 66M-parameter DistilBERT does not need a flagship "
            "card; raise --max-hourly-usd deliberately if the workload really changed."
        )

    if projected_total_usd > max_total_usd:
        raise SpendGuardError(
            f"worst-case run cost ${projected_total_usd:.2f} exceeds the run ceiling of "
            f"${max_total_usd:.2f}"
        )

    if projected_total_usd > st.client_balance:
        raise SpendGuardError(
            f"worst-case run cost ${projected_total_usd:.2f} exceeds the prepaid balance of "
            f"${st.client_balance:.2f}; the pod would be stopped mid-run"
        )

    return st


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuOffer:
    gpu_type_id: str
    memory_gb: int
    spot_usd_per_hr: float | None
    on_demand_usd_per_hr: float | None
    stock_status: str | None
    secure_cloud: bool
    community_cloud: bool

    def price(self, *, interruptible: bool) -> float | None:
        """What this pod would actually pay. Some cards (an L4 today) have no spot offer."""
        if interruptible and self.spot_usd_per_hr is not None:
            return self.spot_usd_per_hr
        return self.on_demand_usd_per_hr


_GPU_QUERY = """
query {
  gpuTypes {
    id
    memoryInGb
    secureCloud
    communityCloud
    lowestPrice(input: {gpuCount: 1}) {
      minimumBidPrice
      uninterruptablePrice
      stockStatus
    }
  }
}
"""


def gpu_offers(*, api_key: str | None = None) -> dict[str, GpuOffer]:
    """Live price and stock per GPU, keyed by the exact string `POST /pods` expects."""
    rows = (graphql(_GPU_QUERY, api_key=api_key) or {}).get("gpuTypes") or []
    offers: dict[str, GpuOffer] = {}
    for row in rows:
        low = row.get("lowestPrice") or {}
        gpu_id = str(row.get("id", ""))
        offers[gpu_id] = GpuOffer(
            gpu_type_id=gpu_id,
            memory_gb=int(row.get("memoryInGb") or 0),
            spot_usd_per_hr=_maybe_float(low.get("minimumBidPrice")),
            on_demand_usd_per_hr=_maybe_float(low.get("uninterruptablePrice")),
            stock_status=low.get("stockStatus"),
            secure_cloud=bool(row.get("secureCloud")),
            community_cloud=bool(row.get("communityCloud")),
        )
    return offers


def _maybe_float(value: Any) -> float | None:
    return None if value is None else float(value)


def choose_gpu(
    *,
    candidates: tuple[str, ...] = (),
    interruptible: bool = True,
    offers: dict[str, GpuOffer] | None = None,
) -> tuple[str, float, str | None]:
    """Pick the cheapest in-stock candidate: `(gpu_type, usd_per_hr, stock_status)`.

    The candidate tuple is the allowlist of cards that suit a 66M-parameter model; the live
    API decides which of them is cheapest today, because a price hardcoded in a comment goes
    stale silently. A card with no price or no stock is not a candidate, and an empty result
    refuses rather than falling back to "whatever is available" -- launching without knowing
    the rate is agreeing to an unknown one.
    """
    live = offers if offers is not None else gpu_offers()
    pool = candidates or tuple(live)
    priced: list[tuple[float, str, str | None]] = []
    for gpu in pool:
        offer = live.get(gpu)
        if offer is None:
            continue
        price = offer.price(interruptible=interruptible)
        if price is None or price <= 0:
            continue
        if (offer.stock_status or "").lower() in ("none", "unavailable"):
            continue
        priced.append((price, gpu, offer.stock_status))
    if not priced:
        raise SpendGuardError(
            f"none of {pool} has a usable price and stock right now; refusing to launch "
            "blind. Retry later, or widen the candidate list deliberately."
        )
    priced.sort(key=lambda row: (row[0], row[1]))
    return priced[0][1], priced[0][0], priced[0][2]


# ---------------------------------------------------------------------------
# What it actually cost
# ---------------------------------------------------------------------------


def pod_spend(
    *, days: int = 7, bucket: str = "day", api_key: str | None = None
) -> tuple[float, list[dict[str, Any]]]:
    """`(total_usd, records)` billed for pods over the last `days`.

    The honest answer to "what did this cost", read from RunPod rather than inferred from
    wall-clock. `GET /billing/pods` returns
    `[{amount, timeBilledMs, diskSpaceBilledGB, podId, time}, ...]`.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    url = (
        f"{REST_BASE}/billing/pods"
        f"?bucketSize={bucket}"
        f"&startTime={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&endTime={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&grouping=podId"
    )
    resp = http_get(url, auth_headers(api_key))
    if resp.status_code != 200:
        raise RunPodError(scrub(f"pod_spend failed ({resp.status_code}): {resp.text[:300]}"))
    records = resp.json() or []
    return sum(float(rec.get("amount") or 0.0) for rec in records), list(records)


def main(argv: list[str] | None = None) -> int:
    """`python -m infra.runpod.runpod_client` -- read-only: cap, balance, prices, recent spend."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m infra.runpod.runpod_client",
        description="Read-only RunPod status: spending cap, balance, live prices, recent spend.",
    )
    parser.add_argument("--days", type=int, default=7, help="billing window (default 7)")
    args = parser.parse_args(argv)

    status = account_status()
    print(
        f"account {status.user_id}\n"
        f"  spend cap       : {status.spend_limit}\n"
        f"  prepaid balance : ${status.client_balance:.2f}\n"
        f"  current burn    : ${status.current_spend_per_hr:.2f}/hr"
    )
    total, records = pod_spend(days=args.days)
    print(f"  spend last {args.days:>2}d : ${total:.2f} across {len(records)} billing record(s)")

    offers = gpu_offers()
    print("  candidate GPUs  :")
    from infra.runpod.deploy_runpod import GPU_CANDIDATES

    for gpu in GPU_CANDIDATES:
        offer = offers.get(gpu)
        if offer is None:
            print(f"    {gpu:26s} not offered")
            continue
        print(
            f"    {gpu:26s} spot={offer.spot_usd_per_hr} "
            f"on-demand={offer.on_demand_usd_per_hr} stock={offer.stock_status}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
