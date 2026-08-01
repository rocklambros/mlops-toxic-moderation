import pytest

import model.normalize as mnorm
from backend.config import MAX_INPUT_CHARS, load_settings

BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/toxic",
    "DEMO_API_KEY": "demo-key-value",
    "MODEL_ARTIFACT_PATH": "/srv/artifacts/toxic-clf.skops",
    "MODEL_CARD_PATH": "MODEL_CARD.md",
    "MODEL_DIGEST": "a" * 64,
    "MODEL_REGISTRY_VERSION": "3",
    "THRESHOLDS_PATH": "/srv/artifacts/thresholds.json",
}


def test_defaults_are_the_documented_ones():
    settings = load_settings(BASE_ENV)
    assert settings.rate_limit_per_minute == 30
    assert settings.rate_limit_burst == 10
    assert settings.max_body_bytes == 16384
    assert settings.spool_max_rows == 10000
    assert settings.db_pool_size == 5
    assert settings.db_timeout_seconds == 2.0
    assert settings.random_audit_rate == 0.05
    assert settings.input_text_retention_days == 30
    assert settings.pending_review_ttl_days == 7
    assert settings.snapshot_retention_days == 30
    assert settings.artifact_name == "toxic-clf"


def test_environment_overrides_are_applied():
    settings = load_settings({**BASE_ENV, "RATE_LIMIT_PER_MINUTE": "5", "SPOOL_MAX_ROWS": "42"})
    assert settings.rate_limit_per_minute == 5
    assert settings.spool_max_rows == 42


@pytest.mark.parametrize("missing", sorted(BASE_ENV))
def test_every_required_variable_is_required(missing):
    env = {key: value for key, value in BASE_ENV.items() if key != missing}
    with pytest.raises(RuntimeError, match=missing):
        load_settings(env)


def test_secrets_never_appear_in_the_repr():
    """A Settings object reaches tracebacks, `uvicorn --log-level debug`, and any crash
    reporter. The DSN carries the RDS master password and the API key is the abuse control."""
    settings = load_settings(BASE_ENV)
    rendered = repr(settings)
    assert "demo-key-value" not in rendered
    assert "u:p@localhost" not in rendered


def test_input_cap_is_not_environment_tunable():
    """REG-6.3a: a control that a deploy-time environment variable can widen is not a control.
    The size cap is a literal, and no Settings field shadows it."""
    settings = load_settings({**BASE_ENV, "MAX_INPUT_CHARS": "1000000"})
    assert MAX_INPUT_CHARS == mnorm.MAX_INPUT_CHARS == 5000
    assert not hasattr(settings, "max_input_chars")


def test_random_audit_rate_must_be_a_probability():
    with pytest.raises(RuntimeError, match="RANDOM_AUDIT_RATE"):
        load_settings({**BASE_ENV, "RANDOM_AUDIT_RATE": "1.5"})
