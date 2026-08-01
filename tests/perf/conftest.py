"""The load pass needs the same real-Postgres fixtures the integration suite uses.

A `conftest.py` only supplies fixtures to its own directory and below, so `tests/perf/`
cannot see `tests/integration/conftest.py`. Re-exporting the fixture functions here
registers them for this directory. Nothing is redefined, so the two suites cannot drift
into measuring different things.
"""

from tests.integration.conftest import (  # noqa: F401
    app_settings,
    artifact_bundle,
    client,
    engine,
    postgres_url,
    session,
)
