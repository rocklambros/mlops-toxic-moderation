"""The production compose files are what a stopped instance comes back to."""

import re
from pathlib import Path

import yaml

from infra.exposure import DEMO_EXPOSED_PORTS, OPERATOR_ONLY_PORTS

FILES = {
    "backend": Path("infra/deploy/compose.backend.yml"),
    "frontend": Path("infra/deploy/compose.frontend.yml"),
    "monitoring": Path("infra/deploy/compose.monitoring.yml"),
}

# Which image each production service is built from, so a compose healthcheck can be
# compared against the HEALTHCHECK the image already declares.
DOCKERFILES = {
    "backend": Path("backend/Dockerfile"),
    "frontend": Path("frontend/Dockerfile"),
    "reviewer": Path("frontend/Dockerfile.reviewer"),
    "monitoring": Path("monitoring/Dockerfile"),
}


def _load(name: str) -> dict:
    return yaml.safe_load(FILES[name].read_text(encoding="utf-8"))


def _all_services() -> dict[str, dict]:
    services: dict[str, dict] = {}
    for name in FILES:
        services.update(_load(name)["services"])
    return services


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)s", str(value).strip())
    assert match, f"express the duration in whole seconds, got {value!r}"
    return int(match.group(1))


def test_every_component_has_its_own_compose_file():
    for name, path in FILES.items():
        assert path.exists(), f"{name} has no production compose file"


def test_every_production_service_restarts_unless_stopped():
    """REG-5. Without this, a stop/start cycle leaves the instance up and the app down."""
    for service_name, spec in _all_services().items():
        assert spec.get("restart") == "unless-stopped", f"{service_name} has no restart policy"


def test_every_production_service_ships_logs_to_cloudwatch():
    """H27. No container log leaves the box today, and nothing pages when /predict is down."""
    for service_name, spec in _all_services().items():
        logging = spec.get("logging", {})
        assert logging.get("driver") == "awslogs", f"{service_name} has no awslogs driver"
        options = logging["options"]
        assert options["awslogs-group"].startswith("${LOG_GROUP")
        assert options["awslogs-region"].startswith("${AWS_REGION")
        assert options["awslogs-create-group"] == "false", "the group is Terraform's, not Docker's"


def test_no_two_services_interleave_into_one_stream():
    """H27 one level down: a log that leaves the box into a stream shared with another
    process is a log nobody can read. The re-scorer additionally needs its OWN GROUP -- it
    shares the monitoring host, where the Docker daemon's default log group is the
    monitoring one, so it is the single service that would silently inherit the wrong group.
    The reviewer console shares the frontend group on purpose: Terraform declares one log
    group per component and `reviewer` is not a component, so it is separated by stream."""
    services = _all_services()
    logging = {name: spec["logging"]["options"] for name, spec in services.items()}
    assert logging["rescorer"]["awslogs-group"] != logging["monitoring"]["awslogs-group"]
    destinations = [(o["awslogs-group"], o["awslogs-stream"]) for o in logging.values()]
    assert len(set(destinations)) == len(destinations), destinations


def test_no_production_service_builds_from_source():
    for service_name, spec in _all_services().items():
        assert "build" not in spec, f"{service_name} builds on the instance instead of pulling"
        assert "image" in spec


def test_every_image_is_pinned_by_digest_through_an_environment_variable():
    """The roll script resolves the tag to a digest; the compose file never floats a tag."""
    for service_name, spec in _all_services().items():
        image = spec["image"]
        assert image.startswith("${") and image.endswith("}"), service_name
        assert "IMAGE" in image, f"{service_name} image variable is misnamed: {image}"


def test_every_service_reads_its_environment_from_a_file_the_roll_writes():
    """The interface with roll.sh: it writes /etc/toxic/<component>.env under 0600 and the
    compose file names it. A relative path would resolve against the working directory of
    whatever invoked compose, which is not the same for systemd and for an operator."""
    for service_name, spec in _all_services().items():
        env_files = spec.get("env_file", [])
        assert env_files, f"{service_name} declares no env_file"
        for entry in env_files:
            assert entry.startswith("/etc/toxic/"), f"{service_name}: {entry}"
            assert entry.endswith(".env"), f"{service_name}: {entry}"


def test_the_backend_file_holds_only_the_backend():
    assert set(_load("backend")["services"]) == {"backend"}


def test_the_frontend_file_holds_the_user_ui_and_the_reviewer_ui():
    assert set(_load("frontend")["services"]) == {"frontend", "reviewer"}


def test_the_monitoring_file_holds_the_dashboard_and_the_severable_rescorer():
    services = _load("monitoring")["services"]
    assert set(services) == {"monitoring", "rescorer"}
    assert services["rescorer"]["profiles"] == ["challenger"], "the challenger must stay severable"


def test_the_reviewer_ui_binds_loopback_only():
    """H12 and Phase 3's exposure contract: 8503 is never carried by the demo toggle."""
    reviewer = _load("frontend")["services"]["reviewer"]
    assert reviewer["ports"] == ["127.0.0.1:8503:8503"]
    assert 8503 in OPERATOR_ONLY_PORTS and 8503 not in DEMO_EXPOSED_PORTS


def test_the_publicly_bound_ports_are_exactly_the_graded_surface():
    published = set()
    for spec in _all_services().values():
        for mapping in spec.get("ports", []):
            parts = mapping.split(":")
            if len(parts) == 2:
                published.add(int(parts[1]))
    assert published == DEMO_EXPOSED_PORTS


def test_no_compose_file_contains_a_secret_value():
    for path in FILES.values():
        body = path.read_text(encoding="utf-8")
        for line in body.splitlines():
            if "SECRET" in line or "API_KEY" in line or "PASSWORD" in line:
                assert "${" in line, f"literal credential in {path}: {line.strip()}"


def test_every_service_declares_a_healthcheck_or_is_a_worker():
    services = _all_services()
    for name in ("backend", "frontend", "reviewer", "monitoring"):
        assert "healthcheck" in services[name], f"{name} has no healthcheck"
    assert "healthcheck" not in services["rescorer"], "the worker publishes no port to probe"


def test_no_production_healthcheck_shortens_the_start_period_the_image_declares():
    """A compose `healthcheck:` block REPLACES the image's HEALTHCHECK wholesale.

    The backend's `--start-period=180s` is deliberate headroom: uvicorn does not bind until
    the lifespan finishes loading and verifying the artifact, so every probe before then is
    refused by a container that is perfectly healthy. `tests/unit/test_dockerfile_hygiene.py`
    holds the Dockerfile to that floor -- and a production compose file that quietly
    overrides it with a shorter one would pass every existing test while restarting a
    healthy backend in a loop on the one host that has no SSH.
    """
    services = _all_services()
    for name, dockerfile in DOCKERFILES.items():
        image = re.search(r"--start-period=(\d+)s", dockerfile.read_text(encoding="utf-8"))
        assert image, f"{dockerfile} declares no --start-period"
        composed = _seconds(services[name]["healthcheck"]["start_period"])
        assert composed >= int(image.group(1)), (
            f"{name}: compose start_period {composed}s is shorter than the image's "
            f"{image.group(1)}s, which silently narrows the cold-start headroom"
        )
