"""Bounded durable spool for prediction rows that could not reach Postgres.

Premortem H30. Delivery spec section 10 returned 503 whenever a prediction could not be
persisted, and accepted that the moderation endpoint is unavailable while the database is.
On a db.t4g.micro with no rate limit that is an off switch an attacker operates: modest
concurrent traffic exhausts connections and moderation is down for as long as the pressure
lasts. Rubric 2.2 requires complete logging, which is a durability requirement, not an
availability trade.

So the failure path is durable and bounded. Rows land in an fsync'd append-only file on the
instance volume and the drainer replays them with persist_status='spooled'. The bound moved
from database connections, which the attacker controls, to local disk, which the operator
controls; and reaching the bound costs SPOOL_MAX_ROWS successful requests through the rate
limiter rather than a handful of concurrent connections.

Durability caveat, stated rather than assumed: fsync on the file guarantees the row survives
a process crash and an instance reboot. It does not survive destruction of the EBS volume,
which is what `terraform destroy` does. A drain therefore runs before teardown.
"""

import datetime as dt
import json
import os
from dataclasses import asdict
from pathlib import Path

from backend.db import PendingWrite, PredictionRow, ReviewIntent


class SpoolFull(RuntimeError):
    """The spool reached its bound. The caller fails closed."""


def _encode(pending: PendingWrite) -> str:
    prediction = asdict(pending.prediction)
    prediction["ts"] = (pending.prediction.ts or dt.datetime.now(dt.UTC)).isoformat()
    review = None
    if pending.review is not None:
        review = asdict(pending.review)
        review["enqueued_ts"] = (
            pending.review.enqueued_ts.isoformat()
            if pending.review.enqueued_ts is not None
            else None
        )
    return json.dumps({"prediction": prediction, "review": review}, sort_keys=True)


def _decode(line: str) -> PendingWrite:
    payload = json.loads(line)
    prediction = payload["prediction"]
    prediction["ts"] = dt.datetime.fromisoformat(prediction["ts"])
    review = payload.get("review")
    if review is not None:
        raw = review.get("enqueued_ts")
        review["enqueued_ts"] = dt.datetime.fromisoformat(raw) if raw else None
        review = ReviewIntent(**review)
    return PendingWrite(prediction=PredictionRow(**prediction), review=review)


class Spool:
    def __init__(self, path: Path, max_rows: int) -> None:
        self.path = Path(path)
        self.max_rows = int(max_rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._count = sum(1 for line in self._lines())

    def _lines(self) -> list[str]:
        with self.path.open("r", encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if line.strip()]

    def depth(self) -> int:
        return self._count

    def append(self, pending: PendingWrite) -> None:
        if self._count >= self.max_rows:
            raise SpoolFull(f"spool already holds {self.max_rows} rows; refusing more")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_encode(pending) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._count += 1

    def read_all(self) -> list[PendingWrite]:
        restored: list[PendingWrite] = []
        for line in self._lines():
            try:
                restored.append(_decode(line))
            except (ValueError, TypeError, KeyError):
                # A partially written tail line after a hard kill. Losing one row is
                # preferable to refusing to drain the rest.
                continue
        return restored

    def truncate(self) -> None:
        self.path.write_text("", encoding="utf-8")
        self._count = 0
