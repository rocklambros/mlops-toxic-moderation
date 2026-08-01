import re
from pathlib import Path

import pytest

# Anchored to the repository rather than the working directory: a cwd-relative path makes
# every assertion below vacuously true whenever pytest is invoked from anywhere else.
REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "backend" / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
SOURCE = DOCKERFILE.read_text(encoding="utf-8") if DOCKERFILE.exists() else ""

# Every image the project ships, discovered rather than listed. Phase 3 adds four to
# Phase 2's one, and a sixth surface must not be able to arrive with an unpinned base and
# no test noticing -- which is exactly how H35 was written up in the first place.
IMAGES = sorted(REPO.glob("*/Dockerfile*"))
BASE_PIN = re.compile(r"^FROM python:3\.11-slim-bookworm@(sha256:[0-9a-f]{64})", re.MULTILINE)


def test_the_image_scan_finds_every_surface():
    """A scan over an empty glob passes vacuously, which is how this control dies."""
    names = {path.parent.name + "/" + path.name for path in IMAGES}
    assert names >= {
        "backend/Dockerfile",
        "frontend/Dockerfile",
        "frontend/Dockerfile.reviewer",
        "monitoring/Dockerfile",
        "rescorer/Dockerfile",
    }, names


@pytest.mark.parametrize("image", IMAGES, ids=lambda path: f"{path.parent.name}/{path.name}")
def test_the_base_image_is_pinned_by_digest(image: Path):
    """H35. A floating tag defeats the SHA traceability the whole deploy pipeline is built
    on: the same git SHA would produce different images on different days."""
    assert BASE_PIN.search(image.read_text(encoding="utf-8")), "pin the base image by digest"


def test_every_image_shares_one_base_digest():
    """Five images pinned to five digests is five base images to audit and five sets of
    CVEs to track. One pin, one audit, and `docker pull` warms one layer for all of them."""
    digests = {
        BASE_PIN.search(image.read_text(encoding="utf-8")).group(1)  # type: ignore[union-attr]
        for image in IMAGES
    }
    assert len(digests) == 1, f"the images pin different bases: {sorted(digests)}"


@pytest.mark.parametrize("image", IMAGES, ids=lambda path: f"{path.parent.name}/{path.name}")
def test_dependencies_install_from_a_hashed_wheels_only_lock(image: Path):
    """--require-hashes pins *what* is installed, not *whether it executes code to install*:
    a hash-matching sdist still runs its setup.py inside the build. The repository's other
    install paths (`make venv`, `make serve-deps`, every `*-lock` target) are wheels-only,
    and an image build that is not would be the one hole in that control."""
    source = image.read_text(encoding="utf-8")
    assert "--require-hashes" in source
    assert "--only-binary=:all:" in source
    locks = re.findall(r"-r requirements/([a-z]+\.txt)", source)
    assert locks, f"{image} installs from no lock file at all"
    for lock in locks:
        assert (REPO / "requirements" / lock).is_file(), f"{image} installs a missing {lock}"


@pytest.mark.parametrize("image", IMAGES, ids=lambda path: f"{path.parent.name}/{path.name}")
def test_no_image_runs_as_root(image: Path):
    source = image.read_text(encoding="utf-8")
    assert re.search(r"^USER (?!root|0\b)\S+", source, re.MULTILINE), "the image runs as root"


@pytest.mark.parametrize("image", IMAGES, ids=lambda path: f"{path.parent.name}/{path.name}")
def test_no_image_bakes_a_secret_into_a_layer(image: Path):
    """Delivery spec section 6.3: a build-arg or ENV bakes a credential into an image layer
    permanently, and these images are pushed to a registry."""
    source = image.read_text(encoding="utf-8")
    for forbidden in (
        "WANDB_API_KEY",
        "DEMO_API_KEY",
        "DATABASE_URL",
        "REVIEWER_SHARED_SECRET",
        "SUBMITTER_FP_KEY",
        "MONITORING_DB_DSN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
    ):
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert forbidden not in stripped, f"{image}: {forbidden} must not appear"


def test_the_container_does_not_run_as_root():
    assert re.search(r"^USER appuser", SOURCE, re.MULTILINE)


def test_the_model_card_is_copied_into_the_image():
    """The digest of record has to travel with the code, or the loader's provenance check
    degrades to reading a value the deploy environment supplied."""
    assert "MODEL_CARD.md" in SOURCE


def test_the_image_declares_a_healthcheck():
    assert "HEALTHCHECK" in SOURCE


def test_the_healthcheck_start_period_covers_the_measured_cold_start():
    """Loading the 382 MB classical artifact was measured at 51-78 s on this hardware, and
    uvicorn does not bind until the lifespan finishes, so every probe before that is a
    connection refusal. A short start period therefore marks a perfectly healthy container
    unhealthy on every cold start, and the orchestrator restarts it into the same 78-second
    load - a crash loop that looks like a broken image."""
    match = re.search(r"--start-period=(\d+)s", SOURCE)
    assert match, "HEALTHCHECK must declare an explicit --start-period"
    assert int(match.group(1)) >= 120, (
        f"--start-period={match.group(1)}s is below the measured 78 s worst-case model load"
    )


def test_the_dockerignore_excludes_local_state():
    ignored = DOCKERIGNORE.read_text(encoding="utf-8").split()
    for entry in (".venv", ".git", "tests", "docs", "*.spool"):
        assert entry in ignored


def test_every_path_an_image_copies_survives_the_dockerignore():
    """A .dockerignore entry filters the build CONTEXT, so an excluded path makes the COPY
    fail the build outright. `infra/` is excluded wholesale and `frontend/Dockerfile` copies
    `infra/exposure.py` out of it, which is only possible because of an explicit `!`
    exception -- a combination no compose-file assertion and no `docker compose config` can
    see, and which fails on the day of the demo rather than in CI."""
    patterns = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    excluded = [p for p in patterns if not p.startswith("!")]
    readmitted = {p.lstrip("!") for p in patterns if p.startswith("!")}

    copied: set[str] = set()
    for image in IMAGES:
        for line in image.read_text(encoding="utf-8").splitlines():
            if not line.startswith("COPY "):
                continue
            copied.update(line.split()[1:-1])

    assert copied, "no COPY lines found, so this test measures nothing"
    for source in sorted(copied):
        head = source.rstrip("/").split("/")[0]
        if head not in excluded or source.rstrip("/") in readmitted:
            continue
        assert source.rstrip("/") in readmitted, (
            f"an image copies {source}, which .dockerignore excludes via {head!r} with no "
            "`!` exception; the build fails at COPY time"
        )
