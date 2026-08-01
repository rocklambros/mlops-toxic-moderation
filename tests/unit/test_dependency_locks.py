"""Every dependency this project installs is pinned AND hashed (premortem C11).

The build box holds an AWS SSO refresh token, the W&B key, the Kaggle token, and the RunPod
key at the same time. One malicious transitive dependency with a post-install hook takes all
four, and the blast radius is the AWS organisation rather than the sandbox. `--require-hashes`
is what makes "pinned" mean "this exact file" instead of "this version string, whatever the
index serves today".

The lock generator is the first thing installed and the last thing anyone thinks to verify,
so it gets its own hand-built bootstrap lock and its own assertions here.
"""

import re
from pathlib import Path

REQUIREMENTS = Path("requirements")
BOOTSTRAP = REQUIREMENTS / "pip-tools.txt"
HASH_RE = re.compile(r"--hash=sha256:[0-9a-f]{64}")
PINNED_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?==(?P<version>\S+)")


def requirement_blocks(text: str) -> list[tuple[str, str]]:
    """Split a lock into (name, block) pairs, joining pip's backslash continuations."""
    blocks: list[tuple[str, str]] = []
    name: str | None = None
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        match = PINNED_RE.match(line)
        if match and not raw.startswith((" ", "\t")):
            if name is not None:
                blocks.append((name, "\n".join(current)))
            name = match.group("name").lower().replace("_", "-")
            current = [line]
        elif name is not None:
            current.append(line)
    if name is not None:
        blocks.append((name, "\n".join(current)))
    return blocks


def test_the_block_parser_reports_before_it_is_trusted():
    """Every assertion below iterates over `requirement_blocks`. A parser that returned an
    empty list would make all of them vacuously true, which is the exact shape of the defect
    this file exists to prevent."""
    sample = "alpha==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n    # via beta\nbeta==2.0\n"
    assert requirement_blocks(sample) == [
        ("alpha", "alpha==1.0 \\\n    --hash=sha256:" + "a" * 64),
        ("beta", "beta==2.0"),
    ]
    assert requirement_blocks("") == []


def test_the_lock_generator_is_itself_installed_from_a_hashed_lock():
    assert BOOTSTRAP.exists(), (
        "requirements/pip-tools.txt is missing: pip-tools is installed unhashed, on the box "
        "that holds four live credentials (premortem C11)"
    )
    blocks = requirement_blocks(BOOTSTRAP.read_text(encoding="utf-8"))
    names = {name for name, _ in blocks}
    assert "pip-tools" in names, f"pip-tools itself is not pinned in the bootstrap lock: {names}"
    for name, block in blocks:
        assert HASH_RE.search(block), f"{name} in pip-tools.txt carries no --hash"


def test_the_bootstrap_lock_carries_the_full_transitive_closure():
    """`pip install --require-hashes` refuses to resolve anything not listed, so a bootstrap
    lock that names only pip-tools installs nothing at all."""
    blocks = requirement_blocks(BOOTSTRAP.read_text(encoding="utf-8"))
    names = {name for name, _ in blocks}
    for dependency in ("build", "click", "packaging", "pyproject-hooks", "setuptools", "wheel"):
        assert dependency in names, (
            f"{dependency} is a pip-tools dependency and is absent from the bootstrap lock; "
            "regenerate it with `make lock-tools`"
        )


def test_the_bootstrap_lock_pins_a_pip_the_lock_generator_can_actually_run_against():
    """`pip` is in pip-tools' own dependency closure, so `pip download pip-tools` fetches it
    too. pip-tools 7.4.1 imports `pip._internal.utils.compat.stdlib_pkgs`, which pip 25
    removed: an unconstrained `pip` here resolves to the newest release, the throwaway lock
    venv installs it, and the very next `pip-compile` dies with an ImportError -- with every
    hash in this file still perfectly valid. Hashes pin what installs, not whether it runs.
    """
    blocks = dict(requirement_blocks(BOOTSTRAP.read_text(encoding="utf-8")))
    assert "pip" in blocks, (
        "pip is part of the pip-tools closure; leaving it out of the bootstrap lock makes "
        "`pip install --require-hashes` refuse the whole file"
    )
    match = PINNED_RE.match(blocks["pip"])
    assert match is not None, blocks["pip"]
    major = int(match.group("version").split(".")[0])
    assert major < 25, (
        f"the bootstrap lock pins pip {match.group('version')}; pip-tools 7.4.1 imports "
        "pip._internal.utils.compat.stdlib_pkgs, which pip 25 removed. Regenerating without "
        "the `pip==` constraint in `make lock-tools` produces a lock that installs cleanly "
        "and then cannot compile anything"
    )


def test_the_bootstrap_lock_is_regenerated_by_a_makefile_target_that_never_builds_an_sdist():
    """A hand-edited bootstrap lock is a hand-chosen hash. The generator must be in the
    repository, and it must download wheels only: `pip download` on an sdist runs its
    setup.py for metadata, on the box holding four live credentials."""
    recipe = Path("Makefile").read_text(encoding="utf-8")
    assert re.search(r"(?m)^lock-tools:", recipe), "no `make lock-tools` target"
    body = recipe.split("\nlock-tools:", 1)[1].split("\n\n", 1)[0]
    assert "requirements/pip-tools.txt" in body, "the target does not write the bootstrap lock"
    # Joined, because the flag that matters sits on the next line behind a backslash. Read
    # per command rather than per body: `--only-binary=:all:` also appears on the *install*
    # line further down, so a body-wide substring check stays green while `pip download`
    # quietly grows the right to build a source distribution.
    joined = re.sub(r"\\\s*\n\s*", " ", body)
    downloads = re.findall(r"pip download[^\n;&|]*", joined)
    assert downloads, "the bootstrap lock is not built from downloaded wheels"
    for command in downloads:
        assert "--only-binary=:all:" in command, (
            f"`make lock-tools` may fetch a source distribution and run its setup.py: {command}"
        )
