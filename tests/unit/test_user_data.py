"""H26. User data runs once, unattended, on a box with no SSH. Every hazard is a test."""

import re
from pathlib import Path

TEMPLATE = Path("infra/terraform/templates/user_data.sh.tftpl")
UNIT = Path("infra/deploy/toxic-stack.service")
NETWORK = Path("infra/terraform/network.tf")


def _body() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _lines() -> list[str]:
    return [line.strip() for line in _body().splitlines()]


def test_the_script_fails_loudly_rather_than_silently():
    assert _body().splitlines()[0] == "#!/bin/bash"
    assert "set -euxo pipefail" in _body()
    assert "/var/log/user-data.log" in _body(), "the only post-mortem is the console log"


def test_compose_v2_is_not_taken_from_the_distribution_repositories():
    """AL2023 does not package it. dnf-installing it is a boot failure with no SSH."""
    body = _body()
    assert "docker-compose-plugin" not in body
    assert not re.search(r"dnf[^\n]*install[^\n]*docker-compose", body)


def test_user_data_installs_compose_v2_as_a_checksummed_binary():
    body = _body()
    assert "docker-compose-linux-aarch64" in body, "arm64 asset, not x86_64"
    assert re.search(r"COMPOSE_SHA256=[0-9a-f]{64}", body), "pin the checksum, do not fetch it"
    assert "sha256sum -c -" in body


def test_the_checksum_is_verified_before_the_binary_is_installed():
    lines = _lines()
    verify = next(i for i, line in enumerate(lines) if "sha256sum -c -" in line)
    install = next(
        i
        for i, line in enumerate(lines)
        if "install -m 0755" in line and "cli-plugins/docker-compose" in line
    )
    assert verify < install, "the binary is installed before its checksum is checked"


def test_the_expected_digest_is_not_fetched_from_beside_the_binary():
    """Fetching the digest from the host that serves the artifact proves the transfer was
    not truncated and proves nothing about provenance: anything able to serve a malicious
    binary can serve its digest too. The delivery spec makes independent recording of an
    expected digest normative."""
    body = _body()
    assert ".sha256" not in body
    assert "checksums.txt" not in body


def test_compose_lands_in_the_system_cli_plugin_directory():
    body = _body()
    assert "/usr/libexec/docker/cli-plugins" in body
    assert "docker compose version" in body, "prove the plugin resolved before relying on it"


def test_downloads_retry_because_a_cold_boot_races_the_network():
    for line in _lines():
        if line.startswith("curl "):
            assert "--retry" in line, f"unretried download: {line}"


def test_docker_is_enabled_so_it_survives_a_reboot():
    assert "systemctl enable --now docker" in _body()


def test_the_stack_unit_is_installed_and_enabled():
    body = _body()
    assert "toxic-stack.service" in body
    assert re.search(r"systemctl enable[^\n]*toxic-stack", body)


def test_the_installed_unit_is_byte_for_byte_the_committed_one():
    """User data has to carry the unit inline, because on a first boot nothing has delivered
    anything to this host yet. That makes two copies of one file, and two copies drift --
    which is exactly how the applied bootstrap ended up installing `toxic-mod.service`
    against a path no Phase 5 artifact uses. The committed file is the one under test in
    tests/unit/test_systemd_unit.py, so this is what makes those assertions true of the
    thing that actually reaches the instance."""
    body = _body()
    start = body.index("<<'TOXICSTACKUNIT'\n") + len("<<'TOXICSTACKUNIT'\n")
    end = body.index("\nTOXICSTACKUNIT\n", start) + 1
    assert body[start:end] == UNIT.read_text(encoding="utf-8")


def test_the_component_compose_file_is_symlinked_to_the_unit_path():
    body = _body()
    assert "ln -sfn" in body
    assert "/opt/toxic/compose.yml" in body
    assert "${component}" in body, "the template must be told which component this host is"


def test_user_data_waits_for_an_internet_route_before_it_needs_one():
    """H26. The EIP associates after boot. Without auto-assign there is no route at all."""
    body = _body()
    assert "wait_for_egress" in body, "no bounded connectivity wait exists"
    lines = _lines()
    call = next(i for i, line in enumerate(lines) if line == "wait_for_egress")
    first_dnf = next(i for i, line in enumerate(lines) if line.startswith("dnf "))
    first_curl = next(i for i, line in enumerate(lines) if line.startswith("curl "))
    assert call < first_dnf, "dnf runs before connectivity is proven"
    assert call < first_curl, "curl runs before connectivity is proven"


def test_the_connectivity_wait_is_bounded_and_fails_loudly():
    body = _body()
    assert re.search(r"EGRESS_WAIT_SECONDS=\d+", body), "an unbounded wait is a silent hang"
    assert "no egress route" in body, "the failure must be greppable in the console log"


def test_the_recorded_public_address_is_fetched_over_443():
    probe = next(line for line in _lines() if "checkip.amazonaws.com" in line)
    assert probe.startswith("curl "), probe
    assert "https://" in probe, "the security group allows 443, not 80"


def test_the_bounded_wait_itself_probes_only_443():
    """The line above records which address this host goes out as, which is a diagnostic.
    The thing that decides whether to continue is the loop, and the loop is what has to be
    reaching 443 -- the security group allows nothing else out, so a probe on any other
    port would time out identically whether the route existed or not, and the wait would be
    measuring its own egress rule."""
    body = _body()
    start = body.index("wait_for_egress() {")
    inside = body[start : body.index("\n}\n", start)]
    urls = re.findall(r"https?://[^\s\"']+", inside)
    assert urls, "a wait with no probe proves nothing"
    assert all(url.startswith("https://") for url in urls), urls


def test_the_public_subnets_auto_assign_an_address_so_the_wait_can_succeed():
    """H26, the other half. The bounded wait cannot succeed if this is off, and there is no
    SSH to turn it on with afterwards. Asserted next to the script that depends on it rather
    than in a Terraform-only file, because the two have to change together or not at all."""
    source = NETWORK.read_text(encoding="utf-8")
    subnets = re.findall(r'resource "aws_subnet" "([a-z0-9_]+)" \{(.*?)\n\}\n', source, re.S)
    public = [(name, body) for name, body in subnets if name.startswith("public")]
    assert public, "no public subnet is declared"
    for name, body in public:
        assert re.search(r"map_public_ip_on_launch\s*=\s*true", body), (
            f"{name} does not auto-assign a public IP; user data boots with no route"
        )


def test_the_configuration_directory_is_not_world_readable():
    """roll.sh writes credentials into /etc/toxic/*.env. The directory it writes them into
    is created here, and it is created private: a 0755 directory would leave a 0600 file
    listable, and the next thing written into it by hand would inherit the default umask."""
    assert re.search(r"install -d -m 0700[^\n]*/etc/toxic", _body())
