import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = "tests/fixtures/mini_jigsaw.csv"


def _run(*args, cwd=REPO):
    env = {**os.environ, "PYTHONHASHSEED": "0", "PYTHONPATH": str(REPO)}
    return subprocess.run(
        [sys.executable, "-m", "model.data.run", *args],
        cwd=cwd, env=env, capture_output=True, text=True,
    )


def test_cli_emits_all_three_version_fields_and_the_firewall_summary(tmp_path):
    out = _run("--csv", FIXTURE, "--profile-out", str(tmp_path / "profile.md"))
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout[out.stdout.index("{") : out.stdout.rindex("}") + 1])
    for key in ("git_sha", "seed", "raw_sha256", "split_version", "env_version"):
        assert payload[key] is not None
    assert "firewall: method=" in out.stdout
    assert (tmp_path / "profile.md").is_file()


def test_cli_is_reproducible_across_two_runs(tmp_path):
    first = _run("--csv", FIXTURE, "--profile-out", str(tmp_path / "a.md"))
    second = _run("--csv", FIXTURE, "--profile-out", str(tmp_path / "b.md"))
    def versions(text):
        payload = json.loads(text[text.index("{") : text.rindex("}") + 1])
        return payload["raw_sha256"], payload["split_version"], payload["env_version"]
    assert versions(first.stdout) == versions(second.stdout)


def test_makefile_data_target_is_parameterized_on_csv():
    recipe = (REPO / "Makefile").read_text()
    assert "CSV ?=" in recipe
    assert "--csv $(CSV)" in recipe
    assert "--csv tests/fixtures/mini_jigsaw.csv" not in recipe


@pytest.mark.integration
def test_real_corpus_is_present_and_matches_recorded_provenance():
    csv = REPO / "data/raw/jigsaw-toxic-comment-train.csv"
    digest_file = csv.with_suffix(csv.suffix + ".sha256")
    if not csv.is_file():
        pytest.skip("run `make fetch-data` first; this is an integration check")
    from model.data.provenance import sha256_file
    recorded = (REPO / "docs/data-provenance.md").read_text()
    actual = sha256_file(csv)
    assert digest_file.read_text().strip() == actual
    assert actual in recorded, "docs/data-provenance.md does not record this corpus digest"
