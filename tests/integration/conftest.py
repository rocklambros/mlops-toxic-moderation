"""Integration fixtures. These run against a REAL Postgres, never SQLite: the schema uses
JSONB and ON CONFLICT, and delivery spec section 3.3 makes a real dependency the phase gate.

TEST_DATABASE_URL wins when set (that is how CI's `services: postgres` is wired). Otherwise a
throwaway container is started, which is how it runs on the build box.
"""

import os

import pytest
from sqlalchemy.orm import sessionmaker

from backend.db import Base, init_schema


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
    init_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
        session.rollback()
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
