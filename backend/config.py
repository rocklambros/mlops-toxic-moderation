"""Environment-driven settings for the serving backend.

Two rules this module enforces rather than documents. Secrets are `repr=False`, because a
Settings object reaches every traceback. And MAX_INPUT_CHARS has no Settings field and no
environment key, because an abuse control that a deploy-time variable can widen is not a
control (delivery spec section 6.3). It is re-exported from `model.normalize`, which owns
the single definition, so it is un-tunable in one place rather than two.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Single source of truth for the input cap; see Phase 0 Task 3. Re-exported here so serving
# callers have one import, but defined once, in model/normalize.py, alongside the normalizer
# that also enforces it. A cap with two definitions is a cap reported wrongly at least once.
from model.normalize import MAX_INPUT_CHARS

__all__ = ["MAX_INPUT_CHARS", "REQUIRED", "Settings", "load_settings"]

REQUIRED = (
    "DATABASE_URL",
    "DEMO_API_KEY",
    "MODEL_ARTIFACT_PATH",
    "MODEL_CARD_PATH",
    "MODEL_DIGEST",
    "MODEL_REGISTRY_VERSION",
    "THRESHOLDS_PATH",
)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    demo_api_key: str = field(repr=False)
    model_artifact_path: Path
    model_card_path: Path
    model_digest: str
    model_registry_version: int
    thresholds_path: Path
    artifact_name: str = "toxic-clf"
    max_body_bytes: int = 16384
    rate_limit_per_minute: int = 30
    rate_limit_burst: int = 10
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_timeout_seconds: float = 2.0
    spool_path: Path = Path("/var/lib/toxic/predictions.spool")
    spool_max_rows: int = 10000
    random_audit_rate: float = 0.05
    input_text_retention_days: int = 30
    pending_review_ttl_days: int = 7
    snapshot_retention_days: int = 30
    latency_budget_p95_ms: int = 500


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    import os

    source: Mapping[str, str] = os.environ if env is None else env
    missing = [name for name in REQUIRED if not source.get(name)]
    if missing:
        raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")

    def integer(name: str, default: int) -> int:
        try:
            return int(source.get(name, default))
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer") from exc

    def number(name: str, default: float) -> float:
        try:
            return float(source.get(name, default))
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a number") from exc

    rate = number("RANDOM_AUDIT_RATE", 0.05)
    if not 0.0 <= rate <= 1.0:
        raise RuntimeError("RANDOM_AUDIT_RATE must be between 0 and 1 inclusive")

    return Settings(
        database_url=source["DATABASE_URL"],
        demo_api_key=source["DEMO_API_KEY"],
        model_artifact_path=Path(source["MODEL_ARTIFACT_PATH"]),
        model_card_path=Path(source["MODEL_CARD_PATH"]),
        model_digest=source["MODEL_DIGEST"],
        model_registry_version=integer("MODEL_REGISTRY_VERSION", 1),
        thresholds_path=Path(source["THRESHOLDS_PATH"]),
        artifact_name=source.get("ARTIFACT_NAME", "toxic-clf"),
        max_body_bytes=integer("MAX_BODY_BYTES", 16384),
        rate_limit_per_minute=integer("RATE_LIMIT_PER_MINUTE", 30),
        rate_limit_burst=integer("RATE_LIMIT_BURST", 10),
        db_pool_size=integer("DB_POOL_SIZE", 5),
        db_max_overflow=integer("DB_MAX_OVERFLOW", 5),
        db_timeout_seconds=number("DB_TIMEOUT_SECONDS", 2.0),
        spool_path=Path(source.get("SPOOL_PATH", "/var/lib/toxic/predictions.spool")),
        spool_max_rows=integer("SPOOL_MAX_ROWS", 10000),
        random_audit_rate=rate,
        input_text_retention_days=integer("INPUT_TEXT_RETENTION_DAYS", 30),
        pending_review_ttl_days=integer("PENDING_REVIEW_TTL_DAYS", 7),
        snapshot_retention_days=integer("SNAPSHOT_RETENTION_DAYS", 30),
        latency_budget_p95_ms=integer("LATENCY_BUDGET_P95_MS", 500),
    )
