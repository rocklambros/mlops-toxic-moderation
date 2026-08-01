"""Request models for the serving backend. The response model is model/contract.py."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.config import MAX_INPUT_CHARS


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value
