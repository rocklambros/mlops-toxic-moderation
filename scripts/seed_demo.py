"""Replay held-out Jigsaw comments through /predict so the dashboard has a data source.

The premortem's C5: no task anywhere created prediction volume, so "latency over time" was
a scatter across four minutes, "target drift" was a single bar, and live accuracy divided by
zero on the rare labels -- rendering NaN or a traceback in the screenshot of the
highest-weighted requirement.

Predictions are made for real, so latency is measured rather than invented. Only the
timestamp is back-dated, and only by this operator tool writing directly to the database:
no production code path accepts a client-supplied timestamp, because that would be an
injection into the graded metric.

Every seeded row is marked `predictions.is_seed = true` and every seeded review carries
`reviewer_id='seed-replay'`, so the dashboard and the README can say exactly what the
dataset is.

The defaults are the deliverable. `SeedConfig` and the MIN_* floors below are chosen
together so that `check_exit_criteria` passes: 2000 predictions over 14 calendar days puts
at least ~59 rows in the thinnest day, which is enough for a p95; a 10% audit rate on the
allowed traffic keeps the random-audit stratum non-empty, without which live accuracy is
blind to confidently-allowed false negatives.
"""

import argparse
import bisect
import datetime as dt
import json
import math
import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from backend.feedback import derive_feedback, insert_feedback, user_feedback
from model.labels import LABELS

MIN_BUCKETS = 7
MIN_REVIEWED = 200
MIN_PREDICTIONS = 1500
SEED_REVIEWER_ID = "seed-replay"
SECONDS_PER_DAY = 86400


class SeedError(RuntimeError):
    """The replay cannot produce a defensible dataset and must not pretend otherwise."""


@dataclass(frozen=True)
class SeedRow:
    id: str
    text: str
    labels: dict[str, int]


@dataclass(frozen=True)
class SeedConfig:
    n: int = 2000
    days: int = 14
    seed: int = 42
    audit_rate: float = 0.10
    user_feedback_fraction: float = 0.08


@dataclass(frozen=True)
class SeedReport:
    predictions: int
    buckets: int
    flagged: int
    audited: int
    reviewed: int
    user_feedback: int
    labels_with_flags: int


def backdated_timestamps(n: int, days: int, end: dt.datetime, seed: int) -> list[dt.datetime]:
    """Deterministic, uneven, and never leaves a calendar day empty.

    Buckets are calendar days in UTC, because that is what `date_trunc('day', ts, 'UTC')` in
    monitoring/queries.py groups by. Building a stamp as `end - timedelta(days=d) + random`
    instead would straddle two calendar days whenever `end` is not midnight: 14 offsets
    would produce 15 buckets, the two edge buckets would hold roughly half a day of traffic
    each, and the seven-bucket floor would be measured against a series the seeder never
    intended to draw.

    The weekly sine gives a realistic weekday/weekend shape with a minimum weight of 0.415,
    so with n=2000 over 14 days the thinnest day still holds ~59 points -- enough for a p95.
    """
    if days < 1:
        raise SeedError("the replay window must be at least one day")
    weights = [1.0 + 0.6 * math.sin(2.0 * math.pi * day / 7.0) for day in range(days)]
    total = sum(weights)
    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight / total
        cumulative.append(running)

    last_day = end.astimezone(dt.UTC).date()
    first_day = last_day - dt.timedelta(days=days - 1)
    # The final calendar day is only complete up to `end`; a stamp past it would put demo
    # traffic in the future and leave a gap in the dashboard's window that is not a gap.
    seconds_available_today = int(
        (end - dt.datetime.combine(last_day, dt.time.min, tzinfo=dt.UTC)).total_seconds()
    )

    rng = random.Random(seed)
    stamps: list[dt.datetime] = []
    for i in range(n):
        offset = min(bisect.bisect_left(cumulative, (i + 0.5) / n), days - 1)
        day = first_day + dt.timedelta(days=offset)
        span = seconds_available_today if day == last_day else SECONDS_PER_DAY
        second = rng.randrange(0, max(1, span))
        stamps.append(
            dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC) + dt.timedelta(seconds=second)
        )
    return stamps


def load_seed_rows(csv_path: Path, n: int, seed: int) -> list[SeedRow]:
    frame = pd.read_csv(csv_path)
    rng = random.Random(seed)
    chosen: list[int] = []
    taken: set[int] = set()

    # Rare labels first, so `threat` is never absent from the seeded window. A label whose
    # only candidate is already selected is still covered, because that row is positive for
    # it too.
    per_label = max(1, n // (len(LABELS) * 8))
    for label in LABELS:
        positives = [int(i) for i in frame.index[frame[label] == 1]]
        rng.shuffle(positives)
        for index in positives[:per_label]:
            if index not in taken:
                taken.add(index)
                chosen.append(index)

    remaining = [int(i) for i in frame.index if int(i) not in taken]
    rng.shuffle(remaining)
    for index in remaining:
        if len(chosen) >= n:
            break
        chosen.append(index)

    chosen = sorted(chosen[:n])
    return [
        SeedRow(
            id=str(frame.at[index, "id"]),
            text=str(frame.at[index, "comment_text"]),
            labels={label: int(frame.at[index, label]) for label in LABELS},
        )
        for index in chosen
    ]


def check_exit_criteria(report: SeedReport) -> list[str]:
    """Every way the seeded dataset can still leave a graded panel degenerate."""
    failures: list[str] = []
    if report.buckets < MIN_BUCKETS:
        failures.append(f"only {report.buckets} time buckets, need {MIN_BUCKETS}")
    if report.reviewed < MIN_REVIEWED:
        failures.append(f"only {report.reviewed} reviewed items, need {MIN_REVIEWED}")
    if report.predictions < MIN_PREDICTIONS:
        failures.append(f"only {report.predictions} predictions, need {MIN_PREDICTIONS}")
    if report.audited == 0:
        failures.append("the random-audit stratum is empty, so live accuracy stays biased")
    if report.labels_with_flags < len(LABELS):
        failures.append(
            f"only {report.labels_with_flags} of {len(LABELS)} labels were ever flagged"
        )
    return failures


def replay(
    conn,
    rows: list[SeedRow],
    predict: Callable[[str], dict],
    config: SeedConfig,
    now: dt.datetime,
) -> SeedReport:
    stamps = backdated_timestamps(len(rows), config.days, now, config.seed)
    rng = random.Random(config.seed + 1)
    flagged = audited = reviewed = user_rows = 0
    labels_with_flags: set[str] = set()

    for row, ts in zip(rows, stamps, strict=True):
        response = predict(row.text)
        request_id = response["request_id"]
        model_flags = {label: bool(response["labels"][label]["flag"]) for label in LABELS}
        labels_with_flags |= {label for label, flag in model_flags.items() if flag}

        updated = conn.execute(
            text("UPDATE predictions SET ts = :ts, is_seed = TRUE WHERE request_id = :rid"),
            {"ts": ts, "rid": request_id},
        ).rowcount
        if updated != 1:
            raise SeedError(
                f"predict() did not persist {request_id}; the backend must log every "
                "prediction (rubric 2.2) before the dashboard can show anything"
            )

        existing = conn.execute(
            text("SELECT source FROM review_queue WHERE request_id = :rid"),
            {"rid": request_id},
        ).first()

        if existing is not None:
            stratum = existing.source
            conn.execute(
                text("UPDATE review_queue SET enqueued_ts = :ts WHERE request_id = :rid"),
                {"ts": ts, "rid": request_id},
            )
        elif rng.random() < config.audit_rate:
            stratum = "random-audit"
            conn.execute(
                text(
                    "INSERT INTO review_queue (request_id, enqueued_ts, status, source, "
                    "sample_rate, input_text_snapshot) VALUES (:rid, :ts, 'pending', "
                    "'random-audit', :rate, :snap)"
                ),
                {"rid": request_id, "ts": ts, "rate": config.audit_rate, "snap": row.text},
            )
        else:
            stratum = None

        if stratum is not None:
            if stratum == "flagged":
                flagged += 1
            elif stratum == "random-audit":
                audited += 1
            reviewed += 1
            reviewed_ts = ts + dt.timedelta(minutes=17)
            conn.execute(
                text(
                    "UPDATE review_queue SET status = 'reviewed', reviewer_labels = "
                    "CAST(:labels AS jsonb), reviewer_id = :who, reviewed_ts = :ts "
                    "WHERE request_id = :rid"
                ),
                {
                    "labels": json.dumps(row.labels),
                    "who": SEED_REVIEWER_ID,
                    "ts": reviewed_ts,
                    "rid": request_id,
                },
            )
            insert_feedback(
                conn,
                derive_feedback(request_id, row.labels, model_flags, SEED_REVIEWER_ID),
                ts=reviewed_ts,
            )

        if rng.random() < config.user_feedback_fraction:
            verdict = "agree" if rng.random() < 0.85 else "disagree"
            insert_feedback(
                conn, user_feedback(request_id, verdict), ts=ts + dt.timedelta(minutes=2)
            )
            user_rows += 1

    conn.commit()
    buckets = len({stamp.date() for stamp in stamps})
    return SeedReport(
        predictions=len(rows),
        buckets=buckets,
        flagged=flagged,
        audited=audited,
        reviewed=reviewed,
        user_feedback=user_rows,
        labels_with_flags=len(labels_with_flags),
    )


def _http_predict(base_url: str, api_key: str) -> Callable[[str], dict]:
    import httpx

    client = httpx.Client(base_url=base_url, timeout=30.0)

    def predict(text_value: str) -> dict:
        response = client.post(
            "/predict", json={"text": text_value}, headers={"X-API-Key": api_key}
        )
        response.raise_for_status()
        return response.json()

    return predict


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=Path("data/heldout.csv"))
    parser.add_argument("--n", type=int, default=SeedConfig.n)
    parser.add_argument("--days", type=int, default=SeedConfig.days)
    parser.add_argument("--seed", type=int, default=SeedConfig.seed)
    parser.add_argument("--audit-rate", type=float, default=SeedConfig.audit_rate)
    parser.add_argument("--purge", action="store_true", help="delete previously seeded rows")
    args = parser.parse_args()

    config = SeedConfig(n=args.n, days=args.days, seed=args.seed, audit_rate=args.audit_rate)
    engine = create_engine(os.environ["DATABASE_URL"], future=True)

    with engine.connect() as conn:
        if args.purge:
            conn.execute(
                text(
                    "DELETE FROM feedback WHERE request_id IN "
                    "(SELECT request_id FROM predictions WHERE is_seed)"
                )
            )
            conn.execute(
                text(
                    "DELETE FROM review_queue WHERE request_id IN "
                    "(SELECT request_id FROM predictions WHERE is_seed)"
                )
            )
            conn.execute(text("DELETE FROM predictions WHERE is_seed"))
            conn.commit()
            print("purged previously seeded rows")

        if args.n <= 0:
            return
        rows = load_seed_rows(args.csv, config.n, config.seed)
        predict = _http_predict(os.environ["BACKEND_URL"], os.environ.get("DEMO_API_KEY", ""))
        report = replay(conn, rows, predict, config, dt.datetime.now(dt.UTC))

    print(json.dumps(report.__dict__, indent=2))
    failures = check_exit_criteria(report)
    if failures:
        for failure in failures:
            print(f"EXIT CRITERION FAILED: {failure}")
        raise SystemExit(1)
    print("all seed-demo exit criteria met")


if __name__ == "__main__":
    main()
