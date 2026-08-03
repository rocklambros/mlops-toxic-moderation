"""Read-only helpers for the deployed traversal gate.

Connects as the monitoring read-only role, not as the RDS master. That is not tidiness: the
gate asserts that a prediction made over the network reaches the database *and is visible to
the dashboard's own credentials*. Counting the row as a superuser would prove the first half
and assume the second, and the half it assumed is the one premortem H16 is about -- a
dashboard that silently sees an empty schema renders empty charts with no error.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text


def _dsn() -> str:
    dsn = os.environ.get("MONITORING_DB_DSN")
    if not dsn:
        raise RuntimeError("MONITORING_DB_DSN is required for the traversal gate")
    return dsn


def count_predictions() -> int:
    engine = create_engine(_dsn(), pool_pre_ping=True)
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM predictions")).scalar_one())


def count_feedback_by_source() -> dict[str, int]:
    engine = create_engine(_dsn(), pool_pre_ping=True)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT source, count(*) FROM feedback GROUP BY source")
        ).all()
    return {source: int(count) for source, count in rows}


def readonly_role_cannot_write() -> str:
    """Return the error the read-only role gets when it tries to create a table.

    The H16 acceptance check, run as part of the traversal rather than asserted from the
    grant statements. A GRANT that was applied and a GRANT that was intended are different
    facts, and only one of them is observable from outside.
    """
    engine = create_engine(_dsn(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("CREATE TABLE traversal_probe (x int)"))
            connection.commit()
    except Exception as exc:  # noqa: BLE001 -- the message is the return value
        return str(exc)
    return ""
