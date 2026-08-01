"""Delivery spec section 6.3, deploy side. Nothing secret may travel in a logged argument.

The full command text of an SSM `SendCommand` is recorded in CloudTrail and returned by
`aws ssm list-commands` in plaintext to anyone who can read the account. A key passed as a
`--parameters` value is a permanently logged credential. So the payload is one line naming a
script, and the script -- running on the instance under its own profile -- is what reads the
secret.

Most of this file RUNS `roll.sh` rather than reading it. `roll.sh` is the file that decides
whether the deployed stack has an env file at all, and a test that greps its source for
`chmod 0600` passes over a script whose control flow never reaches that line. Two mechanisms
make the run possible without a t4g instance:

* `DESTDIR` -- the ordinary packaging convention. Empty in production, and a test asserts
  that the default is empty, so the escape hatch cannot silently relocate a real deploy.
* a stubbed `aws`, `docker` and `systemctl` on PATH, driven by JSON in the environment and
  recording every call, so what the script asked AWS for is an observable fact.

The scenario this is written against is the one the operator actually has. The three running
instances were created by a bootstrap that installed `toxic-mod.service` against
`/opt/toxic-mod/docker-compose.yml`, and `compute.tf` sets `ignore_changes = [user_data]`, so
they will NEVER receive the new bootstrap. `roll.sh` therefore has to lay down every path,
the unit and the ECR helper itself, on a box where none of them exist -- and be re-runnable
on a box where all of them do.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest
import yaml

from tests.infra import tfparse
from tests.infra.shellstub import make_stub, run

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "infra/deploy/instance/bootstrap.sh"
ROLL = REPO / "infra/deploy/instance/roll.sh"
SSM_RUN = REPO / "infra/aws/ssm_run.sh"
WORKFLOW = REPO / ".github/workflows/deploy.yml"
COMPOSE_DIR = REPO / "infra/deploy"

SECRET_NAMES = (
    "WANDB_API_KEY", "DEMO_API_KEY", "REVIEWER_SHARED_SECRET",
    "SUBMITTER_FP_KEY", "POSTGRES_PASSWORD",
)

SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "b" * 64

# Distinctive values, so "did a secret reach stdout?" is a grep rather than a judgement call.
DEMO_KEY = "SENTINEL-demo-api-key-value"
REVIEWER_SECRET = "SENTINEL-reviewer-shared-secret-value"
FP_KEY = "SENTINEL-submitter-fingerprint-key"
MASTER_PASSWORD = "SENTINEL-master-pw-with/slash#hash"
READONLY_PASSWORD = "SENTINEL-readonly-pw"
SENTINELS = (DEMO_KEY, REVIEWER_SECRET, FP_KEY, MASTER_PASSWORD, READONLY_PASSWORD)

PARAMETERS = {
    "/toxic/deploy/bucket": "toxic-mod-deploy-example",
    "/toxic/deploy/registry": "example.dkr.ecr.us-west-2.amazonaws.com",
    "/toxic/logs/backend": "/toxic-mod/backend",
    "/toxic/logs/frontend": "/toxic-mod/frontend",
    "/toxic/logs/monitoring": "/toxic-mod/monitoring",
    "/toxic/logs/rescorer": "/toxic-mod/rescorer",
    "/toxic/images/backend": "toxic-mod-backend",
    "/toxic/images/frontend": "toxic-mod-frontend",
    "/toxic/images/monitoring": "toxic-mod-monitoring",
    "/toxic/images/rescorer": "toxic-mod-rescorer",
    "/toxic/endpoints/backend": "http://198.51.100.7:8000",
    "/toxic/endpoints/frontend": "http://198.51.100.8:8501",
    "/toxic/endpoints/monitoring": "http://198.51.100.9:8502",
    "/toxic/endpoints/backend-internal": "http://10.0.1.23:8000",
    "/toxic/db/endpoint": "toxic-mod-pg.example.us-west-2.rds.amazonaws.com:5432",
    "/toxic/db/name": "toxicmod",
    "/toxic/db/master-secret-arn": "arn:aws:secretsmanager:us-west-2:000000000000:secret:master",
    "/toxic/db/readonly-secret-arn": "arn:aws:secretsmanager:us-west-2:000000000000:secret:ro",
    "/toxic/secrets/wandb-api-key": "arn:aws:secretsmanager:us-west-2:000000000000:secret:wandb",
    "/toxic/secrets/reviewer-shared-secret":
        "arn:aws:secretsmanager:us-west-2:000000000000:secret:rev",
    "/toxic/secrets/demo-api-key": "arn:aws:secretsmanager:us-west-2:000000000000:secret:demo",
    "/toxic/secrets/submitter-fp-key": "arn:aws:secretsmanager:us-west-2:000000000000:secret:fp",
    "/toxic/model/wandb-artifact": "rockcyber-org/wandb-registry-model/toxic-clf:production",
    "/toxic/reviewer/id": "rock",
}

SECRETS = {
    "arn:aws:secretsmanager:us-west-2:000000000000:secret:master": json.dumps(
        {"username": "toxicadmin", "password": MASTER_PASSWORD}
    ),
    "arn:aws:secretsmanager:us-west-2:000000000000:secret:ro": json.dumps(
        {"username": "monitor_ro", "password": READONLY_PASSWORD}
    ),
    "arn:aws:secretsmanager:us-west-2:000000000000:secret:wandb": "40hexlookingwandbkey",
    "arn:aws:secretsmanager:us-west-2:000000000000:secret:rev": REVIEWER_SECRET,
    "arn:aws:secretsmanager:us-west-2:000000000000:secret:demo": DEMO_KEY,
    "arn:aws:secretsmanager:us-west-2:000000000000:secret:fp": FP_KEY,
}

IMAGES = {
    f"toxic-mod-backend:{SHA}": DIGEST,
    f"toxic-mod-frontend:{SHA}": DIGEST,
    f"toxic-mod-frontend:{SHA}-reviewer": DIGEST,
    f"toxic-mod-monitoring:{SHA}": DIGEST,
    f"toxic-mod-rescorer:{SHA}": DIGEST,
}

AWS_STUB = r'''#!/usr/bin/env python3
"""A fake `aws`, driven by JSON in the environment and recording every call it is given."""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["STUB_CALL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(argv) + "\n")


def opt(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


if argv[:2] == ["ssm", "get-parameter"]:
    store = json.loads(os.environ.get("STUB_PARAMS", "{}"))
    name = opt("--name")
    if name not in store:
        print(f"An error occurred (ParameterNotFound) for {name}", file=sys.stderr)
        sys.exit(254)
    print(store[name])
    sys.exit(0)

if argv[:2] == ["secretsmanager", "get-secret-value"]:
    store = json.loads(os.environ.get("STUB_SECRETS", "{}"))
    secret_id = opt("--secret-id")
    if secret_id not in store:
        print(f"An error occurred (ResourceNotFoundException) for {secret_id}", file=sys.stderr)
        sys.exit(254)
    print(store[secret_id])
    sys.exit(0)

if argv[:2] == ["ecr", "batch-get-image"]:
    store = json.loads(os.environ.get("STUB_IMAGES", "{}"))
    key = f"{opt('--repository-name')}:{opt('--image-ids', '').split('=', 1)[-1]}"
    # ECR exits 0 and reports `None` for a tag that does not exist. That is the whole trap.
    print(store.get(key, "None"))
    sys.exit(0)

if argv[:2] == ["ecr", "get-login-password"]:
    print("a-registry-token")
    sys.exit(0)

print(f"unexpected aws call: {argv}", file=sys.stderr)
sys.exit(9)
'''

RECORDER = (
    "#!/usr/bin/env bash\n"
    'printf "%s %s\\n" "$(basename "$0")" "$*" >> "${STUB_CALL_LOG}"\n'
    "cat >/dev/null 2>&1 || true\n"
    "exit 0\n"
)


@pytest.fixture()
def instance(tmp_path: Path):
    """A blank box: no /opt/toxic, no /etc/toxic, no unit, no ECR helper."""
    bin_dir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    make_stub(bin_dir, "aws", AWS_STUB)
    make_stub(bin_dir, "docker", RECORDER)
    make_stub(bin_dir, "systemctl", RECORDER)
    destdir = tmp_path / "root"
    (destdir / "opt/toxic").mkdir(parents=True)
    (destdir / "opt/toxic/MODEL_CARD.md").write_text(
        "## Artifact digest of record\n\n"
        f"| `toxic-clf.skops` | `{'a' * 64}` |\n\n"
        f"- MODEL_ARTIFACT: toxic-clf\n- MODEL_REGISTRY_VERSION: 3\n"
        f"- MODEL_DIGEST: sha256:{'a' * 64}\n",
        encoding="utf-8",
    )
    for name in ("compose.backend.yml", "compose.frontend.yml", "compose.monitoring.yml",
                 "toxic-stack.service"):
        (destdir / "opt/toxic" / name).write_text(
            (COMPOSE_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    # A recording stand-in for the fetcher, which has its own file. Standing it in here --
    # rather than adding a skip flag to roll.sh -- keeps the escape hatch out of the
    # production script, and makes "was it called, and with which artifact names?" an
    # observable fact rather than a source reading.
    (destdir / "opt/toxic/fetch_artifacts.sh").write_text(
        "#!/usr/bin/env bash\n"
        'printf "fetch_artifacts names=%s dir=%s bucket=%s card=%s\\n" '
        '"${ARTIFACT_NAMES:-<default>}" "${ARTIFACT_DIR:-}" "${DEPLOY_BUCKET:-}" '
        '"${MODEL_CARD_PATH:-}" >> "${STUB_CALL_LOG}"\n',
        encoding="utf-8",
    )

    def roll(component: str, *, images=None, parameters=None, sha: str = SHA):
        return run(
            ROLL,
            [sha, component],
            bin_dir,
            env={
                "DESTDIR": str(destdir),
                "STUB_CALL_LOG": str(log),
                "STUB_PARAMS": json.dumps(parameters if parameters is not None else PARAMETERS),
                "STUB_SECRETS": json.dumps(SECRETS),
                "STUB_IMAGES": json.dumps(images if images is not None else IMAGES),
            },
        )

    return type("Instance", (), {"roll": staticmethod(roll), "root": destdir, "log": log})


def _env_file(instance, name: str) -> dict[str, str]:
    text = (instance.root / "etc/toxic" / name).read_text(encoding="utf-8")
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if line and not line.startswith("#")
    )


# --- the roll, run ------------------------------------------------------------------------


@pytest.mark.parametrize("component", ["backend", "frontend", "monitoring"])
def test_the_roll_succeeds_on_a_box_that_has_none_of_the_new_layout(instance, component):
    """The blocker this script is designed around: the fleet never received the new user
    data, so nothing has created /etc/toxic, the unit, or the ECR helper."""
    result = instance.roll(component)
    assert result.returncode == 0, result.stdout + result.stderr
    root = instance.root
    assert (root / "etc/toxic").is_dir()
    assert (root / "var/lib/toxic/artifacts").is_dir()
    assert (root / "var/lib/toxic/spool").is_dir()
    assert (root / "usr/local/bin/toxic-ecr-login").is_file()
    assert (root / "etc/systemd/system/toxic-stack.service").is_file()
    assert (root / "opt/toxic/compose.yml").resolve().name == f"compose.{component}.yml"


def test_the_etc_directory_is_not_world_readable(instance):
    instance.roll("backend")
    mode = stat.S_IMODE((instance.root / "etc/toxic").stat().st_mode)
    assert mode == 0o700, oct(mode)


@pytest.mark.parametrize("component", ["backend", "frontend", "monitoring"])
def test_every_env_file_a_compose_file_names_is_written(instance, component):
    """`docker compose` hard-fails on a missing env_file, so a name in a compose file is a
    promise this script has to keep. Read out of the compose files rather than listed, so a
    service added later cannot arrive with an env file nothing writes."""
    instance.roll(component)
    compose = yaml.safe_load((COMPOSE_DIR / f"compose.{component}.yml").read_text())
    named = {
        entry
        for service in compose["services"].values()
        for entry in service.get("env_file", [])
    }
    assert named, f"compose.{component}.yml names no env file, so this asserts nothing"
    for path in named:
        assert (instance.root / path.lstrip("/")).is_file(), f"{path} was never written"


@pytest.mark.parametrize("component", ["backend", "frontend", "monitoring"])
def test_the_env_files_are_readable_only_by_root(instance, component):
    instance.roll(component)
    for path in (instance.root / "etc/toxic").iterdir():
        if path.suffix == ".env":
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600, f"{path.name} is {oct(mode)}"


def test_stack_env_defines_every_variable_a_compose_file_interpolates(instance):
    instance.roll("backend")
    stack = _env_file(instance, "stack.env")
    referenced = set()
    for path in COMPOSE_DIR.glob("compose.*.yml"):
        referenced |= set(re.findall(r"\$\{([A-Z_]+)\}", path.read_text(encoding="utf-8")))
    assert referenced, "no compose file interpolates anything, so this asserts nothing"
    assert referenced <= set(stack), f"stack.env is missing {sorted(referenced - set(stack))}"


def test_the_images_this_host_runs_are_pinned_to_digests(instance):
    for component, keys in (
        ("backend", ["BACKEND_IMAGE"]),
        ("frontend", ["FRONTEND_IMAGE", "REVIEWER_IMAGE"]),
        ("monitoring", ["MONITORING_IMAGE"]),
    ):
        instance.roll(component)
        stack = _env_file(instance, "stack.env")
        for key in keys:
            assert "@sha256:" in stack[key], f"{key} floats a tag: {stack[key]}"
            assert f":{SHA}" not in stack[key], f"{key} still names a tag: {stack[key]}"


def test_a_missing_image_fails_the_roll_instead_of_writing_an_unpullable_reference(instance):
    """`aws ecr batch-get-image` exits 0 and prints `None` for a tag that does not exist.
    A script that trusts the exit code writes `...@None` into stack.env, the roll reports
    success, and the unit fails to pull minutes later on a host with no SSH."""
    result = instance.roll("backend", images={})
    assert result.returncode != 0
    assert "None" not in (instance.root / "etc/toxic/stack.env").read_text(encoding="utf-8") \
        if (instance.root / "etc/toxic/stack.env").exists() else True
    assert re.search(r"no image (digest )?for", result.stderr), result.stderr


def test_the_backend_env_carries_every_setting_the_application_requires(instance):
    """backend/config.py refuses to start when one of these is missing. Imported rather than
    listed, so a setting added there turns this red instead of turning the container red."""
    from backend.config import REQUIRED

    instance.roll("backend")
    written = _env_file(instance, "backend.env")
    missing = sorted(set(REQUIRED) - set(written))
    assert not missing, f"the backend will refuse to start: missing {missing}"
    assert written["MODEL_DIGEST"] == "a" * 64, (
        "MODEL_DIGEST must be the bare hex backend/model_card.py compares against, "
        "not the sha256:-prefixed form"
    )
    assert written["MODEL_CARD_PATH"] == "/app/MODEL_CARD.md"
    assert written["THRESHOLDS_PATH"].startswith("/artifacts/")


def test_the_backend_roll_fetches_the_model_artifact(instance):
    """The env file above promises /artifacts/toxic-clf.skops. Something has to put it there,
    and the container mounts that directory read-only, so it cannot be the container."""
    instance.roll("backend")
    calls = instance.log.read_text(encoding="utf-8")
    assert "fetch_artifacts" in calls, calls
    assert "bucket=toxic-mod-deploy-example" in calls
    assert f"dir={instance.root}/var/lib/toxic/artifacts" in calls


def test_the_reviewer_console_can_actually_authenticate(instance):
    """backend/review_api.py reads both, and `current_reviewer` returns None if either is
    empty -- so a backend missing them serves a review queue nobody can ever log in to."""
    instance.roll("backend")
    written = _env_file(instance, "backend.env")
    assert written["REVIEWER_SHARED_SECRET"] == REVIEWER_SECRET
    assert written["REVIEWER_ID"] == "rock"


def test_the_frontend_talks_to_the_backend_over_the_private_address(instance):
    """sg-frontend permits egress to 8000 only inside the public subnet CIDRs. A UI pointed
    at the backend's Elastic IP renders perfectly and times out on every prediction."""
    instance.roll("frontend")
    written = _env_file(instance, "frontend.env")
    assert written["BACKEND_URL"] == PARAMETERS["/toxic/endpoints/backend-internal"]
    assert written["DEMO_API_KEY"] == DEMO_KEY


def test_the_monitoring_host_gets_a_read_only_dsn_and_never_the_master_credential(instance):
    instance.roll("monitoring")
    written = _env_file(instance, "monitoring.env")
    assert "monitor_ro" in written["MONITORING_DB_DSN"]
    assert MASTER_PASSWORD not in written["MONITORING_DB_DSN"]
    assert written["BASELINE_PATH"].startswith("/artifacts/")
    calls = instance.log.read_text(encoding="utf-8")
    assert "secret:master" not in calls, "the dashboard tier read the RDS master secret"


def test_a_password_with_url_metacharacters_survives_into_the_dsn(instance):
    """RDS generates the master password and it is not guaranteed to be URL-safe. An
    unencoded `#` truncates the DSN at the host and the backend connects to the wrong
    database -- or, with a `/`, to a database whose name is half a password."""
    instance.roll("backend")
    written = _env_file(instance, "backend.env")
    assert MASTER_PASSWORD not in written["DATABASE_URL"], "the password is not URL-encoded"
    assert written["DATABASE_URL"].endswith("/toxicmod")
    assert "%23" in written["DATABASE_URL"] and "%2F" in written["DATABASE_URL"]


def test_no_secret_value_reaches_stdout_stderr_or_the_call_log(instance):
    """The instance's stdout is captured verbatim into the SSM invocation record, which is
    readable by anyone who can call `aws ssm get-command-invocation`."""
    for component in ("backend", "frontend", "monitoring"):
        result = instance.roll(component)
        printed = result.stdout + result.stderr + instance.log.read_text(encoding="utf-8")
        for sentinel in SENTINELS:
            assert sentinel not in printed, f"{sentinel} was printed during the {component} roll"


def test_the_roll_is_idempotent(instance):
    """The fleet is up for an unknown grading window and must not go dark, so re-rolling the
    same SHA has to be a no-op that succeeds rather than a second first-run."""
    first = instance.roll("backend")
    assert first.returncode == 0, first.stderr
    before = (instance.root / "etc/toxic/stack.env").read_text(encoding="utf-8")
    second = instance.roll("backend")
    assert second.returncode == 0, second.stderr
    assert (instance.root / "etc/toxic/stack.env").read_text(encoding="utf-8") == before


def test_the_superseded_unit_from_the_applied_bootstrap_is_stood_down(instance):
    """The running instances carry `toxic-mod.service` pointing at
    /opt/toxic-mod/docker-compose.yml. Two units managing containers on one Docker daemon is
    a race whose loser is whichever one ran `compose down` last."""
    legacy = instance.root / "etc/systemd/system/toxic-mod.service"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("[Unit]\nDescription=superseded\n", encoding="utf-8")
    result = instance.roll("backend")
    assert result.returncode == 0, result.stderr
    calls = instance.log.read_text(encoding="utf-8")
    assert re.search(r"systemctl .*disable.*toxic-mod\.service", calls), calls


def test_the_unit_installed_is_the_one_in_version_control(instance):
    instance.roll("backend")
    installed = (instance.root / "etc/systemd/system/toxic-stack.service").read_text()
    assert installed == (COMPOSE_DIR / "toxic-stack.service").read_text()


def test_an_unknown_component_is_refused(instance):
    result = instance.roll("kubernetes")
    assert result.returncode != 0
    assert "component" in result.stderr


def test_a_missing_parameter_fails_the_roll_rather_than_writing_an_empty_value(instance):
    thinned = {k: v for k, v in PARAMETERS.items() if k != "/toxic/db/endpoint"}
    result = instance.roll("backend", parameters=thinned)
    assert result.returncode != 0


@pytest.mark.parametrize("empty", ["", "None"])
def test_a_parameter_that_exists_but_is_empty_also_fails_the_roll(instance, empty):
    """`aws ssm get-parameter --output text` prints `None` for a null value and exits 0, and
    an empty String parameter prints a blank line and exits 0. Either one silently becomes
    `DATABASE_URL=postgresql+psycopg://u:p@/toxicmod` -- a DSN that connects to a local
    socket -- if the only check is the exit code."""
    hollowed = dict(PARAMETERS, **{"/toxic/db/endpoint": empty})
    result = instance.roll("backend", parameters=hollowed)
    assert result.returncode != 0, result.stdout
    assert "empty" in result.stderr, result.stderr


# --- bootstrap, run -----------------------------------------------------------------------

BOOTSTRAP_AWS_STUB = r'''#!/usr/bin/env python3
"""A fake `aws` for the payload pull. `s3 cp --recursive` copies whatever STUB_PAYLOAD holds
-- including nothing, which is exactly what a real one does for a prefix that has no
objects, while still exiting 0."""
import json
import os
import shutil
import sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["STUB_CALL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(argv) + "\n")

if argv[:2] == ["ssm", "get-parameter"]:
    print(json.loads(os.environ["STUB_PARAMS"])[argv[argv.index("--name") + 1]])
    sys.exit(0)

if argv[:2] == ["s3", "cp"]:
    source, destination = argv[-2], argv[-1]
    payload = Path(os.environ["STUB_PAYLOAD"])
    if payload.is_dir():
        shutil.copytree(payload, destination, dirs_exist_ok=True)
    # Exit 0 either way. A prefix with no objects is not an error to the S3 API.
    sys.exit(0)

print(f"unexpected aws call: {argv}", file=sys.stderr)
sys.exit(9)
'''


@pytest.fixture()
def box(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    make_stub(bin_dir, "aws", BOOTSTRAP_AWS_STUB)
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "roll.sh").write_text(
        '#!/usr/bin/env bash\nprintf "roll.sh %s\\n" "$*" >> "${STUB_CALL_LOG}"\n',
        encoding="utf-8",
    )
    destdir = tmp_path / "root"

    def bootstrap(*args, payload_dir: Path | None = None):
        return run(
            BOOTSTRAP,
            list(args),
            bin_dir,
            env={
                "DESTDIR": str(destdir),
                "STUB_CALL_LOG": str(log),
                "STUB_PARAMS": json.dumps({"/toxic/deploy/bucket": "toxic-mod-deploy-example"}),
                "STUB_PAYLOAD": str(payload if payload_dir is None else payload_dir),
            },
        )

    return type("Box", (), {"bootstrap": staticmethod(bootstrap), "log": log,
                            "payload": payload, "root": destdir})


def test_bootstrap_pulls_the_payload_and_hands_off_to_the_roll(box):
    result = box.bootstrap(SHA, "backend")
    assert result.returncode == 0, result.stderr
    calls = box.log.read_text(encoding="utf-8")
    assert f"s3://toxic-mod-deploy-example/deploy/{SHA}/" in calls, calls
    assert f"roll.sh {SHA} backend" in calls, calls


def test_bootstrap_fails_when_the_prefix_for_that_sha_holds_nothing(box, tmp_path):
    """`aws s3 cp --recursive` exits 0 having copied NOTHING when the prefix does not exist.
    Trusting that exit code re-runs the PREVIOUS roll.sh already on the box, reports success,
    and leaves the old version serving -- a deploy that goes green and changes nothing."""
    empty = tmp_path / "empty"
    empty.mkdir()
    result = box.bootstrap(SHA, "backend", payload_dir=empty)
    assert result.returncode != 0
    assert "nothing was copied" in result.stderr, result.stderr
    assert "roll.sh" not in box.log.read_text(encoding="utf-8")


def test_bootstrap_without_a_sha_is_a_usage_error_and_touches_nothing(box):
    result = box.bootstrap()
    assert result.returncode != 0
    assert "usage" in result.stderr
    assert "s3" not in box.log.read_text(encoding="utf-8")


# --- properties asserted about the source ------------------------------------------------


def test_the_destdir_escape_hatch_defaults_to_the_real_filesystem():
    """A test-only prefix that defaults to anything else is a deploy that writes nowhere."""
    body = ROLL.read_text(encoding="utf-8")
    assert re.search(r'DESTDIR="\$\{DESTDIR:-\}"', body), "DESTDIR does not default to empty"


def test_every_secret_is_read_on_the_instance_from_secrets_manager():
    body = ROLL.read_text(encoding="utf-8")
    assert "secretsmanager get-secret-value" in body


def test_no_secret_name_is_hardcoded_in_the_roll():
    """The plan for this phase hardcoded `--secret-id toxic/wandb-api-key`. The applied
    account has `toxic-mod/wandb-api-key`, and every read would have failed with
    ResourceNotFoundException inside an SSM invocation. Names come from Terraform."""
    body = ROLL.read_text(encoding="utf-8")
    offenders = re.findall(r"--secret-id\s+([\"']?)(?!\$)([^\s\"']+)", body)
    assert not offenders, (
        f"a literal secret id cannot be contradicted by a terraform plan: {offenders}"
    )


def test_the_written_env_files_are_not_world_readable():
    body = ROLL.read_text(encoding="utf-8")
    assert re.search(r"(umask 0?077|chmod 0?600)", body), "env files hold live credentials"


def test_secret_values_are_never_echoed():
    """Read per statement, not over the whole file. A substring scan for `set -x` matches the
    comment in roll.sh explaining why there is no `set -x` -- the fifth time in this
    repository that a text scan has flagged the documentation of the rule it enforces, and a
    scanner that cries wolf on comments is a scanner someone switches off."""
    for path in (BOOTSTRAP, ROLL):
        body = path.read_text(encoding="utf-8")
        statements = [line.split("#", 1)[0].strip() for line in body.splitlines()]
        traced = [line for line in statements if re.match(r"^set\s+(-\w*x|-o\s+xtrace)", line)]
        assert not traced, f"{path} traces every expansion, including secrets: {traced}"
        for name in SECRET_NAMES:
            assert not re.search(rf"echo[^\n]*\$\{{?{name}", body), f"{path} echoes {name}"


def test_bootstrap_pins_the_payload_to_the_requested_sha():
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert "deploy/${SHA}/" in body
    assert "aws s3 cp" in body


def test_bootstrap_refuses_to_run_without_a_sha():
    assert "${1:?" in BOOTSTRAP.read_text(encoding="utf-8")


def test_the_database_password_is_never_written_to_a_file_in_plaintext_by_the_operator():
    """RDS manages it in Secrets Manager. roll.sh reads it, nothing else may."""
    body = ROLL.read_text(encoding="utf-8")
    assert "master-secret-arn" in body


def test_every_parameter_the_scripts_read_is_one_terraform_publishes():
    """Both directions. A name a script reads and Terraform never published is a
    ParameterNotFound inside an SSM invocation; a name Terraform publishes and nothing reads
    is a contract nobody is maintaining."""
    read = set()
    for path in (BOOTSTRAP, ROLL):
        body = path.read_text(encoding="utf-8")
        read |= set(re.findall(r'"?\$\{PARAM_PREFIX\}(/[a-z0-9/-]+)"?', body))
        read |= set(re.findall(r'--name\s+"?(/toxic/[a-z0-9/-]+)"?', body))
    read = {name if name.startswith("/toxic") else f"/toxic{name}" for name in read}
    published = set(re.findall(r'"(/toxic/[A-Za-z0-9/_-]+)"\s*=', tfparse.source_of("deploy.tf")))
    assert read, "no parameter reads were found, so this comparison certifies nothing"
    assert read <= published, f"read but never published: {sorted(read - published)}"
    # The three public listener URLs are read by the deploy job, which passes them to
    # verify_deploy.sh as BACKEND_URL / FRONTEND_URL / MONITORING_URL. Nothing on an instance
    # reads them, and the instance that COULD read its own would be reporting on itself.
    # Named here so that any OTHER unread parameter is a failure rather than a shrug.
    read_by_the_deploy_job_not_the_instance = {
        "/toxic/endpoints/backend",
        "/toxic/endpoints/frontend",
        "/toxic/endpoints/monitoring",
    }
    unread = published - read - read_by_the_deploy_job_not_the_instance
    assert not unread, f"published but read by nothing at all: {sorted(unread)}"


# --- the SendCommand payload -------------------------------------------------------------
#
# deploy.yml is written by a later task. The two rules below are pure functions so they are
# exercised NOW, against workflows that do leak, and applied to the real file the moment it
# exists. A rule that has only ever been run over a corpus with nothing in it is a rule that
# would keep passing after being deleted.


def payload_defects(text: str) -> list[str]:
    """SendCommand payloads that are a script BODY rather than a script reference."""
    defects = []
    for command in re.findall(r"ssm_run\.sh\s+\S+\s+\S+\s+(.+)", text):
        if "/opt/toxic/" not in command:
            defects.append(f"not a script reference: {command}")
        elif len(command) >= 120:
            defects.append(f"a script body, not a reference: {command[:60]}...")
    return defects


def secret_leaks(text: str) -> list[str]:
    """Secrets interpolated into a line that becomes a CloudTrail record."""
    leaks = []
    for line in text.splitlines():
        if "ssm_run.sh" not in line:
            continue
        if "${{ secrets." in line:
            leaks.append(f"a GitHub secret is interpolated into SSM: {line.strip()}")
        for name in SECRET_NAMES:
            if f"{name}=" in line:
                leaks.append(f"{name} travels as a SendCommand parameter: {line.strip()}")
    return leaks


GOOD_INVOCATION = (
    "          infra/aws/ssm_run.sh backend 1 bash /opt/toxic/bootstrap.sh $SHA backend\n"
)
BODY_INVOCATION = (
    "          infra/aws/ssm_run.sh backend 1 "
    "set -e; cd /opt/toxic; aws s3 cp s3://b/deploy/$SHA/ . --recursive; bash roll.sh $SHA; "
    "systemctl restart toxic-stack\n"
)
LEAKY_INVOCATION = (
    "          infra/aws/ssm_run.sh backend 1 "
    "WANDB_API_KEY=${{ secrets.WANDB_API_KEY }} bash /opt/toxic/bootstrap.sh $SHA\n"
)


def test_the_payload_rules_actually_refuse_something():
    assert not payload_defects(GOOD_INVOCATION)
    assert payload_defects(BODY_INVOCATION)
    assert not secret_leaks(GOOD_INVOCATION)
    assert len(secret_leaks(LEAKY_INVOCATION)) == 2


@pytest.mark.skipif(not WORKFLOW.exists(), reason="deploy.yml is written by a later task")
def test_the_send_command_payload_is_one_line_naming_a_script():
    body = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"ssm_run\.sh\s", body), "deploy.yml does not call ssm_run.sh"
    assert not payload_defects(body)


@pytest.mark.skipif(not WORKFLOW.exists(), reason="deploy.yml is written by a later task")
def test_send_command_payload_contains_no_secret_value():
    assert not secret_leaks(WORKFLOW.read_text(encoding="utf-8"))


def test_the_scripts_are_executable():
    for path in (BOOTSTRAP, ROLL):
        assert os.access(path, os.X_OK), f"chmod +x {path}"
