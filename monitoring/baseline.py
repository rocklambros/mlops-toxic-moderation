"""Load the drift reference and the decision rule, and fail closed on either.

`baseline_flag_rates.json` is written by Phase 1 over the locked held-out split using the
same `thresholds.json` this module also loads. Sharing one decision rule between the
reference and the production series is what makes a PSI comparison meaningful; a chart of
production flag rates alone cannot answer whether anything changed.

Every rejection below exists because the alternative -- a default, a skipped label, a
silently coerced value -- renders a chart that looks exactly like a working one.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from model.labels import LABELS

SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


class BaselineMissingError(RuntimeError):
    """The reference artifact is absent. The drift panel must not render without it."""


class BaselineContractError(RuntimeError):
    """The reference artifact is present but does not match the pinned shape."""


@dataclass(frozen=True)
class Baseline:
    schema_version: int
    data_version: str
    model_version: str
    n: int
    flag_rates: dict[str, float]


def _read_json(path: Path, what: str) -> dict:
    if not path.is_file():
        raise BaselineMissingError(
            f"{what} not found at {path}. Phase 1 must publish it alongside the promoted "
            "model; the dashboard refuses to plot drift without a reference."
        )
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BaselineContractError(f"{what} at {path} is not valid JSON: {exc}") from exc


def _rates(raw: dict, path: Path, what: str) -> dict[str, float]:
    """Exactly the six labels, each a real number in [0, 1], in `LABELS` order.

    An unexpected label is rejected rather than ignored: it means the artifact was produced
    for a different label set, and comparing it to this model's production series compares
    two different decision rules. A bool is rejected rather than coerced, because
    `isinstance(True, int)` is True and JSON `true` would otherwise read as a 100% rate.
    """
    unknown = sorted(set(raw) - set(LABELS))
    if unknown:
        raise BaselineContractError(
            f"{what} at {path} carries labels this model does not score: {unknown}"
        )
    ordered: dict[str, float] = {}
    for label in LABELS:
        if label not in raw:
            raise BaselineContractError(f"{what} at {path} is missing label {label!r}")
        value = raw[label]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise BaselineContractError(
                f"{what} at {path} has {label!r}={value!r}, which is not a number"
            )
        if not 0.0 <= float(value) <= 1.0:
            raise BaselineContractError(
                f"{what} at {path} has {label!r}={value!r}, outside [0, 1]"
            )
        ordered[label] = float(value)
    return ordered


def load_baseline(path: Path) -> Baseline:
    payload = _read_json(path, "baseline_flag_rates.json")
    version = payload.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise BaselineContractError(
            f"baseline_flag_rates.json at {path} has schema_version={version!r}; "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    rates = payload.get("flag_rates")
    if not isinstance(rates, dict):
        raise BaselineContractError(f"baseline_flag_rates.json at {path} has no flag_rates object")
    return Baseline(
        schema_version=int(version),
        data_version=str(payload.get("data_version", "")),
        model_version=str(payload.get("model_version", "")),
        n=int(payload.get("n", 0)),
        flag_rates=_rates(rates, path, "baseline_flag_rates.json"),
    )


def load_thresholds(path: Path) -> dict[str, float]:
    payload = _read_json(path, "thresholds.json")
    return _rates(payload, path, "thresholds.json")
