"""Idempotent DDL for the columns Phase 3 needs on Phase 2's tables.

Every statement is safe to re-run, so a partially-applied migration is not a broken
database and `apply_phase3_schema` can sit unconditionally in the application's startup
path next to `init_db`.

Phase 2 already ships most of the sampling design -- `review_queue.source`,
`review_queue.sample_rate`, `input_text_snapshot`, the `feedback` table and their CHECK
constraints. What is genuinely new here is `predictions.is_seed` (so the dashboard can tell
replayed demo traffic from live traffic), `predictions.submitter_fp` (the per-source
rate-limit key the schema did not have; `predictions.client_fp` is a digest of the API key
and is therefore identical for every request the shared frontend proxies), the composite
index the per-source quota counts against, and `feedback_one_user_row`.

The CHECK constraints restated below are load-bearing, not decoration.
`review_queue_sample_rate_ck` is what makes the Horvitz-Thompson estimator in
`monitoring/stats.py` sound: a row in a design stratum cannot exist without its inclusion
probability, so the estimator can never silently degrade to the unweighted pool the
premortem (H8) found biased. They are restated under Phase 2's own constraint names, so on
a Phase 2 database the `duplicate_object` handler fires and nothing is added -- restating
them under a fresh name would leave two constraints enforcing one rule, and a later DROP of
either would silently change nothing.
"""

from sqlalchemy import Engine

_STATEMENTS: tuple[str, ...] = (
    # --- predictions: seed provenance and the rate-limit key ---
    "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS is_seed BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS submitter_fp CHAR(16)",
    # `admit_review` counts one fingerprint's rows inside a time window on every enqueue,
    # on the hot path of a public endpoint. Phase 2 already indexes ts alone.
    "CREATE INDEX IF NOT EXISTS predictions_fp_ts_idx ON predictions (submitter_fp, ts)",
    # --- review_queue: the sampling design ---
    "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'flagged'",
    "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS sample_rate DOUBLE PRECISION",
    "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS input_text_snapshot TEXT",
    "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS reviewed_ts TIMESTAMPTZ",
    """
    DO $$ BEGIN
      ALTER TABLE review_queue ADD CONSTRAINT ck_review_source
        CHECK (source IN ('flagged', 'random-audit', 'user-report'));
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    """
    DO $$ BEGIN
      ALTER TABLE review_queue ADD CONSTRAINT review_queue_sample_rate_ck
        CHECK (
          (source IN ('flagged', 'random-audit')
             AND sample_rate IS NOT NULL AND sample_rate > 0 AND sample_rate <= 1)
          OR (source = 'user-report' AND sample_rate IS NULL)
        );
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    # --- feedback: created here if it is absent, then pinned column by column ---
    """
    CREATE TABLE IF NOT EXISTS feedback (
      id BIGSERIAL PRIMARY KEY,
      request_id VARCHAR(36) NOT NULL REFERENCES predictions (request_id),
      ts TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'reviewer'",
    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS reviewer_id TEXT",
    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS agreement JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS exact_match BOOLEAN NOT NULL DEFAULT FALSE",
    """
    DO $$ BEGIN
      ALTER TABLE feedback ADD CONSTRAINT ck_feedback_source
        CHECK (source IN ('user', 'reviewer'));
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    """
    DO $$ BEGIN
      ALTER TABLE feedback ADD CONSTRAINT feedback_reviewer_agreement_ck
        CHECK (source <> 'reviewer' OR agreement <> '{}'::jsonb);
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    # One anonymous verdict per request. Partial, so a second reviewer's row is unaffected.
    "CREATE UNIQUE INDEX IF NOT EXISTS feedback_one_user_row "
    "ON feedback (request_id) WHERE source = 'user'",
)


def apply_phase3_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        for statement in _STATEMENTS:
            conn.exec_driver_sql(statement)
