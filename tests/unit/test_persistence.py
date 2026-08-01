import time
from contextlib import contextmanager

import pytest
from sqlalchemy.exc import OperationalError

from backend.persistence import persist_prediction
from backend.spool import Spool, SpoolFull
from tests.unit.test_spool import pending


class FakeSession:
    def __init__(self, fail: bool, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.written = []

    def execute(self, statement):
        if self.fail:
            raise OperationalError("insert", {}, Exception("connection refused"))
        time.sleep(self.delay)
        self.written.append(statement)

    def commit(self):
        if self.fail:
            raise OperationalError("commit", {}, Exception("connection refused"))


def factory_for(session):
    @contextmanager
    def factory():
        yield session

    return factory


def test_healthy_database_takes_the_direct_path(tmp_path):
    session = FakeSession(fail=False)
    spool = Spool(tmp_path / "s.jsonl", max_rows=5)
    result = persist_prediction(factory_for(session), spool, pending(), t0=time.perf_counter())
    assert result.persist_status == "direct"
    assert spool.depth() == 0


def test_latency_includes_the_persistence_component(tmp_path):
    """H28. Stamping latency before persistence omits the slowest component from the graded
    chart. A 50 ms insert must show up in the stamped value."""
    session = FakeSession(fail=False, delay=0.05)
    spool = Spool(tmp_path / "s.jsonl", max_rows=5)
    result = persist_prediction(factory_for(session), spool, pending(), t0=time.perf_counter())
    assert result.latency_ms >= 50


def test_unreachable_database_spools_instead_of_failing(tmp_path):
    """H30. This is the test that fails under the original 'return 503' design."""
    session = FakeSession(fail=True)
    spool = Spool(tmp_path / "s.jsonl", max_rows=5)
    result = persist_prediction(factory_for(session), spool, pending(), t0=time.perf_counter())
    assert result.persist_status == "spooled"
    assert result.error == "OperationalError"
    assert spool.depth() == 1
    assert spool.read_all()[0].prediction.persist_status == "spooled"


def test_the_direct_path_is_retried_once_before_spooling(tmp_path):
    attempts = {"count": 0}

    @contextmanager
    def flaky():
        attempts["count"] += 1
        yield FakeSession(fail=attempts["count"] == 1)

    spool = Spool(tmp_path / "s.jsonl", max_rows=5)
    result = persist_prediction(flaky, spool, pending(), t0=time.perf_counter())
    assert attempts["count"] == 2
    assert result.persist_status == "direct"
    assert spool.depth() == 0


def test_a_full_spool_fails_closed(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=1)
    spool.append(pending("r0"))
    with pytest.raises(SpoolFull):
        persist_prediction(
            factory_for(FakeSession(fail=True)), spool, pending(), t0=time.perf_counter()
        )
