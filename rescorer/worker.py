"""Async DistilBERT re-scorer. EC2 #3, CPU, no ingress.

Reads `input_text_snapshot` rather than `predictions.input_text`, because the 30-day
retention purge nulls the latter and re-scoring must not depend on data that is designed to
disappear.

Idempotent by construction: rows are claimed with FOR UPDATE SKIP LOCKED and the update is
guarded on `status = 'pending'`, so a second pass, a crashed pass, or a second worker
cannot double-advance a row.

The array-to-dict conversion goes through `model.contract.probs_to_dict` and nowhere else.
A fourth independent `zip(LABELS, row)` here would mislabel every probability the day the
column order drifts, and the response validator that would notice is order-blind by
construction (premortem H23).

Which ONNX file this loads is configuration: `CHALLENGER_MODEL_FILE` selects it and
`CHALLENGER_SHA256` pins it. As of Phase 1's export the valid artifact is the float32
`model.onnx`; the int8 `model_quantized.onnx` failed the load-time parity gate at
max |logit delta| 2.7206 (worst label `identity_hate`) against a 0.25 tolerance, so
promoting it is a re-export plus two environment variables, not a code change.
"""

import json
import os
import time

from sqlalchemy import create_engine, text

from model.contract import probs_to_dict

BATCH_SIZE = int(os.environ.get("RESCORER_BATCH_SIZE", "16"))
IDLE_SLEEP_SECONDS = float(os.environ.get("RESCORER_IDLE_SLEEP", "5"))
MAX_SLEEP_SECONDS = float(os.environ.get("RESCORER_MAX_SLEEP", "120"))
DEFAULT_MODEL_FILE = "model.onnx"


def drain_once(conn, challenger, batch_size: int = BATCH_SIZE) -> int:
    rows = conn.execute(
        text(
            "SELECT request_id, input_text_snapshot FROM review_queue "
            "WHERE status = 'pending' AND distilbert_probs IS NULL "
            "ORDER BY enqueued_ts LIMIT :n FOR UPDATE SKIP LOCKED"
        ),
        {"n": batch_size},
    ).all()
    if not rows:
        return 0

    probabilities = challenger.predict_proba([row.input_text_snapshot or "" for row in rows])
    advanced = 0
    for row, values in zip(rows, probabilities, strict=True):
        # The `status = 'pending'` guard IS the idempotency control, and the count comes from
        # the UPDATE's own rowcount rather than from len(rows): a row that stopped being
        # pending between the claim and the write must not be reported as re-scored.
        advanced += conn.execute(
            text(
                "UPDATE review_queue SET distilbert_probs = CAST(:probs AS jsonb), "
                "status = 'rescored' WHERE request_id = :rid AND status = 'pending'"
            ),
            {"probs": json.dumps(probs_to_dict(values)), "rid": row.request_id},
        ).rowcount
    conn.commit()
    return advanced


def run_forever() -> None:  # pragma: no cover - exercised by the container smoke test
    from pathlib import Path

    from rescorer.challenger import load_challenger

    challenger = load_challenger(
        Path(os.environ["CHALLENGER_DIR"]),
        os.environ["CHALLENGER_SHA256"],
        model_filename=os.environ.get("CHALLENGER_MODEL_FILE", DEFAULT_MODEL_FILE),
    )
    engine = create_engine(os.environ["DATABASE_URL"], future=True, pool_pre_ping=True)
    sleep_for = IDLE_SLEEP_SECONDS
    while True:
        with engine.connect() as conn:
            processed = drain_once(conn, challenger)
        if processed:
            sleep_for = IDLE_SLEEP_SECONDS
        else:
            sleep_for = min(sleep_for * 2, MAX_SLEEP_SECONDS)
        time.sleep(sleep_for)


if __name__ == "__main__":  # pragma: no cover
    run_forever()
