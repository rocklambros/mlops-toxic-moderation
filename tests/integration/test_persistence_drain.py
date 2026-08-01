import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.db import Prediction, ReviewQueue
from backend.persistence import drain_spool, persist_prediction
from backend.spool import Spool
from tests.unit.test_persistence import FakeSession, factory_for
from tests.unit.test_spool import pending

pytestmark = pytest.mark.integration


def test_spooled_rows_reach_postgres_when_it_recovers(engine, session, tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    # t0 is placed 31 ms in the past so the request-time measurement is distinguishable from
    # anything a drain-time stamp could produce: a drain that re-measured would store ~0.
    result = persist_prediction(
        factory_for(FakeSession(fail=True)),
        spool,
        pending("r1"),
        t0=time.perf_counter() - 0.031,
    )
    assert spool.depth() == 1

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    assert drain_spool(factory, spool) == 1
    assert spool.depth() == 0

    stored = session.scalars(select(Prediction)).all()
    assert len(stored) == 1
    assert stored[0].persist_status == "spooled"
    # The value measured at request time and reported to the client, not the drain time.
    assert stored[0].latency_ms >= 31
    assert stored[0].latency_ms == result.latency_ms
    assert session.get(ReviewQueue, "r1") is not None


def test_draining_twice_writes_one_row(engine, session, tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    spool.append(pending("r1"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    drain_spool(factory, spool)
    spool.append(pending("r1"))
    drain_spool(factory, spool)
    assert len(session.scalars(select(Prediction)).all()) == 1


def test_draining_an_empty_spool_is_a_no_op(engine, tmp_path):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    assert drain_spool(factory, Spool(tmp_path / "s.jsonl", max_rows=10)) == 0
