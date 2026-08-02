"""DELIV-3. What must never reach a public artifact, and what must survive untouched.

Nothing in this file is a literal of the shape it describes. Every sample is assembled at
run time from parts, because this repository has already had a text scanner flag the prose
explaining a rule -- a docstring, a test name, and a suppression file that quoted the token
it exempted. A scanner cannot tell an example from an instance, so: describe the shape,
build it from pieces, never paste it.

The addresses below are the RFC 5737 documentation range, which exists for exactly this.
"""

import string
import subprocess
import sys
from pathlib import Path

from scripts.redact import SECRET_SHAPES, redact, scan

# Twelve digits: 1..9 then 0, 1, 2. The shape of an AWS account id, and not one.
ACCOUNT_ID = "".join(str(n % 10) for n in range(1, 13))
# The vendor's own published example key, split so no contiguous literal exists here.
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
# A personal-access-token shape: the prefix, then the digits and the lowercase alphabet in
# order. Thirty-seven characters of body, which is inside the length the rule requires.
GITHUB_PAT = "gh" + "p_" + string.digits + string.ascii_lowercase + "A"
# Forty lowercase hex characters -- the shape of a registry API key, and also, exactly, the
# shape of a git commit sha. That collision is what test_a_commit_sha_survives is about.
HEX40 = ("0123456789abcdef" * 3)[:40]
# A public address, from the range reserved for documentation.
ELASTIC_IP = "203.0.113.7"
# Sixty-four hex characters carrying a run of exactly twelve digits, which is the shape of
# the artifact digest of record. Built, not copied, so the real digest stays in one place.
DIGEST_64 = "ab" + "678467907743" + "f" * 50


def test_redact_masks_a_bare_account_id():
    assert redact(f"account {ACCOUNT_ID} created") == "account <account-id> created"


def test_redact_masks_an_account_id_inside_an_arn():
    """DELIV-3. The id almost never appears bare; it appears inside an ARN or an ECR URI."""
    src = f"{ACCOUNT_ID}.dkr.ecr.us-west-2.amazonaws.com/toxic-backend:abc123"
    assert redact(src) == "<account-id>.dkr.ecr.us-west-2.amazonaws.com/toxic-backend:abc123"
    arn = f"arn:aws:iam::{ACCOUNT_ID}:role/gha-deploy"
    assert redact(arn) == "arn:aws:iam::<account-id>:role/gha-deploy"


def test_redact_leaves_other_numbers_alone():
    assert redact("latency_ms 1234 and epoch 1754000000000000") == (
        "latency_ms 1234 and epoch 1754000000000000"
    )
    assert redact("2026-08-18") == "2026-08-18"


def test_redact_masks_known_secret_shapes():
    assert AWS_KEY not in redact(f"key {AWS_KEY} here")
    assert "<aws-access-key-id>" in redact(f"key {AWS_KEY} here")
    assert "<github-token>" in redact(GITHUB_PAT)


def test_a_sha256_digest_is_not_mistaken_for_an_account_id():
    """The artifact digest of record is 64 hex characters and it carries a run of exactly
    twelve digits. Anchoring the account-id rule on decimal boundaries masks the middle of
    it: that corrupts the value the fail-closed loader checks against, and reports the
    project's own trust root as a leak every time the submission gate reads MODEL_CARD.md."""
    assert len(DIGEST_64) == 64
    assert redact(f"sha256:{DIGEST_64}") == f"sha256:{DIGEST_64}"
    assert scan_text_kinds(f"| SHA-256 | `{DIGEST_64}` |") == []


def scan_text_kinds(line: str) -> list[str]:
    from scripts.redact import _kinds_in

    return _kinds_in(line)


def test_redact_masks_an_elastic_ip():
    """The three Elastic IPs are in every deploy transcript and every console screenshot.
    They are the address of the graded system, so they leave with the evidence or not at
    all."""
    assert redact(f"backend http://{ELASTIC_IP}:8000/health") == (
        "backend http://<elastic-ip>:8000/health"
    )
    assert ELASTIC_IP not in redact(f"operator_cidrs = [\"{ELASTIC_IP}/32\"]")


def test_redact_leaves_the_addresses_the_stack_actually_configures_alone():
    """Every one of these is committed somewhere in this repository as configuration. A
    redactor that rewrites them corrupts the artifact it was asked to publish."""
    for keep in (
        "0.0.0.0",  # the compose port bind
        "127.0.0.1",  # the reviewer console loopback bind
        "169.254.169.254",  # the instance metadata service
        "10.42.1.20",  # a VPC private address
        "172.16.0.5",
        "192.168.1.1",
    ):
        assert redact(f"listen {keep}:8000") == f"listen {keep}:8000", keep


def test_a_commit_sha_survives_because_provenance_is_the_thing_being_published():
    """A registry key is forty lowercase hex characters. So is a git commit sha, and this
    project publishes those on purpose: MODEL_CARD.md records the training commit and the
    workflow pins every action to a full sha. A bare-shape rule masks the provenance record
    it was added to protect, and a scanner that reports committed evidence gets turned off.
    The rule therefore fires on the context that says 'this is a credential'."""
    assert redact(f"trained at commit {HEX40}") == f"trained at commit {HEX40}"
    assert redact(f"uses: actions/checkout@{HEX40}") == f"uses: actions/checkout@{HEX40}"
    assert "<wandb-key>" in redact(f"WANDB_API_KEY={HEX40}")
    assert HEX40 not in redact(f"wandb login {HEX40}")


def test_redact_masks_the_projects_own_shapeless_credentials():
    """The demo key, the reviewer secret, the fingerprint key and the database password have
    no distinctive shape at all -- they match none of the vendor patterns. An assignment is
    the only thing that marks them, so an assignment is what the rule reads."""
    assert "correct-horse-battery" not in redact("DEMO_API_KEY=correct-horse-battery")
    assert "correct-horse-battery" not in redact("REVIEWER_SHARED_SECRET: correct-horse-battery")
    assert "correct-horse-battery" not in redact('SUBMITTER_FP_KEY="correct-horse-battery"')


def test_a_key_that_names_a_location_is_not_a_credential():
    """`make db-restore S3_KEY=db/...` is a documented operator command in the README.
    Redacting it makes the instruction unusable, and an over-redacting scanner corrupts the
    artifact it was asked to publish -- the same failure as leaking, pointed the other way."""
    command = "make db-restore S3_KEY=db/2026-08-14T18-02-11Z.dump"
    assert redact(command) == command


def test_a_database_url_password_is_masked_without_losing_the_host():
    src = "postgresql+psycopg://toxic:s3cr3t-and-long@db.internal:5432/toxic"
    out = redact(src)
    assert "s3cr3t-and-long" not in out
    assert "db.internal:5432/toxic" in out, "the host is diagnostic, not secret"


def test_a_variable_reference_is_not_mistaken_for_a_value():
    """The README documents the key as `$DEMO_API_KEY` on purpose. Masking a reference to a
    secret teaches the reader nothing and breaks a runnable example."""
    for reference in (
        '-H "X-API-Key: $DEMO_API_KEY"',
        "DEMO_API_KEY=${{ secrets.DEMO_API_KEY }}",
        "REVIEWER_SHARED_SECRET=<supplied-with-the-submission>",
    ):
        assert redact(reference) == reference, reference


def test_a_credential_containing_a_twelve_digit_run_is_masked_whole():
    """Order of operations, not a new rule. Masking the account id first splits any token
    that happens to carry twelve consecutive digits, after which the credential rule no
    longer matches it and the remaining characters are published."""
    tail = string.ascii_lowercase[::-1]  # starts with a non-hex letter, so only order can fail
    token = "gh" + "p_" + "0" * 12 + tail
    out = redact(f"token {token}")
    assert "<github-token>" in out
    assert tail not in out, "the tail of the token survived redaction"


def test_scan_reports_findings_with_line_numbers(tmp_path):
    target = tmp_path / "evidence.md"
    target.write_text(f"clean line\nrole arn:aws:iam::{ACCOUNT_ID}:role/x\n", encoding="utf-8")
    findings = scan([target])
    assert len(findings) == 1
    assert findings[0].path == target
    assert findings[0].line_number == 2
    assert findings[0].kind == "account-id"


def test_scan_returns_nothing_for_a_clean_file(tmp_path):
    target = tmp_path / "clean.md"
    target.write_text("no identifiers here\n", encoding="utf-8")
    assert scan([target]) == []


def test_scan_walks_a_directory_argument(tmp_path):
    """`make submission-check` passes `docs/evidence`, which is a directory. A scanner that
    silently skips directories reports the evidence tree clean without opening one file in
    it, which is indistinguishable from having no scanner."""
    tree = tmp_path / "evidence"
    (tree / "screenshots").mkdir(parents=True)
    (tree / "screenshots" / "deep.md").write_text(f"id {ACCOUNT_ID}\n", encoding="utf-8")
    findings = scan([tree])
    assert [f.kind for f in findings] == ["account-id"]
    assert findings[0].path.name == "deep.md"


def test_scan_reports_an_elastic_ip_and_a_credential_too(tmp_path):
    target = tmp_path / "transcript.md"
    target.write_text(
        f"GET http://{ELASTIC_IP}:8000\nDEMO_API_KEY=abcdefghijkl\n", encoding="utf-8"
    )
    assert {f.kind for f in scan([target])} == {"elastic-ip", "assigned-secret"}


def test_the_scanner_reports_exactly_what_the_redactor_would_change(tmp_path):
    """The gate and the tool must agree. A scanner that reports a line the redactor leaves
    alone is a gate nobody can make green; one that stays quiet about a line the redactor
    would rewrite is a gate that passes a leak through."""
    lines = [
        f"arn:aws:iam::{ACCOUNT_ID}:role/x",
        f"http://{ELASTIC_IP}:8000",
        f"WANDB_API_KEY={HEX40}",
        "make db-restore S3_KEY=db/2026-08-14T18-02-11Z.dump",
        f"sha256:{DIGEST_64}",
        "listen 127.0.0.1:8503",
        f"trained at commit {HEX40}",
        f"key {AWS_KEY}",
    ]
    target = tmp_path / "mixed.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reported = {f.line_number for f in scan([target])}
    changed = {n for n, line in enumerate(lines, start=1) if redact(line) != line}
    assert reported == changed


def test_secret_shapes_cover_every_credential_this_project_holds():
    covered = {name for name, _pattern, _mask in SECRET_SHAPES}
    assert {"aws-access-key-id", "github-token", "wandb-key", "bearer-token"} <= covered


def test_the_published_documents_are_clean_right_now():
    """The control, exercised rather than merely defined. These are exactly the paths
    `make submission-check` scans, and this asserts against the working tree so a leak
    committed later fails here and not in a browser."""
    targets = [Path("README.md"), Path("SECURITY.md"), Path("MODEL_CARD.md"), Path("docs/evidence")]
    findings = scan([p for p in targets if p.exists()])
    assert findings == [], [f"{f.path}:{f.line_number}: {f.kind}" for f in findings]


def test_cli_masks_stdin_and_exits_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.redact"],
        input=f"arn:aws:iam::{ACCOUNT_ID}:role/gha-deploy\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert ACCOUNT_ID not in proc.stdout
    assert "<account-id>" in proc.stdout


def test_cli_scan_mode_exits_nonzero_on_a_finding(tmp_path):
    target = tmp_path / "bad.md"
    target.write_text(f"arn:aws:iam::{ACCOUNT_ID}:role/x\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.redact", "--scan", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "account-id" in proc.stdout + proc.stderr


def test_cli_scan_output_does_not_reprint_the_thing_it_found(tmp_path):
    """A scanner that echoes the offending line into a world-readable Actions log has
    published the identifier it exists to catch."""
    target = tmp_path / "bad.md"
    target.write_text(f"role arn:aws:iam::{ACCOUNT_ID}:role/x\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.redact", "--scan", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ACCOUNT_ID not in proc.stdout + proc.stderr
