"""The re-scorer drain, against a real Postgres.

The correctness that matters here is in the SQL, not in Python: `FOR UPDATE SKIP LOCKED`
claims a batch, and the UPDATE is guarded on `status = 'pending'` so a second pass, a
crashed pass, or a second worker cannot double-advance a row. None of that is observable
against SQLite or a mock.

Every helper below writes a row that satisfies every NOT NULL column Phase 2 declares --
`input_chars`, `status`, `persist_status`. Omitting one aborts the transaction, and every
assertion that follows would then be a statement about an empty table.
"""

import datetime as dt

import numpy as np
import pytest
from sqlalchemy import text

from model.contract import probs_to_dict
from model.labels import LABELS
from rescorer.worker import drain_once

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 16, 9, 0, tzinfo=dt.UTC)
# Six distinct values, so any permutation of the label mapping changes at least one of them.
STUB_PROBS = np.array([0.9, 0.2, 0.3, 0.05, 0.7, 0.1], dtype=np.float32)


class StubChallenger:
    def __init__(self):
        self.batches: list[int] = []
        self.texts: list[str] = []

    def predict_proba(self, texts):
        self.batches.append(len(texts))
        self.texts.extend(texts)
        return np.tile(STUB_PROBS, (len(texts), 1))


def _pending(conn, request_id: str, snapshot: str = "text here") -> None:
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    vals = ", ".join("0.5" for _ in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, input_chars, model_version, "
            f"{cols}, decision, max_prob, latency_ms, status, persist_status) "
            f"VALUES (:rid, :ts, :snapshot, 9, 'm', {vals}, 'review', 0.5, 11, 'ok', 'direct')"
        ),
        {"rid": request_id, "ts": NOW, "snapshot": snapshot},
    )
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot) VALUES (:rid, :ts, 'pending', 'flagged', 1.0, :snapshot)"
        ),
        {"rid": request_id, "ts": NOW, "snapshot": snapshot},
    )


def test_the_fixture_helper_actually_writes_both_rows(conn):
    """Non-vacuity. A silently rejected INSERT would make every test below a statement
    about an empty queue, and `drain_once` returning 0 would look like correct behaviour."""
    _pending(conn, "probe")
    conn.commit()
    assert conn.execute(text("SELECT count(*) FROM predictions")).scalar_one() == 1
    assert conn.execute(text("SELECT count(*) FROM review_queue")).scalar_one() == 1


def test_drain_writes_probs_and_advances_status(conn):
    _pending(conn, "q1")
    conn.commit()
    assert drain_once(conn, StubChallenger(), batch_size=16) == 1
    row = conn.execute(
        text("SELECT status, distilbert_probs FROM review_queue WHERE request_id = 'q1'")
    ).one()
    assert row.status == "rescored"
    # jsonb does not preserve insertion order -- it stores keys sorted by length then bytes
    # -- so the stored object is compared by mapping rather than by key sequence. The label
    # ORDER is what matters and it is checked where it is decided, in probs_to_dict: six
    # distinct probabilities mean any permutation moves at least one value.
    assert row.distilbert_probs == probs_to_dict(STUB_PROBS)
    assert set(row.distilbert_probs) == set(LABELS)
    assert row.distilbert_probs["toxic"] == pytest.approx(0.9, abs=1e-6)
    assert row.distilbert_probs["identity_hate"] == pytest.approx(0.1, abs=1e-6)


def test_a_transposed_probability_row_is_visible_in_the_stored_object(conn):
    """The guard on the assertion above. If the six stub probabilities were not distinct, a
    label transposition would store an object equal to the correct one and this suite would
    certify nothing about ordering."""
    _pending(conn, "q1t")
    conn.commit()

    class Transposed(StubChallenger):
        def predict_proba(self, texts):
            return np.tile(STUB_PROBS[::-1], (len(texts), 1))

    assert drain_once(conn, Transposed(), batch_size=16) == 1
    stored = conn.execute(
        text("SELECT distilbert_probs FROM review_queue WHERE request_id = 'q1t'")
    ).scalar_one()
    assert stored != probs_to_dict(STUB_PROBS)


def test_drain_is_idempotent(conn):
    _pending(conn, "q2")
    conn.commit()
    assert drain_once(conn, StubChallenger(), batch_size=16) == 1
    assert drain_once(conn, StubChallenger(), batch_size=16) == 0
    count = conn.execute(
        text("SELECT count(*) FROM review_queue WHERE request_id = 'q2' AND status = 'rescored'")
    ).scalar_one()
    assert count == 1


def test_drain_batches_rather_than_looping_one_row_at_a_time(conn):
    for i in range(20):
        _pending(conn, f"b{i}")
    conn.commit()
    challenger = StubChallenger()
    assert drain_once(conn, challenger, batch_size=8) == 8
    assert challenger.batches == [8]


def test_drain_on_an_empty_queue_returns_zero(conn):
    assert drain_once(conn, StubChallenger(), batch_size=16) == 0


def test_drain_on_an_empty_queue_does_not_call_the_challenger(conn):
    """A model forward pass on zero rows is a wasted 200 ms on a CPU instance that wakes up
    every five seconds, and `predict_proba([])` is a shape the adapter would have to guess
    about."""
    challenger = StubChallenger()
    assert drain_once(conn, challenger, batch_size=16) == 0
    assert challenger.batches == []


def test_drain_never_touches_a_reviewed_row(conn):
    _pending(conn, "q3")
    conn.execute(text("UPDATE review_queue SET status = 'reviewed' WHERE request_id = 'q3'"))
    conn.commit()
    assert drain_once(conn, StubChallenger(), batch_size=16) == 0
    probs = conn.execute(
        text("SELECT distilbert_probs FROM review_queue WHERE request_id = 'q3'")
    ).scalar_one()
    assert probs is None


def test_drain_uses_the_snapshot_not_the_purgeable_input_text(conn):
    """The 30-day retention purge nulls `predictions.input_text`. Re-scoring must not depend
    on a column that is designed to disappear."""
    _pending(conn, "q4", snapshot="the snapshot")
    conn.execute(text("UPDATE predictions SET input_text = NULL WHERE request_id = 'q4'"))
    conn.commit()
    challenger = StubChallenger()
    assert drain_once(conn, challenger, batch_size=16) == 1
    assert challenger.texts == ["the snapshot"]


def test_drain_takes_the_oldest_items_first(conn):
    """The queue is a queue. A batch that took the newest rows would starve the oldest ones
    forever once arrival rate exceeded drain rate."""
    for i in range(4):
        _pending(conn, f"age{i}")
        conn.execute(
            text("UPDATE review_queue SET enqueued_ts = :ts WHERE request_id = :rid"),
            {"ts": NOW - dt.timedelta(days=i), "rid": f"age{i}"},
        )
    conn.commit()
    assert drain_once(conn, StubChallenger(), batch_size=2) == 2
    rescored = conn.execute(
        text("SELECT request_id FROM review_queue WHERE status = 'rescored' ORDER BY request_id")
    ).scalars().all()
    assert rescored == ["age2", "age3"]


def test_a_row_that_stops_being_pending_mid_batch_is_not_counted_as_rescored(conn):
    """The `AND status = 'pending'` guard on the UPDATE, exercised rather than asserted.

    The claim and the write are separated by a model forward pass, and the count that the
    worker reports -- and that its back-off schedule reads -- has to be the number of rows
    it actually advanced, not the number it hoped to. The interleaving is produced from
    inside the drain's own transaction, which is the only writer that can reach a row this
    transaction has locked with FOR UPDATE.
    """

    class StealsARow(StubChallenger):
        def predict_proba(self, texts):
            conn.execute(
                text("UPDATE review_queue SET status = 'reviewed' WHERE request_id = 'steal0'")
            )
            return super().predict_proba(texts)

    for i in range(3):
        _pending(conn, f"steal{i}")
    conn.commit()
    assert drain_once(conn, StealsARow(), batch_size=8) == 2
    assert conn.execute(
        text("SELECT status FROM review_queue WHERE request_id = 'steal0'")
    ).scalar_one() == "reviewed"
    assert conn.execute(
        text("SELECT distilbert_probs FROM review_queue WHERE request_id = 'steal0'")
    ).scalar_one() is None


def test_a_second_worker_does_not_double_process_a_claimed_row(engine, conn):
    """SKIP LOCKED is the whole reason this is SQL rather than a Python loop. Two workers
    running concurrently must divide the queue, not duplicate it.

    `lock_timeout` is what makes this test *fail* rather than hang. Without SKIP LOCKED the
    drain blocks on the competing worker's row locks, and a hang in CI is indistinguishable
    from a slow container until the job's whole timeout expires.
    """
    for i in range(4):
        _pending(conn, f"race{i}")
    conn.commit()
    conn.execute(text("SET lock_timeout = '3s'"))
    conn.commit()

    first, second = StubChallenger(), StubChallenger()
    with engine.connect() as other:
        rows = other.execute(
            text(
                "SELECT request_id FROM review_queue WHERE status = 'pending' "
                "ORDER BY enqueued_ts LIMIT 2 FOR UPDATE SKIP LOCKED"
            )
        ).all()
        assert len(rows) == 2, "the competing worker did not claim anything to skip"
        assert drain_once(conn, first, batch_size=4) == 2
        other.rollback()

    assert first.batches == [2]
    assert drain_once(conn, second, batch_size=4) == 2
    assert conn.execute(
        text("SELECT count(*) FROM review_queue WHERE status = 'rescored'")
    ).scalar_one() == 4
