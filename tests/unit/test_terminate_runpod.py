"""Contract tests for the RunPod reaper, `infra/runpod/terminate_runpod.py`.

Ported from the canonical nine-scenario suite in `incident-rank-validation`
(`tests/unit/test_terminate_runpod.py`) and extended for this project's higher ceiling:
$1000 of authorised spend means a single forgotten GPU pod is the largest uncontrolled
cost in Phase 1, and it burns money at a constant rate whether or not anyone is looking.

**These tests are the gate. They exist before any pod is launched, on purpose.**

No network, no credentials, no `pass` binary. Every HTTP call goes through module-level
seams that the tests replace:

    monkeypatch.setattr(trm, "_http_get", fake_get)
    monkeypatch.setattr(trm, "_http_delete", fake_delete)
    monkeypatch.setattr(trm, "load_secret", lambda *_a, **_k: "test-key")

The contract under test, restated so an implementer cannot satisfy the letter and miss the
point:

1. Dry-run is the default. `--execute` is required before a single DELETE leaves the box.
2. The name guard is an allowlist and it fails **closed** — an entry the guard cannot
   classify is skipped, never deleted.
3. Reconcile reports live-but-unregistered pods loudly and never auto-terminates them.
   A human decides, because an orphan may belong to someone else.
4. Termination is idempotent: 404 means the pod is already gone, which is the goal state.
5. One pod's failure never aborts the remaining terminations. A loop that dies on the
   first error leaves every pod after it billing.
6. Secrets load through `pass` with a 5-second timeout, and every error string is scrubbed
   of token-shaped text before it reaches a log, a traceback, or a summary dict.

Tests marked HARDENING assert behaviour the current implementation does not yet have. Each
names the leak it closes. They are not decoration: every one of them describes a path on
which a live GPU survives the process that created it.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from infra.runpod import runpod_client as rpc
from infra.runpod import terminate_runpod as trm

FAKE_KEY = "test-runpod-api-key-xyz"
SECRET_MARKER = "sk-do-not-log-this-value"

# Captured at import, before the autouse fixture below replaces it, so the section that
# exercises `load_secret` itself can put the real implementation back.
_REAL_LOAD_SECRET = trm.load_secret


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class _Resp:
    """Duck-typed stand-in for `runpod_client.Response`, so a test never opens a socket."""

    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this suite may read a real key or shell out to `pass`."""
    monkeypatch.setattr(trm, "load_secret", lambda *_a, **_k: FAKE_KEY)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)


@pytest.fixture
def deleted() -> list[str]:
    return []


@pytest.fixture
def spy_delete(monkeypatch: pytest.MonkeyPatch, deleted: list[str]) -> list[str]:
    """Records every DELETE URL and returns 200. The list is the assertion surface: a test
    that expects no deletion asserts the list is empty, which is stronger than asserting on
    the returned summary alone."""

    def _delete(url: str, headers: dict[str, str]) -> _Resp:
        deleted.append(url)
        return _Resp(204, {})

    monkeypatch.setattr(trm, "_http_delete", _delete)
    return deleted


@pytest.fixture
def real_load_secret(monkeypatch: pytest.MonkeyPatch):
    """Undo the autouse stub for the tests that exercise `load_secret` itself."""
    monkeypatch.setattr(trm, "load_secret", _REAL_LOAD_SECRET)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    return _REAL_LOAD_SECRET


def _registry(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    path = tmp_path / "runpod_pods.json"
    path.write_text(json.dumps(entries, indent=2))
    return path


def _live(monkeypatch: pytest.MonkeyPatch, pods: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(trm, "_http_get", lambda *_a, **_k: _Resp(200, pods))


def _fake_subprocess(run) -> SimpleNamespace:
    """A stand-in for the `subprocess` module that keeps the real exception classes, so the
    implementation's `except subprocess.TimeoutExpired` still matches."""
    return SimpleNamespace(
        run=run,
        TimeoutExpired=subprocess.TimeoutExpired,
        CalledProcessError=subprocess.CalledProcessError,
    )


# ---------------------------------------------------------------------------
# 1 — terminate_pod issues one DELETE to the right URL with a bearer header
# ---------------------------------------------------------------------------


def test_terminate_pod_deletes_the_right_url_with_a_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def _delete(url: str, headers: dict[str, str]) -> _Resp:
        calls.append((url, dict(headers)))
        return _Resp(204, {})

    monkeypatch.setattr(trm, "_http_delete", _delete)

    assert trm.terminate_pod("mypodid123") is True
    assert len(calls) == 1
    url, headers = calls[0]
    assert url == f"{trm.REST_BASE}/pods/mypodid123"
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_rest_base_is_the_v1_rest_surface() -> None:
    """The canonical pattern is REST v1, not the deprecated GraphQL endpoint for pod
    lifecycle. Pinned because a silent move changes every status code this suite relies on."""
    assert trm.REST_BASE == "https://rest.runpod.io/v1"


# ---------------------------------------------------------------------------
# 2 — 404 is idempotent success
# ---------------------------------------------------------------------------


def test_a_missing_pod_is_idempotent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already gone is the goal state. A reaper that raises on 404 cannot be re-run, and a
    reaper that cannot be re-run is a reaper nobody runs twice."""
    monkeypatch.setattr(trm, "_http_delete", lambda *_a, **_k: _Resp(404, text="pod not found"))
    assert trm.terminate_pod("already-gone") is True


def test_terminate_pod_is_idempotent_across_repeated_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter([_Resp(204, {}), _Resp(404, text="gone"), _Resp(404, text="gone")])
    monkeypatch.setattr(trm, "_http_delete", lambda *_a, **_k: next(responses))
    assert [trm.terminate_pod("p1") for _ in range(3)] == [True, True, True]


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_any_2xx_is_success(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setattr(trm, "_http_delete", lambda *_a, **_k: _Resp(status, {}))
    assert trm.terminate_pod("p1") is True


# ---------------------------------------------------------------------------
# 3 — errors raise, with anything token-shaped scrubbed
# ---------------------------------------------------------------------------


def test_a_server_error_raises_with_the_bearer_token_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        trm, "_http_delete", lambda *_a, **_k: _Resp(500, text=f"Bearer {SECRET_MARKER} failed")
    )
    with pytest.raises(trm.TerminateError) as excinfo:
        trm.terminate_pod("p1")
    assert SECRET_MARKER not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 401, 403, 409, 429, 500, 502, 503])
def test_a_non_2xx_non_404_status_raises_terminate_error(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """401 must not be swallowed. A revoked key that silently "succeeds" produces a clean
    summary with every pod still running, which is the worst possible outcome."""
    monkeypatch.setattr(trm, "_http_delete", lambda *_a, **_k: _Resp(status, text="nope"))
    with pytest.raises(trm.TerminateError):
        trm.terminate_pod("p1")


@pytest.mark.parametrize(
    "leaky",
    [
        "Authorization: Bearer sk-live-abcdefgh",
        "key rpa_ABCDEFGHIJKLMNOP0123 rejected",
        "token hf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa refused",
    ],
)
def test_scrub_redacts_every_token_shape_this_project_handles(leaky: str) -> None:
    """Three credentials reach a pod: the RunPod key, the W&B key, the HF token. Any of
    them landing in a public GitHub Actions log is a rotation event."""
    scrubbed = rpc.scrub(leaky)
    assert "[REDACTED]" in scrubbed
    tokens = ("sk-live-abcdefgh", "rpa_ABCDEFGHIJKLMNOP0123", "hf_" + "a" * 30)
    for token in tokens:
        if token in leaky:
            assert token not in scrubbed


def test_list_live_pods_raises_scrubbed_on_a_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        trm, "_http_get", lambda *_a, **_k: _Resp(500, text=f"Bearer {SECRET_MARKER}")
    )
    with pytest.raises(trm.TerminateError) as excinfo:
        trm.list_live_pods()
    assert SECRET_MARKER not in str(excinfo.value)


def test_list_live_pods_accepts_both_the_bare_list_and_the_wrapped_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future response envelope must not silently read as "no pods" and make a leak
    invisible."""
    monkeypatch.setattr(trm, "_http_get", lambda *_a, **_k: _Resp(200, [{"id": "p1"}]))
    assert trm.list_live_pods() == [{"id": "p1"}]
    monkeypatch.setattr(trm, "_http_get", lambda *_a, **_k: _Resp(200, {"pods": [{"id": "p2"}]}))
    assert trm.list_live_pods() == [{"id": "p2"}]


# ---------------------------------------------------------------------------
# 4 — dry-run is the default and issues zero DELETEs
# ---------------------------------------------------------------------------


def test_dry_run_reconcile_issues_no_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    _live(monkeypatch, [{"id": "p1", "name": "toxic-sweep-a"}])
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])

    summary = trm.reconcile(path, execute=False)

    assert spy_delete == []
    assert len(summary["live_and_ours"]) == 1
    assert summary["terminated"] == []


def test_a_dry_run_never_mutates_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    """A plan that edits the file it is planning against is not a plan. The registry is the
    only durable record of a live pod, so a dry run must leave it byte-identical."""
    _live(monkeypatch, [])
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])
    before = path.read_bytes()

    trm.reconcile(path, execute=False)

    assert path.read_bytes() == before


def test_dry_run_terminate_all_registered_issues_no_delete(
    tmp_path: Path, spy_delete: list[str]
) -> None:
    path = _registry(
        tmp_path,
        [
            {"name": "toxic-sweep-a", "pod_id": "pod-aaa"},
            {"name": "toxic-finetune-b", "pod_id": "pod-bbb"},
        ],
    )

    summary = trm.terminate_all_registered(path, execute=False)

    assert spy_delete == []
    assert {e["pod_id"] for e in summary["would_terminate"]} == {"pod-aaa", "pod-bbb"}
    assert summary["terminated"] == []
    assert summary["errors"] == []


@pytest.mark.parametrize("func_name", ["reconcile", "terminate_all_registered"])
def test_execute_is_keyword_only_and_never_defaults_to_true(func_name: str) -> None:
    """Destruction must be typed out at the call site. Either `execute` has no default at
    all (the caller is forced to state intent) or its default is False. A positional
    `execute` invites `reconcile(path, True)` in a hurry at 2 a.m."""
    param = inspect.signature(getattr(trm, func_name)).parameters["execute"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default in (inspect.Parameter.empty, False)


def test_the_cli_with_no_flags_issues_no_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str], capsys
) -> None:
    """`python -m infra.runpod.terminate_runpod` is what a panicking human types first. It
    must be safe: a plan, never an action."""
    _live(monkeypatch, [{"id": "p1", "name": "toxic-sweep-a"}])
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])

    trm.main(["--registry", str(path)])

    assert spy_delete == []
    assert "DRY-RUN" in capsys.readouterr().out.upper()


def test_the_cli_with_execute_issues_the_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    _live(monkeypatch, [{"id": "p1", "name": "toxic-sweep-a"}])
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])

    trm.main(["--registry", str(path), "--execute"])

    assert spy_delete == [f"{trm.REST_BASE}/pods/p1"]


def test_the_cli_exits_non_zero_when_a_termination_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduled reaper workflow is only useful if a failed reap turns the run red. A
    silent exit 0 on a pod that is still billing is worse than no reaper at all."""
    _live(monkeypatch, [{"id": "p1", "name": "toxic-sweep-a"}])
    monkeypatch.setattr(trm, "_http_delete", lambda *_a, **_k: _Resp(500, text="boom"))
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])

    assert trm.main(["--registry", str(path), "--execute"]) != 0


def test_the_cli_exits_non_zero_when_an_orphan_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    """An orphan is money burning with nobody's name on it. The hourly reaper has to make
    that visible as a red run, not as a stderr line in a log nobody opens."""
    _live(monkeypatch, [{"id": "p-unknown", "name": "mystery"}])
    path = _registry(tmp_path, [])

    assert trm.main(["--registry", str(path)]) != 0
    assert spy_delete == []


def test_the_cli_exits_zero_on_a_clean_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    _live(monkeypatch, [])
    path = _registry(tmp_path, [])
    assert trm.main(["--registry", str(path)]) == 0


# ---------------------------------------------------------------------------
# 5 — the name guard is an allowlist and it fails closed
# ---------------------------------------------------------------------------


def test_the_name_guard_refuses_a_pod_outside_the_allowlist(
    tmp_path: Path, spy_delete: list[str]
) -> None:
    path = _registry(tmp_path, [{"name": "someone-elses-pod", "pod_id": "p9"}])

    summary = trm.terminate_all_registered(path, execute=True)

    assert spy_delete == []
    assert summary["skipped_by_guard"] == [{"name": "someone-elses-pod", "pod_id": "p9"}]
    assert summary["terminated"] == []


def test_the_guard_deletes_the_allowed_pod_and_skips_the_rest_in_the_same_run(
    tmp_path: Path, spy_delete: list[str]
) -> None:
    path = _registry(
        tmp_path,
        [
            {"name": "toxic-finetune-a", "pod_id": "pod-guarded"},
            {"name": "customer-prod-db", "pod_id": "pod-unguarded"},
        ],
    )

    summary = trm.terminate_all_registered(path, execute=True)

    assert spy_delete == [f"{trm.REST_BASE}/pods/pod-guarded"]
    assert [e["pod_id"] for e in summary["terminated"]] == ["pod-guarded"]
    assert [e["pod_id"] for e in summary["skipped_by_guard"]] == ["pod-unguarded"]


@pytest.mark.parametrize(
    "name",
    [
        "not-toxic-sweep-a",  # the prefix appears, but not at position 0
        "xtoxic-finetune-a",
        "toxic",
        "",
        "sweep-toxic-a",
    ],
)
def test_the_guard_matches_a_prefix_not_a_substring(
    tmp_path: Path, spy_delete: list[str], name: str
) -> None:
    """`"toxic-sweep-" in name` and `name.startswith("toxic-sweep-")` differ on exactly the
    pods someone else owns. Pinned so a refactor to `in` fails here rather than in the
    RunPod console."""
    path = _registry(tmp_path, [{"name": name, "pod_id": "p9"}])
    summary = trm.terminate_all_registered(path, execute=True)
    assert spy_delete == []
    assert len(summary["skipped_by_guard"]) == 1


def test_default_allow_is_a_prefix_tuple_scoped_to_this_project() -> None:
    assert isinstance(trm.DEFAULT_ALLOW, tuple)
    assert trm.DEFAULT_ALLOW, "an empty allowlist would guard nothing"
    assert all(p and p.startswith("toxic-") for p in trm.DEFAULT_ALLOW), (
        f"every allow-prefix must be scoped to this project, got {trm.DEFAULT_ALLOW}"
    )


def test_an_entry_with_no_name_field_fails_closed(
    tmp_path: Path, spy_delete: list[str]
) -> None:
    """A registry entry the guard cannot classify must be skipped, not deleted. Fail-open
    here deletes an arbitrary pod id out of a corrupt file."""
    path = _registry(tmp_path, [{"pod_id": "p9"}])
    summary = trm.terminate_all_registered(path, execute=True)
    assert spy_delete == []
    assert len(summary["skipped_by_guard"]) == 1


def test_an_entry_with_an_empty_pod_id_never_produces_a_collection_delete(
    tmp_path: Path, spy_delete: list[str]
) -> None:
    """HARDENING. `f"{REST_BASE}/pods/{pod_id}"` with an empty id yields `/v1/pods/`, a
    DELETE against the *collection*. The name guard passes — the name is ours — so nothing
    else stops it. A truncated registry write or a hand-edit is enough to produce the entry,
    and the request that goes out is not the one anybody intended."""
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": ""}])

    summary = trm.terminate_all_registered(path, execute=True)

    assert all(not url.rstrip("/").endswith("/pods") for url in spy_delete), (
        f"a DELETE was issued against the pod collection: {spy_delete}"
    )
    assert spy_delete == []
    assert summary["terminated"] == []


def test_a_custom_allowlist_is_honoured(tmp_path: Path, spy_delete: list[str]) -> None:
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])
    summary = trm.terminate_all_registered(
        path, name_allow_prefixes=("something-else-",), execute=True
    )
    assert spy_delete == []
    assert len(summary["skipped_by_guard"]) == 1


# ---------------------------------------------------------------------------
# 6 — reconcile is orphan-safe
# ---------------------------------------------------------------------------


def test_orphans_are_reported_and_never_auto_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str], capsys
) -> None:
    _live(
        monkeypatch,
        [{"id": "p1", "name": "toxic-sweep-a"}, {"id": "p2", "name": "mystery-pod"}],
    )
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])

    summary = trm.reconcile(path, execute=True)

    assert [o["id"] for o in summary["orphans"]] == ["p2"]
    assert spy_delete == [f"{trm.REST_BASE}/pods/p1"]
    assert "ORPHAN" in capsys.readouterr().err


def test_an_orphan_whose_name_matches_the_allowlist_is_still_not_auto_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    """The registry is the authority, not the name. A live `toxic-sweep-*` pod that this
    process never registered was created by something else — a concurrent run, a colleague,
    a half-finished launch — and killing it silently destroys their work."""
    _live(monkeypatch, [{"id": "p-unknown", "name": "toxic-sweep-from-another-run"}])
    path = _registry(tmp_path, [])

    summary = trm.reconcile(path, execute=True)

    assert [o["id"] for o in summary["orphans"]] == ["p-unknown"]
    assert spy_delete == []


def test_an_orphan_is_reported_in_dry_run_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    _live(monkeypatch, [{"id": "orphan-xyz", "name": "external", "desiredStatus": "RUNNING"}])
    path = _registry(tmp_path, [])

    summary = trm.reconcile(path, execute=False)

    assert len(summary["orphans"]) == 1
    assert spy_delete == []


def test_the_orphan_report_tells_the_operator_what_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str], capsys
) -> None:
    """A warning without a next step is a warning that gets ignored. The one thing the
    reaper refuses to do automatically is the one thing it must explain how to do by hand."""
    _live(monkeypatch, [{"id": "orphan-xyz", "name": "external", "costPerHr": 0.34}])
    path = _registry(tmp_path, [])

    trm.reconcile(path, execute=False)

    err = capsys.readouterr().err
    assert "orphan-xyz" in err
    assert "--force" in err and "--pod-id" in err


def test_reconcile_partitions_registered_gone_live_and_ours_and_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    _live(
        monkeypatch,
        [{"id": "live-ours", "name": "toxic-sweep-a"}, {"id": "live-orphan", "name": "other"}],
    )
    path = _registry(
        tmp_path,
        [
            {"name": "toxic-sweep-a", "pod_id": "live-ours"},
            {"name": "toxic-sweep-b", "pod_id": "already-gone"},
        ],
    )

    summary = trm.reconcile(path, execute=False)

    assert [e["pod_id"] for e in summary["live_and_ours"]] == ["live-ours"]
    assert [e["pod_id"] for e in summary["registered_gone"]] == ["already-gone"]
    assert [o["id"] for o in summary["orphans"]] == ["live-orphan"]


def test_reconcile_applies_the_name_guard_to_registered_pods_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    """A hand-edited registry must not become a delete-anything primitive."""
    _live(monkeypatch, [{"id": "p1", "name": "prod-database"}])
    path = _registry(tmp_path, [{"name": "prod-database", "pod_id": "p1"}])

    summary = trm.reconcile(path, execute=True)

    assert spy_delete == []
    assert [e["pod_id"] for e in summary["skipped_by_guard"]] == ["p1"]


def test_reconcile_survives_a_malformed_registry_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    """A `KeyError` on entry 1 aborts reconcile, so entries 2..n never get reaped. Corrupt
    input must degrade to "skip that row", not "abandon the sweep"."""
    _live(monkeypatch, [{"id": "p2", "name": "toxic-sweep-b"}])
    path = _registry(
        tmp_path, [{"name": "toxic-sweep-a"}, {"name": "toxic-sweep-b", "pod_id": "p2"}]
    )

    summary = trm.reconcile(path, execute=True)

    assert [e["pod_id"] for e in summary["terminated"]] == ["p2"]


def test_reconcile_survives_a_live_pod_with_no_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    """The API response is untrusted input. One odd record must not stop the reap of
    everything else."""
    _live(monkeypatch, [{"name": "no-id-here"}, {"id": "p1", "name": "toxic-sweep-a"}])
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])

    summary = trm.reconcile(path, execute=True)

    assert [e["pod_id"] for e in summary["terminated"]] == ["p1"]


# ---------------------------------------------------------------------------
# 7 — a partial failure never aborts the remaining terminations
# ---------------------------------------------------------------------------


def test_a_failing_pod_is_recorded_and_the_loop_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most expensive bug available here: pod 1 500s, the exception escapes the
    loop, and pods 2..n keep billing until someone notices."""
    seen: list[str] = []

    def _delete(url: str, headers: dict[str, str]) -> _Resp:
        seen.append(url)
        if "pod-bad" in url:
            return _Resp(500, text="internal error")
        return _Resp(204, {})

    monkeypatch.setattr(trm, "_http_delete", _delete)
    path = _registry(
        tmp_path,
        [
            {"name": "toxic-sweep-a", "pod_id": "pod-bad"},
            {"name": "toxic-sweep-b", "pod_id": "pod-good"},
        ],
    )

    summary = trm.terminate_all_registered(path, execute=True)

    assert [e["pod_id"] for e in summary["errors"]] == ["pod-bad"]
    assert [e["pod_id"] for e in summary["terminated"]] == ["pod-good"]
    assert any("pod-good" in url for url in seen)


def test_a_pod_that_failed_to_terminate_stays_in_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning a pod the API refused to delete erases the only record that would have found
    it. Prune what is confirmed gone; keep what is not."""

    def _delete(url: str, headers: dict[str, str]) -> _Resp:
        return _Resp(500, text="internal error") if "pod-bad" in url else _Resp(204, {})

    monkeypatch.setattr(trm, "_http_delete", _delete)
    path = _registry(
        tmp_path,
        [
            {"name": "toxic-sweep-a", "pod_id": "pod-bad"},
            {"name": "toxic-sweep-b", "pod_id": "pod-good"},
        ],
    )

    trm.terminate_all_registered(path, execute=True)

    remaining = {e["pod_id"] for e in rpc.read_registry(path)}
    assert "pod-bad" in remaining, "a failed termination must stay on the books"
    assert "pod-good" not in remaining


def test_an_unexpected_exception_from_one_pod_does_not_abort_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DNS failure, a TLS error, a malformed body — none of them are `TerminateError`, and
    any one of them leaves every later pod alive if the handler is too narrow."""

    def _delete(url: str, headers: dict[str, str]) -> _Resp:
        if "pod-bad" in url:
            raise OSError("simulated transport failure")
        return _Resp(204, {})

    monkeypatch.setattr(trm, "_http_delete", _delete)
    path = _registry(
        tmp_path,
        [
            {"name": "toxic-sweep-a", "pod_id": "pod-bad"},
            {"name": "toxic-sweep-b", "pod_id": "pod-good"},
        ],
    )

    summary = trm.terminate_all_registered(path, execute=True)

    assert [e["pod_id"] for e in summary["errors"]] == ["pod-bad"]
    assert "simulated transport failure" in summary["errors"][0]["error"]
    assert [e["pod_id"] for e in summary["terminated"]] == ["pod-good"]


def test_a_partial_failure_inside_reconcile_also_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _delete(url: str, headers: dict[str, str]) -> _Resp:
        return _Resp(503, text="unavailable") if "pod-bad" in url else _Resp(204, {})

    monkeypatch.setattr(trm, "_http_delete", _delete)
    _live(
        monkeypatch,
        [{"id": "pod-bad", "name": "toxic-sweep-a"}, {"id": "pod-good", "name": "toxic-sweep-b"}],
    )
    path = _registry(
        tmp_path,
        [
            {"name": "toxic-sweep-a", "pod_id": "pod-bad"},
            {"name": "toxic-sweep-b", "pod_id": "pod-good"},
        ],
    )

    summary = trm.reconcile(path, execute=True)

    assert [e["pod_id"] for e in summary["errors"]] == ["pod-bad"]
    assert [e["pod_id"] for e in summary["terminated"]] == ["pod-good"]


def test_a_recorded_error_is_scrubbed_of_bearer_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary dict is printed, logged, and in CI ends up in a public Actions log.
    Scrubbing the exception but not the summary entry leaks the key anyway."""
    monkeypatch.setattr(
        trm, "_http_delete", lambda *_a, **_k: _Resp(500, text=f"Bearer {SECRET_MARKER} rejected")
    )
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])

    summary = trm.terminate_all_registered(path, execute=True)

    assert SECRET_MARKER not in json.dumps(summary)


# ---------------------------------------------------------------------------
# 8 — the registry never crashes the reaper, and never lies quietly
# ---------------------------------------------------------------------------


def test_a_missing_registry_file_yields_an_empty_plan(tmp_path: Path) -> None:
    summary = trm.terminate_all_registered(tmp_path / "does-not-exist.json", execute=False)
    assert summary == {
        "would_terminate": [],
        "terminated": [],
        "skipped_by_guard": [],
        "errors": [],
    }


@pytest.mark.parametrize("body", ["", "   \n\n  "])
def test_an_empty_or_whitespace_registry_yields_an_empty_plan(tmp_path: Path, body: str) -> None:
    path = tmp_path / "runpod_pods.json"
    path.write_text(body)
    assert trm.terminate_all_registered(path, execute=False)["would_terminate"] == []


def test_a_corrupt_registry_fails_loudly_rather_than_silently_reaping_nothing(
    tmp_path: Path,
) -> None:
    """Truncated JSON is the expected artefact of a crash mid-write, and it is exactly when
    pods are most likely to be live. Returning `[]` would report "nothing to reap" while a
    GPU bills; the operator must be told to look in the console instead."""
    path = tmp_path / "runpod_pods.json"
    path.write_text('[{"name": "toxic-sweep-a", "pod_i')

    with pytest.raises(ValueError):  # json.JSONDecodeError subclasses ValueError
        rpc.read_registry(path)


def test_read_registry_returns_the_entries_in_file_order(tmp_path: Path) -> None:
    entries = [
        {"name": "toxic-sweep-a", "pod_id": "p1"},
        {"name": "toxic-sweep-b", "pod_id": "p2"},
    ]
    assert rpc.read_registry(_registry(tmp_path, entries)) == entries


def test_the_registry_write_is_atomic_write_temp_fsync_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-written registry is worse than no registry: parsing raises, the reaper refuses
    to guess, and the pod bills. Write to a temp file, fsync it, then `os.replace`, which is
    atomic within a filesystem. Order matters — an fsync after the rename does not make the
    rename durable."""
    import os

    path = tmp_path / "runpod_pods.json"
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    def spy_replace(src: Any, dst: Any) -> None:
        order.append("replace")
        assert Path(src) != Path(dst), "replace must move a temp file onto the target"
        assert json.loads(Path(src).read_text())[0]["pod_id"] == "p1", (
            "the temp file must hold complete JSON before the rename"
        )
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)

    rpc.append_registry(path, {"name": "toxic-sweep-a", "pod_id": "p1"})

    assert "replace" in order, "the registry must land via os.replace, not a plain write"
    assert "fsync" in order, "the temp file must be fsynced before the rename"
    assert order.index("fsync") < order.index("replace")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["runpod_pods.json"]


def test_appending_a_second_pod_preserves_the_first(tmp_path: Path) -> None:
    """A sweep is several pods. An append that truncates loses the record of everything
    launched before it, and those pods become orphans the reaper refuses to touch."""
    path = tmp_path / "runpod_pods.json"
    rpc.append_registry(path, {"name": "toxic-sweep-a", "pod_id": "p1"})
    rpc.append_registry(path, {"name": "toxic-sweep-b", "pod_id": "p2"})
    assert [e["pod_id"] for e in rpc.read_registry(path)] == ["p1", "p2"]


def test_removing_one_pod_preserves_a_concurrently_written_entry(tmp_path: Path) -> None:
    """Two launchers can share a registry. Teardown must delete its own row, not rewrite the
    file to whatever it happened to read at start-up."""
    path = _registry(
        tmp_path,
        [
            {"name": "toxic-sweep-a", "pod_id": "p1"},
            {"name": "toxic-sweep-b", "pod_id": "p2"},
        ],
    )
    rpc.remove_from_registry(path, {"p1"})
    assert [e["pod_id"] for e in rpc.read_registry(path)] == ["p2"]


# ---------------------------------------------------------------------------
# 9 — CLI single-pod path: force is required to touch an untracked pod
# ---------------------------------------------------------------------------


def test_the_cli_refuses_an_untracked_pod_without_force(
    tmp_path: Path, spy_delete: list[str], capsys
) -> None:
    """The recovery path in the RUNBOOK. A pod id typed by hand is untrusted: if the registry
    has never heard of it, the reaper stops and says so."""
    path = _registry(tmp_path, [])

    code = trm.main(["--pod-id", "untracked-pod-xyz", "--execute", "--registry", str(path)])

    assert code == 1
    assert spy_delete == [], "DELETE must not be issued when the guard fires"
    err = capsys.readouterr().err
    assert "unrecognized arguments" not in err, (
        "the CLI must implement --pod-id, not reject it: an argparse usage error would "
        "satisfy the exit code while leaving the operator with no force-terminate path"
    )
    assert "--force" in err, "the message must tell the operator how to proceed"


def test_the_cli_terminates_an_untracked_pod_when_force_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    _live(monkeypatch, [])
    path = _registry(tmp_path, [])

    code = trm.main(
        ["--pod-id", "untracked-pod-xyz", "--execute", "--force", "--registry", str(path)]
    )

    assert code == 0
    assert spy_delete == [f"{trm.REST_BASE}/pods/untracked-pod-xyz"]


def test_the_cli_terminates_a_registered_allowed_pod_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    _live(monkeypatch, [])
    path = _registry(tmp_path, [{"name": "toxic-finetune-a", "pod_id": "reg-pod-abc"}])

    code = trm.main(["--pod-id", "reg-pod-abc", "--execute", "--registry", str(path)])

    assert code == 0
    assert spy_delete == [f"{trm.REST_BASE}/pods/reg-pod-abc"]
    assert rpc.read_registry(path) == [], "a confirmed-gone pod is pruned"


def test_the_cli_refuses_a_registered_pod_whose_name_is_outside_the_allowlist(
    tmp_path: Path, spy_delete: list[str]
) -> None:
    path = _registry(tmp_path, [{"name": "prod-database", "pod_id": "reg-pod-abc"}])

    code = trm.main(["--pod-id", "reg-pod-abc", "--execute", "--registry", str(path)])

    assert code == 1
    assert spy_delete == []


def test_the_cli_single_pod_path_is_dry_run_without_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy_delete: list[str]
) -> None:
    gets: list[str] = []
    monkeypatch.setattr(
        trm, "_http_get", lambda url, *_a, **_k: (gets.append(url), _Resp(200, []))[1]
    )
    path = _registry(tmp_path, [])

    trm.main(["--pod-id", "some-pod", "--registry", str(path)])

    assert spy_delete == []
    assert gets == [], "a dry run must not need the network at all"


# ---------------------------------------------------------------------------
# 10 — a DELETE is a claim; assert_no_survivors is the evidence
# ---------------------------------------------------------------------------


def test_assert_no_survivors_passes_when_the_pod_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _live(monkeypatch, [])
    trm.assert_no_survivors({"p1"})


def test_assert_no_survivors_raises_when_the_pod_is_still_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 204 from DELETE is the API's claim, not proof. The re-query is the proof, and it
    is the difference between "we tore down" and "we believe we tore down"."""
    _live(monkeypatch, [{"id": "p1", "name": "toxic-sweep-a"}])

    with pytest.raises(trm.SurvivingPodsError) as excinfo:
        trm.assert_no_survivors({"p1"})

    assert "p1" in str(excinfo.value)
    assert "terminate_runpod" in str(excinfo.value), "the message must carry the fix command"


# ---------------------------------------------------------------------------
# 11 — secret loading: 5-second timeout, redacted failures, no argv exposure
# ---------------------------------------------------------------------------


def test_load_secret_prefers_the_environment_variable_and_never_shells_out(
    monkeypatch: pytest.MonkeyPatch, real_load_secret
) -> None:
    def _run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("pass must not be invoked when the env var is set")

    monkeypatch.setenv("RUNPOD_API_KEY", "from-env")
    monkeypatch.setattr(rpc, "subprocess", _fake_subprocess(_run))

    assert rpc.load_secret("runpod/api-key", "RUNPOD_API_KEY") == "from-env"


def test_load_secret_shells_out_to_pass_with_a_five_second_timeout(
    monkeypatch: pytest.MonkeyPatch, real_load_secret
) -> None:
    """A hung `pass` — a locked GPG agent waiting on a pinentry that will never appear —
    must not wedge the launcher forever with a pod already running."""
    seen: dict[str, Any] = {}

    def _run(cmd: list[str], **kwargs: Any) -> Any:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(stdout="  the-key  \n", stderr="", returncode=0)

    monkeypatch.setattr(rpc, "subprocess", _fake_subprocess(_run))

    assert rpc.load_secret("runpod/api-key", "RUNPOD_API_KEY") == "the-key"
    assert seen["cmd"] == ["pass", "show", "runpod/api-key"]
    assert seen["kwargs"]["timeout"] == 5
    assert seen["kwargs"].get("capture_output") is True
    assert seen["kwargs"].get("text") is True


def test_load_secret_never_puts_the_secret_in_argv(
    monkeypatch: pytest.MonkeyPatch, real_load_secret
) -> None:
    """`ps aux` is readable by every process on this box. The secret comes back on stdout;
    it must never be an argument, and `shell=True` must never appear."""
    seen: dict[str, Any] = {}

    def _run(cmd: list[str], **kwargs: Any) -> Any:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return SimpleNamespace(stdout=SECRET_MARKER, stderr="", returncode=0)

    monkeypatch.setattr(rpc, "subprocess", _fake_subprocess(_run))

    rpc.load_secret("runpod/api-key", "RUNPOD_API_KEY")

    assert SECRET_MARKER not in " ".join(seen["cmd"])
    assert seen["cmd"][0] == "pass"
    assert seen["kwargs"].get("shell") in (None, False)


def test_load_secret_raises_a_redacted_error_on_timeout(
    monkeypatch: pytest.MonkeyPatch, real_load_secret
) -> None:
    def _run(cmd: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5, output=SECRET_MARKER.encode())

    monkeypatch.setattr(rpc, "subprocess", _fake_subprocess(_run))

    with pytest.raises(RuntimeError) as excinfo:
        rpc.load_secret("runpod/api-key", "RUNPOD_API_KEY")

    message = str(excinfo.value)
    assert "timed out" in message.lower()
    assert "5" in message
    assert SECRET_MARKER not in message
    assert excinfo.value.__cause__ is None, (
        "chain the exception and the captured output reappears in the traceback; "
        "`raise ... from None` is what keeps it out"
    )


def test_load_secret_raises_a_redacted_error_when_pass_fails(
    monkeypatch: pytest.MonkeyPatch, real_load_secret
) -> None:
    def _run(cmd: list[str], **kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(
            returncode=2, cmd=cmd, output=SECRET_MARKER, stderr=f"leaked {SECRET_MARKER}"
        )

    monkeypatch.setattr(rpc, "subprocess", _fake_subprocess(_run))

    with pytest.raises(RuntimeError) as excinfo:
        rpc.load_secret("runpod/api-key", "RUNPOD_API_KEY")

    message = str(excinfo.value)
    assert SECRET_MARKER not in message
    assert "runpod/api-key" in message
    assert excinfo.value.__cause__ is None


def test_load_secret_refuses_an_empty_value(
    monkeypatch: pytest.MonkeyPatch, real_load_secret
) -> None:
    """An empty secret produces `Authorization: Bearer `, which the API answers 401 to. That
    reads as "the reaper ran and found nothing" unless the empty value fails first."""
    monkeypatch.setattr(
        rpc,
        "subprocess",
        _fake_subprocess(lambda *_a, **_k: SimpleNamespace(stdout="\n", stderr="", returncode=0)),
    )
    with pytest.raises(RuntimeError):
        rpc.load_secret("runpod/api-key", "RUNPOD_API_KEY")


def test_headers_carry_the_key_and_nothing_writes_it_anywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The key belongs in exactly one place: the Authorization header of a live request. It
    must never reach the registry file, which is committed and world-readable."""
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])
    monkeypatch.setattr(trm, "load_secret", lambda *_a, **_k: SECRET_MARKER)
    monkeypatch.setattr(trm, "_http_delete", lambda *_a, **_k: _Resp(204, {}))

    assert trm._headers()["Authorization"] == f"Bearer {SECRET_MARKER}"

    trm.terminate_all_registered(path, execute=True)
    assert SECRET_MARKER not in path.read_text()


def test_redact_env_replaces_every_value() -> None:
    """A pod payload carries `WANDB_API_KEY` and `HF_TOKEN`. Printing it for debugging is a
    normal thing to want and must not be a rotation event."""
    redacted = rpc.redact_env({"HF_TOKEN": SECRET_MARKER, "WANDB_API_KEY": SECRET_MARKER})
    assert SECRET_MARKER not in json.dumps(redacted)
    assert set(redacted) == {"HF_TOKEN", "WANDB_API_KEY"}


# ---------------------------------------------------------------------------
# 12 — importing the module is inert
# ---------------------------------------------------------------------------


def test_importing_the_reaper_reads_no_secret_and_makes_no_request() -> None:
    """Import-time side effects turn `--help` into a credential read and a network call. The
    module is imported by this suite and by a GitHub Actions job with no key set, and
    neither may fail or hang."""
    source = Path(trm.__file__).read_text()
    offenders = [
        line
        for line in source.splitlines()
        if line
        and not line[0].isspace()
        and not line.startswith(("def ", "class ", "#", "from ", "import "))
        and any(call in line for call in ("load_secret(", "_headers()", "list_live_pods("))
    ]
    assert not offenders, f"these run at import time: {offenders}"


def test_the_reaper_does_not_import_the_launcher() -> None:
    """Layering, and it is load-bearing. The reaper must still run when `deploy_runpod` is
    broken, half-edited, or absent — which is the state the machine is in when a launch
    crashed and somebody needs to stop the meter."""
    source = Path(trm.__file__).read_text()
    assert "deploy_runpod" not in source.split('"""', 2)[-1], (
        "terminate_runpod must not depend on deploy_runpod"
    )


def test_the_default_registry_path_lives_under_infra_runpod() -> None:
    """The RUNBOOK, the reaper workflow, and the launcher must all agree on one path, or the
    reaper reads an empty file while pods bill."""
    parts = Path(trm.DEFAULT_REGISTRY).parts
    assert parts[-3:-1] == ("infra", "runpod")
    assert parts[-1].endswith(".json")
