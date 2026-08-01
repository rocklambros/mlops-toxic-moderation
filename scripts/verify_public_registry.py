#!/usr/bin/env python3
"""Prove the Registry page rubric 1.3 is graded on opens with NO credentials.

`curl` against `https://wandb.ai/...` is not evidence. wandb.ai is a single-page app: it
returns HTTP 200 with the same JavaScript shell for a private project as for a public one, and
the authorization decision happens afterwards, in the browser, against the GraphQL API. A
200 from the HTML therefore proves only that the CDN is up.

So this asserts on the API the page itself calls, with every credential stripped from the
environment: no `WANDB_API_KEY`, no `Authorization` header, no `~/.netrc` (HOME is redirected
to an empty directory, which is what makes `requests` skip netrc lookup). What comes back is
what an anonymous grader's browser would render.

Three things must be true, and all three are checked:

1. the registry project resolves anonymously and its access is a public read;
2. the `toxic-clf` collection exists inside it;
3. some version of that collection carries a promoted alias -- rubric 1.3 grades the stage,
   not the existence of an artifact.

Exit status is non-zero if any of them fails, so this is usable as a CI gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

GRAPHQL = "https://api.wandb.ai/graphql"
PROMOTED_STAGES = ("production", "staging")

COLLECTION_QUERY = """
query($entity: String!, $project: String!, $collection: String!) {
  project(name: $project, entityName: $entity) {
    id
    name
    entityName
    access
    artifactType(name: "model") {
      artifactCollection(name: $collection) {
        name
        artifacts(first: 20) {
          edges { node { id versionIndex aliases { alias } } }
        }
      }
    }
  }
}
"""


def anonymous_post(query: str, variables: dict) -> dict:
    """POST to the public GraphQL endpoint with no credential of any kind.

    HOME is pointed at an empty temporary directory for the duration: urllib does not read
    ~/.netrc, but a future switch to `requests` silently would, and a check that quietly
    authenticates itself is worse than no check.
    """
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(  # noqa: S310 - literal https URL
        GRAPHQL, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    saved = {k: os.environ.get(k) for k in ("HOME", "WANDB_API_KEY", "NETRC")}
    with tempfile.TemporaryDirectory() as empty_home:
        os.environ["HOME"] = empty_home
        os.environ.pop("WANDB_API_KEY", None)
        os.environ.pop("NETRC", None)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                status = response.status
                payload = json.loads(response.read().decode())
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    payload["_http_status"] = status
    return payload


def head_status(url: str) -> int:
    """The status a grader's browser gets for the page URL itself. Reported, never trusted."""
    request = urllib.request.Request(url, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return -1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entity", default="rockcyber-org")
    parser.add_argument("--project", default="wandb-registry-model")
    parser.add_argument("--collection", default="toxic-clf")
    parser.add_argument("--page-url", default=None)
    parser.add_argument("--json", type=str, default=None, help="write the receipt here")
    args = parser.parse_args()

    page_url = args.page_url or (
        f"https://wandb.ai/{args.entity}/{args.project}/artifacts/model/{args.collection}"
    )

    payload = anonymous_post(
        COLLECTION_QUERY,
        {"entity": args.entity, "project": args.project, "collection": args.collection},
    )
    project = (payload.get("data") or {}).get("project")
    failures: list[str] = []

    if project is None:
        failures.append(
            f"{args.entity}/{args.project} does not resolve anonymously: it is private, and "
            f"the Registry page a logged-out grader opens would render 'page not found'"
        )
        collection = None
        aliases: list[str] = []
        access = None
    else:
        access = project.get("access")
        if access not in ("USER_READ", "USER_WRITE"):
            failures.append(f"project access is {access!r}, which is not a public read")
        collection = (project.get("artifactType") or {}).get("artifactCollection")
        if collection is None:
            failures.append(f"no {args.collection!r} collection under artifact type 'model'")
            aliases = []
        else:
            aliases = sorted(
                {
                    alias["alias"]
                    for edge in collection["artifacts"]["edges"]
                    for alias in edge["node"]["aliases"]
                }
            )
            if not set(aliases) & set(PROMOTED_STAGES):
                failures.append(
                    f"no version carries a promoted stage {list(PROMOTED_STAGES)}; aliases "
                    f"seen anonymously: {aliases}. Rubric 1.3 grades the stage"
                )

    receipt = {
        "checked_at_entity": args.entity,
        "project": args.project,
        "collection": args.collection,
        "page_url": page_url,
        "page_http_status_no_auth": head_status(page_url),
        "graphql_http_status_no_auth": payload.get("_http_status"),
        "graphql_endpoint": GRAPHQL,
        "project_access": access,
        "collection_resolved_anonymously": collection is not None,
        "aliases_visible_anonymously": aliases if project else [],
        "n_versions_visible": (
            len(collection["artifacts"]["edges"]) if collection else 0
        ),
        "promoted": bool(set(aliases) & set(PROMOTED_STAGES)) if project else False,
        "failures": failures,
        "verdict": "PUBLIC AND PROMOTED" if not failures else "NOT PUBLICLY PROMOTED",
    }
    text = json.dumps(receipt, indent=2, sort_keys=True)
    print(text)
    if args.json:
        from pathlib import Path

        Path(args.json).write_text(text + "\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
