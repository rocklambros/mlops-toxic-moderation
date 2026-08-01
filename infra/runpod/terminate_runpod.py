"""Safe RunPod teardown: name-guarded, orphan-safe, dry-run by default.

Ported from the canonical lifecycle in `incident-rank-validation`
(`tools/terminate_runpod.py`), with the hardening its own suite and this project's premortem
added.

A forgotten GPU pod is the largest uncontrolled cost in Phase 1, and it is uncontrolled
precisely because nobody is watching when it happens. This module is the thing that watches.
It imports only `runpod_client`, never the launcher, so it keeps working when the launcher is
broken, half-edited, or absent -- which is the state the machine is in when a launch crashed
and somebody needs to stop the meter. It is stdlib-only for the same reason: it has to run
from a bare CI container with no install step.

Six rules, none negotiable:

1. **Dry-run by default.** `python -m infra.runpod.terminate_runpod` issues zero DELETEs and
   prints the plan. `--execute` is required before one destructive call leaves the box.
2. **The name guard is an allowlist that fails closed.** Only pods whose name starts with a
   prefix this project owns may be terminated, and an entry the guard cannot classify -- no
   name, empty name, empty pod id -- is skipped rather than deleted.
3. **Orphan-safe reconcile.** A live pod absent from the registry is reported loudly and
   never auto-terminated, in either mode. The registry is the authority, not the name: a live
   `toxic-*` pod this process never recorded belongs to a concurrent run, a colleague, or a
   half-finished launch, and killing it silently destroys their work.
4. **404 on DELETE is success.** Already-gone is the goal state. Confirmed against the live
   API on 2026-07-31: a missing pod answers `404 {"error": "pod not found"}`, and DELETE
   documents 204 for success.
5. **One pod's failure never aborts the loop.** Every later pod is still billing, so errors
   are collected per entry -- `except Exception`, not a curated list of HTTP types, because a
   DNS failure and a malformed body are neither -- and the sweep continues.
   `KeyboardInterrupt` and `SystemExit` still propagate: neither derives from `Exception`.
6. **Anything token-shaped is scrubbed from every string that escapes**, including the
   summary dict, which gets printed into a public GitHub Actions log.

Every HTTP call goes through a module-level seam, so the tests need no network::

    monkeypatch.setattr(terminate_runpod, "_http_get", fake_get)
    monkeypatch.setattr(terminate_runpod, "_http_delete", fake_delete)
    monkeypatch.setattr(terminate_runpod, "load_secret", lambda *_a, **_k: "test-key")
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from infra.runpod.runpod_client import (
    REST_BASE,
    auth_headers,
    http_delete,
    http_get,
    load_secret,
    read_registry,
    remove_from_registry,
    scrub,
)

# The launcher names every pod with one of these prefixes and refuses to create any other
# name, so the two halves cannot drift apart. A prefix match, never a substring match: the
# difference between `startswith` and `in` is exactly the set of pods somebody else owns.
DEFAULT_ALLOW: tuple[str, ...] = ("toxic-finetune-", "toxic-sweep-")

# One path, shared with the launcher. Two paths would mean pods are recorded in a file the
# reaper never reads, which is indistinguishable from having no registry at all.
DEFAULT_REGISTRY = Path("infra/runpod/runpod_pods.json")

# Rebound at module level so a test can replace them without touching `runpod_client`, and so
# every call below goes through the patched name. Nothing here may call the imported
# functions directly: that would bypass the seam and hit the wire.
_http_get = http_get
_http_delete = http_delete


class TerminateError(RuntimeError):
    """A non-recoverable HTTP error during termination."""


class SurvivingPodsError(RuntimeError):
    """Pods are still live after teardown claimed success. SEV-1: the meter is running."""


def _headers() -> dict[str, str]:
    """Bearer headers, reading the key through this module's `load_secret` name."""
    return auth_headers(load_secret("runpod/api-key", "RUNPOD_API_KEY"))


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------


def list_live_pods() -> list[dict[str, Any]]:
    """`GET /pods` -> the live pod list.

    The live API returns a bare JSON array (confirmed 2026-07-31; the OpenAPI `Pods` schema is
    `type: array`), but a `{"pods": [...]}` envelope is tolerated too, so a future API change
    degrades to "still reports pods" rather than to "reports none", which would make a leak
    invisible at exactly the moment it matters.
    """
    resp = _http_get(f"{REST_BASE}/pods", _headers())
    if resp.status_code != 200:
        raise TerminateError(
            scrub(f"list_live_pods failed ({resp.status_code}): {resp.text[:300]}")
        )
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return list(data.get("pods", []))
    return []


def terminate_pod(pod_id: str) -> bool:
    """`DELETE /pods/{id}`. Idempotent: an already-gone pod is success, not an error.

    Any 2xx or a 404 is True; everything else raises. 401 raises loudest of all, because a
    revoked key that silently "succeeds" produces a clean summary with every pod still
    running, which is the worst outcome available here.
    """
    if not str(pod_id).strip():
        # `f"{REST_BASE}/pods/{pod_id}"` with an empty id is a DELETE against the pod
        # *collection*. On an API that interprets that generously it is an account-wide wipe,
        # and a truncated registry write or a hand-edit is enough to produce the entry.
        raise TerminateError("terminate_pod called with an empty pod id; refusing to issue it")
    resp = _http_delete(f"{REST_BASE}/pods/{pod_id}", _headers())
    if resp.status_code == 404 or 200 <= resp.status_code < 300:
        return True
    raise TerminateError(
        scrub(f"terminate_pod({pod_id}) failed ({resp.status_code}): {resp.text[:300]}")
    )


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def _allowed(entry: dict[str, Any], prefixes: tuple[str, ...]) -> bool:
    """Fail closed. An entry with no name, an empty name, or no pod id is unclassifiable."""
    name = entry.get("name")
    pod_id = entry.get("pod_id")
    if not isinstance(name, str) or not name:
        return False
    if not isinstance(pod_id, str) or not pod_id.strip():
        return False
    return name.startswith(prefixes)


def _record(entry: dict[str, Any]) -> dict[str, Any]:
    """The two fields every summary row carries, normalised out of an untrusted entry."""
    return {"name": entry.get("name", ""), "pod_id": entry.get("pod_id", "")}


def _terminate_entries(
    entries: list[dict[str, Any]],
    summary: dict[str, Any],
    prefixes: tuple[str, ...],
    *,
    execute: bool,
) -> None:
    """Shared inner loop for `terminate_all_registered` and `reconcile`.

    Errors are collected rather than raised so that one pod's failure never leaves the pods
    after it billing, and every error string is scrubbed on the way into the summary as well
    as on the way to stderr -- scrubbing the exception but not the summary leaks the key
    anyway, because the summary is what gets printed in CI.
    """
    for entry in entries:
        record = _record(entry)
        if not _allowed(entry, prefixes):
            print(
                f"  SKIP (guard): {record['name']!r} is not in the allowlist {prefixes} -- "
                f"refusing to touch pod {record['pod_id']!r}",
                file=sys.stderr,
            )
            summary["skipped_by_guard"].append(record)
            continue
        if not execute:
            summary["would_terminate"].append(record)
            continue
        try:
            terminate_pod(record["pod_id"])
        except Exception as exc:  # noqa: BLE001 - one failure must not abort the rest
            summary["errors"].append({**record, "error": scrub(str(exc))})
            print(
                f"  ERROR: {record['name']} ({record['pod_id']}): {scrub(str(exc))}",
                file=sys.stderr,
            )
            continue
        summary["terminated"].append(record)
        print(f"  TERMINATED: {record['name']} ({record['pod_id']})")


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def terminate_all_registered(
    registry_path: Path | str = DEFAULT_REGISTRY,
    *,
    name_allow_prefixes: tuple[str, ...] = DEFAULT_ALLOW,
    execute: bool = False,
) -> dict[str, Any]:
    """Terminate every registry pod that passes the name guard.

    `execute=False` returns the plan and issues no DELETE, and `execute` is keyword-only so
    destruction has to be typed out at the call site: a positional flag invites
    `terminate_all_registered(path, True)` in a hurry at 2 a.m.

    Confirmed-terminated pods are pruned from the registry; pods that errored are deliberately
    left on the books, because that record is the only thing standing between a failed
    teardown and an invisible leak.
    """
    summary: dict[str, Any] = {
        "would_terminate": [],
        "terminated": [],
        "skipped_by_guard": [],
        "errors": [],
    }
    _terminate_entries(
        read_registry(registry_path), summary, name_allow_prefixes, execute=execute
    )
    if execute and summary["terminated"]:
        remove_from_registry(registry_path, {e["pod_id"] for e in summary["terminated"]})
    return summary


def reconcile(
    registry_path: Path | str = DEFAULT_REGISTRY,
    *,
    execute: bool = False,
    name_allow_prefixes: tuple[str, ...] = DEFAULT_ALLOW,
) -> dict[str, Any]:
    """Cross-check the registry against what is actually live, then optionally terminate.

    Three partitions, and the third is the one that matters:

    - `live_and_ours`   in the registry and live -> terminated when `execute=True`, subject to
      the same name guard, because a hand-edited registry must not become a delete-anything
      primitive.
    - `registered_gone` in the registry, already gone -> nothing to do.
    - `orphans`         live but *not* in the registry -> printed loudly to stderr, with the
      exact command to kill one by hand, and never auto-terminated in either mode. A warning
      without a next step is a warning that gets ignored.

    Both inputs are untrusted. A registry row with no `pod_id` and an API record with no `id`
    are classified without raising, so one odd row cannot abandon the sweep. A dry run never
    writes: a plan that edits the file it is planning against is not a plan.
    """
    registered = read_registry(registry_path)
    registered_ids = {str(e.get("pod_id", "")) for e in registered if e.get("pod_id")}
    live = list_live_pods()
    live_ids = {str(p.get("id", "")) for p in live if p.get("id")}

    summary: dict[str, Any] = {
        "registered_gone": [e for e in registered if str(e.get("pod_id", "")) not in live_ids],
        "live_and_ours": [e for e in registered if str(e.get("pod_id", "")) in live_ids],
        "orphans": [p for p in live if str(p.get("id", "")) not in registered_ids],
        "would_terminate": [],
        "terminated": [],
        "skipped_by_guard": [],
        "errors": [],
    }

    if summary["orphans"]:
        print("\n*** ORPHAN PODS DETECTED - not auto-terminated ***", file=sys.stderr)
        for orphan in summary["orphans"]:
            print(
                f"    ORPHAN id={orphan.get('id', '?')} name={orphan.get('name', '?')} "
                f"status={orphan.get('desiredStatus', '?')} "
                f"costPerHr={orphan.get('costPerHr', '?')}",
                file=sys.stderr,
            )
        print(
            "*** A human decides. To kill one by hand:\n"
            "***   python -m infra.runpod.terminate_runpod "
            "--pod-id <ID> --execute --force\n",
            file=sys.stderr,
        )

    if execute:
        _terminate_entries(
            summary["live_and_ours"], summary, name_allow_prefixes, execute=True
        )
        gone = {str(e.get("pod_id", "")) for e in summary["registered_gone"] if e.get("pod_id")}
        gone |= {e["pod_id"] for e in summary["terminated"]}
        if gone:
            remove_from_registry(registry_path, gone)

    return summary


def assert_no_survivors(pod_ids: set[str]) -> None:
    """Re-query the API and raise if any of `pod_ids` is still live.

    A 204 from DELETE is the API's claim, not proof. This is the proof, and it is the
    difference between "we tore down" and "we believe we tore down" -- a distinction that
    costs money for as long as nobody checks.
    """
    survivors = {pid for pid in pod_ids if pid} & {
        str(p.get("id", "")) for p in list_live_pods()
    }
    if survivors:
        raise SurvivingPodsError(
            f"SEV-1: {len(survivors)} pod(s) still live after teardown: {sorted(survivors)}. "
            "They are billing now. Run: "
            "python -m infra.runpod.terminate_runpod --execute"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m infra.runpod.terminate_runpod",
        description=(
            "Reap RunPod pods safely. With no flags this is a DRY-RUN reconcile: it prints "
            "the plan and issues no DELETE."
        ),
    )
    parser.add_argument(
        "--execute", action="store_true", help="actually terminate (default: dry run)"
    )
    parser.add_argument("--pod-id", dest="pod_id", metavar="ID", help="terminate one pod by id")
    parser.add_argument(
        "--force",
        action="store_true",
        help="bypass the name guard; only meaningful together with --pod-id",
    )
    parser.add_argument(
        "--registry", type=Path, default=DEFAULT_REGISTRY, metavar="PATH",
        help=f"pod registry JSON (default: {DEFAULT_REGISTRY})",
    )
    return parser


def _terminate_one(args: argparse.Namespace) -> int:
    """The `--pod-id` recovery path from the runbook. A hand-typed id is untrusted input."""
    if not args.execute:
        print(f"DRY-RUN: would terminate pod {args.pod_id} (pass --execute to act)")
        return 0
    if args.force:
        print(f"WARNING: --force bypasses the name guard for pod {args.pod_id}", file=sys.stderr)
    else:
        entry = next(
            (e for e in read_registry(args.registry) if e.get("pod_id") == args.pod_id), None
        )
        if entry is None:
            print(
                f"ERROR: pod {args.pod_id} is not in {args.registry}. An untracked pod is "
                "never deleted silently -- pass --force if you are certain it is yours.",
                file=sys.stderr,
            )
            return 1
        if not _allowed(entry, DEFAULT_ALLOW):
            print(
                f"ERROR: pod {args.pod_id} has name {entry.get('name', '')!r}, which is not in "
                f"the allowlist {DEFAULT_ALLOW}. Pass --force to override.",
                file=sys.stderr,
            )
            return 1
    try:
        terminate_pod(args.pod_id)
    except Exception as exc:  # noqa: BLE001 - one message, no traceback, no secret
        print(f"ERROR: {scrub(str(exc))}", file=sys.stderr)
        return 1
    print(f"OK: pod {args.pod_id} terminated")
    remove_from_registry(args.registry, {args.pod_id})
    return 0


def main(argv: list[str] | None = None) -> int:
    """Exit code, not an exception: a scheduled reaper is only useful if a failure is red."""
    args = _build_parser().parse_args(argv)

    if args.pod_id:
        return _terminate_one(args)

    if not args.execute:
        print("=== DRY-RUN reconcile: no DELETE calls will be made ===")
    summary = reconcile(args.registry, execute=args.execute)
    print(
        f"  registered_gone : {len(summary['registered_gone'])}\n"
        f"  live_and_ours   : {len(summary['live_and_ours'])}\n"
        f"  orphans         : {len(summary['orphans'])}"
    )
    if args.execute:
        print(
            f"  terminated      : {len(summary['terminated'])}\n"
            f"  skipped_by_guard: {len(summary['skipped_by_guard'])}\n"
            f"  errors          : {len(summary['errors'])}"
        )
    else:
        print(f"  would_terminate : {len(summary['live_and_ours'])} (pass --execute to act)")

    # Orphans exit non-zero too: "a GPU is running with nobody's name on it" is a finding,
    # not a stderr line in a log nobody opens.
    if summary["errors"] or summary["skipped_by_guard"] or summary["orphans"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
