"""Every `pip install` in this repository verifies hashes (premortem C11, delivery spec 6.3).

A lock is worth exactly what the commands that read it respect. There is no allowlist of
"trusted" installs: an allowlist is a place to hide, and the install that hid there for three
phases was the lock generator's own -- `pip-tools==7.4.1`, pinned and unhashed, run five times
by five nearly identical Makefile targets on the box that holds the AWS SSO refresh token, the
W&B key, the Kaggle token, and the RunPod key at once.

`pip install --upgrade pip` is forbidden for the same reason: bootstrapping the tool that
checks integrity by fetching it without checking integrity is the exact circularity this
control exists to remove. The interpreter's bundled pip is sufficient.

Scope, stated rather than assumed. `docs/` and `tests/` are not scanned: the plans under
`docs/` quote dozens of example commands, and the test modules -- this one included -- are
scanners whose prose necessarily spells out the very commands being forbidden. Everything
else is scanned, and `test_no_install_command_in_this_repository_escapes_the_scan` asserts
that SEARCH_ROOTS actually reaches all of it, so a new `tools/setup.sh` cannot install
unhashed simply by living somewhere nobody listed.
"""

import re
from pathlib import Path

SEARCH_ROOTS = ("Makefile", ".github", "backend", "frontend", "monitoring", "rescorer",
                "infra", "scripts", "model")
SKIP_PARTS = {".venv", ".venv-lock", "build", "node_modules", ".git", "__pycache__"}
# Directories whose text describes install commands instead of running them. Named here, in
# one place, so the exemption is visible rather than spread across per-file skips.
PROSE_ROOTS = {"docs", "claudedocs", "tests"}
INSTALL_RE = re.compile(
    r"(?:python\s+-m\s+|\$\([A-Z_]+\)/|\./|[\w/.$(){}-]*/)?pip3?\s+install\b[^\n;&|]*"
)


def candidate_files() -> list[Path]:
    found: list[Path] = []
    for root in SEARCH_ROOTS:
        path = Path(root)
        if not path.exists():
            continue
        if path.is_file():
            found.append(path)
            continue
        for child in path.rglob("*"):
            if not child.is_file() or SKIP_PARTS & set(child.parts):
                continue
            if child.suffix in {".yml", ".yaml", ".sh", ".py"} or child.name.startswith(
                "Dockerfile"
            ):
                found.append(child)
    return sorted(set(found))


def install_commands(text: str) -> list[str]:
    """Join backslash continuations so a flag on the next line still counts as the same
    command. Without this, `--require-hashes` on line two reads as a different command."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    return [m.group(0) for m in INSTALL_RE.finditer(joined)]


def scanned_commands() -> list[tuple[Path, str]]:
    return [
        (path, command)
        for path in candidate_files()
        for command in install_commands(path.read_text(encoding="utf-8", errors="replace"))
    ]


def test_the_scanner_actually_looks_at_something():
    files = candidate_files()
    assert files, "the install scanner found no files to scan; SEARCH_ROOTS is wrong"
    assert any(p.name == "Makefile" for p in files)
    dockerfiles = [p for p in files if p.name.startswith("Dockerfile")]
    assert len(dockerfiles) >= 5, (
        f"only {len(dockerfiles)} Dockerfiles reached the scan; this project ships five"
    )


def test_the_scanner_reports_the_commands_it_certifies():
    """An empty offender list means one of two things: every install is hash-checked, or the
    regex stopped matching. They are indistinguishable unless the scan says what it found."""
    commands = scanned_commands()
    assert len(commands) >= 8, f"only {len(commands)} install commands matched; the regex broke"
    joined = " ".join(command for _, command in commands)
    assert "requirements/dev.lock" in joined, "the `make venv` install is not being scanned"
    assert "requirements/serve.txt" in joined, "the backend image install is not being scanned"


def test_no_install_command_in_this_repository_escapes_the_scan():
    """SEARCH_ROOTS is a list, and a list is a place to hide. This walks the whole working
    tree and fails if an install command exists in a file the scan above never opens."""
    root = Path(".")
    missed = []
    covered = set(candidate_files())
    for path in root.rglob("*"):
        if not path.is_file() or SKIP_PARTS & set(path.parts):
            continue
        if set(path.parts) & PROSE_ROOTS or path in covered:
            continue
        if path.suffix in {".png", ".onnx", ".csv", ".json", ".parquet", ".lock", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if install_commands(text):
            missed.append(str(path))
    assert not missed, (
        "these files install Python packages and are outside SEARCH_ROOTS, so nothing checks "
        f"them for --require-hashes: {missed}"
    )


def test_no_install_command_escapes_require_hashes():
    offenders = [
        f"{path}: {command.strip()}"
        for path, command in scanned_commands()
        if "--require-hashes" not in command
    ]
    assert not offenders, (
        "these installs run without hash verification on a box holding live credentials "
        "(premortem C11):\n  " + "\n  ".join(offenders)
    )


def test_the_lock_generator_is_installed_from_the_bootstrap_lock_and_nowhere_else():
    """`--require-hashes` alone would be satisfied by an inline `pip-tools==7.4.1 --hash=...`
    typed into one target and never updated again. The bootstrap lock exists so there is one
    file to regenerate; an install of pip-tools that does not read it is an orphan."""
    installs = [
        f"{path}: {command.strip()}"
        for path, command in scanned_commands()
        if re.search(r"\bpip-tools\b", command)
    ]
    assert installs, (
        "nothing in this repository installs pip-tools, so requirements/pip-tools.txt is a "
        "lock no command reads"
    )
    offenders = [c for c in installs if "requirements/pip-tools.txt" not in c]
    assert not offenders, f"pip-tools installed from somewhere other than its lock: {offenders}"


def test_pip_itself_is_never_upgraded_from_the_network():
    offenders = []
    for path, command in scanned_commands():
        if re.search(r"(--upgrade|-U)\b[^\n]*\bpip\b", command):
            offenders.append(f"{path}: {command.strip()}")
    assert not offenders, f"bootstrapping pip over the network defeats the lock: {offenders}"


def test_no_dockerfile_installs_from_an_unpinned_curl_pipe():
    offenders = []
    for path in candidate_files():
        if not path.name.startswith("Dockerfile"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"curl[^|\n]*\|\s*(ba)?sh", line):
                offenders.append(f"{path}:{lineno} curl-pipe-to-shell")
    assert not offenders, f"unverified remote code at image build time: {offenders}"
