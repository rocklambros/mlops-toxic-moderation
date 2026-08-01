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
DEV_LOCK = REQUIREMENTS / "dev.lock"
SERVE_LOCK = REQUIREMENTS / "serve.txt"
MAKEFILE = Path("Makefile")
HASH_RE = re.compile(r"--hash=sha256:[0-9a-f]{64}")
PINNED_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?==(?P<version>\S+)")
# Every lock this project is known to ship. Discovery does the work below; this is the floor
# that stops a discovery bug from certifying an empty set (the pre-existing supply-chain
# suite uses the same idiom for the same reason).
KNOWN_LOCKS = frozenset(
    {"pip-tools.txt", "dev.lock", "serve.txt", "ui.txt", "monitor.txt", "rescorer.txt",
     "security.txt"}
)
# The unit and integration jobs install ONE lock. These are lazily imported, faked, or behind
# the day-8 cut line, and none of them belongs in it: streamlit and altair are imported inside
# the functions that draw and stubbed in tests, and the inference runtimes are the challenger's
# alone (tests/unit/test_severability.py).
NOT_IN_THE_TEST_ENVIRONMENT = (
    "streamlit", "altair", "onnxruntime", "tokenizers", "torch", "transformers", "optimum",
)


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


def requirements_files() -> list[Path]:
    return sorted(path for path in REQUIREMENTS.iterdir() if path.is_file())


def locks() -> list[Path]:
    """A lock is a requirements file that carries hashes. Discovered rather than listed, so
    a sixth surface cannot arrive with a hand-written `.txt` and no test noticing."""
    return [
        path
        for path in requirements_files()
        if HASH_RE.search(path.read_text(encoding="utf-8"))
    ]


def compile_command(path: Path) -> str | None:
    """The `pip-compile ...` line pip-tools writes into the header of everything it emits."""
    match = re.search(r"(?m)^#\s+(pip-compile\s+.*)$", path.read_text(encoding="utf-8")[:2000])
    return match.group(1) if match else None


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
    too, and the two have to be chosen as a pair. pip-tools 7.4.1 imported
    `pip._internal.utils.compat.stdlib_pkgs`, which pip 25 removed: an unconstrained `pip`
    resolved to the newest release, the throwaway lock venv installed it, and the very next
    `pip-compile` died with an ImportError -- with every hash in this file still perfectly
    valid. Hashes pin what installs, not whether it runs.

    This originally asserted `pip major < 25`, which was a proxy for that pairing and went
    stale the moment pip-tools was upgraded: 7.6.0 runs against pip 26 quite happily, and
    holding pip below 25 then kept six advisories open to satisfy a constraint that no longer
    existed. The invariant is the pairing, so that is what is asserted -- both versions
    pinned, and the lock agreeing with the target that generates it, so neither can drift
    alone. Bumping either means running `make lock-tools` and confirming `pip-compile` still
    executes, which the target itself does.
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    blocks = dict(requirement_blocks(text))
    versions = {}
    for dependency in ("pip", "pip-tools"):
        assert dependency in blocks, (
            f"{dependency} is part of the bootstrap closure; leaving it out makes "
            "`pip install --require-hashes` refuse the whole file"
        )
        match = PINNED_RE.match(blocks[dependency])
        assert match is not None, blocks[dependency]
        versions[dependency] = match.group("version")

    recipe = Path("Makefile").read_text(encoding="utf-8")
    for dependency, version in versions.items():
        assert f"{dependency}=={version}" in recipe, (
            f"the bootstrap lock pins {dependency}=={version}, which `make lock-tools` does "
            "not name. The lock and the target that regenerates it have drifted, so the next "
            f"regeneration silently changes {dependency}"
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


# --- every surface: one human-edited input, one compiled hashed lock (C11, H35) ----------


def test_every_file_in_the_requirements_directory_is_an_input_or_a_fully_hashed_lock():
    """The partition is the control. A file that is neither -- a hand-written `.txt` with a
    few pins, or a lock where one requirement lost its hash during a merge -- is exactly what
    a `"--hash" in text` check certifies as safe while it installs something nobody verified.
    """
    files = requirements_files()
    assert files, "the requirements scan found nothing, so it certifies nothing"
    for path in files:
        blocks = requirement_blocks(path.read_text(encoding="utf-8"))
        assert blocks, f"{path} declares no requirements at all"
        hashed = [name for name, block in blocks if HASH_RE.search(block)]
        assert len(hashed) in (0, len(blocks)), (
            f"{path} hashes {len(hashed)} of {len(blocks)} requirements. Partially hashed is "
            "neither an input nor a lock: "
            + ", ".join(sorted({n for n, _ in blocks} - set(hashed)))
        )


def test_every_lock_pins_every_requirement_with_a_hash():
    discovered = {path.name for path in locks()}
    assert discovered >= KNOWN_LOCKS, (
        f"a known lock is missing or carries no hashes at all: {sorted(KNOWN_LOCKS - discovered)}"
    )
    offenders = []
    for lock in locks():
        for name, block in requirement_blocks(lock.read_text(encoding="utf-8")):
            if not HASH_RE.search(block):
                offenders.append(f"{lock.name}:{name}")
    assert not offenders, (
        "these requirements install without hash verification (premortem C11): "
        + ", ".join(offenders)
    )


def test_every_compiled_lock_names_an_input_that_exists_and_is_not_itself_a_lock():
    """pip-compile writes the exact command into the header. If it names a file that is gone,
    or names another compiled lock, then `make lock` no longer reproduces this file and the
    hashes in it are whatever the last person happened to have on disk."""
    hashed = set(locks())
    checked = 0
    for lock in sorted(hashed):
        command = compile_command(lock)
        if command is None:
            assert lock.name == "pip-tools.txt", (
                f"{lock.name} carries hashes but no pip-compile header, so nothing in this "
                "repository regenerates it"
            )
            continue
        operands = [token for token in command.split()[1:] if not token.startswith("-")]
        assert operands, f"{lock.name}: the pip-compile header names no input file"
        for operand in operands:
            source = Path(operand)
            assert source.is_file(), f"{lock.name} was compiled from {operand}, which is gone"
            assert source not in hashed, (
                f"{lock.name} was compiled from {operand}, which is itself a compiled lock; a "
                "hand-edit to the input would be silently reverted by the next `make lock`"
            )
        checked += 1
    assert checked >= len(KNOWN_LOCKS) - 1, f"only {checked} compiled locks examined"


def test_no_lock_carries_an_unpinned_or_ranged_requirement():
    ranged = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]*\s*(>=|<=|~=|>|<|!=)")
    offenders = []
    for lock in locks():
        for lineno, raw in enumerate(lock.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if line and ranged.match(line):
                offenders.append(f"{lock.name}:{lineno} {line}")
    assert not offenders, f"version ranges in a lock defeat the lock: {offenders}"


def test_every_lock_has_a_makefile_target_that_regenerates_it():
    """A lock nothing regenerates is a hand-edited lock one merge conflict from being wrong."""
    recipe = MAKEFILE.read_text(encoding="utf-8")
    for lock in locks():
        if lock.name == "pip-tools.txt":
            assert re.search(r"(?m)^lock-tools:", recipe)
            continue
        assert re.search(rf"--output-file[= ]requirements/{re.escape(lock.name)}\b", recipe), (
            f"no Makefile target recompiles requirements/{lock.name}"
        )


def test_every_pip_compile_invocation_resolves_wheels_only():
    """`--only-binary=:all:` somewhere in a target body is not the control, and every lock
    target in this repository proved it: the flag sat on the `pip install pip-tools` line two
    lines above, while `pip-compile` itself was free to download a source distribution and run
    its setup.py -- on the box holding the AWS SSO refresh token, the W&B key, the Kaggle
    token, and the RunPod key (premortem C11). Resolution is the exposed moment, not install.
    """
    joined = re.sub(r"\\\s*\n\s*", " ", MAKEFILE.read_text(encoding="utf-8"))
    commands = [
        line.strip()
        for line in joined.splitlines()
        if "pip-compile" in line and "--output-file" in line
    ]
    assert len(commands) >= len(KNOWN_LOCKS) - 1, (
        f"only {len(commands)} pip-compile invocations found; the scan is too narrow"
    )
    for command in commands:
        assert "PIP_ONLY_BINARY=:all:" in command or "--only-binary=:all:" in command, (
            f"this resolution may build a source distribution: {command}"
        )


# --- the one lock CI installs (rubric 4.1, premortem C11) --------------------------------


def test_the_development_lock_is_the_superset_the_test_job_installs():
    """CI installs ONE lock. Until this phase `make venv` produced an environment in which
    seven test modules could not be collected at all -- backend.db imports SQLAlchemy,
    backend.app imports FastAPI, and requirements/dev.lock carried neither, because Phase 2
    put them in requirements/serve.txt and installed that by hand. Every green local run was
    against an environment no command in this repository could reproduce.

    Derived from serve.txt rather than listed, so a dependency added to the serving surface
    turns this red instead of turning CI's collection red three commits later."""
    dev = {name for name, _ in requirement_blocks(DEV_LOCK.read_text(encoding="utf-8"))}
    serve = {name for name, _ in requirement_blocks(SERVE_LOCK.read_text(encoding="utf-8"))}
    assert serve, "the serving lock parsed to nothing, so the comparison certifies nothing"
    missing = sorted(serve - dev)
    assert not missing, (
        "the serving surface is not inside the lock the test job installs, so the FastAPI "
        f"integration suite cannot be collected from a clean `make venv`: {missing}"
    )
    for tool in ("pytest", "pytest-cov", "ruff", "pyyaml", "testcontainers", "httpx"):
        assert tool in dev, f"{tool} is absent from the compiled development lock"


def test_the_development_lock_carries_no_streamlit_and_no_inference_runtime():
    """The other half of the superset property, and the half that gets lost first. Phase 3
    kept Streamlit out of the unit job on purpose (the UI modules import it inside the
    functions that draw, and the tests stub it), and the inference runtimes belong to the
    severable challenger alone. Folding ui.in and monitor.in into the development surface --
    which is one plausible way to read "superset" -- reverses both."""
    dev = {name for name, _ in requirement_blocks(DEV_LOCK.read_text(encoding="utf-8"))}
    intruders = sorted(dev & set(NOT_IN_THE_TEST_ENVIRONMENT))
    assert not intruders, (
        f"{intruders} reached the lock the unit job installs. Streamlit is stubbed in tests "
        "and the inference runtimes are the cut-line challenger's (tests/unit/"
        "test_severability.py); neither is importable from a test and both are hundreds of MB"
    )


def test_the_security_surface_is_locked_and_isolated_from_the_test_environment():
    """The scanners resolve into their own lock on purpose. semgrep drags in a large,
    aarch64-sensitive dependency tree, and a resolution failure there must not be able to stop
    the test suite from being lockable at all."""
    source = REQUIREMENTS / "security.in"
    lock = REQUIREMENTS / "security.txt"
    assert source.is_file(), "requirements/security.in is missing; the scanners are unpinned"
    assert lock in locks(), "requirements/security.txt is not a compiled hashed lock"
    scanners = {name for name, _ in requirement_blocks(lock.read_text(encoding="utf-8"))}
    for scanner in ("pip-audit", "semgrep"):
        assert scanner in scanners, f"{scanner} is absent from the security lock"
    dev = {name for name, _ in requirement_blocks(DEV_LOCK.read_text(encoding="utf-8"))}
    leaked = sorted(dev & {"pip-audit", "semgrep"})
    assert not leaked, (
        f"{leaked} reached requirements/dev.lock; the security surface is deliberately "
        "separate so a scanner resolution failure cannot block the test suite"
    )
