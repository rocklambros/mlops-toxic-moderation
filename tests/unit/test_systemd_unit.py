"""REG-5. Today nothing starts containers on a stop/start cycle."""

import configparser
from pathlib import Path

import yaml

UNIT = Path("infra/deploy/toxic-stack.service")
COMPOSE_FILES = sorted(Path("infra/deploy").glob("compose.*.yml"))


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str  # systemd keys are case sensitive
    parser.read_string(UNIT.read_text(encoding="utf-8"))
    return parser


def test_unit_is_enabled_for_multi_user_target():
    assert _unit()["Install"]["WantedBy"] == "multi-user.target"


def test_unit_waits_for_docker_and_the_network():
    unit = _unit()["Unit"]
    assert "docker.service" in unit["After"]
    assert "network-online.target" in unit["After"]
    assert unit["Wants"] == "network-online.target"
    assert unit["Requires"] == "docker.service"


def test_unit_is_a_remain_after_exit_oneshot():
    """compose up -d returns immediately; without RemainAfterExit systemd calls it dead."""
    service = _unit()["Service"]
    assert service["Type"] == "oneshot"
    assert service["RemainAfterExit"] == "yes"


def test_start_brings_the_compose_project_up_and_stop_takes_it_down():
    service = _unit()["Service"]
    assert service["ExecStart"] == (
        "/usr/bin/docker compose --env-file /etc/toxic/stack.env "
        "-f /opt/toxic/compose.yml up -d --remove-orphans"
    )
    assert service["ExecStop"] == (
        "/usr/bin/docker compose --env-file /etc/toxic/stack.env "
        "-f /opt/toxic/compose.yml down"
    )


def test_start_has_a_timeout_large_enough_for_an_ecr_pull():
    assert int(_unit()["Service"]["TimeoutStartSec"]) >= 600


def test_unit_retries_rather_than_giving_up_on_a_cold_boot():
    """RDS can still be starting when EC2 finishes booting. One failure is not terminal."""
    service = _unit()["Service"]
    assert service["Restart"] == "on-failure"
    assert int(service["RestartSec"]) >= 15


def test_unit_carries_no_secret():
    body = UNIT.read_text(encoding="utf-8")
    for forbidden in ("WANDB_API_KEY", "DEMO_API_KEY", "PASSWORD", "AKIA"):
        assert forbidden not in body


def test_the_unit_does_not_run_before_the_deploy_has_landed_a_compose_file():
    """A first boot happens before any deploy has uploaded anything. Without a condition the
    unit fails, retries every RestartSec forever, and leaves a red unit and a scrolling
    journal on a host with no SSH -- which is indistinguishable from a real fault. With one,
    `systemctl status` reads 'condition failed', which is the truth."""
    assert _unit()["Unit"]["ConditionPathExists"] == "/opt/toxic/compose.yml"


def test_the_unit_refreshes_the_ecr_credential_before_every_start():
    """An ECR authorization token expires after 12 hours. The stack is stopped between
    sessions by design, so the token obtained during bootstrap is always expired by the next
    start, and a replaced or pruned image cannot then be pulled. The leading '-' makes a
    failure non-fatal: a transient IAM problem must not stop containers whose images are
    already on disk from coming back."""
    exec_start_pre = _unit()["Service"]["ExecStartPre"]
    assert exec_start_pre.startswith("-"), "an ECR outage must not block a local-image start"
    assert "ecr" in exec_start_pre.lower()


def test_the_unit_and_the_compose_files_agree_on_the_configuration_directory():
    """Anti-drift, and not hypothetical: the bootstrap this project already applied wrote
    `toxic-mod.service` against `/opt/toxic-mod/docker-compose.yml`, while this unit and
    these compose files say `/opt/toxic`. A unit pointing one place and env files written
    another is a running instance with no containers, reported as a successful deploy."""
    service = _unit()["Service"]
    env_file = service["ExecStart"].split("--env-file ")[1].split()[0]
    unit_dir = str(Path(env_file).parent)
    for path in COMPOSE_FILES:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, body in spec["services"].items():
            for entry in body.get("env_file", []):
                assert str(Path(entry).parent) == unit_dir, f"{path.name}:{name} -> {entry}"
    assert service["WorkingDirectory"] == str(
        Path(service["ExecStart"].split("-f ")[1].split()[0]).parent
    )
