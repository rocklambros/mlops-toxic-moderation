"""REG-10d. A registry outage must not turn the fail-closed loader into a demo outage.

The serving loader refuses any artifact whose SHA-256 differs from the value in the committed
model card. That is correct and non-negotiable, and it also means a Weights & Biases outage
at bring-up is indistinguishable from a poisoned artifact: both produce "no artifact", and
the demo is down either way.

The fallback is an S3 mirror keyed BY THE DIGEST ITSELF, so the mirror is not a second trust
root. An object that does not hash to the value in the card cannot be found under the name
the fetcher asks for, because the name IS that value.

Every test here runs the real script against a stubbed `wandb` and `aws`. A test that read
the script and asserted on its text would pass over a script whose control flow does the
opposite of what it says.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra/deploy/instance/fetch_artifacts.sh"

GOOD = b"pretend skops artifact bytes"
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()
EVIL = b"poisoned artifact bytes"


# A 64-hex value that is NOT the artifact's, placed ahead of it in the fixture card. The
# real MODEL_CARD.md carries several -- the raw corpus digest, the realized split digest, the
# environment digest -- and any lookup that takes "the first 64-hex string in the file"
# resolves to whichever section happens to come first today. The decoy is what turns that
# from a latent reordering hazard into a failing test.
DECOY_SHA = hashlib.sha256(b"the realized split, not the artifact").hexdigest()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A model card carrying the digest of record, in the row shape the real card uses."""
    (tmp_path / "artifacts").mkdir()
    card = tmp_path / "MODEL_CARD.md"
    card.write_text(
        "## Training data\n\n"
        f"| Realized split SHA-256 | `{DECOY_SHA}` |\n\n"
        "## Artifact digest of record\n\n"
        "| Artifact | sha256 |\n|---|---|\n"
        f"| `toxic-clf.skops` | `{GOOD_SHA}` |\n",
        encoding="utf-8",
    )
    return tmp_path


def _wandb_stub(payload: bytes | None) -> str:
    """A `wandb` that either drops one file into --root, or fails the way an outage does."""
    if payload is None:
        return '#!/bin/bash\necho "wandb: 503 Service Unavailable" >&2\nexit 1\n'
    return (
        "#!/bin/bash\n"
        'root=""\n'
        'while [ $# -gt 0 ]; do if [ "$1" = "--root" ]; then root="$2"; fi; shift; done\n'
        'mkdir -p "$root"\n'
        f"printf '%s' {payload.decode()!r} > \"$root/toxic-clf.skops\"\n"
    )


def _aws_stub(payload: bytes | None) -> str:
    """An `aws` whose `s3 cp` writes `payload` to the destination, or 404s."""
    if payload is None:
        return (
            "#!/bin/bash\n"
            'echo "An error occurred (404) when calling the GetObject operation" >&2\n'
            "exit 1\n"
        )
    return (
        "#!/bin/bash\n"
        'dest="${@: -1}"\n'
        f"printf '%s' {payload.decode()!r} > \"$dest\"\n"
    )


def _env(workspace: Path) -> dict[str, str]:
    return {
        "MODEL_CARD_PATH": str(workspace / "MODEL_CARD.md"),
        "ARTIFACT_DIR": str(workspace / "artifacts"),
        "ARTIFACT_NAME": "toxic-clf.skops",
        "WANDB_ARTIFACT": "rockcyber-org/wandb-registry-model/toxic-clf:production",
        "DEPLOY_BUCKET": "example-bucket",
    }


def test_primary_path_installs_the_verified_artifact(tmp_path, workspace):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(GOOD))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode == 0, result.stderr
    assert (workspace / "artifacts" / "toxic-clf.skops").read_bytes() == GOOD


def test_mirror_is_used_when_wandb_fails_and_the_digest_still_gates(tmp_path, workspace):
    """The whole point: a registry outage falls back, and the fallback is still verified."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(None))
    make_stub(bin_dir, "aws", _aws_stub(GOOD))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode == 0, result.stderr
    assert "falling back to the mirror" in result.stdout + result.stderr
    assert (workspace / "artifacts" / "toxic-clf.skops").read_bytes() == GOOD


def test_a_tampered_mirror_object_is_rejected_and_nothing_is_installed(tmp_path, workspace):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(None))
    make_stub(bin_dir, "aws", _aws_stub(EVIL))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode != 0
    assert not (workspace / "artifacts" / "toxic-clf.skops").exists()


def test_a_tampered_primary_is_rejected_and_does_not_silently_fall_back(tmp_path, workspace):
    """A digest mismatch is a security event, not a transport failure. Do not retry it.

    Falling back here would be the worst possible behaviour: an attacker who can publish to
    the registry gets a free retry against whichever source is easier to poison, and the
    operator sees a successful deploy.
    """
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(EVIL))
    make_stub(bin_dir, "aws", _aws_stub(GOOD))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode != 0
    assert "digest mismatch" in (result.stdout + result.stderr).lower()
    assert "falling back to the mirror" not in result.stdout + result.stderr
    assert not (workspace / "artifacts" / "toxic-clf.skops").exists()


def test_both_sources_failing_is_a_hard_failure(tmp_path, workspace):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(None))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode != 0


def test_a_card_with_no_digest_of_record_fails_before_any_fetch(tmp_path, workspace):
    """Fail closed on a card that names no digest, rather than installing whatever arrives."""
    (workspace / "MODEL_CARD.md").write_text("no digests here\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(GOOD))
    make_stub(bin_dir, "aws", _aws_stub(GOOD))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode != 0
    assert "digest of record" in result.stderr
    assert not (workspace / "artifacts" / "toxic-clf.skops").exists()


def test_the_mirror_key_is_the_digest_itself(tmp_path, workspace):
    """The mirror is not a second trust root; the digest IS the lookup key.

    Asserted by observation rather than by reading the source: the stub records the S3 URI
    it was asked for, and the test checks that the digest appears in it.
    """
    bin_dir = tmp_path / "bin"
    log = tmp_path / "s3.log"
    make_stub(bin_dir, "wandb", _wandb_stub(None))
    make_stub(
        bin_dir,
        "aws",
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        'dest="${@: -1}"\n'
        f"printf '%s' {GOOD.decode()!r} > \"$dest\"\n",
    )
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode == 0, result.stderr
    requested = log.read_text(encoding="utf-8")
    assert f"s3://example-bucket/artifacts/{GOOD_SHA}/toxic-clf.skops" in requested, requested


def test_the_expected_digest_cannot_be_supplied_by_the_environment(tmp_path, workspace):
    """"The thing that gave me the artifact also told me what it should hash to" is not
    provenance. The card is the only source, so no environment variable may name a digest."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "MODEL_CARD_PATH" in body
    reads = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[:}\-?]", body)
    offenders = [name for name in reads if "DIGEST" in name.upper() or "SHA" in name.upper()]
    assert not offenders, f"the environment can supply the expected digest through {offenders}"


def test_the_lookup_finds_a_digest_in_the_repositorys_own_model_card(tmp_path):
    """The fixture above is a card this test wrote. This one is the card the deploy ships.

    Everything else here would pass against a real card that carries no row the fetcher can
    find -- the fixture and reality would agree on the parse and disagree on the file, and
    the first symptom would be `no digest of record` inside an SSM invocation.
    """
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(None))
    make_stub(bin_dir, "aws", _aws_stub(None))
    env = {
        "MODEL_CARD_PATH": str(REPO / "MODEL_CARD.md"),
        "ARTIFACT_DIR": str(tmp_path / "artifacts"),
        "ARTIFACT_NAME": "toxic-clf.skops",
        "WANDB_ARTIFACT": "rockcyber-org/wandb-registry-model/toxic-clf:production",
        "DEPLOY_BUCKET": "example-bucket",
    }
    result = run(SCRIPT, [], bin_dir, env=env)
    # Both sources are down, so this must fail -- but on the mirror, having already resolved
    # a digest. "no digest of record" here would mean the real card has no usable row.
    assert "no digest of record" not in result.stderr, result.stderr
    assert re.search(r"digest of record for toxic-clf\.skops is [0-9a-f]{64}", result.stdout), (
        result.stdout + result.stderr
    )


def test_an_environment_supplied_digest_is_not_honoured_even_if_someone_exports_one(
    tmp_path, workspace
):
    """The static rule above is worth little unless the running script agrees with it."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(EVIL))
    make_stub(bin_dir, "aws", _aws_stub(None))
    env = _env(workspace)
    env["MODEL_DIGEST"] = hashlib.sha256(EVIL).hexdigest()
    env["EXPECTED"] = hashlib.sha256(EVIL).hexdigest()
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode != 0
    assert not (workspace / "artifacts" / "toxic-clf.skops").exists()
