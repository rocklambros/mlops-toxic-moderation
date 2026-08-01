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

# The two sidecars. thresholds.json IS the decision boundary and baseline_flag_rates.json is
# the reference the drift panel measures against, so both get the same treatment as the
# coefficients: fetched by name, verified against the committed card, installed only if every
# one of the three verified.
THRESHOLDS = b'{"toxic": 0.31, "severe_toxic": 0.05}'
THRESHOLDS_SHA = hashlib.sha256(THRESHOLDS).hexdigest()
BASELINE = b'{"schema_version": 1, "flag_rates": {}}'
BASELINE_SHA = hashlib.sha256(BASELINE).hexdigest()
TAMPERED_THRESHOLDS = b'{"toxic": 0.99, "severe_toxic": 0.99}'
ALL_THREE = ("toxic-clf.skops", "thresholds.json", "baseline_flag_rates.json")


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
        f"| `toxic-clf.skops` | `{GOOD_SHA}` |\n"
        f"| `thresholds.json` | `{THRESHOLDS_SHA}` |\n"
        f"| `baseline_flag_rates.json` | `{BASELINE_SHA}` |\n",
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
        "ARTIFACT_NAMES": "toxic-clf.skops",
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
    # Only the environment-configurable forms -- `${X:-default}`, `${X:?required}`,
    # `${X:=...}`. A bare `${digest}` is a local the script assigned itself two lines up, and
    # flagging it would make the rule unsatisfiable by any script that names a digest at all.
    configurable = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):[-?=]", body)
    offenders = [n for n in configurable if "DIGEST" in n.upper() or "SHA" in n.upper()]
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
        "ARTIFACT_NAMES": "toxic-clf.skops",
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


# --- the two sidecars (gap DRIFT-ARTIFACTS) ------------------------------------------------


def _wandb_stub_multi(model: bytes | None, thresholds: bytes | None,
                      baseline: bytes | None) -> str:
    """A `wandb` that drops whichever of the three artifacts the test says the registry has.

    `None` for all three is an outage; `None` for one is the far more interesting case: a
    registry that answers, successfully, with an incomplete artifact set.
    """
    if (model, thresholds, baseline) == (None, None, None):
        return '#!/bin/bash\necho "wandb: 503 Service Unavailable" >&2\nexit 1\n'
    lines = [
        "#!/bin/bash",
        'root=""',
        'while [ $# -gt 0 ]; do if [ "$1" = "--root" ]; then root="$2"; fi; shift; done',
        'mkdir -p "$root"',
    ]
    for name, payload in (("toxic-clf.skops", model), ("thresholds.json", thresholds),
                          ("baseline_flag_rates.json", baseline)):
        if payload is not None:
            lines.append(f"printf '%s' {payload.decode()!r} > \"$root/{name}\"")
    return "\n".join(lines) + "\n"


def _env_all(workspace: Path) -> dict[str, str]:
    """The DEFAULT artifact set, so the default itself is what the tests exercise."""
    env = _env(workspace)
    del env["ARTIFACT_NAMES"]
    return env


def test_the_fetcher_installs_thresholds_and_the_drift_baseline(tmp_path, workspace):
    """The dashboard fails closed without baseline_flag_rates.json, and the backend fails
    closed without thresholds.json. Both must land in ARTIFACT_DIR alongside the model."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub_multi(GOOD, THRESHOLDS, BASELINE))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env_all(workspace))
    assert result.returncode == 0, result.stdout + result.stderr
    installed = {path.name for path in (workspace / "artifacts").iterdir()}
    assert installed == set(ALL_THREE)
    assert (workspace / "artifacts/thresholds.json").read_bytes() == THRESHOLDS
    assert (workspace / "artifacts/baseline_flag_rates.json").read_bytes() == BASELINE


def test_a_missing_sidecar_artifact_fails_the_fetch(tmp_path, workspace):
    """Fail at fetch time on the instance, not at import time inside the container, where
    the only symptom is a restart loop and a log line nobody is watching yet."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub_multi(GOOD, THRESHOLDS, None))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env_all(workspace))
    assert result.returncode != 0
    assert "baseline_flag_rates.json" in result.stderr


def test_a_sidecar_missing_from_the_registry_is_taken_from_the_mirror(tmp_path, workspace):
    """The registry artifact carries the model and nothing else -- `log_model_artifact` calls
    `artifact.add_file(model_path)` once -- so on the real system this is the ORDINARY path
    for both sidecars, not an edge case."""
    bin_dir = tmp_path / "bin"
    log = tmp_path / "s3.log"
    make_stub(bin_dir, "wandb", _wandb_stub_multi(GOOD, None, None))
    make_stub(
        bin_dir,
        "aws",
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        'dest="${@: -1}"\n'
        'case "$dest" in\n'
        f"  *thresholds.json) printf '%s' {THRESHOLDS.decode()!r} > \"$dest\" ;;\n"
        f"  *baseline_flag_rates.json) printf '%s' {BASELINE.decode()!r} > \"$dest\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    result = run(SCRIPT, [], bin_dir, env=_env_all(workspace))
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path.name for path in (workspace / "artifacts").iterdir()} == set(ALL_THREE)
    requested = log.read_text(encoding="utf-8")
    # Each sidecar is keyed by ITS OWN digest, not the model's.
    assert f"artifacts/{THRESHOLDS_SHA}/thresholds.json" in requested, requested
    assert f"artifacts/{BASELINE_SHA}/baseline_flag_rates.json" in requested, requested
    assert f"artifacts/{GOOD_SHA}/toxic-clf.skops" not in requested, "the model came from S3"


def test_sidecar_artifacts_are_digest_verified_against_the_model_card(tmp_path, workspace):
    """Each name resolves to its OWN row. A lookup by position gives every artifact the
    model's digest, and the two sidecars then fail to verify against bytes that are correct."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "ARTIFACT_NAMES" in body
    assert "DIGEST_OF" in body or "SIDECAR_DIGESTS" in body


def test_a_tampered_sidecar_is_refused_and_nothing_at_all_is_installed(tmp_path, workspace):
    """thresholds.json IS the decision boundary: a swapped copy is a silent policy change
    that no metric flags. And the model must not be installed either -- a half-updated
    /artifacts is a backend scoring with this SHA's model at last SHA's thresholds."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub_multi(GOOD, TAMPERED_THRESHOLDS, BASELINE))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env_all(workspace))
    assert result.returncode != 0
    assert "digest mismatch" in result.stderr
    assert "thresholds.json" in result.stderr
    assert not list((workspace / "artifacts").iterdir()), "a rejected set was partly installed"


def test_a_named_artifact_with_no_row_in_the_card_fails_before_anything_is_fetched(
    tmp_path, workspace
):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub_multi(GOOD, THRESHOLDS, BASELINE))
    make_stub(bin_dir, "aws", _aws_stub(None))
    env = _env_all(workspace)
    env["ARTIFACT_NAMES"] = "toxic-clf.skops thresholds.json calibration.json"
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode != 0
    assert "calibration.json" in result.stderr
    assert not list((workspace / "artifacts").iterdir())


def test_the_default_artifact_set_matches_the_rows_the_repositorys_card_declares():
    """The default list and the card are two halves of one contract. A name in one and not
    the other is a fetch that fails closed on a healthy system, or a row nobody reads."""
    default = re.search(r'ARTIFACT_NAMES="\$\{ARTIFACT_NAMES:-([^}]+)\}"',
                        SCRIPT.read_text(encoding="utf-8"))
    assert default, "the fetcher declares no default artifact set"
    named = set(default.group(1).split())
    card = (REPO / "MODEL_CARD.md").read_text(encoding="utf-8")
    rows = set(re.findall(r"^\|\s*`([A-Za-z0-9_.-]+)`\s*\|\s*`[0-9a-f]{64}`\s*\|$", card, re.M))
    assert named == rows, f"fetcher defaults {sorted(named)}, card declares {sorted(rows)}"
