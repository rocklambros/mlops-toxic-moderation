"""Data profile: per-label counts, the 6x6 co-occurrence matrix, hierarchy assertion."""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from model.labels import LABELS


@dataclass(frozen=True)
class DataProfile:
    n_rows: int
    label_counts: dict[str, int]
    label_rates: dict[str, float]
    cooccurrence: np.ndarray
    all_negative_rows: int


def assert_label_hierarchy(df: pd.DataFrame) -> None:
    violations = int(((df["severe_toxic"] == 1) & (df["toxic"] == 0)).sum())
    if violations:
        raise AssertionError(
            f"{violations} rows have severe_toxic=1 with toxic=0; the label hierarchy "
            "severe_toxic <= toxic is violated in the source corpus"
        )


def profile(df: pd.DataFrame) -> DataProfile:
    y = df[list(LABELS)].to_numpy(dtype=np.int64)
    counts = {label: int(y[:, i].sum()) for i, label in enumerate(LABELS)}
    return DataProfile(
        n_rows=len(df),
        label_counts=counts,
        label_rates={label: counts[label] / len(df) for label in LABELS},
        cooccurrence=y.T @ y,
        all_negative_rows=int((y.sum(axis=1) == 0).sum()),
    )


def render_markdown(prof: DataProfile, source: str, raw_sha256: str) -> str:
    lines = [
        "# Data Profile",
        "",
        f"- Source: `{source}`",
        f"- `raw_sha256`: `{raw_sha256}`",
        f"- Rows after dedup: {prof.n_rows}",
        f"- Rows with no positive label: {prof.all_negative_rows}",
        "",
        "## Per-label counts",
        "",
        "| Label | Positives | Rate |",
        "|---|---:|---:|",
    ]
    for label in LABELS:
        lines.append(f"| `{label}` | {prof.label_counts[label]} | {prof.label_rates[label]:.4%} |")
    header = " | ".join(f"`{lb}`" for lb in LABELS)
    lines += ["", "## Co-occurrence (6x6)", "", f"| | {header} |", "|---|" + "---:|" * len(LABELS)]
    for i, label in enumerate(LABELS):
        row = " | ".join(str(int(v)) for v in prof.cooccurrence[i])
        lines.append(f"| `{label}` | {row} |")
    lines += ["", "`severe_toxic <= toxic` asserted by `assert_label_hierarchy`.", ""]
    return "\n".join(lines)


def write_profile(df: pd.DataFrame, out_path: Path, source: str, raw_sha256: str) -> DataProfile:
    assert_label_hierarchy(df)
    prof = profile(df)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(render_markdown(prof, source, raw_sha256))
    return prof
