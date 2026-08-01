"""Every container image this project builds or runs is pinned by digest (premortem H35).

`FROM python:3.11-slim-bookworm` resolves to a different filesystem every few weeks. Image tags
are immutable in ECR by design in this project, and that guarantee is worthless if the base the
image was built FROM is not.

`tests/unit/test_dockerfile_hygiene.py` already checks the five images this repository builds,
against a regex naming the one base they share. This file is the other direction: it walks
whatever is on disk -- Dockerfiles the project does not build yet, and the compose services it
runs against -- so a sixth surface, or a database nobody thought of as an image, cannot arrive
unpinned. The two overlap on purpose; the overlapping half is the one that would otherwise be
deleted along with the file that introduced it.
"""

import re
from pathlib import Path

DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}")
FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$", re.IGNORECASE)
SKIP_PARTS = {".venv", ".venv-lock", ".venv-scan", "build", "node_modules", ".git", "__pycache__"}


def dockerfiles() -> list[Path]:
    found = [
        path
        for path in Path(".").rglob("Dockerfile*")
        if path.is_file() and not SKIP_PARTS & set(path.parts)
    ]
    return sorted(found)


def compose_files() -> list[Path]:
    return sorted(
        path
        for path in Path("infra").rglob("*compose*.y*ml")
        if path.is_file() and not SKIP_PARTS & set(path.parts)
    )


def compose_images() -> list[tuple[Path, int, str]]:
    found = []
    for path in compose_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("image:"):
                continue
            found.append((path, lineno, stripped.split(":", 1)[1].strip().strip("'\"")))
    return found


def test_the_scanner_finds_the_dockerfiles_the_project_has():
    assert dockerfiles(), "no Dockerfile found; rubric 5.1 requires containerized components"


def test_the_scanner_finds_the_compose_services_the_project_runs():
    """The Dockerfile half of this file would pass on its own today. Without this assertion a
    compose file that stopped being discovered -- renamed, moved out of infra/ -- would make
    `test_every_compose_service_image_is_pinned_by_digest` certify an empty list."""
    assert compose_files(), "no compose file found under infra/, so the scan below is vacuous"
    assert compose_images(), "no compose service declares an image, so nothing is being checked"


def test_every_dockerfile_base_image_is_pinned_by_digest():
    offenders = []
    for path in dockerfiles():
        stages: set[str] = set()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = FROM_RE.match(line)
            if match is None:
                continue
            image = match.group("image")
            if match.group("stage"):
                stages.add(match.group("stage"))
            if image in stages or image == "scratch":
                continue  # an earlier stage in the same file, or the empty base
            if not DIGEST_RE.search(image):
                offenders.append(f"{path}:{lineno} FROM {image}")
    assert not offenders, (
        "pin these base images by digest; resolve one with "
        "`docker buildx imagetools inspect <image> --format '{{ .Manifest.Digest }}'`:\n  "
        + "\n  ".join(offenders)
    )


def test_every_compose_service_image_is_pinned_by_digest():
    offenders = [
        f"{path}:{lineno} {image}"
        for path, lineno, image in compose_images()
        # An ECR image resolved at deploy time by immutable git-sha tag.
        if "${" not in image and not DIGEST_RE.search(image)
    ]
    assert not offenders, f"pin these compose images by digest: {offenders}"


# The three places a Postgres is started: the local stack, the CI service container, and the
# testcontainers fallback the integration suite uses when TEST_DATABASE_URL is unset. Each is
# read structurally rather than by grepping the file for `postgres:`, which also matches the
# host and the port inside `postgresql+psycopg://postgres:postgres@postgres:5432/toxic`.
CI = Path(".github/workflows/ci.yml")
INTEGRATION_CONFTEST = Path("tests/integration/conftest.py")


def workflow_service_images() -> set[str]:
    import yaml

    document = yaml.safe_load(CI.read_text(encoding="utf-8")) or {}
    return {
        str(service["image"])
        for job in (document.get("jobs") or {}).values()
        for service in (job.get("services") or {}).values()
        if isinstance(service, dict) and "image" in service
    }


def postgres_references() -> dict[str, set[str]]:
    """image references, per site, for the database only."""
    return {
        "infra/docker-compose.yml": {
            image for _, _, image in compose_images() if image.startswith("postgres:")
        },
        str(CI): {image for image in workflow_service_images() if image.startswith("postgres:")},
        str(INTEGRATION_CONFTEST): set(
            re.findall(
                r"PostgresContainer\(\s*[\"']([^\"']+)[\"']",
                INTEGRATION_CONFTEST.read_text(encoding="utf-8"),
            )
        ),
    }


def test_the_database_scan_finds_all_three_places_a_postgres_is_started():
    found = postgres_references()
    empty = sorted(site for site, refs in found.items() if not refs)
    assert not empty, f"no postgres image reference found in {empty}; the scan below is vacuous"


def test_the_database_is_the_same_image_everywhere_it_runs():
    """Until this phase these were three different images: 16.4-alpine locally, 16-alpine in
    CI and in the testcontainers fallback. A digest pin on each of three divergent tags is
    three images to audit and a patch-level difference that shows up only in whichever
    environment nobody ran."""
    tags = {ref.split("@", 1)[0] for refs in postgres_references().values() for ref in refs}
    assert len(tags) == 1, f"the three Postgres environments name {len(tags)} images: {tags}"


def test_the_pinned_database_digest_is_identical_everywhere():
    """The tags agreeing is not the control; the digests agreeing is. Two `postgres:16-alpine`
    pins resolved a month apart are two different filesystems under one name -- and the one
    that would diverge is the testcontainers fallback, which pulls whatever the tag means on
    the day the developer ran it."""
    references = {ref for refs in postgres_references().values() for ref in refs}
    unpinned = sorted(ref for ref in references if "@sha256:" not in ref)
    assert not unpinned, f"an unpinned database image is started by a committed file: {unpinned}"
    assert len({ref.split("@", 1)[1] for ref in references}) == 1, (
        f"the three Postgres environments pin different digests: {sorted(references)}"
    )


def test_no_dockerfile_runs_as_root_at_the_end():
    """Not H35, but the same one-line class of omission, and it is free to assert once a test
    file is walking every Dockerfile anyway. `USER appuser` followed later by `USER root` is
    an image that runs as root, and a scan for "does a non-root USER appear" says it does."""
    offenders = []
    for path in dockerfiles():
        users = re.findall(r"^\s*USER\s+(\S+)", path.read_text(encoding="utf-8"), re.MULTILINE)
        if not users or users[-1] in {"root", "0"}:
            offenders.append(str(path))
    assert not offenders, f"these images run as root: {offenders}"
