"""Suite-wide determinism guard.

PYTHONHASHSEED must be set BEFORE the interpreter starts, so no pytest ini option and no
plugin can retroactively fix it -- and any plugin that writes os.environ["PYTHONHASHSEED"]
during startup would defeat an env-var check while changing nothing. The interpreter's own
flag cannot be spoofed, so that is what this reads.
"""

import sys

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if sys.flags.hash_randomization:
        raise pytest.UsageError(
            "PYTHONHASHSEED=0 is required: string hash randomization is ON, so any "
            "accidental dependence on set/dict iteration order is environment-dependent. "
            "Run `make test`, or in CI set `env: {PYTHONHASHSEED: '0'}` on the job."
        )
