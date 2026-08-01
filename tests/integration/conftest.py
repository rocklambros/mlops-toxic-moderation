"""Integration fixtures. These run against a REAL Postgres, never SQLite: the schema uses
JSONB and ON CONFLICT, and delivery spec section 3.3 makes a real dependency the phase gate.

TEST_DATABASE_URL wins when set (that is how CI's `services: postgres` is wired). Otherwise a
throwaway container is started, which is how it runs on the build box.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.config import load_settings
from backend.db import Base, init_db
from backend.schema_phase3 import apply_phase3_schema
from model.labels import LABELS
from tests.fixtures.make_model import build_demo_artifact

DEMO_KEY = "test-demo-key"
AUTH = {"X-API-Key": DEMO_KEY}


@pytest.fixture(scope="session")
def postgres_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def engine(postgres_url):
    from sqlalchemy import create_engine

    engine = create_engine(postgres_url, future=True)
    init_db(engine)
    apply_phase3_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def conn(engine):
    """A raw connection for the SQL-level Phase 3 suites.

    Phase 3's admission control and re-scorer drain are written in SQL against a connection
    rather than through the ORM, because their correctness is in `FOR UPDATE SKIP LOCKED`
    and in partial-index conflicts that an ORM round trip hides. Truncating on entry rather
    than on exit means a test that commits (admission control does) cannot leak into the
    next one even if it fails mid-way.
    """
    from sqlalchemy import text

    with engine.connect() as connection:
        connection.execute(text("TRUNCATE TABLE feedback, review_queue, predictions CASCADE"))
        connection.commit()
        yield connection
        connection.rollback()


@pytest.fixture()
def session(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
        session.rollback()
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())


@pytest.fixture(scope="session")
def artifact_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp("artifacts")
    path, digest = build_demo_artifact(directory / "toxic-clf.skops")
    card = directory / "MODEL_CARD.md"
    card.write_text(f"- MODEL_DIGEST: sha256:{digest}\n", encoding="utf-8")
    thresholds = directory / "thresholds.json"
    thresholds.write_text(json.dumps({label: 0.5 for label in LABELS}), encoding="utf-8")
    return {"artifact": path, "digest": digest, "card": card, "thresholds": thresholds}


@pytest.fixture()
def app_settings(artifact_bundle, postgres_url, tmp_path):
    return load_settings(
        {
            "DATABASE_URL": postgres_url,
            "DEMO_API_KEY": DEMO_KEY,
            "MODEL_ARTIFACT_PATH": str(artifact_bundle["artifact"]),
            "MODEL_CARD_PATH": str(artifact_bundle["card"]),
            "MODEL_DIGEST": artifact_bundle["digest"],
            "MODEL_REGISTRY_VERSION": "3",
            "THRESHOLDS_PATH": str(artifact_bundle["thresholds"]),
            "SUBMITTER_FP_KEY": "0" * 64,
            "SPOOL_PATH": str(tmp_path / "spool.jsonl"),
            "RATE_LIMIT_PER_MINUTE": "600",
            "RATE_LIMIT_BURST": "300",
            "RANDOM_AUDIT_RATE": "0.0",
        }
    )


@pytest.fixture()
def client(app_settings, session):
    from backend.app import create_app

    with TestClient(create_app(app_settings)) as client:
        yield client
