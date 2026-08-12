"""Every Streamlit image must pin Arrow to the system allocator.

pyarrow 25 on aarch64 defaults to its bundled mimalloc. Streamlit runs each script run on a
new ScriptRunner thread, so every page load calls into mimalloc's per-thread heap
initialisation from a thread it has not seen before. On 2026-08-12 that segfaulted the
monitoring dashboard five times; the core, decoded with gdb against the container's rootfs,
put `mi_thread_init` at frame 0 under `arrow::py::NdarrayToArrow` under
`pyarrow.lib.Table.from_pandas` -- which is what `st.dataframe` calls to serialise a
DataFrame for the browser.

The crash therefore reached every page with a table on it, which is all three of them, and
that included the graded user interface. This test exists because the fix is one environment
variable in a Dockerfile, which is exactly the kind of line a later edit drops without
noticing: the failure it prevents is intermittent, architecture-specific, and looks like an
infrastructure flake rather than a missing ENV.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The images that render DataFrames. Keyed to the requirements file that pulls pyarrow in, so
# an image that gains pyarrow later has a visible reason to join this list.
STREAMLIT_IMAGES = {
    "frontend/Dockerfile": "requirements/ui.txt",
    "frontend/Dockerfile.reviewer": "requirements/ui.txt",
    "monitoring/Dockerfile": "requirements/monitor.txt",
}


@pytest.mark.parametrize("dockerfile", sorted(STREAMLIT_IMAGES))
def test_the_image_pins_arrow_to_the_system_allocator(dockerfile):
    body = (ROOT / dockerfile).read_text(encoding="utf-8")
    assert "ARROW_DEFAULT_MEMORY_POOL=system" in body, (
        f"{dockerfile} does not pin the Arrow allocator; pyarrow's default mimalloc "
        f"segfaults under Streamlit's per-run threads on aarch64"
    )


@pytest.mark.parametrize("dockerfile", sorted(STREAMLIT_IMAGES))
def test_the_image_keeps_faulthandler_on(dockerfile):
    """The allocator pin shipped without a reproduction, so it is a hypothesis with a good
    backtrace behind it rather than a measured fix. faulthandler is what makes the next
    occurrence legible: Python prints the offending frame to stderr on SIGSEGV, and awslogs
    ships stderr to CloudWatch. Without it a recurrence is another stripped core.

    Pinned separately from the allocator so that removing one does not quietly remove both --
    the instrumentation matters most in exactly the case where the fix turns out to be wrong.
    """
    body = (ROOT / dockerfile).read_text(encoding="utf-8")
    assert "PYTHONFAULTHANDLER=1" in body, (
        f"{dockerfile} lost PYTHONFAULTHANDLER; a future segfault would again be a stripped "
        f"core with no Python frame"
    )


@pytest.mark.parametrize("dockerfile,reqs", sorted(STREAMLIT_IMAGES.items()))
def test_the_pinned_image_is_one_that_actually_installs_pyarrow(dockerfile, reqs):
    """Non-vacuity. If pyarrow left these requirements the pin would be guarding nothing, and
    this test would keep passing while saying it had checked something."""
    assert "pyarrow" in (ROOT / reqs).read_text(encoding="utf-8"), (
        f"{reqs} no longer installs pyarrow, so {dockerfile}'s allocator pin is now inert -- "
        f"re-derive this list rather than leaving a guard over nothing"
    )


def test_every_image_that_installs_pyarrow_is_covered():
    """The inverse sweep: a fourth image that gains pyarrow must not silently skip the pin."""
    covered = set(STREAMLIT_IMAGES)
    for dockerfile in ROOT.glob("*/Dockerfile*"):
        rel = dockerfile.relative_to(ROOT).as_posix()
        body = dockerfile.read_text(encoding="utf-8")
        installs = [
            reqs.name
            for reqs in (ROOT / "requirements").glob("*.txt")
            if reqs.name in body and "pyarrow" in reqs.read_text(encoding="utf-8")
        ]
        if installs and rel not in covered:
            pytest.fail(f"{rel} installs pyarrow via {installs} but is not in STREAMLIT_IMAGES")
