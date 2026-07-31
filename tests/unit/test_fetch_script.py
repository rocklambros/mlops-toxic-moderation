"""The download script is the day-1 gate. These assertions are about credential hygiene."""

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path("scripts/fetch_jigsaw.sh")


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR


def test_script_fails_fast():
    assert "set -euo pipefail" in SCRIPT.read_text()


def test_script_never_materializes_a_credential_file():
    source = SCRIPT.read_text()
    assert "kaggle" + ".json" not in source
    assert "~/.kaggle" not in source


def test_api_key_never_enters_argv():
    source = SCRIPT.read_text()
    assert "--config -" in source
    assert "--user " not in source
    assert " -u " not in source


def test_script_never_echoes_the_key():
    for line in SCRIPT.read_text().splitlines():
        if line.strip().startswith("echo"):
            assert "$key" not in line and "KAGGLE_KEY" not in line


def test_script_targets_the_english_six_label_member_file_only():
    source = SCRIPT.read_text()
    assert "jigsaw-toxic-comment-train.csv" in source
    assert "unintended-bias-train" + ".csv" not in source
    assert "/datasets/download/${DATASET}/${MEMBER}" in source


def test_script_records_raw_sha256_next_to_the_csv():
    source = SCRIPT.read_text()
    assert "sha256sum" in source
    assert '"${DEST}.sha256"' in source


def test_script_sources_credentials_from_pass_or_env():
    source = SCRIPT.read_text()
    assert "${KAGGLE_USERNAME:-$(pass show kaggle/username" in source
    assert "${KAGGLE_KEY:-$(pass show kaggle/api-key" in source


def _sandbox_path(tmp_path):
    """A PATH holding only the tools the skip branch needs, plus stubs that fail loudly.

    `curl` and `pass` are present but non-functional, so a script that reaches the network
    or the credential store instead of short-circuiting fails this test rather than
    downloading 91 MB or touching a live secret.
    """
    sandbox = tmp_path / "bin"
    sandbox.mkdir()
    for tool in ("tr", "sha256sum", "awk", "cat", "mkdir", "mktemp", "rm"):
        found = shutil.which(tool)
        if found is None:
            return None
        (sandbox / tool).symlink_to(found)
    for stub in ("curl", "pass", "unzip"):
        target = sandbox / stub
        target.write_text("#!/bin/sh\necho 'network or credential access attempted' >&2\nexit 97\n")
        target.chmod(0o755)
    return sandbox


def test_script_skips_the_download_when_the_recorded_digest_matches(tmp_path):
    """Idempotence: a corpus that still matches its digest is never refetched."""
    sandbox = _sandbox_path(tmp_path)
    if sandbox is None:
        pytest.skip("coreutils not available under this PATH")
    dest_dir = tmp_path / "raw"
    dest_dir.mkdir()
    csv = dest_dir / "jigsaw-toxic-comment-train.csv"
    payload = b"id,comment_text,toxic\n1,hello,0\n"
    csv.write_bytes(payload)
    csv.with_suffix(".csv.sha256").write_text(hashlib.sha256(payload).hexdigest() + "\n")

    result = subprocess.run(
        ["/bin/bash", str(SCRIPT.resolve())],
        env={"PATH": str(sandbox), "DEST_DIR": str(dest_dir)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "skipping download" in result.stdout
    assert csv.read_bytes() == payload
