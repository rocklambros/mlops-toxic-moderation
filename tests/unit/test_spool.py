import datetime as dt

import pytest

from backend.db import PendingWrite, PredictionRow, ReviewIntent
from backend.spool import Spool, SpoolFull
from model.labels import LABELS


def pending(request_id="r1") -> PendingWrite:
    return PendingWrite(
        prediction=PredictionRow(
            request_id=request_id,
            input_text="you are an idiot",
            input_chars=16,
            model_version="toxic-clf:v3@sha256:" + "a" * 64,
            probs={label: 0.25 for label in LABELS},
            decision="review",
            max_prob=0.25,
            latency_ms=31,
            status="ok",
            persist_status="spooled",
            ts=dt.datetime(2026, 8, 4, 9, 30, tzinfo=dt.UTC),
        ),
        review=ReviewIntent(
            request_id=request_id,
            source="flagged",
            sample_rate=1.0,
            input_text_snapshot="you are an idiot",
        ),
    )


def test_append_then_read_round_trips_every_field(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    spool.append(pending())
    restored = spool.read_all()
    assert len(restored) == 1
    assert restored[0] == pending()


def test_depth_tracks_appends_and_survives_a_restart(tmp_path):
    path = tmp_path / "s.jsonl"
    spool = Spool(path, max_rows=10)
    spool.append(pending("r1"))
    spool.append(pending("r2"))
    assert spool.depth() == 2
    assert Spool(path, max_rows=10).depth() == 2


def test_spool_refuses_to_grow_past_its_bound(tmp_path):
    """H30. The bound is what keeps the degraded path from becoming an unbounded disk write
    primitive, and it is deliberately large enough that reaching it costs an attacker
    SPOOL_MAX_ROWS successful requests through the rate limiter."""
    spool = Spool(tmp_path / "s.jsonl", max_rows=2)
    spool.append(pending("r1"))
    spool.append(pending("r2"))
    with pytest.raises(SpoolFull, match="2 rows"):
        spool.append(pending("r3"))
    assert spool.depth() == 2


def test_truncate_empties_the_spool(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    spool.append(pending())
    spool.truncate()
    assert spool.depth() == 0
    assert spool.read_all() == []


def test_a_row_without_a_review_round_trips(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    entry = PendingWrite(prediction=pending().prediction, review=None)
    spool.append(entry)
    assert spool.read_all() == [entry]


def test_a_corrupt_line_does_not_lose_the_rest(tmp_path):
    path = tmp_path / "s.jsonl"
    spool = Spool(path, max_rows=10)
    spool.append(pending("r1"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{ this is not json\n")
    spool.append(pending("r2"))
    restored = spool.read_all()
    assert [entry.prediction.request_id for entry in restored] == ["r1", "r2"]


def test_the_directory_is_created_on_demand(tmp_path):
    spool = Spool(tmp_path / "nested" / "dir" / "s.jsonl", max_rows=10)
    spool.append(pending())
    assert spool.depth() == 1
