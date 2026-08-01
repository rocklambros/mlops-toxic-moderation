"""Cutting the challenger must be a one-line change with no Terraform edit and no red test.

Premortem C8. The DistilBERT branch is item 3 on the delivery spec's ordered cut list, and
a cut-line that cannot fire cleanly recovers zero days -- which is the same defect the
checkpoint log exists to prevent. Two properties make it fire cleanly:

* the default compose stack is complete without the re-scorer, so `docker compose up`
  brings up every GRADED component and nothing else has to change;
* no graded module imports an inference runtime, so the machine that runs the UIs and the
  dashboard does not need onnxruntime installed at all.

The compose file is parsed as data rather than grepped. A text assertion passes on a file
that does not parse, and "does it parse" is the one thing `docker compose config` already
covers.
"""

import subprocess
import sys
from pathlib import Path

import yaml

COMPOSE = Path("infra/docker-compose.yml")
GRADED = frozenset({"postgres", "backend", "frontend", "reviewer", "monitoring"})
# Every runtime that only the challenger needs. The graded surfaces must pull in none of
# them, which is what makes EC2 #3's second container severable.
INFERENCE_RUNTIMES = ("onnxruntime", "tokenizers", "torch", "transformers", "optimum")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _services() -> dict:
    services = _compose()["services"]
    assert services, "the compose file declares no services, so nothing below is measured"
    return services


def test_the_default_stack_is_complete_without_the_rescorer():
    """C8: cutting the challenger must not remove a graded component."""
    services = _services()
    default = {name for name, spec in services.items() if not spec.get("profiles")}
    assert GRADED <= default
    assert "rescorer" not in default
    assert services["rescorer"]["profiles"] == ["challenger"]


def test_no_graded_service_depends_on_the_rescorer():
    """A profile keeps the container from starting. A `depends_on: rescorer` anywhere would
    make the default stack refuse to come up at all once the profile is not selected."""
    services = _services()
    for name, spec in services.items():
        if name == "rescorer":
            continue
        depends = spec.get("depends_on") or {}
        names = depends.keys() if isinstance(depends, dict) else depends
        assert "rescorer" not in names, f"{name} depends on the severable service"


def test_each_ui_publishes_exactly_its_own_port():
    services = _services()
    assert services["frontend"]["ports"] == ["8501:8501"]
    assert services["reviewer"]["ports"] == ["127.0.0.1:8503:8503"]
    assert services["monitoring"]["ports"] == ["8502:8502"]
    assert "ports" not in services["rescorer"]


def test_the_published_ports_match_the_exposure_contract():
    """`infra/exposure.py` is the single Python source of truth for which port is demo-
    exposed. A compose file that published the reviewer console on 0.0.0.0 would contradict
    it while every exposure test stayed green, because those tests read the module, not
    this file (premortem H12)."""
    from infra.exposure import OPERATOR_ONLY_PORTS, PORTS

    services = _services()
    published = {
        name: [str(entry) for entry in spec.get("ports", [])] for name, spec in services.items()
    }
    for key, service in (("user_ui", "frontend"), ("monitoring", "monitoring")):
        assert published[service] == [f"{PORTS[key].number}:{PORTS[key].number}"]
    reviewer_port = PORTS["reviewer_ui"].number
    assert reviewer_port in OPERATOR_ONLY_PORTS
    assert published["reviewer"] == [f"127.0.0.1:{reviewer_port}:{reviewer_port}"]
    for name, entries in published.items():
        for entry in entries:
            host_port = entry.split(":")[-2] if entry.count(":") >= 2 else entry.split(":")[0]
            if int(host_port) in OPERATOR_ONLY_PORTS:
                assert entry.startswith("127.0.0.1:"), (
                    f"{name} publishes operator-only port {host_port} on every interface"
                )


def test_no_ui_service_receives_a_database_url():
    services = _services()
    for name in ("frontend", "reviewer"):
        env = services[name].get("environment", {})
        assert not any("DATABASE" in key or "DSN" in key for key in env), (
            f"{name} must reach Postgres only through the backend (H12/H16)"
        )
    assert "MONITORING_DB_DSN" in services["monitoring"]["environment"]


def test_the_dashboard_connects_as_the_read_only_role():
    """H16. The role is created by infra/postgres-init, and the DSN has to actually use it:
    a dashboard pointed at the superuser satisfies every SELECT-only source scan and still
    holds a write grant."""
    dsn = _services()["monitoring"]["environment"]["MONITORING_DB_DSN"]
    assert "monitoring_ro" in dsn
    assert "postgres:postgres@" not in dsn
    grants = Path("infra/postgres-init/02-monitoring-role.sql").read_text(encoding="utf-8")
    assert "CREATE ROLE monitoring_ro" in grants
    assert "GRANT SELECT" in grants
    for forbidden in ("INSERT", "UPDATE", "DELETE", "ALL PRIVILEGES", "SUPERUSER", "CREATEDB"):
        assert forbidden not in grants.upper().replace("GRANT SELECT", ""), (
            f"the monitoring role is granted {forbidden}"
        )


def test_no_application_secret_is_baked_into_the_compose_file():
    """Delivery spec section 6.3. Every application credential is an interpolation with a
    `:?` guard, so `docker compose up` fails loudly rather than starting with a default
    nobody chose -- a shared reviewer secret with a default is the whole of H12 again.

    `postgres` is exempt and only `postgres`: its password belongs to a throwaway local
    container that publishes on 5433 and holds nothing but replayed public Jigsaw comments.
    In AWS the database is RDS and the password is an SSM SecureString.
    """
    checked = 0
    for name, spec in _services().items():
        if name == "postgres":
            continue
        for key, value in (spec.get("environment") or {}).items():
            if not any(token in key for token in ("SECRET", "KEY", "PASSWORD", "SHA256")):
                continue
            checked += 1
            assert str(value).startswith("${"), f"{name}.{key} is a literal, not a variable"
            assert ":?" in str(value), f"{name}.{key} has a silent default"
    assert checked >= 4, f"only {checked} credentials examined; the scan is too narrow"


def test_importing_the_uis_and_dashboard_pulls_in_no_inference_runtime():
    code = (
        "import sys; import frontend.render, frontend.api_client, frontend.ui, "
        "frontend.reviewer, monitoring.queries, monitoring.stats, monitoring.baseline, "
        "monitoring.dashboard; "
        f"leaked = sorted(set({INFERENCE_RUNTIMES!r}) & set(sys.modules)); "
        "assert not leaked, leaked; print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_no_graded_image_installs_an_inference_runtime():
    """The runtime property above is measured on this box, where none of these packages is
    installed and therefore none of them can leak. The lock files are where the property is
    actually decided, and they can be checked whatever is installed here."""
    for surface in ("ui", "monitor", "serve"):
        lock = Path(f"requirements/{surface}.txt").read_text(encoding="utf-8")
        for runtime in INFERENCE_RUNTIMES:
            assert f"\n{runtime}==" not in lock, f"{surface}.txt installs {runtime}"
    rescorer = Path("requirements/rescorer.txt").read_text(encoding="utf-8")
    assert "\nonnxruntime==" in rescorer, "the scan above would pass on an empty lock set"


def test_the_reviewer_ui_degrades_to_a_caption_when_the_challenger_is_absent():
    source = Path("frontend/reviewer.py").read_text(encoding="utf-8")
    assert "Challenger scores are not available" in source


def test_the_reviewer_ui_still_works_with_no_challenger_column():
    """Item 4 on the cut list is the second-opinion column, and item 3 takes it away for
    free: a queue row whose `distilbert_probs` is NULL must render, not raise."""
    from frontend.reviewer import challenger_column
    from model.labels import LABELS

    assert challenger_column(None) == dict.fromkeys(LABELS, None)
    assert all(value is None for value in challenger_column({}).values())


def test_the_cut_procedure_is_documented_in_one_place():
    readme = COMPOSE.read_text(encoding="utf-8")
    assert "--profile challenger" in readme


def test_every_service_that_builds_names_a_dockerfile_that_exists():
    """A compose file is not `docker compose config`-validated against the filesystem, so a
    build context pointing at a Dockerfile nobody wrote fails at `up` time -- on the day of
    the demo -- rather than in CI."""
    built = 0
    for name, spec in _services().items():
        build = spec.get("build")
        if not build:
            continue
        built += 1
        assert build["context"] == "..", f"{name} builds from {build['context']!r}, not the repo"
        assert Path(build["dockerfile"]).is_file(), f"{name}: {build['dockerfile']} is missing"
    assert built >= 5, f"only {built} services build an image; the scan is too narrow"
