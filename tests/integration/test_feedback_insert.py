"""`insert_feedback` against a real Postgres.

The derivation is unit-tested; this covers the write, which the unit suite cannot reach.
Three things it can only get wrong here: the JSONB cast, the back-dating parameter (a bare
untyped NULL inside COALESCE is a resolution Postgres may refuse), and whether the record
it produces actually satisfies the two CHECK constraints the schema puts on this table.
"""

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.feedback import FeedbackRecord, derive_feedback, insert_feedback, user_feedback
from model.labels import LABELS

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 14, 9, 0, tzinfo=dt.UTC)


def _predict_row(conn, request_id: str) -> None:
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    vals = ", ".join("0.1" for _ in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, input_chars, model_version, "
            f"{cols}, decision, max_prob, latency_ms, status, persist_status) "
            f"VALUES (:rid, now(), 'hello', 5, 'm', {vals}, 'allow', 0.1, 5, 'ok', 'direct')"
        ),
        {"rid": request_id},
    )


def test_a_reviewer_record_round_trips_including_its_agreement_object(conn):
    _predict_row(conn, "a1")
    record = derive_feedback(
        "a1",
        {label: 0 for label in LABELS} | {"toxic": 1},
        {label: False for label in LABELS} | {"toxic": True},
        "rock",
    )
    insert_feedback(conn, record)
    row = conn.execute(
        text("SELECT source, reviewer_id, agreement, exact_match FROM feedback "
             "WHERE request_id = 'a1'")
    ).one()
    assert row.source == "reviewer"
    assert row.reviewer_id == "rock"
    assert row.exact_match is True
    assert row.agreement == {label: True for label in LABELS}


def test_a_user_record_round_trips_with_an_empty_agreement_object(conn):
    _predict_row(conn, "a2")
    insert_feedback(conn, user_feedback("a2", "disagree"))
    row = conn.execute(
        text("SELECT source, reviewer_id, agreement, exact_match FROM feedback "
             "WHERE request_id = 'a2'")
    ).one()
    assert row.source == "user"
    assert row.reviewer_id is None
    assert row.agreement == {}
    assert row.exact_match is False


def test_an_explicit_timestamp_is_honoured_so_the_seeder_can_back_date(conn):
    _predict_row(conn, "a3")
    insert_feedback(conn, user_feedback("a3", "agree"), ts=NOW)
    assert conn.execute(
        text("SELECT ts FROM feedback WHERE request_id = 'a3'")
    ).scalar_one() == NOW


def test_an_omitted_timestamp_defaults_to_now(conn):
    """The COALESCE branch the seeder does not take. An untyped NULL parameter here is the
    failure this test exists to catch."""
    _predict_row(conn, "a4")
    insert_feedback(conn, user_feedback("a4", "agree"))
    stored = conn.execute(text("SELECT ts FROM feedback WHERE request_id = 'a4'")).scalar_one()
    assert stored is not None
    assert abs((stored - dt.datetime.now(dt.UTC)).total_seconds()) < 300


def test_the_database_refuses_a_reviewer_row_with_no_agreement_vector(conn):
    """`derive_feedback` cannot build one, but a hand-assembled record can. Both halves of
    the control are asserted, so removing either is red."""
    _predict_row(conn, "a5")
    hand_made = FeedbackRecord(
        request_id="a5", source="reviewer", reviewer_id="rock", agreement={}, exact_match=True
    )
    with pytest.raises(IntegrityError, match="feedback_reviewer_agreement_ck"):
        insert_feedback(conn, hand_made)


def test_the_database_refuses_a_third_source(conn):
    _predict_row(conn, "a6")
    hand_made = FeedbackRecord(
        request_id="a6", source="bot", reviewer_id=None, agreement={}, exact_match=True
    )
    with pytest.raises(IntegrityError, match="ck_feedback_source"):
        insert_feedback(conn, hand_made)


def test_a_second_user_row_for_one_request_is_refused_by_the_partial_index(conn):
    _predict_row(conn, "a7")
    insert_feedback(conn, user_feedback("a7", "agree"))
    with pytest.raises(IntegrityError, match="feedback_one_user_row"):
        insert_feedback(conn, user_feedback("a7", "disagree"))
