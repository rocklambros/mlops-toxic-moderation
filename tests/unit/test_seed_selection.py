"""What `make seed-demo` selects and when it says the traffic happened.

The replay itself is exercised against a real database in
`tests/integration/test_seed_demo.py`. These are the pure parts: the corpus selection, the
back-dating, and the exit criteria that decide whether the resulting dataset is defensible
enough to screenshot.
"""

import datetime as dt
import re
from pathlib import Path

import pandas as pd

from model.labels import LABELS
from scripts.seed_demo import (
    MIN_BUCKETS,
    MIN_PREDICTIONS,
    MIN_REVIEWED,
    SeedConfig,
    SeedReport,
    backdated_timestamps,
    check_exit_criteria,
    load_seed_rows,
)

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")
END = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


def test_backdating_spreads_across_every_day_of_the_window():
    stamps = backdated_timestamps(2000, days=14, end=END, seed=42)
    assert len(stamps) == 2000
    days = sorted({stamp.date() for stamp in stamps})
    assert len(days) == 14
    assert min(stamps) >= END - dt.timedelta(days=14)
    assert max(stamps) <= END


def test_backdating_buckets_on_calendar_days_not_on_offsets_from_now():
    """A stamp built as `start + timedelta(days=d, seconds=rand)` straddles two calendar
    days whenever `start` is not midnight, so 14 offsets produce 15 date buckets and the two
    edge buckets hold half the points. `date_trunc('day', ts)` in monitoring/queries.py
    buckets by calendar day, so that is what the seeder must fill."""
    stamps = backdated_timestamps(2000, days=14, end=END, seed=42)
    days = sorted({stamp.date() for stamp in stamps})
    assert days[0] == dt.date(2026, 8, 2)
    assert days[-1] == END.date()
    assert days == [dt.date(2026, 8, 2) + dt.timedelta(days=i) for i in range(14)]


def test_every_day_bucket_has_enough_points_for_a_percentile():
    stamps = backdated_timestamps(2000, days=14, end=END, seed=42)
    counts = pd.Series([stamp.date() for stamp in stamps]).value_counts()
    assert counts.min() >= 50
    assert counts.max() != counts.min(), "a perfectly flat series looks synthetic"


def test_backdating_is_deterministic():
    assert backdated_timestamps(500, 14, END, 42) == backdated_timestamps(500, 14, END, 42)
    assert backdated_timestamps(500, 14, END, 42) != backdated_timestamps(500, 14, END, 7)


def test_small_windows_still_produce_seven_buckets():
    stamps = backdated_timestamps(120, days=7, end=END, seed=42)
    assert len({stamp.date() for stamp in stamps}) == 7


def test_no_backdated_stamp_lands_in_the_future():
    """The seeder writes `ts` directly. A stamp past `end` would put demo traffic in the
    future, and the dashboard's window would show a gap that is not a gap."""
    for days in (7, 14, 30):
        stamps = backdated_timestamps(300, days=days, end=END, seed=1)
        assert max(stamps) <= END
        assert min(stamps) >= END - dt.timedelta(days=days)


def test_selection_is_deterministic_and_covers_every_label():
    rows_a = load_seed_rows(FIXTURE, n=25, seed=42)
    rows_b = load_seed_rows(FIXTURE, n=25, seed=42)
    assert [row.id for row in rows_a] == [row.id for row in rows_b]
    for label in LABELS:
        assert any(row.labels[label] == 1 for row in rows_a), f"{label} has no positive"


def test_selection_carries_the_known_ground_truth():
    rows = load_seed_rows(FIXTURE, n=25, seed=42)
    assert all(set(row.labels) == set(LABELS) for row in rows)
    assert all(value in (0, 1) for row in rows for value in row.labels.values())
    assert all(row.text for row in rows)


def test_selection_never_returns_more_than_the_corpus():
    rows = load_seed_rows(FIXTURE, n=10_000, seed=42)
    assert len(rows) == len(pd.read_csv(FIXTURE))


def test_selection_returns_exactly_the_requested_size():
    """`n` is what the exit criteria are checked against. A selection that quietly returned
    fewer would fail MIN_PREDICTIONS with no explanation of why."""
    assert len(load_seed_rows(FIXTURE, n=25, seed=42)) == 25
    assert len({row.id for row in load_seed_rows(FIXTURE, n=25, seed=42)}) == 25


def test_a_rare_label_survives_a_selection_smaller_than_the_corpus():
    """`threat` is 13 rows in the fixture and would be absent from most uniform samples of
    8. Without the rare-label pass the drift panel shows an empty column for it."""
    rows = load_seed_rows(FIXTURE, n=8, seed=7)
    assert len(rows) == 8
    for label in LABELS:
        assert any(row.labels[label] == 1 for row in rows), label


def test_exit_criteria_reject_a_thin_dataset():
    thin = SeedReport(predictions=30, buckets=2, flagged=5, audited=0, reviewed=5,
                      user_feedback=1, labels_with_flags=2)
    failures = check_exit_criteria(thin)
    assert any(str(MIN_BUCKETS) in message for message in failures)
    assert any(str(MIN_REVIEWED) in message for message in failures)
    assert any(str(MIN_PREDICTIONS) in message for message in failures)
    assert any("audit" in message for message in failures)


def test_exit_criteria_reject_a_dataset_that_never_flagged_a_label():
    """A label with no flagged row anywhere is an empty column in the drift panel and a
    stratum the accuracy estimate never sees."""
    missing = SeedReport(predictions=2000, buckets=14, flagged=210, audited=180, reviewed=390,
                         user_feedback=160, labels_with_flags=len(LABELS) - 1)
    failures = check_exit_criteria(missing)
    assert len(failures) == 1
    assert str(len(LABELS)) in failures[0]


def test_exit_criteria_accept_a_populated_dataset():
    full = SeedReport(predictions=2000, buckets=14, flagged=210, audited=180, reviewed=390,
                      user_feedback=160, labels_with_flags=6)
    assert check_exit_criteria(full) == []


def test_the_seeder_and_the_dashboard_agree_on_the_daily_bucket_floor():
    """Seven daily buckets is defined twice: once as the seeder's exit criterion and once as
    the floor below which the dashboard's latency caption refuses to claim a trend. Raising
    one alone lets `make seed-demo` certify a dataset that the panel it exists to populate
    calls insufficient -- and both suites stay green while it happens."""
    from monitoring.dashboard import MIN_BUCKETS as DASHBOARD_MIN_BUCKETS

    assert MIN_BUCKETS == DASHBOARD_MIN_BUCKETS


def test_seed_config_defaults_match_the_exit_criteria():
    config = SeedConfig()
    assert config.n >= 2000
    assert config.n >= MIN_PREDICTIONS
    assert config.days >= MIN_BUCKETS
    assert 0.0 < config.audit_rate <= 1.0


def test_the_makefile_exposes_the_seeder_the_readme_and_the_rubric_name():
    """`make seed-demo` is named in the dashboard's own captions and in rubric 3.2's
    evidence. A target that does not exist makes both of those sentences false."""
    recipe = Path("Makefile").read_text(encoding="utf-8")
    assert re.search(r"^seed-demo:", recipe, re.M)
    assert "scripts.seed_demo" in recipe
    assert re.search(r"^heldout:", recipe, re.M)
    assert "scripts.export_heldout" in recipe


def test_no_served_surface_can_reach_the_seeder():
    """The seeder is the one thing in the repository that writes a chosen `ts`. No served
    process accepts a client-supplied timestamp, because that would be an injection into the
    graded latency series -- so no served process imports the module that can write one."""
    offenders = []
    for directory in ("backend", "frontend", "monitoring"):
        for path in sorted(Path(directory).rglob("*.py")):
            body = path.read_text(encoding="utf-8")
            if re.search(r"\b(from|import)\s+scripts\b", body):
                offenders.append(str(path))
    assert offenders == [], offenders
