import re
from pathlib import Path

# Anchored to the repository rather than the working directory: a cwd-relative path makes
# every assertion below vacuously true whenever pytest is invoked from anywhere else.
REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "backend" / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
SOURCE = DOCKERFILE.read_text(encoding="utf-8") if DOCKERFILE.exists() else ""


def test_the_base_image_is_pinned_by_digest():
    """H35. A floating tag defeats the SHA traceability the whole deploy pipeline is built
    on: the same git SHA would produce different images on different days."""
    assert re.search(
        r"^FROM python:3\.11-slim-bookworm@sha256:[0-9a-f]{64}", SOURCE, re.MULTILINE
    ), "pin the base image by digest"


def test_dependencies_install_from_a_hashed_lock():
    assert "--require-hashes" in SOURCE
    assert "requirements/serve.txt" in SOURCE


def test_dependencies_refuse_source_distributions():
    """--require-hashes pins *what* is installed, not *whether it executes code to install*:
    a hash-matching sdist still runs its setup.py inside the build. The repository's other
    two install paths (`make venv`, `make serve-deps`) are wheels-only, and an image build
    that is not would be the one hole in that control."""
    assert "--only-binary=:all:" in SOURCE


def test_the_container_does_not_run_as_root():
    assert re.search(r"^USER appuser", SOURCE, re.MULTILINE)


def test_no_secret_is_baked_into_a_layer():
    """Delivery spec section 6.3: a build-arg or ENV bakes a credential into an image layer
    permanently, and this image is pushed to a registry."""
    for forbidden in (
        "WANDB_API_KEY",
        "DEMO_API_KEY",
        "DATABASE_URL",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
    ):
        assert forbidden not in SOURCE, f"{forbidden} must never appear in the Dockerfile"


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
