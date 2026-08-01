"""Contract tests for the RunPod launcher, `infra/runpod/deploy_runpod.py`.

Companion to `tests/unit/test_terminate_runpod.py`. That file proves a recorded pod can
always be killed; this one proves a created pod is always recorded, and that the process
which created it does not exit while it is still alive.

**These tests are the gate. They exist before any pod is launched, on purpose.** The
authorised ceiling for Phase 1 is $1000. A mid-tier card is roughly $0.30-$0.45 an hour,
which is invisible for an afternoon and material over a weekend, and the moment a leak is
most likely is the moment the launcher crashes — which is also the moment every in-process
safety net stops running.

The ordering rule is the whole design, and it is not negotiable:

    create pod  ->  ATOMICALLY record it  ->  only then block on readiness

`atexit`, `try/finally` and signal handlers all die with `SIGKILL`, an OOM kill, a pulled
power cord, or a closed laptop lid. The registry file does not. It is the only teardown
mechanism that survives the failure modes that actually orphan GPUs, so it must be on disk,
complete, and fsynced before the process blocks on anything.

No network, no credentials, no SSH. Every side effect goes through a module-level seam the
tests replace: `dep._create_pod`, `dep._get_pod`, `dep.wait_until_ready`, `dep.terminate_pod`,
`dep.load_secret`, and the injected `probe` / `sleep` / `monotonic` callables.

Tests marked HARDENING assert behaviour the current implementation does not yet have. Each
names the leak it closes.
"""

from __future__ import annotations

import inspect
import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from infra.runpod import deploy_runpod as dep
from infra.runpod import runpod_client as rpc
from infra.runpod import terminate_runpod as trm

FAKE_KEY = "test-runpod-api-key-xyz"
SECRET_MARKER = "sk-do-not-log-this-value"

# Anything above this tier is wasted money for a 66M-parameter DistilBERT over 212k short
# comments. Substrings, so "NVIDIA H100 PCIe" and "H100 80GB HBM3" both trip it.
OVERSIZED_CARDS = ("H100", "H200", "A100", "B200", "GH200", "MI300")

READY_RAW = {
    "id": "p1",
    "name": "toxic-finetune-a",
    "desiredStatus": "RUNNING",
    "lastStartedAt": "2026-07-31T12:00:00Z",
    "publicIp": "203.0.113.7",
    "portMappings": {"22": 22001},
    "costPerHr": 0.30,
}


class _Resp:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dep, "load_secret", lambda *_a, **_k: FAKE_KEY)
    monkeypatch.setattr(trm, "load_secret", lambda *_a, **_k: FAKE_KEY)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Records the payload of every pod-creation request and hands back a fixed id."""
    calls: list[dict[str, Any]] = []

    def _create(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return dict(READY_RAW)

    monkeypatch.setattr(dep, "_create_pod", _create)
    return calls


@pytest.fixture
def terminated(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        dep, "terminate_pod", lambda pod_id, *_a, **_k: (calls.append(pod_id), True)[1]
    )
    monkeypatch.setattr(dep, "assert_no_survivors", lambda *_a, **_k: None)
    return calls


@pytest.fixture
def instant_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dep, "wait_until_ready", lambda *_a, **_k: dict(READY_RAW))


def _launch(tmp_path: Path, **kwargs: Any):
    return dep.launch_pod(
        name=kwargs.pop("name", "toxic-finetune-a"),
        registry_path=kwargs.pop("registry_path", tmp_path / "runpod_pods.json"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1 — the registry reaches disk BEFORE anything can block
# ---------------------------------------------------------------------------


def test_the_registry_is_written_before_the_readiness_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created: list[dict[str, Any]]
) -> None:
    """The single scenario this whole module exists for: the pod is created, the readiness
    wait explodes, and a terminatable record is already on disk."""
    path = tmp_path / "runpod_pods.json"

    def exploding_wait(_pod_id: str, **_kw: Any) -> None:
        assert json.loads(path.read_text())[0]["pod_id"] == "p1", (
            "the registry must be complete on disk before the readiness wait can fail"
        )
        raise dep.ReadinessTimeout("pod never became ready")

    monkeypatch.setattr(dep, "wait_until_ready", exploding_wait)

    with pytest.raises(dep.ReadinessTimeout):
        _launch(tmp_path)

    assert json.loads(path.read_text())[0]["pod_id"] == "p1"


def test_a_hard_kill_during_the_wait_still_leaves_a_terminatable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created: list[dict[str, Any]]
) -> None:
    """`SystemExit` is the closest in-process stand-in for SIGKILL: it derives from
    `BaseException`, so no `except Exception` handler catches it. Whatever survives is what
    a real kill would leave behind, and it must be enough for the reaper to work from with
    no further information."""
    path = tmp_path / "runpod_pods.json"

    def killed_wait(_pod_id: str, **_kw: Any) -> None:
        raise SystemExit(137)

    monkeypatch.setattr(dep, "wait_until_ready", killed_wait)

    with pytest.raises(SystemExit):
        _launch(tmp_path)

    assert [e["pod_id"] for e in rpc.read_registry(path)] == ["p1"]

    plan = trm.terminate_all_registered(path, execute=False)
    assert [e["pod_id"] for e in plan["would_terminate"]] == ["p1"]


def test_the_registry_entry_carries_everything_the_reaper_needs(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    path = tmp_path / "runpod_pods.json"
    _launch(tmp_path, registry_path=path)
    entry = rpc.read_registry(path)[0]
    assert entry["pod_id"] == "p1"
    assert entry["name"] == "toxic-finetune-a"


def test_the_registry_directory_is_created_if_absent(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    path = tmp_path / "nested" / "deeper" / "runpod_pods.json"
    _launch(tmp_path, registry_path=path)
    assert rpc.read_registry(path)[0]["pod_id"] == "p1"


def test_no_temporary_file_is_left_behind(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    path = tmp_path / "runpod_pods.json"
    _launch(tmp_path, registry_path=path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["runpod_pods.json"]


def test_a_failed_registry_write_terminates_the_pod_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    created: list[dict[str, Any]],
    terminated: list[str],
    instant_ready: None,
) -> None:
    """HARDENING. A read-only volume, a full disk, or a permissions error means the pod
    exists and nothing on disk knows about it. That is an *unreapable* pod: `reconcile` will
    correctly refuse to auto-terminate it as an orphan, forever, and only a human reading the
    console will ever find it. The only safe response is to kill it right now, while the id
    is still in a local variable."""

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(dep, "append_registry", boom)

    with pytest.raises(OSError):
        _launch(tmp_path)

    assert terminated == ["p1"], (
        "a pod that could not be recorded must be terminated before the exception "
        f"propagates; terminate_pod calls were {terminated}"
    )


def test_a_failed_creation_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instant_ready: None
) -> None:
    monkeypatch.setattr(
        dep,
        "_create_pod",
        lambda _payload: (_ for _ in ()).throw(dep.LaunchError("creation failed")),
    )
    path = tmp_path / "runpod_pods.json"

    with pytest.raises(dep.LaunchError):
        _launch(tmp_path, registry_path=path)

    assert rpc.read_registry(path) == []


def test_a_creation_response_with_no_id_is_rejected_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`raw["id"]` raising `KeyError` after a 200 leaves a pod that may well exist, recorded
    nowhere, with an opaque traceback. Fail with the response in hand."""
    monkeypatch.setattr(dep, "http_post", lambda *_a, **_k: _Resp(201, {"status": "created"}))

    with pytest.raises(dep.LaunchError) as excinfo:
        dep._create_pod({"name": "toxic-finetune-a"})
    assert "id" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# 2 — the launcher can only create pods the reaper is allowed to kill
# ---------------------------------------------------------------------------


def test_a_launched_pod_is_reapable_end_to_end(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    """The cross-module invariant, asserted rather than assumed. The launcher chooses the
    pod name; the reaper's allowlist decides what may be deleted. If those drift, the reaper
    skips our own pod by guard and the GPU bills until somebody reads a log line that says
    SKIP. Launch, then run the real reaper against the real registry file."""
    path = tmp_path / "runpod_pods.json"
    _launch(tmp_path, registry_path=path)

    plan = trm.terminate_all_registered(path, execute=False)

    assert [e["pod_id"] for e in plan["would_terminate"]] == ["p1"]
    assert plan["skipped_by_guard"] == [], (
        "the launcher created a pod the reaper refuses to terminate: the pod name and "
        f"DEFAULT_ALLOW={trm.DEFAULT_ALLOW} have drifted apart"
    )


@pytest.mark.parametrize("name", ["sweep-a", "distilbert-run", "", "my-pod"])
def test_the_launcher_refuses_a_name_the_reaper_could_not_kill(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None, name: str
) -> None:
    """Validate the name before spending money, not after. A pod named outside the reaper's
    allowlist is unkillable by the automation that exists to kill it."""
    with pytest.raises(dep.LaunchError) as excinfo:
        _launch(tmp_path, name=name)
    assert not created, "no pod may be created with an unreapable name"
    assert "toxic-" in str(excinfo.value), "the error must name the required prefix"


def test_the_default_pod_name_prefix_is_inside_the_reaper_allowlist() -> None:
    assert dep.POD_NAME_PREFIX.startswith(trm.DEFAULT_ALLOW), (
        f"POD_NAME_PREFIX={dep.POD_NAME_PREFIX!r} is not covered by {trm.DEFAULT_ALLOW}"
    )


def test_the_launcher_and_the_reaper_share_one_registry_path() -> None:
    """Two paths means the launcher records pods the reaper never reads."""
    assert Path(dep.DEFAULT_REGISTRY) == Path(trm.DEFAULT_REGISTRY)


# ---------------------------------------------------------------------------
# 3 — cost controls at the point of creation
# ---------------------------------------------------------------------------


def test_the_launcher_requests_interruptible_spot_capacity(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    """A fine-tune restarts cleanly from a checkpoint, so on-demand pricing buys little. Spot
    is the default and it has to be visible in the request, not in a comment."""
    _launch(tmp_path)
    assert created[0].get("interruptible") is True


def test_the_gpu_allowlist_holds_only_mid_tier_cards() -> None:
    """66M parameters over 212k short comments does not need a flagship card, and the price
    difference between an A40 and an H200 is roughly an order of magnitude."""
    assert dep.GPU_CANDIDATES, "an empty GPU allowlist would accept anything"
    for gpu in dep.GPU_CANDIDATES:
        assert not any(card in gpu.upper() for card in OVERSIZED_CARDS), (
            f"{gpu!r} is a flagship card; a mid-tier A40/4090/A6000/L4 is the right size here"
        )


def test_the_project_ceilings_are_far_below_the_authorised_budget() -> None:
    """These are the numbers a human reasons about, and they are the reason a typo cannot
    turn $25 into $1000."""
    assert 0 < dep.MAX_HOURLY_USD <= 2.0
    assert 0 < dep.MAX_RUN_USD <= 100.0
    assert 0 < dep.DEFAULT_MAX_HOURS <= 12.0


def test_the_launcher_refuses_a_gpu_outside_the_allowlist(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    """HARDENING. `preflight` picks the GPU from the allowlist, but `launch_pod` is public
    and `PodLease` forwards whatever it is handed. A caller that skips preflight — a
    notebook, a retry script, a future sweep driver — can rent eight H200s with a typo. The
    allowlist must be enforced where the money is actually spent."""
    with pytest.raises((ValueError, dep.LaunchError, dep.SpendGuardError)) as excinfo:
        _launch(tmp_path, gpu_type="NVIDIA H200")
    assert not created
    assert "H200" in str(excinfo.value)


def test_the_launcher_requests_exactly_one_gpu(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    _launch(tmp_path)
    assert created[0].get("gpuCount") == 1
    assert created[0].get("gpuTypeIds") == [dep.GPU_CANDIDATES[0]]


def test_no_network_volume_is_attached(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    """A *network* volume outlives the pod and keeps billing after termination, which is
    exactly the silent cost this suite exists to prevent. The pod-local volume disk is fine:
    it dies with the pod and it is what keeps a checkpoint across a spot preemption."""
    _launch(tmp_path)
    assert "networkVolumeId" not in created[0]


def test_the_pod_carries_its_own_dead_man_switch(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    """The one teardown mechanism that does not depend on this machine still existing. If
    the laptop's battery dies mid-run, the pod's own start command still returns and the
    container exits."""
    _launch(tmp_path, max_hours=2.0)
    start_cmd = " ".join(created[0]["dockerStartCmd"])
    assert "timeout" in start_cmd
    assert str(int(2.0 * 3600)) in start_cmd


def test_the_dead_man_switch_matches_the_priced_duration(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    """The worst case is priced at the dead-man-switch duration. If the switch is longer than
    what was priced, the ceiling that was approved is not the ceiling that applies."""
    _launch(tmp_path, max_hours=dep.DEFAULT_MAX_HOURS)
    start_cmd = " ".join(created[0]["dockerStartCmd"])
    assert str(int(dep.DEFAULT_MAX_HOURS * 3600)) in start_cmd


def test_the_spend_guard_refuses_an_uncapped_account() -> None:
    """An account with no cap set is a configuration error, not a warning. The API exposes
    the cap read-only, so it has to be set in the console before anything launches."""
    uncapped = rpc.AccountStatus(
        user_id="u", spend_limit=None, client_balance=1000.0, current_spend_per_hr=0.0
    )
    with pytest.raises(rpc.SpendGuardError) as excinfo:
        rpc.assert_spend_guard(
            projected_hourly_usd=0.30,
            projected_total_usd=1.20,
            max_hourly_usd=dep.MAX_HOURLY_USD,
            max_total_usd=dep.MAX_RUN_USD,
            status=uncapped,
        )
    assert "cap" in str(excinfo.value).lower()


def test_the_spend_guard_counts_what_is_already_burning(tmp_path: Path) -> None:
    """The cap is a rate. A second pod launched next to a first one is what actually breaches
    it, and only the combined figure catches that."""
    busy = rpc.AccountStatus(
        user_id="u", spend_limit=1.00, client_balance=1000.0, current_spend_per_hr=0.90
    )
    with pytest.raises(rpc.SpendGuardError) as excinfo:
        rpc.assert_spend_guard(
            projected_hourly_usd=0.30,
            projected_total_usd=1.20,
            max_hourly_usd=dep.MAX_HOURLY_USD,
            max_total_usd=dep.MAX_RUN_USD,
            status=busy,
        )
    assert "already running" in str(excinfo.value)


def test_the_spend_guard_refuses_a_run_that_outlives_the_prepaid_balance() -> None:
    """RunPod is prepaid. A run priced above the balance stops mid-training, which wastes
    the whole spend rather than part of it."""
    thin = rpc.AccountStatus(
        user_id="u", spend_limit=5.00, client_balance=1.00, current_spend_per_hr=0.0
    )
    with pytest.raises(rpc.SpendGuardError) as excinfo:
        rpc.assert_spend_guard(
            projected_hourly_usd=0.30,
            projected_total_usd=1.20,
            max_hourly_usd=dep.MAX_HOURLY_USD,
            max_total_usd=dep.MAX_RUN_USD,
            status=thin,
        )
    assert "balance" in str(excinfo.value).lower()


def test_the_spend_guard_passes_a_sane_plan() -> None:
    healthy = rpc.AccountStatus(
        user_id="u", spend_limit=5.00, client_balance=1000.0, current_spend_per_hr=0.0
    )
    assert (
        rpc.assert_spend_guard(
            projected_hourly_usd=0.30,
            projected_total_usd=1.20,
            max_hourly_usd=dep.MAX_HOURLY_USD,
            max_total_usd=dep.MAX_RUN_USD,
            status=healthy,
        )
        is healthy
    )


def test_choose_gpu_picks_the_cheapest_in_stock_candidate() -> None:
    offers = {
        "NVIDIA A40": rpc.GpuOffer("NVIDIA A40", 48, 0.30, 0.35, "High", True, False),
        "NVIDIA L4": rpc.GpuOffer("NVIDIA L4", 24, None, 0.44, "Medium", True, True),
    }
    gpu, price, _stock = dep.choose_gpu(offers=offers, interruptible=True)
    assert (gpu, price) == ("NVIDIA A40", 0.30)


def test_choose_gpu_refuses_to_launch_blind_when_nothing_is_priced() -> None:
    """A card with no price and no stock is not a candidate. Launching anyway means agreeing
    to an unknown hourly rate."""
    offers = {"NVIDIA A40": rpc.GpuOffer("NVIDIA A40", 48, None, None, "None", True, False)}
    with pytest.raises(rpc.SpendGuardError):
        dep.choose_gpu(offers=offers, interruptible=True)


def test_preflight_refuses_to_launch_on_top_of_an_existing_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launching on top of a leak doubles the leak and buries the evidence: from then on you
    cannot tell the new pod from the old one."""
    monkeypatch.setattr(
        trm, "_http_get", lambda *_a, **_k: _Resp(200, [{"id": "old", "name": "toxic-sweep-x"}])
    )
    path = tmp_path / "runpod_pods.json"
    path.write_text(json.dumps([{"name": "toxic-sweep-x", "pod_id": "old"}]))

    with pytest.raises(rpc.SpendGuardError) as excinfo:
        dep.preflight(name="toxic-finetune-a", registry_path=path, check_account=False)

    assert "terminate_runpod" in str(excinfo.value), "the message must carry the fix command"


def test_preflight_refuses_to_launch_when_an_orphan_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        trm, "_http_get", lambda *_a, **_k: _Resp(200, [{"id": "who", "name": "mystery"}])
    )
    path = tmp_path / "runpod_pods.json"
    path.write_text("[]")

    with pytest.raises(rpc.SpendGuardError):
        dep.preflight(name="toxic-finetune-a", registry_path=path, check_account=False)


# ---------------------------------------------------------------------------
# 4 — teardown on success, on failure, and on interrupt
# ---------------------------------------------------------------------------


def test_the_lease_terminates_the_pod_on_success(
    tmp_path: Path, created: list[dict[str, Any]], terminated: list[str], instant_ready: None
) -> None:
    path = tmp_path / "runpod_pods.json"
    with dep.PodLease(name="toxic-finetune-a", registry_path=path) as pod:
        assert pod.pod_id == "p1"
    assert terminated == ["p1"]
    assert rpc.read_registry(path) == [], "a confirmed-gone pod leaves the registry"


def test_the_lease_terminates_the_pod_when_the_body_raises(
    tmp_path: Path, created: list[dict[str, Any]], terminated: list[str], instant_ready: None
) -> None:
    """Dies on failure, not only on success. Any exception at all, not a curated list."""
    with pytest.raises(RuntimeError):
        with dep.PodLease(name="toxic-finetune-a", registry_path=tmp_path / "r.json"):
            raise RuntimeError("the fine-tune blew up")
    assert terminated == ["p1"]


def test_the_lease_terminates_the_pod_on_keyboard_interrupt(
    tmp_path: Path, created: list[dict[str, Any]], terminated: list[str], instant_ready: None
) -> None:
    """Ctrl-C is the most common way a human ends a run. `except Exception` does not catch
    `KeyboardInterrupt`; only `finally` / `__exit__` does."""
    with pytest.raises(KeyboardInterrupt):
        with dep.PodLease(name="toxic-finetune-a", registry_path=tmp_path / "r.json"):
            raise KeyboardInterrupt
    assert terminated == ["p1"]


def test_the_lease_does_not_swallow_the_body_exception(
    tmp_path: Path, created: list[dict[str, Any]], terminated: list[str], instant_ready: None
) -> None:
    """A teardown that returns True from `__exit__` hides the failure that caused it."""
    with pytest.raises(ValueError, match="the real problem"):
        with dep.PodLease(name="toxic-finetune-a", registry_path=tmp_path / "r.json"):
            raise ValueError("the real problem")


def test_teardown_is_idempotent(
    tmp_path: Path, created: list[dict[str, Any]], terminated: list[str], instant_ready: None
) -> None:
    """`__exit__` and the atexit hook both fire on some paths. The pod must be deleted once,
    and a second call must not raise."""
    lease = dep.PodLease(name="toxic-finetune-a", registry_path=tmp_path / "r.json")
    lease.__enter__()
    lease.teardown()
    lease.teardown()
    assert terminated == ["p1"]


def test_teardown_verifies_the_pod_is_actually_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created: list[dict[str, Any]],
    instant_ready: None,
) -> None:
    """A 204 from DELETE is the API's claim. Teardown re-reads the truth, because the failure
    it guards against is silent and expensive."""
    monkeypatch.setattr(dep, "terminate_pod", lambda *_a, **_k: True)
    monkeypatch.setattr(
        trm, "_http_get", lambda *_a, **_k: _Resp(200, [{"id": "p1", "name": "toxic-finetune-a"}])
    )

    with pytest.raises(trm.SurvivingPodsError):
        with dep.PodLease(name="toxic-finetune-a", registry_path=tmp_path / "r.json"):
            pass


def test_a_pod_that_survived_teardown_stays_in_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created: list[dict[str, Any]],
    instant_ready: None,
) -> None:
    """De-registering a pod the API says is still live erases the one record that would have
    found it. Evidence first, then the registry edit."""
    path = tmp_path / "runpod_pods.json"
    monkeypatch.setattr(dep, "terminate_pod", lambda *_a, **_k: True)
    monkeypatch.setattr(
        trm, "_http_get", lambda *_a, **_k: _Resp(200, [{"id": "p1", "name": "toxic-finetune-a"}])
    )

    with pytest.raises(trm.SurvivingPodsError):
        with dep.PodLease(name="toxic-finetune-a", registry_path=path):
            pass

    assert [e["pod_id"] for e in rpc.read_registry(path)] == ["p1"]


def test_the_lease_terminates_a_pod_whose_launch_never_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created: list[dict[str, Any]],
    terminated: list[str],
) -> None:
    """HARDENING, and the most expensive gap in this module.

    `with PodLease(...) as pod:` only calls `__exit__` if `__enter__` *returned*. `__enter__`
    calls `launch_pod`, which creates the pod, records it, and then blocks on readiness — so
    a readiness timeout, a spot preemption during boot, or a Ctrl-C in the wait raises from
    inside `__enter__`. `__exit__` never runs. `self.pod` is still None, so the atexit hook
    returns immediately too. The pod is live, recorded, and abandoned until somebody runs the
    reaper by hand.

    This is precisely the failure mode the lease was written for, and it is the one path the
    lease does not cover. Teardown must be driven by what is on disk for this invocation, not
    by an attribute a raising call never assigned."""
    monkeypatch.setattr(
        dep,
        "wait_until_ready",
        lambda *_a, **_k: (_ for _ in ()).throw(dep.ReadinessTimeout("never ready")),
    )

    with pytest.raises(dep.ReadinessTimeout):
        with dep.PodLease(name="toxic-finetune-a", registry_path=tmp_path / "runpod_pods.json"):
            pass

    assert terminated == ["p1"], (
        "launch_pod raised after the pod was created and recorded; the lease must still kill "
        f"it, but terminate_pod calls were {terminated}"
    )


def test_a_failing_teardown_tells_the_operator_what_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created: list[dict[str, Any]],
    instant_ready: None, capsys,
) -> None:
    """HARDENING. If the DELETE itself fails, the operator has to learn the pod id and the
    recovery command from somewhere. A traceback is not a runbook."""
    monkeypatch.setattr(
        dep,
        "terminate_pod",
        lambda *_a, **_k: (_ for _ in ()).throw(trm.TerminateError("delete failed")),
    )

    with pytest.raises(trm.TerminateError):
        with dep.PodLease(name="toxic-finetune-a", registry_path=tmp_path / "r.json"):
            pass

    out = capsys.readouterr()
    combined = out.out + out.err
    assert "p1" in combined, f"the leaked pod id must be printed, got {combined!r}"
    assert "terminate_runpod" in combined, "and the operator must be told what to run next"


# ---------------------------------------------------------------------------
# 5 — the readiness wait is bounded, mockable, and not fooled by desiredStatus
# ---------------------------------------------------------------------------


def test_desired_status_running_alone_is_not_readiness() -> None:
    """`desiredStatus` is the status the pod was *asked* to be in. It reads RUNNING from the
    instant of creation, long before a machine is assigned or a 7 GB image is pulled. A
    readiness check that trusts it returns immediately and every later step fails against a
    pod that is not there yet."""
    assert dep.pod_is_ready({"desiredStatus": "RUNNING"}, probe=lambda *_a: True) is False


def test_readiness_requires_a_started_machine_and_a_reachable_endpoint() -> None:
    assert dep.pod_is_ready(dict(READY_RAW), probe=lambda *_a: True) is True
    assert dep.pod_is_ready(dict(READY_RAW), probe=lambda *_a: False) is False
    no_start = {k: v for k, v in READY_RAW.items() if k != "lastStartedAt"}
    assert dep.pod_is_ready(no_start, probe=lambda *_a: True) is False


def test_the_readiness_wait_goes_through_a_mockable_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every HTTP call in this package has a module-level seam so the suite needs no network.
    A bare socket call inside `wait_until_ready` would make the readiness path the one path
    that only ever runs for the first time in production."""
    polls: list[str] = []

    def _get(pod_id: str) -> dict[str, Any]:
        polls.append(pod_id)
        return dict(READY_RAW)

    monkeypatch.setattr(dep, "_get_pod", _get)

    raw = dep.wait_until_ready("p1", timeout_s=60, interval_s=0, probe=lambda *_a: True)

    assert polls == ["p1"]
    assert raw["id"] == "p1"


def test_the_readiness_wait_is_bounded_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded poll holds the process, and therefore the teardown, hostage while the pod
    bills. The deadline must be real."""
    clock = {"now": 0.0}
    monkeypatch.setattr(dep, "_get_pod", lambda _pid: {"desiredStatus": "RUNNING"})

    with pytest.raises(dep.ReadinessTimeout):
        dep.wait_until_ready(
            "p1",
            timeout_s=30,
            interval_s=5,
            probe=lambda *_a: True,
            sleep=lambda s: clock.__setitem__("now", clock["now"] + max(s, 1)),
            monotonic=lambda: clock["now"],
        )


def test_the_readiness_wait_has_a_finite_default_timeout() -> None:
    default = inspect.signature(dep.wait_until_ready).parameters["timeout_s"].default
    assert isinstance(default, int | float) and 0 < default <= 3600, (
        f"timeout_s default must be finite and under an hour, got {default!r}"
    )


def test_a_pod_that_exits_during_boot_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A preempted spot pod, or a container that crashed on the image pull. Polling it to the
    full timeout wastes fifteen minutes and reports the wrong cause."""
    monkeypatch.setattr(dep, "_get_pod", lambda _pid: {"desiredStatus": "EXITED"})
    with pytest.raises(dep.LaunchError, match="EXITED"):
        dep.wait_until_ready("p1", timeout_s=900, probe=lambda *_a: True)


def test_a_pod_that_vanished_during_boot_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dep, "_get_pod", lambda _pid: None)
    with pytest.raises(dep.LaunchError):
        dep.wait_until_ready("p1", timeout_s=900, probe=lambda *_a: True)


# ---------------------------------------------------------------------------
# 6 — the training objective is the other thing that cannot be silently wrong
# ---------------------------------------------------------------------------


def test_the_spec_refuses_any_problem_type_but_multi_label() -> None:
    """The single highest-consequence flag in the launcher. With anything else, HF `Trainer`
    applies softmax cross-entropy to a six-column multi-label target: it trains the wrong
    objective, does not error, and every metric downstream measures the wrong model."""
    with pytest.raises(ValueError, match="multi_label_classification"):
        dep.FinetuneSpec(problem_type="single_label_classification")


def test_the_default_spec_is_multi_label() -> None:
    assert dep.FinetuneSpec().problem_type == "multi_label_classification"


def test_the_train_command_carries_every_non_negotiable() -> None:
    """Each of these is a design decision that is invisible in the output if it silently
    stops being passed.

    `problem_type` and safetensors are deliberately absent: `model/train_distilbert.py`
    defines neither as a flag, and each is enforced there by something that raises
    (`assert_bce_objective`, `assert_safetensors_only`) rather than by a flag a caller can
    forget. Asserting a flag that the trainer does not parse is what produced the launch bug
    this test now guards; `test_every_flag_the_launcher_sends_is_one_the_trainer_parses` is
    the assertion that keeps the two vocabularies together.
    """
    cmd = dep.FinetuneSpec().train_command()
    assert "--weight-decay" in cmd
    assert "--patience" in cmd, "early stopping on validation"
    assert "--train-probe-rows" in cmd, "overfit must be visible per epoch"
    assert "--epochs 3" in cmd
    assert "PYTHONHASHSEED=0" in cmd


def _split_at_shim(command: str) -> tuple[list[str], list[str]]:
    """`(podenv argv, target argv)`. Every remote command is `<shim> [--require …] -- <cmd>`.

    Splitting on the literal `--` is what keeps the two halves honest: the shim's own flags
    must parse against `podenv`, and the trainer's against the trainer, and neither may be
    checked against the other's parser -- which is precisely the mistake that made a launch
    fail with argparse exit 2 after the pod was already billing.
    """
    argv = shlex.split(command)
    index = argv.index("--")
    return argv[:index], argv[index + 1:]


def _emitted_flags(argv: list[str]) -> set[str]:
    return {token for token in argv if token.startswith("--") and token != "--"}


def test_every_flag_the_launcher_sends_is_one_the_trainer_parses() -> None:
    """The launcher and the trainer are written by different people in different files, and
    argparse exits 2 on an unrecognised flag. Sent to a live pod, that mismatch costs the whole
    dead-man-switch window and produces no training step at all -- the failure is expensive,
    silent until the GPU bill, and invisible to every test that checks the command as a string.
    This asserts the command against the real parser instead.
    """
    train_distilbert = pytest.importorskip("model.train_distilbert")
    parser = train_distilbert.build_arg_parser()
    known = {
        option for action in parser._actions for option in action.option_strings  # noqa: SLF001
    }

    spec = dep.FinetuneSpec(expect_split_version="a24b8dd6", fp16=False)
    shim, target = _split_at_shim(spec.train_command())
    unknown = _emitted_flags(target) - known
    assert not unknown, f"train_command emits flags the trainer does not define: {sorted(unknown)}"

    # And the command must actually parse, not merely use known spellings: a flag given a
    # value it cannot accept (an int flag handed a float) also exits 2.
    assert shim[0] == "PYTHONHASHSEED=0"
    assert shim[1:4] == ["python", "-m", "infra.runpod.podenv"]
    assert target[:3] == ["python", "-m", "model.train_distilbert"]
    parser.parse_args(target[3:])


def test_the_shim_half_of_every_remote_command_parses_against_podenv() -> None:
    """The credential shim is itself a program with a parser, and a `--require` it does not
    define fails at exec time on the pod rather than here."""
    podenv = pytest.importorskip("infra.runpod.podenv")
    spec = dep.FinetuneSpec()
    for command in (spec.train_command(), spec.export_command()):
        shim, _ = _split_at_shim(command)
        podenv.build_arg_parser().parse_args(shim[4:])


def test_the_training_command_requires_the_wandb_key_before_it_execs() -> None:
    """The run must not reach the first training step to discover the key is missing: at that
    point a corpus is on a GPU and the pod has been billing for minutes."""
    shim, _ = _split_at_shim(dep.FinetuneSpec().train_command())
    assert "--require" in shim and "WANDB_API_KEY" in shim


def test_the_export_command_is_one_the_onnx_exporter_parses() -> None:
    """The ONNX export is the single highest-risk site for a silent label transposition, and
    it runs on the pod, where a typo in a flag name is a lost checkpoint rather than a
    stacktrace on a laptop."""
    export_onnx = pytest.importorskip("model.export_onnx")
    parser = export_onnx.build_arg_parser()
    known = {
        option for action in parser._actions for option in action.option_strings  # noqa: SLF001
    }
    _, target = _split_at_shim(dep.FinetuneSpec().export_command())
    assert not _emitted_flags(target) - known
    assert target[:3] == ["python", "-m", "model.export_onnx"]
    parser.parse_args(target[3:])


def test_the_train_command_points_at_the_directory_the_bundle_is_delivered_to() -> None:
    """`deliver_dataset` scps a directory named `bundle` into the remote data dir, so the
    trainer's `--cache` has to name exactly that path. Drift here is a `BundleCacheError` on a
    pod that is already billing."""
    assert dep.REMOTE_CACHE_DIR == f"{dep.REMOTE_DATA_DIR}/bundle"
    assert f"--cache {dep.REMOTE_CACHE_DIR}" in dep.FinetuneSpec().train_command()


def test_the_spec_refuses_to_silence_the_per_epoch_gap() -> None:
    with pytest.raises(ValueError, match="train_probe_rows"):
        dep.FinetuneSpec(train_probe_rows=0)


@pytest.mark.parametrize("epochs", [1, 4, 10])
def test_the_spec_holds_the_epoch_budget(epochs: int) -> None:
    with pytest.raises(ValueError):
        dep.FinetuneSpec(epochs=epochs)


def test_the_spec_requires_weight_decay() -> None:
    with pytest.raises(ValueError):
        dep.FinetuneSpec(weight_decay=0.0)


# ---------------------------------------------------------------------------
# 7 — secret hygiene and an inert import
# ---------------------------------------------------------------------------


def test_the_api_key_never_reaches_the_registry_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created: list[dict[str, Any]],
    instant_ready: None,
) -> None:
    monkeypatch.setattr(dep, "load_secret", lambda *_a, **_k: SECRET_MARKER)
    path = tmp_path / "runpod_pods.json"

    _launch(tmp_path, registry_path=path)

    assert SECRET_MARKER not in path.read_text()


def test_no_credential_shaped_key_reaches_the_registry_file(
    tmp_path: Path, created: list[dict[str, Any]], instant_ready: None
) -> None:
    path = tmp_path / "runpod_pods.json"
    _launch(tmp_path, registry_path=path)
    on_disk = path.read_text().lower()
    for leaky in ("hf_", "wandb", "bearer ", "api-key", "api_key", "token", "public_key"):
        assert leaky not in on_disk, f"{leaky!r} appears in the registry file"


def test_creation_reads_the_key_from_pass_at_the_point_of_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never from a shell profile, never from a file this repo writes."""
    asked: list[tuple[str, str]] = []
    posted: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    monkeypatch.setattr(
        dep, "load_secret", lambda p, e: (asked.append((p, e)), FAKE_KEY)[1]
    )

    def _post(url: str, headers: dict[str, str], body: dict[str, Any]) -> _Resp:
        posted.append((url, dict(headers), dict(body)))
        return _Resp(201, {"id": "p1"})

    monkeypatch.setattr(dep, "http_post", _post)

    assert dep._create_pod({"name": "toxic-finetune-a"})["id"] == "p1"

    assert ("runpod/api-key", "RUNPOD_API_KEY") in asked
    url, headers, body = posted[0]
    assert url == f"{dep.REST_BASE}/pods"
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert body["name"] == "toxic-finetune-a"


def test_a_failed_creation_error_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A validation error echoes the payload, and the payload carries the W&B key and the HF
    token. The error string is the leak path."""
    monkeypatch.setattr(dep, "load_secret", lambda *_a, **_k: SECRET_MARKER)
    monkeypatch.setattr(
        dep,
        "http_post",
        lambda *_a, **_k: _Resp(401, text=f"Bearer {SECRET_MARKER} is not authorised"),
    )

    with pytest.raises(dep.LaunchError) as excinfo:
        dep._create_pod({"name": "toxic-finetune-a"})

    assert SECRET_MARKER not in str(excinfo.value)


def test_the_pod_environment_is_built_from_pass_and_pins_determinism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dep, "load_secret", lambda p, _e: f"secret-for-{p}")
    env = dep.build_pod_env(wandb_project="toxic-moderation")
    assert env["PYTHONHASHSEED"] == "0", "the pod must be as deterministic as the Jetson"
    assert env["WANDB_API_KEY"] == "secret-for-wandb/api-key"
    assert env["HF_TOKEN"] == "secret-for-huggingface/token"


def test_importing_the_launcher_reads_no_secret_and_creates_nothing() -> None:
    source = Path(dep.__file__).read_text()
    offenders = [
        line
        for line in source.splitlines()
        if line
        and not line[0].isspace()
        and not line.startswith(("def ", "class ", "#", "from ", "import ", "@"))
        and any(c in line for c in ("load_secret(", "_create_pod(", "launch_pod(", "preflight("))
    ]
    assert not offenders, f"these run at import time: {offenders}"


def test_the_launcher_cli_creates_nothing_without_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created: list[dict[str, Any]], capsys
) -> None:
    """Same discipline as the reaper: with no flags it prints the plan, the price, and the
    spend guard, and creates nothing."""
    monkeypatch.setattr(trm, "_http_get", lambda *_a, **_k: _Resp(200, []))
    monkeypatch.setattr(
        dep,
        "choose_gpu",
        lambda **_k: ("NVIDIA A40", 0.30, "High"),
    )
    monkeypatch.setattr(
        dep,
        "assert_spend_guard",
        lambda **_k: rpc.AccountStatus("u", 5.0, 1000.0, 0.0),
    )
    path = tmp_path / "runpod_pods.json"
    path.write_text("[]")

    code = dep.main(["--registry", str(path)])

    assert code == 0
    assert created == [], "a plan-only run must not create a pod"
    assert "DRY RUN" in capsys.readouterr().out.upper()
