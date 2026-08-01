"""`make seed-demo`, end to end, measured against the panels it exists to feed.

The seeder is only useful if the three graded aggregations come out non-degenerate, so this
runs the whole replay against a real Postgres and then asserts on shapes **and** values.
The stand-in backend below is deliberately imperfect: a simulated model that agrees with the
reviewer on every single row would produce a live accuracy of exactly 1.0, whose Wilson
interval is [x, 1.0] -- a degenerate interval that hides the very failure C5 is about, and a
number no grader should believe.
"""

import datetime as dt
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from model.labels import LABELS
from monitoring.baseline import Baseline, load_thresholds
from monitoring.queries import (
    drift_report,
    flag_rate_series,
    latency_over_time,
    live_accuracy,
    seeded_share,
    user_feedback_panel,
)
from scripts.seed_demo import (
    MIN_BUCKETS,
    MIN_REVIEWED,
    SeedConfig,
    check_exit_criteria,
    load_seed_rows,
    replay,
)

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)
HELDOUT = Path("tests/fixtures/mini_jigsaw.csv")
THRESHOLDS = load_thresholds(Path("tests/fixtures/thresholds.json"))
BASELINE = Baseline(
    schema_version=1, data_version="d", model_version="toxic-clf:v3", n=1000,
    flag_rates={"toxic": 0.10, "severe_toxic": 0.01, "obscene": 0.05,
                "threat": 0.003, "insult": 0.05, "identity_hate": 0.009},
)

# The simulated model's two failure modes, as a fraction of requests. Both are deterministic
# functions of the request counter, so the seeded dataset is reproducible.
MISS_EVERY = 13     # the model allows a genuinely toxic comment: the false negative that the
#                     random-audit stratum exists to catch, and the flagged set never sees
FLIP_EVERY = 11     # the model gets `toxic` backwards: a false positive, or one wrong label


def _fake_backend(conn, rows):
    """Stands in for Phase 2's /predict: scores, persists, and enqueues on review.

    Probabilities are a deterministic function of the known labels, so the seeded corpus
    produces a realistic mix of agreements and disagreements in BOTH strata. Every NOT NULL
    column Phase 2 declares is supplied; omitting `input_chars`, `status` or `persist_status`
    aborts the transaction and the replay then measures an empty table.
    """
    lookup = {row.text: row for row in rows}
    state = {"i": 0}

    def predict(comment: str) -> dict:
        state["i"] += 1
        i = state["i"]
        request_id = str(uuid.uuid4())
        row = lookup[comment]
        missed = i % MISS_EVERY == 0
        flipped = i % FLIP_EVERY == 0
        probs = {}
        for offset, label in enumerate(LABELS):
            truth = row.labels[label]
            if missed:
                truth = 0
            elif flipped and label == "toxic":
                truth = 1 - truth
            jitter = ((i * 7 + offset * 13) % 20) / 100.0
            probs[label] = min(0.98, 0.55 + jitter) if truth else max(0.01, 0.12 - jitter / 4)
        flags = {label: probs[label] >= THRESHOLDS[label] for label in LABELS}
        decision = "review" if any(flags.values()) else "allow"
        cols = ", ".join(f"prob_{label}" for label in LABELS)
        binds = ", ".join(f":p_{label}" for label in LABELS)
        conn.execute(
            text(
                f"INSERT INTO predictions (request_id, ts, input_text, input_chars, "
                f"model_version, {cols}, decision, max_prob, latency_ms, status, "
                f"persist_status) VALUES (:rid, :ts, :txt, :chars, 'toxic-clf:v3', "
                f"{binds}, :dec, :mx, :lat, 'ok', 'direct')"
            ),
            {
                "rid": request_id, "ts": NOW, "txt": comment, "chars": len(comment),
                "dec": decision, "mx": max(probs.values()), "lat": 15 + (i % 60),
                **{f"p_{label}": probs[label] for label in LABELS},
            },
        )
        if decision == "review":
            conn.execute(
                text(
                    "INSERT INTO review_queue (request_id, enqueued_ts, status, source, "
                    "sample_rate, input_text_snapshot) VALUES (:rid, :ts, 'pending', "
                    "'flagged', 1.0, :snap)"
                ),
                {"rid": request_id, "ts": NOW, "snap": comment},
            )
        return {
            "request_id": request_id,
            "model_version": "toxic-clf:v3",
            "labels": {label: {"prob": probs[label], "flag": flags[label]} for label in LABELS},
            "decision": decision,
            "max_prob": max(probs.values()),
            "latency_ms": 15 + (i % 60),
        }

    return predict


@pytest.fixture(scope="module")
def seeded(engine):
    """One replay for the whole module: 2000 predictions is a few thousand round trips, and
    seven copies of it buys no coverage. The connection is opened here and truncated once, so
    nothing this module asserts depends on another module's rows."""
    with engine.connect() as conn:
        conn.execute(text("TRUNCATE TABLE feedback, review_queue, predictions CASCADE"))
        conn.commit()
        rows = load_seed_rows(HELDOUT, n=32, seed=42)
        # 2000 rows out of a 32-row fixture: repeat with distinct request ids, which is
        # exactly what the real corpus does at volume without needing the real corpus in CI.
        corpus = [rows[i % len(rows)] for i in range(2000)]
        report = replay(conn, corpus, _fake_backend(conn, rows), SeedConfig(), NOW)
        yield report, conn
        conn.rollback()


@pytest.fixture()
def report(seeded):
    return seeded[0]


@pytest.fixture()
def seeded_conn(seeded):
    return seeded[1]


def test_seed_demo_meets_every_exit_criterion(report):
    assert check_exit_criteria(report) == [], report
    assert report.buckets >= MIN_BUCKETS
    assert report.reviewed >= MIN_REVIEWED
    assert report.audited > 0
    assert report.labels_with_flags == len(LABELS)


def test_latency_chart_is_not_a_scatter_across_four_minutes(report, seeded_conn):
    buckets = latency_over_time(seeded_conn, since=NOW - dt.timedelta(days=20))
    assert len(buckets) >= MIN_BUCKETS
    assert all(bucket.n >= 50 for bucket in buckets)
    assert all(bucket.p95 >= bucket.p50 for bucket in buckets)


def test_drift_chart_has_a_reference_and_more_than_one_bucket(report, seeded_conn):
    rows = drift_report(seeded_conn, since=NOW - dt.timedelta(days=20),
                        thresholds=THRESHOLDS, baseline=BASELINE)
    assert len(rows) == len(LABELS)
    assert any(row.production_rate > 0 for row in rows)
    assert all(row.baseline_rate > 0 for row in rows)
    series = flag_rate_series(seeded_conn, since=NOW - dt.timedelta(days=20),
                              thresholds=THRESHOLDS)
    assert len(series) >= MIN_BUCKETS


def test_every_label_appears_in_the_drift_panel_with_a_production_rate(report, seeded_conn):
    """A label the seeded corpus never flags is an empty column in the graded screenshot,
    which is why `check_exit_criteria` counts them. This is the same claim, read back out of
    the panel rather than out of the seeder's own report."""
    rows = drift_report(seeded_conn, since=NOW - dt.timedelta(days=20),
                        thresholds=THRESHOLDS, baseline=BASELINE)
    assert all(row.production_rate > 0 for row in rows), [
        (row.label, row.production_rate) for row in rows
    ]


def test_live_accuracy_is_a_real_number_over_both_strata(report, seeded_conn):
    accuracy = live_accuracy(seeded_conn, since=NOW - dt.timedelta(days=20))
    assert accuracy.n >= MIN_REVIEWED
    assert accuracy.point is not None
    assert 0.0 <= accuracy.point <= 1.0
    assert accuracy.lo < accuracy.point < accuracy.hi
    strata = {stratum.stratum for stratum in accuracy.strata}
    assert strata == {"flagged", "random-audit"}
    for stratum in accuracy.strata:
        assert stratum.n > 0


def test_the_audit_stratum_carries_the_rate_it_was_drawn_at(report, seeded_conn):
    """H8, read back out of the database: the estimator weights by what was recorded at
    enqueue time, so the audit rows must carry the rate the seeder drew them at, not 1.0."""
    accuracy = live_accuracy(seeded_conn, since=NOW - dt.timedelta(days=20))
    rates = {stratum.stratum: stratum.sample_rate for stratum in accuracy.strata}
    assert rates["flagged"] == pytest.approx(1.0)
    assert rates["random-audit"] == pytest.approx(SeedConfig().audit_rate)
    # The design weight is doing work: the pooled mean is a different number.
    pooled = sum(s.correct for s in accuracy.strata) / sum(s.n for s in accuracy.strata)
    assert accuracy.point != pytest.approx(pooled, abs=1e-6)


def test_the_audit_stratum_catches_something_the_flagged_set_cannot(report, seeded_conn):
    """The point of auditing allowed traffic is confidently-allowed false negatives. If the
    audited rows all agreed, the stratum would be decoration and the estimate would be no
    better informed than the flagged set alone."""
    disagreements = seeded_conn.execute(
        text(
            "SELECT count(*) FROM feedback f JOIN review_queue q "
            "ON q.request_id = f.request_id "
            "WHERE f.source = 'reviewer' AND q.source = 'random-audit' AND NOT f.exact_match"
        )
    ).scalar_one()
    assert disagreements > 0


def test_user_panel_is_populated_and_separate(report, seeded_conn):
    panel = user_feedback_panel(seeded_conn, since=NOW - dt.timedelta(days=20))
    assert panel.n > 0
    assert panel.rate is not None
    assert panel.n < report.reviewed, "user verdicts are not reviewer labels"


def test_every_seeded_row_is_marked_as_seeded(report, seeded_conn):
    total, marked = seeded_share(seeded_conn, since=NOW - dt.timedelta(days=20))
    assert total == marked == report.predictions
    reviewers = seeded_conn.execute(
        text("SELECT DISTINCT reviewer_id FROM review_queue WHERE reviewer_id IS NOT NULL")
    ).scalars().all()
    assert reviewers == ["seed-replay"]


def test_replay_is_deterministic_in_its_timestamps(report, seeded_conn):
    days = seeded_conn.execute(
        text("SELECT count(DISTINCT date_trunc('day', ts, 'UTC')) FROM predictions")
    ).scalar_one()
    assert days == SeedConfig().days


def test_no_seeded_prediction_lands_in_the_future(report, seeded_conn):
    """The replay back-dates. A row stamped after `now` would sit outside every dashboard
    window and silently shrink the series the exit criteria just certified."""
    latest = seeded_conn.execute(text("SELECT max(ts) FROM predictions")).scalar_one()
    assert latest <= NOW
