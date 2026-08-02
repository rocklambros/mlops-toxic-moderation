"""Delivery spec section 6.3: the key reaches the build only through a secret mount.

A `--build-arg` or an `ENV` writes the value into an image layer permanently, and every
image here is pushed to a registry. The serving images do not need `WANDB_API_KEY` at all --
the artifact is fetched at deploy time by `infra/deploy/instance/fetch_artifacts.sh` -- so
the only image allowed to see it is the optional artifact-bake image, and it sees it through
a mount that exists for the duration of one `RUN` and leaves no layer behind.

Two of the properties below would be vacuously true today, and that is the failure mode this
file is written against. No workflow mentions `WANDB_API_KEY` right now, so a test that only
walks `.github/workflows/*.yml` asserts nothing and would keep asserting nothing after being
deleted. The decisions are therefore pure functions over text -- `build_arg_leaks` and
`unmounted_key_uses` -- exercised against a corpus of workflows that DO leak, and then
applied to the real files. A rule that has never been observed refusing anything is
indistinguishable from `return []`.

`infra/deploy/Dockerfile.artifacts` is deliberately outside the `*/Dockerfile*` glob that
`tests/unit/test_dockerfile_hygiene.py` walks. It is not a shipped image: its final stage is
`FROM scratch`, it has no `USER` and no runtime, and it installs one wheel from PyPI rather
than from a hashed lock. Holding it to the serving-image rules would fail for reasons that
say nothing about the control this file is about.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SERVING_DOCKERFILES = [
    REPO / "backend/Dockerfile",
    REPO / "frontend/Dockerfile",
    REPO / "frontend/Dockerfile.reviewer",
    REPO / "monitoring/Dockerfile",
    REPO / "rescorer/Dockerfile",
]
ARTIFACTS = REPO / "infra/deploy/Dockerfile.artifacts"
WORKFLOWS = sorted((REPO / ".github/workflows").glob("*.yml"))

# A name that authenticates rather than one that locates. Same distinction, and deliberately
# the same vocabulary, as scripts/redact.py's `_CREDENTIAL_NAME`.
_CREDENTIAL_NAME = re.compile(r"(?i)SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|ACCESS_?KEY|_KEY$")

# A build argument or an inline environment assignment carrying the key. Both forms end up
# in `docker history`; the second also ends up in the process table of the runner.
_BUILD_ARG = re.compile(r"--build-arg[^\n]*WANDB")
_WITH_BUILD_ARGS = re.compile(r"build-args:[^\n]*(?:\n\s+[^\n]*)*")


def build_arg_leaks(text: str) -> list[str]:
    """Lines that hand the key to a build as an argument rather than as a mount."""
    leaks = [line.strip() for line in text.splitlines() if _BUILD_ARG.search(line)]
    for block in _WITH_BUILD_ARGS.findall(text):
        if "WANDB" in block:
            leaks.append(block.strip().splitlines()[0])
    return leaks


def unmounted_key_uses(text: str) -> list[str]:
    """True when a file names the key but declares no secret plumbing for it.

    `secrets:` is the `docker/build-push-action` form and `--secret id=` is the CLI form.
    Either one means the value travels through a mount; neither means it travels through an
    argument or an environment variable that a build layer can capture.
    """
    if "WANDB_API_KEY" not in text:
        return []
    if "secrets:" in text or "--secret id=wandb_api_key" in text:
        return []
    return [line.strip() for line in text.splitlines() if "WANDB_API_KEY" in line]


# Workflows that leak, so the two rules above are observed refusing something.
LEAKY_BUILD_ARG_CLI = """
      - run: |
          docker build --build-arg WANDB_API_KEY=$KEY -f infra/deploy/Dockerfile.artifacts .
"""
LEAKY_BUILD_ARG_ACTION = """
      - uses: docker/build-push-action@v6
        with:
          build-args: |
            WANDB_API_KEY=${{ secrets.WANDB_API_KEY }}
"""
LEAKY_PLAIN_ENV = """
      - run: docker build -t artifacts .
        env:
          WANDB_API_KEY: ${{ secrets.WANDB_API_KEY }}
"""
CLEAN_SECRET_MOUNT = """
      - run: |
          printf '%s' "$KEY" > /tmp/k
          docker build --secret id=wandb_api_key,src=/tmp/k -f infra/deploy/Dockerfile.artifacts .
        env:
          WANDB_API_KEY: ${{ secrets.WANDB_API_KEY }}
"""


def test_the_serving_image_list_matches_what_the_repository_actually_ships():
    """A list that has drifted is a list that skips the image that leaked."""
    shipped = {path.parent.name + "/" + path.name for path in REPO.glob("*/Dockerfile*")}
    listed = {path.parent.name + "/" + path.name for path in SERVING_DOCKERFILES}
    assert listed == shipped, f"listed {sorted(listed)} but the repository ships {sorted(shipped)}"


def test_no_serving_image_mentions_the_wandb_key_at_all():
    for path in SERVING_DOCKERFILES:
        assert "WANDB" not in path.read_text(encoding="utf-8"), path


def test_no_build_arg_or_env_carries_a_credential():
    """`ARG WANDB` is too coarse and `ARG WANDB_API_KEY` is too narrow.

    The distinction is the one scripts/redact.py already draws: a name that LOCATES
    something (`WANDB_ENTITY`, `WANDB_PROJECT`, `ARTIFACT`, `VERSION`) is build metadata and
    belongs in an ARG, and a name that AUTHENTICATES is a value no layer may capture. A rule
    banning every `ARG WANDB*` would forbid the registry coordinates -- which then get
    hardcoded or, worse, moved into the RUN line where they are just as visible and no
    longer overridable.
    """
    for path in [*SERVING_DOCKERFILES, ARTIFACTS]:
        for line in path.read_text(encoding="utf-8").splitlines():
            declared = re.match(r"\s*(?:ARG|ENV)\s+([A-Za-z0-9_]+)", line)
            if declared and _CREDENTIAL_NAME.search(declared.group(1)):
                raise AssertionError(f"{path}: {line.strip()} bakes a credential into a layer")


def test_the_artifact_image_uses_a_buildkit_secret_mount():
    body = ARTIFACTS.read_text(encoding="utf-8")
    assert body.splitlines()[0].startswith("# syntax=docker/dockerfile:1"), "BuildKit frontend"
    assert "--mount=type=secret,id=wandb_api_key" in body
    assert "/run/secrets/wandb_api_key" in body


def test_the_artifact_image_verifies_the_digest_it_fetched():
    body = ARTIFACTS.read_text(encoding="utf-8")
    assert "sha256sum -c" in body, "an unverified artifact defeats the fail-closed loader"
    assert "MODEL_CARD.md" in body, "the expected digest comes from the committed card"


def test_the_expected_digest_is_anchored_on_a_name_not_a_position():
    """`grep -oE '[0-9a-f]{64}' CARD | head -1` is the whole bug: MODEL_CARD.md carries
    several 64-hex values -- the corpus digest, the split digest, the environment digest --
    and "the first one" silently becomes the wrong one the moment a section is reordered.
    `backend/model_card.py` anchors on the `- MODEL_DIGEST:` line for exactly this reason,
    and the bake has to agree with the loader or the two verify different things."""
    body = ARTIFACTS.read_text(encoding="utf-8")
    assert "MODEL_DIGEST" in body, "anchor the expected digest on its label"
    assert not re.search(r"grep -oE '\[0-9a-f\]\{64\}'[^|]*\| *head", body), (
        "the expected digest is taken by position"
    )


def test_the_key_is_never_written_anywhere_the_build_cache_can_see_it():
    """A mount that is then copied into the layer is not a mount. `wandb login <key>` writes
    ~/.netrc, and a later `COPY`/`RUN` in the same stage would carry it forward."""
    body = ARTIFACTS.read_text(encoding="utf-8")
    assert "wandb login" not in body, "wandb login persists the key to ~/.netrc"
    assert not re.search(r"/run/secrets/wandb_api_key\s*(>|>>)", body), "the key is written out"


def test_the_leak_rules_actually_refuse_something():
    """Without this, both workflow tests below pass over a corpus that never leaks."""
    assert build_arg_leaks(LEAKY_BUILD_ARG_CLI)
    assert build_arg_leaks(LEAKY_BUILD_ARG_ACTION)
    assert not build_arg_leaks(CLEAN_SECRET_MOUNT)
    assert unmounted_key_uses(LEAKY_PLAIN_ENV)
    assert not unmounted_key_uses(CLEAN_SECRET_MOUNT)
    assert not unmounted_key_uses("nothing here names the key")
    for authenticates in ("WANDB_API_KEY", "DEMO_API_KEY", "REVIEWER_SHARED_SECRET",
                          "POSTGRES_PASSWORD", "GITHUB_TOKEN", "SUBMITTER_FP_KEY"):
        assert _CREDENTIAL_NAME.search(authenticates), authenticates
    for locates in ("WANDB_ENTITY", "WANDB_PROJECT", "ARTIFACT", "VERSION", "S3_KEY_PREFIX"):
        assert not _CREDENTIAL_NAME.search(locates), locates


def test_no_workflow_passes_the_key_as_a_build_argument():
    for path in WORKFLOWS:
        assert not build_arg_leaks(path.read_text(encoding="utf-8")), path


def test_a_workflow_that_needs_the_key_mounts_it_as_a_secret():
    for path in WORKFLOWS:
        assert not unmounted_key_uses(path.read_text(encoding="utf-8")), path
