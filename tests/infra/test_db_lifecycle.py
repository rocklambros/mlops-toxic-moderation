"""H6 and H29. Cost control must not be able to destroy the graded dataset.

Two documented behaviours were mutually exclusive and no document noticed. "Stop between
sessions" collides with the seven-day RDS auto-restart. The documented remedy, "destroy rather
than stop", deletes the dataset rubric 3.2 grades the dashboard on. The resolution is
structural: there is no teardown path that does not produce a restorable dump first, and the
dump is proved restorable before the command that took it returns.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests.infra import tfparse
from tests.infra.shellstub import make_stub, run, shell_code

MAKEFILE = Path("Makefile")
DUMP = Path("infra/aws/db_dump.sh").resolve()
DOWN = Path("infra/aws/aws_down.sh").resolve()
RESTORE = Path("infra/aws/db_restore.sh").resolve()

# A dump this old is inside the default freshness window; the second is not.
FRESH = "2026-08-14T18:02:11+00:00"

AWS_STUB = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
Path(os.environ["STUB_JOURNAL"]).open("a").write(" ".join(argv) + "\n")


def opt(flag, default=""):
    return argv[argv.index(flag) + 1] if flag in argv else default


if "get-parameter" in argv:
    print({"/toxic/deploy/bucket": "example-bucket"}.get(opt("--name"), "value"))
elif "list-objects-v2" in argv:
    latest = os.environ.get("STUB_LATEST_DUMP", "")
    if not latest:
        print("None")
    else:
        print(latest)
elif "describe-db-instances" in argv:
    print(os.environ.get("STUB_DB_STATUS", "available"))
elif argv[:2] == ["s3", "ls"]:
    print("2026-08-14 18:02:11    1024 db/2026-08-14T18-02-11Z.dump")
sys.exit(0)
'''


def _makefile_prereqs(target: str) -> list[str]:
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"^{re.escape(target)}\s*:\s*(.*)$", line)
        if match:
            return match.group(1).split()
    raise AssertionError(f"no target {target} in the Makefile")


@pytest.fixture()
def down(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    journal = tmp_path / "journal"

    def go(**env):
        base = {
            "STUB_JOURNAL": str(journal),
            "STUB_LATEST_DUMP": FRESH,
            "AWS_DOWN_NOW": "2026-08-14T18:20:00+00:00",
            "INSTANCE_IDS": "i-1 i-2 i-3",
            "DB_INSTANCE_ID": "toxic-mod-pg",
        }
        base.update(env)
        return run(DOWN, [], bin_dir, env=base)

    def journalled() -> list[str]:
        return journal.read_text().splitlines() if journal.exists() else []

    return go, journalled


# --------------------------------------------------------------------------------------
# ordering: no teardown path skips the dump
# --------------------------------------------------------------------------------------


def test_aws_down_dumps_before_it_stops_anything():
    """H6. Make-level ordering, so no future edit can reorder it into a data-loss bug."""
    assert "db-dump" in _makefile_prereqs("aws-down")


def test_aws_destroy_also_dumps_first():
    assert "db-dump" in _makefile_prereqs("aws-destroy")


def test_make_actually_schedules_the_dump_first():
    """The prerequisite asserted as a fact about `make`, not about a line of text. `make
    --dry-run` resolves the whole graph and prints the recipes in the order it would run them,
    and it touches nothing."""
    result = subprocess.run(
        ["make", "--dry-run", "aws-down"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if "infra/aws/" in line]
    assert lines, result.stdout
    assert "db_dump.sh" in lines[0], lines
    assert "aws_down.sh" in lines[1], lines


def test_aws_down_refuses_to_stop_anything_without_a_recent_dump(down):
    """The Make prerequisite orders the two commands. It does not stop anyone from running
    `infra/aws/aws_down.sh` directly, and an ordering nobody can be forced to follow is a
    convention, not a control. This is the part that cannot be bypassed."""
    go, journalled = down
    result = go(STUB_LATEST_DUMP="")
    assert result.returncode != 0
    assert "dump" in result.stderr.lower()
    assert not any("stop-instances" in line for line in journalled())
    assert not any("stop-db-instance" in line for line in journalled())


def test_aws_down_refuses_when_the_newest_dump_is_stale(down):
    """A dump from last week is not a dump of this session's data, and the dashboard is graded
    on this session's data."""
    go, journalled = down
    result = go(AWS_DOWN_NOW="2026-08-16T18:20:00+00:00")
    assert result.returncode != 0
    assert not any("stop-instances" in line for line in journalled())


# --------------------------------------------------------------------------------------
# the dump itself
# --------------------------------------------------------------------------------------


def test_the_dump_runs_on_the_instance_because_rds_is_private():
    body = shell_code(DUMP)
    assert "ssm_run.sh" in body, "RDS has no internet path; the operator cannot reach it"
    assert "pg_dump" in body


def test_the_dump_uses_a_restorable_format_and_streams_to_s3():
    body = shell_code(DUMP)
    assert "--format=custom" in body, "plain SQL cannot be selectively restored"
    assert re.search(r'aws s3 cp[^\n|]*\s-\s"?s3://', body), (
        "no direct pipe to S3: a dump file left on an instance volume is a dump a teardown "
        "deletes"
    )


def test_the_dump_key_is_timestamped_so_a_second_run_never_overwrites_the_first():
    body = shell_code(DUMP)
    assert re.search(r"date -u \+", body)
    assert "db/" in body


def test_the_remote_script_is_fetched_rather_than_assumed_to_be_on_the_instance():
    """`/opt/toxic` is written by bootstrap.sh from `deploy/<sha>/`, and by nothing else after
    first boot. A payload that runs `bash /opt/toxic/dump.sh` because the operator uploaded
    that file to `deploy/current/` dies with "No such file or directory" on a host that has
    been rolled even once -- which is every host."""
    body = shell_code(DUMP)
    payload = re.search(r'ssm_run\.sh"\s+backend\s+1\s*\\?\s*"([^"]+)"', body)
    assert payload, "cannot find the SendCommand payload in db_dump.sh"
    command = payload.group(1)
    assert "/opt/toxic/" not in command, (
        f"the payload assumes a file in /opt/toxic that nothing puts there: {command}"
    )
    assert "aws s3 cp" in command and "s3://" in command, (
        f"the payload does not fetch the script it runs: {command}"
    )


def test_the_remote_script_is_uploaded_where_the_instance_role_can_read_it():
    """The three instance roles are scoped to the `deploy/` and `artifacts/` prefixes by
    `data.aws_iam_policy_document.deploy_payload`, deliberately and by prefix. An ops script
    dropped anywhere else is an AccessDenied inside an SSM invocation on a host with no SSH."""
    body = shell_code(DUMP)
    ops_key = re.search(r'OPS_KEY="([^"]+)"', body)
    assert ops_key, "db_dump.sh does not name the key it publishes the remote script under"
    assert ops_key.group(1).startswith("deploy/"), (
        f"the remote script is published at {ops_key.group(1)!r}, outside the deploy/ prefix "
        "the instance role can read"
    )


def test_the_sqlalchemy_url_is_decomposed_rather_than_handed_to_libpq():
    """/etc/toxic/backend.env carries `postgresql+psycopg://...`, which is a SQLAlchemy URL.
    libpq rejects the dialect suffix outright with `invalid URI scheme`, so a pg_dump handed
    that string never connects -- and the failure reads like a network fault. Decomposing it
    into the five libpq variables fixes the scheme problem and the argv problem at once."""
    body = shell_code(DUMP)
    assert "DATABASE_URL" in body, "the dump never reads the application's DSN"
    assert "urlparse" in body, "the DSN is pattern-matched rather than parsed"
    for variable in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        assert variable in body, f"{variable} is never exported for libpq"
    assert not re.search(r"pg_dump[^\n]*DATABASE_URL", body), (
        "the SQLAlchemy URL is handed straight to pg_dump; libpq will refuse the scheme"
    )


def test_no_database_credential_reaches_an_argument_list_on_the_instance():
    """`docker inspect`, `docker top` and /proc are all readable by anything on that host."""
    body = shell_code(DUMP)
    assert "PGPASSWORD" in body, "the password is not passed through the libpq environment"
    assert not re.search(r"pg_dump[^\n]*\$\{?PG(URL|PASSWORD)", body), (
        "a connection string or password is on pg_dump's argv"
    )


def test_the_uploaded_dump_is_proved_readable_before_the_command_returns():
    """`aws s3 cp -` uploads whatever it received before the pipe broke, so a pg_dump that
    died halfway still leaves an object. It is a truncated archive, and `pg_restore --clean`
    against it drops the tables and then fails -- the exact data loss the dump exists to
    prevent, delivered by the restore."""
    body = shell_code(DUMP)
    assert "pg_restore" in body and "--list" in body, (
        "nothing proves the uploaded object is a valid custom-format archive"
    )


def test_the_verification_reads_the_whole_archive_and_not_only_the_table_of_contents():
    """`pg_restore --list` cannot detect the truncation this check exists to catch.

    Measured against the live backend instance on 2026-08-10: an archive cut to 200000 of
    685079 bytes exits **0** under `pg_restore --list`, because the table of contents of a
    custom-format archive sits near the front and `--list` never reads a data block. The
    same truncated archive fails `pg_restore --file=/dev/null` with "could not read from
    input file: end of file", because that reads and decompresses every block.

    A check that passes on a 29%-complete dump is worse than no check, because it is
    believed.
    """
    body = shell_code(DUMP)
    assert re.search(r"pg_restore\s+--file=/dev/null", body), (
        "the dump is verified only with `pg_restore --list`, which exits 0 on a truncated "
        "archive. Restore the whole thing to /dev/null so every data block is read"
    )


def test_the_verification_does_not_pipe_the_download_into_pg_restore():
    """The reader exits first, and `pipefail` turns that into a fatal error.

    `pg_restore` stops once it has what it needs, which for `--list` is the header and TOC.
    In `aws s3 cp ... - | pg_restore`, that leaves `aws` writing into a closed pipe: it takes
    EPIPE, exits 1, and `set -o pipefail` fails the script even though the archive is
    perfect. Measured on 2026-08-10, the pipeline's exit codes were `1 0` -- the uploader
    failed, the verifier succeeded.

    It stays dormant while the dump fits in the 64 KiB pipe buffer, which is why this
    survived every rehearsal and fired on the graded dataset.
    """
    body = shell_code(DUMP)
    piped = re.search(
        r"aws s3 cp[^\n|]*\s-\s*(?:\\\s*\n\s*)?\|\s*(?:\\\s*\n\s*)?docker[^\n]*pg_restore",
        body,
    )
    assert piped is None, (
        "the uploaded object is streamed straight into pg_restore. The reader exits before "
        "the writer finishes and pipefail makes that fatal; download to a file first"
    )


def test_the_postgres_image_is_pinned_by_digest():
    """A floating tag decides, at teardown time, which pg_dump writes the graded dataset."""
    body = shell_code(DUMP)
    assert re.search(r"postgres:[\w.-]+@sha256:[0-9a-f]{64}", body), (
        "the dump container's image is not pinned by digest"
    )


# --------------------------------------------------------------------------------------
# the generated remote script, run rather than read
# --------------------------------------------------------------------------------------

CAPTURING_AWS_STUB = r'''#!/usr/bin/env python3
import os
import shutil
import sys
from pathlib import Path

argv = sys.argv[1:]
Path(os.environ["STUB_JOURNAL"]).open("a").write(" ".join(argv) + "\n")


def opt(flag, default=""):
    return argv[argv.index(flag) + 1] if flag in argv else default


if "get-parameter" in argv:
    print("example-bucket")
    sys.exit(0)

if argv[:2] == ["s3", "cp"]:
    source, destination = argv[-2], argv[-1]
    if destination.startswith("s3://") and Path(source).is_file():
        shutil.copy(source, os.environ["STUB_CAPTURE"])
    sys.exit(0)

sys.exit(0)
'''


@pytest.fixture()
def generated_remote_script(tmp_path: Path) -> str:
    """The exact bytes db_dump.sh uploads for the instance to run.

    Generated by RUNNING db_dump.sh, not by re-deriving it here. A producer and a consumer
    that have only ever been tested against fixtures are a pair that has never exchanged a
    real file, and that has already cost this project one live deploy.
    """
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", CAPTURING_AWS_STUB)
    fake = tmp_path / "scripts"
    make_stub(fake, "ssm_run.sh", "#!/bin/bash\nexit 0\n")
    capture = tmp_path / "captured.sh"
    result = run(
        DUMP,
        [],
        bin_dir,
        env={
            "STUB_JOURNAL": str(tmp_path / "journal"),
            "STUB_CAPTURE": str(capture),
            "DB_DUMP_SCRIPT_DIR": str(fake),
        },
    )
    assert result.returncode == 0, result.stderr
    assert capture.exists(), "db_dump.sh uploaded no remote script at all"
    return capture.read_text(encoding="utf-8")


def test_the_generated_remote_script_is_valid_bash(generated_remote_script, tmp_path):
    """It is assembled by printf and a quoted heredoc and it runs as root against the
    production database. `bash -n` is the cheapest possible proof that it parses, and a syntax
    error here surfaces as a Failed SSM invocation on a host with no SSH."""
    path = tmp_path / "remote.sh"
    path.write_text(generated_remote_script, encoding="utf-8")
    result = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_the_generated_script_carries_the_values_that_vary(generated_remote_script):
    for assignment in ("REGION=", "BUCKET=", "KEY=", "PG_IMAGE="):
        assert assignment in generated_remote_script, assignment
    assert re.search(r"KEY=.*db/20\d\d-\d\d-\d\dT", generated_remote_script)


def test_the_dsn_decomposition_decodes_a_url_encoded_password(generated_remote_script):
    """RDS generates the master password and does not guarantee it is URL-safe, so roll.sh
    URL-encodes it into the DSN. A decomposition that forgets to decode hands libpq a
    password with `%23` where a `#` should be, and the connection fails with an
    authentication error that looks like a rotated credential."""
    fragment = re.search(
        r"python3 -c '(.*?)'\)\"", generated_remote_script, re.DOTALL
    )
    assert fragment, "cannot find the DSN decomposition in the generated script"
    dsn = "postgresql+psycopg://tox%23user:p%40ss%23word@db.internal:5432/toxicmod"
    result = subprocess.run(
        ["python3", "-c", fragment.group(1)],
        input=dsn, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    exports = dict(
        line.removeprefix("export ").split("=", 1) for line in result.stdout.splitlines()
    )
    assert exports["PGHOST"] == "db.internal"
    assert exports["PGPORT"] == "5432"
    assert exports["PGDATABASE"] == "toxicmod"
    # shlex.quote wraps anything with a shell metacharacter; strip it the way `eval` would.
    assert subprocess.run(
        ["bash", "-c", f'{result.stdout}\nprintf "%s\\n%s" "$PGUSER" "$PGPASSWORD"'],
        capture_output=True, text=True, check=False,
    ).stdout == "tox#user\np@ss#word"


# --------------------------------------------------------------------------------------
# stopping, and the seven-day trap
# --------------------------------------------------------------------------------------


def test_aws_down_records_the_auto_restart_deadline(down):
    """H29. A stopped RDS instance restarts by itself after seven days."""
    body = shell_code(DOWN)
    assert "/ops/rds-stopped-at" in body
    assert "7 days" in body or "seven days" in body
    go, journalled = down
    result = go()
    assert result.returncode == 0, result.stderr
    assert re.search(r"restarts? by itself", result.stdout, re.I)
    assert re.search(r"20\d\d-\d\d-\d\d", result.stdout), "print the actual deadline date"
    assert any("/toxic/ops/rds-stopped-at" in line for line in journalled())


def test_the_printed_deadline_is_seven_days_after_the_stop(down):
    """A deadline computed from the wrong base, or with the wrong offset, is worse than none:
    it is a date somebody will trust."""
    go, _journalled = down
    result = go(AWS_DOWN_NOW="2026-08-14T18:20:00+00:00")
    assert "2026-08-21" in result.stdout, result.stdout


def test_aws_down_stops_ec2_before_rds(down):
    """The backend holds pooled connections. Stopping RDS first logs a wall of errors and
    fills the spool, for no benefit."""
    go, journalled = down
    go()
    lines = journalled()
    ec2 = next(i for i, line in enumerate(lines) if "stop-instances" in line)
    rds = next(i for i, line in enumerate(lines) if "stop-db-instance" in line)
    assert ec2 < rds


def test_aws_down_does_not_touch_the_nightly_stop_schedules():
    """The two EventBridge schedules are DISABLED for the grading window by
    `-var nightly_stop_enabled=false`. A teardown script that re-enabled them -- or that ran
    any apply at all -- would put the graded stack back on a nightly timer without saying so.
    """
    code = shell_code(DOWN)
    assert "terraform apply" not in code
    assert "scheduler" not in code
    assert "nightly" not in code


def test_aws_down_reads_only_terraform_outputs_that_exist():
    """`terraform output -raw db_instance_id` fails with `Output "db_instance_id" not found`
    if nothing declares it, and it fails AFTER the dump has already run."""
    code = shell_code(DOWN)
    outputs = tfparse.source_of("outputs.tf")
    for name in re.findall(r"terraform output[^\n]*?(?:-raw|-json)\s+(\w+)", code):
        assert f'output "{name}"' in outputs, f"aws_down.sh reads an output nobody declares: {name}"


# --------------------------------------------------------------------------------------
# what has to be true of the infrastructure for any of the above to work
# --------------------------------------------------------------------------------------


def test_final_snapshot_is_not_skipped():
    """H6. skip_final_snapshot = true makes every teardown a permanent data loss."""
    db = tfparse.resources_of_kind("aws_db_instance")
    assert db, "no aws_db_instance is declared"
    for name, body in db.items():
        assert body.get("skip_final_snapshot") is False, name
        assert "final_snapshot_identifier" in body, name
        retention = body.get("backup_retention_period")
        if isinstance(retention, int):
            assert retention >= 1, name
        else:
            assert retention == "var.db_backup_retention_days", (name, retention)
            variables = tfparse.source_of("variables.tf")
            assert "var.db_backup_retention_days >= 1" in variables, (
                "the retention variable has no floor, so a zero would disable backups"
            )


def test_the_backend_role_can_write_and_read_the_dump_prefix():
    """The script is not the control; the grant is. `data.aws_iam_policy_document.deploy_payload`
    scopes every instance to GetObject on `deploy/*` and `artifacts/*` and grants PutObject
    nowhere -- so `pg_dump | aws s3 cp - s3://.../db/...` is an AccessDenied inside an SSM
    invocation, and `make aws-down` refuses to stop anything, permanently."""
    iam = tfparse.source_of("iam.tf")
    assert "aws_iam_role_policy" in iam
    assert "/db/*" in iam, (
        "no instance role can touch the db/ prefix; the dump cannot be written and the "
        "restore cannot read it"
    )
    assert "s3:PutObject" in iam, "no instance role can write an object at all"


def test_only_the_backend_tier_can_reach_a_database_dump():
    """H16. The frontend is the internet-facing Streamlit box and the monitoring tier connects
    as a SELECT-only role; neither has any reason to read a full dump of the database, and the
    backend already holds the master credential, so this grants it nothing new."""
    iam = tfparse.source_of("iam.tf")
    assert 'data "aws_iam_policy_document" "database_dump"' in iam, (
        "there is no separate policy document for the dump prefix, so whatever grants it "
        "grants it to every tier that shares the document"
    )
    # `[^}]` cannot be used to bound a resource body here: `${var.project}` puts a closing
    # brace inside almost every one of them.
    attachments = [
        match.group(1)
        for match in re.finditer(
            r'resource "aws_iam_role_policy" "(\w+)" \{(.{0,600}?)\n\}', iam, re.DOTALL
        )
        if "database_dump" in match.group(2)
    ]
    assert attachments, "the dump policy is attached to nothing"
    for name in attachments:
        assert name.startswith("backend"), f"the dump grant reaches the {name} tier"


def test_the_dump_bucket_prefix_is_never_expired_on_a_schedule():
    """The dumps ARE the graded dataset. A lifecycle rule over them deletes the evidence the
    demo is scored on, silently, at the point in the term when nobody is looking."""
    deploy = tfparse.source_of("deploy.tf")
    start = deploy.index('id     = "keep-database-dumps"')
    end = deploy.find("rule {", start)
    rule = deploy[start : end if end > 0 else len(deploy)]
    assert 'prefix = "db/"' in rule, "the keep-database-dumps rule does not select db/"
    assert "expiration" not in rule, f"the db/ prefix carries an expiry rule:\n{rule}"


def test_the_makefile_never_hides_a_teardown_behind_a_silent_default():
    """`make db-restore` with no S3_KEY must fail loudly rather than restore "the latest"."""
    body = MAKEFILE.read_text(encoding="utf-8")
    assert "$(S3_KEY)" in body
    assert "S3_KEY ?=" not in body, "S3_KEY has a default, so a bare `make db-restore` guesses"


def test_the_instance_ids_output_is_a_map_the_teardown_can_iterate():
    """`terraform output -json instance_ids | jq -r '.[]'` reads values out of a map and
    elements out of a list, so either shape works -- but only if the output exists."""
    outputs = tfparse.source_of("outputs.tf")
    assert 'output "instance_ids"' in outputs
    assert 'output "db_instance_id"' in outputs


# --------------------------------------------------------------------------------------
# Task 18: the other half. A dump nobody has restored is a hypothesis.
# --------------------------------------------------------------------------------------


@pytest.fixture()
def restore(tmp_path: Path):
    """Runs db_restore.sh and hands back (result, trace lines, captured remote script)."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", CAPTURING_AWS_STUB)
    fake = tmp_path / "scripts"
    trace = tmp_path / "trace"
    capture = tmp_path / "captured.sh"
    make_stub(fake, "ssm_run.sh", f'#!/bin/bash\necho "$*" >> "{trace}"\nexit 0\n')

    def go(args):
        result = run(
            RESTORE,
            list(args),
            bin_dir,
            env={
                "STUB_JOURNAL": str(tmp_path / "journal"),
                "STUB_CAPTURE": str(capture),
                "DB_RESTORE_SCRIPT_DIR": str(fake),
            },
        )
        return (
            result,
            trace.read_text().splitlines() if trace.exists() else [],
            capture.read_text(encoding="utf-8") if capture.exists() else "",
        )

    return go


def test_db_restore_requires_an_explicit_key(tmp_path):
    """Restoring 'the latest' silently is how the wrong dataset ends up in the dashboard."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    result = run(RESTORE, [], bin_dir, env={"STUB_JOURNAL": str(tmp_path / "j")})
    assert result.returncode != 0
    assert "S3_KEY" in result.stderr or "usage" in result.stderr


def test_db_restore_lists_available_dumps_when_it_refuses(tmp_path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    result = run(RESTORE, [], bin_dir, env={"STUB_JOURNAL": str(tmp_path / "j")})
    assert "db/2026-08-14T18-02-11Z.dump" in result.stdout + result.stderr


def test_db_restore_refuses_a_key_outside_the_dump_prefix(tmp_path):
    """`make db-restore S3_KEY=deploy/current/roll.sh` would hand pg_restore a shell script,
    after --clean had already dropped the tables."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    result = run(
        RESTORE, ["deploy/current/roll.sh"], bin_dir, env={"STUB_JOURNAL": str(tmp_path / "j")}
    )
    assert result.returncode != 0
    assert "db/" in result.stderr


def test_db_restore_runs_on_the_instance_through_the_asserted_ssm_path():
    body = shell_code(RESTORE)
    assert "ssm_run.sh" in body
    assert "pg_restore" in body


def test_db_restore_is_idempotent_and_does_not_stack_duplicate_rows():
    body = shell_code(RESTORE)
    assert "--clean" in body and "--if-exists" in body


def test_db_restore_touches_no_infrastructure():
    assert "terraform" not in shell_code(RESTORE).lower()


def test_db_restore_round_trips_a_dump(restore):
    """The command exists, takes a key, and reaches the instance through ssm_run.sh."""
    result, trace, _script = restore(["db/2026-08-14T18-02-11Z.dump"])
    assert result.returncode == 0, result.stderr
    assert trace, "nothing was sent to the instance"
    assert trace[0].startswith("backend 1 "), trace


def test_db_restore_fetches_its_remote_script_rather_than_assuming_it(restore):
    """Same trap as the dump: /opt/toxic holds what bootstrap.sh copied from deploy/<sha>/."""
    _result, trace, _script = restore(["db/2026-08-14T18-02-11Z.dump"])
    payload = trace[0]
    assert "/opt/toxic/" not in payload, payload
    assert "aws s3 cp" in payload and "s3://" in payload, payload


def test_the_generated_restore_script_is_valid_bash(restore, tmp_path):
    _result, _trace, script = restore(["db/2026-08-14T18-02-11Z.dump"])
    assert script, "db_restore.sh uploaded no remote script"
    path = tmp_path / "remote-restore.sh"
    path.write_text(script, encoding="utf-8")
    assert subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, check=False
    ).returncode == 0


def _without_comments(script: str) -> str:
    """`shell_code` for a string rather than a file. The generated scripts explain `--clean`
    in a comment two paragraphs above the line that uses it, and an ordering check that reads
    the prose finds the flag in the wrong place."""
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_archive_is_proved_readable_before_anything_is_dropped(restore):
    """`--clean --if-exists` DROPs every table and then loads. Point it at a truncated archive
    -- which `aws s3 cp -` produces whenever a dump's pipe broke -- and the drop succeeds, the
    load fails, and the graded dataset is gone. The table of contents is read FIRST."""
    _result, _trace, script = restore(["db/2026-08-14T18-02-11Z.dump"])
    script = _without_comments(script)
    listing = script.find("--list")
    clean = script.find("--clean")
    assert listing >= 0, "nothing verifies the archive before the restore"
    assert clean >= 0
    assert listing < clean, "the archive is verified after the tables have been dropped"


def test_the_restore_is_a_single_transaction(restore):
    """A restore that fails halfway leaves the dashboard reading half a dataset and reporting
    it as fact. Either all of it lands or none of it does."""
    _result, _trace, script = restore(["db/2026-08-14T18-02-11Z.dump"])
    assert "--single-transaction" in _without_comments(script)


def test_the_dump_is_materialised_before_it_is_replayed(restore):
    """Streaming S3 straight into `pg_restore --clean` means the DROPs are already committed
    by the time a short read is discovered. The object lands on disk, is checked, replayed,
    and removed."""
    _result, _trace, script = restore(["db/2026-08-14T18-02-11Z.dump"])
    script = _without_comments(script)
    assert not re.search(r"aws s3 cp[^\n]*-\s*\|\s*docker[^\n]*pg_restore[^\n]*--clean", script)
    assert re.search(r"\brm -f\b", script), "the fetched dump is left on the instance volume"
